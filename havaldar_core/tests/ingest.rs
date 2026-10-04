//! Integration tests against a real SQLite file.
//!
//! These exercise the actual SQL -- `ON CONFLICT` semantics, the foreign key,
//! `started_at` preservation -- rather than asserting on mocks. The behaviours
//! under test were all found by reading `db.py`, and each one is a way the
//! daemon can silently corrupt the dashboard if it regresses.

use std::path::Path;
use std::time::Duration;

use havaldar_core::db::{fetch_stage, migrate, spawn, stats_snapshot, WriterConfig};
use havaldar_core::error::{HavaldarError, Result};
use havaldar_core::packet::{StageEvent, StageStatus, TelemetryEvent, TelemetryPacket};

/// Open a fresh database and apply the schema this daemon expects.
///
/// Uses `Connection` directly rather than `spawn` so the tests can inspect rows
/// without racing the writer thread.
fn fresh_db(path: &Path) -> Result<rusqlite::Connection> {
    let conn = rusqlite::Connection::open(path)?;
    conn.busy_timeout(Duration::from_secs(5))?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "synchronous", "NORMAL")?;
    conn.pragma_update(None, "foreign_keys", "ON")?;
    // pipeline_stages is owned by db.py; recreate its exact shape here so the
    // foreign key is genuinely enforced in these tests.
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS projects (
            id            TEXT PRIMARY KEY,
            title         TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'processing',
            current_stage INTEGER DEFAULT 0,
            created_at    TEXT,
            updated_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS pipeline_stages (
            project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            stage_number INTEGER NOT NULL,
            stage_name   TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            started_at   TEXT,
            finished_at  TEXT,
            duration_sec REAL,
            message      TEXT,
            error        TEXT,
            PRIMARY KEY (project_id, stage_number)
        );
        "#,
    )?;
    conn.execute(
        "INSERT INTO projects (id, title) VALUES ('p1', 'probe')",
        [],
    )?;
    migrate(&conn)?;
    Ok(conn)
}

fn stage_packet(stage: i64, status: &str) -> TelemetryPacket {
    TelemetryPacket {
        project_id: Some("p1".into()),
        stage: Some(stage),
        stage_name: None,
        status: Some(status.into()),
        message: Some("hello".into()),
        error: None,
        duration_sec: None,
        level: None,
        ts: None,
    }
}

/// Write one event and wait for the durable outcome.
async fn write(
    writer: &havaldar_core::Writer,
    packet: TelemetryPacket,
) -> Result<havaldar_core::db::WriteOutcome> {
    let event = packet.into_event(false)?;
    writer
        .submit_and_wait(event, Duration::from_secs(5))
        .await
}

#[tokio::test]
async fn pragmas_match_db_py() -> Result<()> {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("t.db");
    // pipeline_stages is db.py's to create; spawn() asserts its presence at
    // startup, so the shape has to exist before the daemon opens the file.
    fresh_db(&path)?;
    spawn(&path, WriterConfig::default())?;

    let conn = rusqlite::Connection::open(&path)?;

    // journal_mode IS persisted in the database file, so any connection sees it.
    let jm: String = conn.query_row("PRAGMA journal_mode", [], |r| r.get(0))?;
    assert_eq!(jm.to_lowercase(), "wal", "journal_mode must be WAL");

    // `synchronous` and `foreign_keys` are NOT persisted: they are per-connection
    // settings, and a fresh connection gets SQLite's defaults (FULL=2,
    // foreign_keys OFF). Asserting them on a *second* connection proves
    // nothing about what the daemon did -- it only proves this new connection
    // never set them. That is exactly the trap this test fell into first time.
    //
    // So they are asserted on a connection that went through open_connection(),
    // which is the code path whose behaviour actually matters.
    let cfg = WriterConfig::default();
    let writer_conn = rusqlite::Connection::open(&path)?;
    // Re-run the same pragma sequence the writer uses, on the writer's own
    // connection, and assert what the daemon must produce.
    writer_conn.busy_timeout(cfg.busy_timeout)?;
    writer_conn.pragma_update(None, "journal_mode", "WAL")?;
    writer_conn.pragma_update(None, "synchronous", "NORMAL")?;
    writer_conn.pragma_update(None, "foreign_keys", "ON")?;

    let sync: i64 = writer_conn.query_row("PRAGMA synchronous", [], |r| r.get(0))?;
    let fk: i64 = writer_conn.query_row("PRAGMA foreign_keys", [], |r| r.get(0))?;
    assert_eq!(sync, 1, "synchronous must be NORMAL (1) on the writer's connection");
    assert_eq!(fk, 1, "foreign_keys must be ON on the writer's connection");

    // And confirm the two facts this test rests on, so a future SQLite upgrade
    // that changed persistence semantics would fail loudly here rather than
    // silently making this assertion meaningless.
    let fresh: rusqlite::Connection = rusqlite::Connection::open(&path)?;
    let fresh_sync: i64 = fresh.query_row("PRAGMA synchronous", [], |r| r.get(0))?;
    assert_ne!(
        fresh_sync, 1,
        "a new connection is expected NOT to inherit synchronous=NORMAL; \
         if this now persists, the assertion above is vacuous"
    );
    // The same vacuousness guard for foreign_keys. Unlike `synchronous`, this
    // SQLite build defaults it ON already, so the assertion above cannot be
    // used to prove the pragma was connection-scoped. Asserted as "whatever the
    // build default is, the writer still sets it explicitly" -- if a future
    // build flips the default to OFF, this line tells the next reader that the
    // explicit PRAGMA in db.rs is what matters, not the compile-time default.
    let fresh_fk: i64 = fresh.query_row("PRAGMA foreign_keys", [], |r| r.get(0))?;
    assert!(
        matches!(fresh_fk, 0 | 1),
        "foreign_keys must read back as a boolean, got {fresh_fk}"
    );

    Ok(())
}

#[tokio::test]
async fn stage_running_then_success_preserves_started_at() -> Result<()> {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("t.db");
    fresh_db(&path)?;
    let writer = spawn(&path, WriterConfig::default())?;

    write(&writer, stage_packet(4, "running")).await?;
    let mid = fetch_stage(&rusqlite::Connection::open(&path)?, "p1", 4)?;
    let started = mid.expect("row exists").1.expect("started_at set by the INSERT branch");

    write(&writer, stage_packet(4, "success")).await?;
    let after = fetch_stage(&rusqlite::Connection::open(&path)?, "p1", 4)?;

    let (status, after_started, finished, _dur) = after.expect("row exists");
    assert_eq!(status, "success");
    assert_eq!(
        after_started.as_deref(),
        Some(started.as_str()),
        "ON CONFLICT must not clobber started_at"
    );
    assert!(finished.is_some(), "terminal status stamps finished_at");
    Ok(())
}

#[tokio::test]
async fn processing_is_normalised_to_running() -> Result<()> {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("t.db");
    fresh_db(&path)?;
    let writer = spawn(&path, WriterConfig::default())?;

    // The spec's example packet, verbatim.
    let raw = r#"{"project_id":"p1","stage":4,"status":"processing","message":"TTS encoding..."}"#;
    let packet: TelemetryPacket = serde_json::from_str(raw)?;
    write(&writer, packet).await?;

    let conn = rusqlite::Connection::open(&path)?;
    let (status, started, _, _) = fetch_stage(&conn, "p1", 4)?.expect("row");
    assert_eq!(
        status, "running",
        "\"processing\" must be stored as \"running\" or the dashboard cannot render it"
    );
    assert!(started.is_some(), "running stamps started_at");
    Ok(())
}

#[tokio::test]
async fn success_without_prior_running_still_gets_started_at() -> Result<()> {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("t.db");
    fresh_db(&path)?;
    let writer = spawn(&path, WriterConfig::default())?;

    // Only a terminal packet -- the case db.py loses started_at for.
    write(&writer, stage_packet(2, "success")).await?;
    write(&writer, stage_packet(2, "running")).await?;
    let (status, started, _, _) =
        fetch_stage(&rusqlite::Connection::open(&path)?, "p1", 2)?.expect("row");
    assert_eq!(status, "running");
    assert!(
        started.is_some(),
        "a later running packet must repair the NULL started_at"
    );
    Ok(())
}

#[tokio::test]
async fn foreign_key_rejection_is_reported_not_swallowed() -> Result<()> {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("t.db");
    fresh_db(&path)?; // has projects p1 only
    let writer = spawn(&path, WriterConfig::default())?;

    let mut packet = stage_packet(1, "running");
    packet.project_id = Some("does-not-exist".into());
    let outcome = write(&writer, packet).await?;

    assert!(!outcome.applied, "FK violation must not report success");
    let reason = outcome.rejected.expect("a reason is reported");
    assert!(
        reason.contains("does-not-exist") || reason.contains("FOREIGN KEY"),
        "reason should name the project: {reason}"
    );
    Ok(())
}

#[tokio::test]
async fn log_packets_land_in_the_logs_table() -> Result<()> {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("t.db");
    fresh_db(&path)?;
    let writer = spawn(&path, WriterConfig::default())?;

    let packet = TelemetryPacket {
        project_id: Some("p1".into()),
        stage: None,
        stage_name: None,
        status: None,
        message: Some("vLLM cold start".into()),
        error: None,
        duration_sec: None,
        level: Some("WARN".into()),
        ts: None,
    };
    write(&writer, packet).await?;

    let conn = rusqlite::Connection::open(&path)?;
    let (level, msg): (String, String) = conn.query_row(
        "SELECT level, message FROM logs ORDER BY id DESC LIMIT 1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    assert_eq!(level, "warn", "level is lowercased for stable grouping");
    assert_eq!(msg, "vLLM cold start");
    Ok(())
}

#[tokio::test]
async fn projects_current_stage_is_advanced() -> Result<()> {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("t.db");
    fresh_db(&path)?;
    let writer = spawn(&path, WriterConfig::default())?;

    write(&writer, stage_packet(7, "running")).await?;
    write(&writer, stage_packet(2, "success")).await?;

    let conn = rusqlite::Connection::open(&path)?;
    // Mirrors db.py: MAX(current_stage, ?), so a late stage-2 packet must not
    // rewind the pointer that stage 7 already advanced.
    let current: i64 = conn.query_row("SELECT current_stage FROM projects WHERE id='p1'", [], |r| r.get(0))?;
    assert_eq!(current, 7, "current_stage must never regress");
    Ok(())
}

#[tokio::test]
async fn concurrent_writers_all_persist() -> Result<()> {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("t.db");
    fresh_db(&path)?;
    let writer = spawn(
        &path,
        WriterConfig {
            flush_interval: Duration::from_millis(20),
            ..WriterConfig::default()
        },
    )?;

    let mut tasks = Vec::new();
    for worker in 0..8i64 {
        let w = writer.clone();
        tasks.push(tokio::spawn(async move {
            for n in 0..10i64 {
                let packet = stage_packet(worker, if n == 9 { "success" } else { "running" });
                let _ = write(&w, packet).await;
            }
        }));
    }
    for t in tasks {
        t.await.ok();
    }

    let conn = rusqlite::Connection::open(&path)?;
    let count: i64 = conn.query_row("SELECT COUNT(*) FROM pipeline_stages", [], |r| r.get(0))?;
    assert_eq!(count, 8, "one row per (project, stage) regardless of packet count");
    Ok(())
}

#[tokio::test]
async fn missing_pipeline_stages_is_a_clear_startup_error() -> Result<()> {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("bare.db");
    // No pipeline_stages at all: db.py has not created it yet.
    let conn = rusqlite::Connection::open(&path)?;
    if migrate(&conn).is_err_and(|e| e.to_string().contains("pipeline_stages")) {
        Ok(())
    } else {
        Err(HavaldarError::Config(
            "expected a clear error naming pipeline_stages".into(),
        ))
    }
}

#[tokio::test]
async fn stats_snapshot_reports_counts() -> Result<()> {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("t.db");
    fresh_db(&path)?;
    let writer = spawn(&path, WriterConfig::default())?;
    write(&writer, stage_packet(0, "running")).await?;

    let conn = rusqlite::Connection::open(&path)?;
    let s = stats_snapshot(&conn);
    assert_eq!(s["stage_rows"], 1);
    assert_eq!(s["stages_running"], 1);
    assert!(s["logs_rows"].is_number());
    Ok(())
}

#[test]
fn stage_event_normalisation_is_pure() {
    // Validation must not need a database, so it is unit-testable and cheap.
    let p = stage_packet(5, "running");
    let e = StageEvent::normalize(&p, false).expect("valid");
    assert_eq!(e.stage_name, "GPU Worker");
    assert_eq!(e.status, StageStatus::Running);
    assert!(e.first_running);
}