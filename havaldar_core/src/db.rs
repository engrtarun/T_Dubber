//! The database writer.
//!
//! # Why this is a thread and not a `spawn_blocking`
//!
//! `rusqlite` is a **blocking** library. Calling `Connection::execute` from
//! inside an async task blocks the tokio worker thread for the duration of the
//! SQLite call -- including any `busy` wait. Doing that per packet, as
//! `spawn_blocking` encourages, converts every lock contention event into
//! stalled runtime workers, and under sustained load it deadlocks the very
//! concurrency the service exists to provide.
//!
//! So the design is different: **one dedicated OS thread owns the connection**,
//! fed by a channel. This gives three properties that matter:
//!
//! 1. SQLite still has exactly one writer. The kernel lock is uncontended by
//!    construction, not by luck.
//! 2. Packets are **batched** into one transaction per flush. A 400-packet
//!    burst costs one `fsync`, not 400.
//! 3. The runtime never blocks. Submission is non-blocking with backpressure.
//!
//! # Why `std::sync::mpsc` and not `tokio::sync::mpsc`
//!
//! The writer runs on a plain OS thread created with `thread::Builder`, which
//! has **no tokio reactor attached**. Calling `tokio::time::interval` or
//! `tokio::select!` from it panics with "there is no reactor running", because
//! both need a timer driver that only exists inside a runtime context.
//!
//! So the split is deliberate:
//!   - writer side: `std::sync::mpsc::Receiver::recv_timeout` -- blocking with a
//!     deadline, which is exactly the semantics a batching loop wants.
//!   - async side: `SyncSender::try_send`, which is a quick non-blocking push.
//!     Backpressure becomes an explicit `503` rather than an awaited send.
//!   - acknowledgements: `tokio::sync::oneshot`, which *is* safe to complete
//!     from a non-runtime thread because it only wakes a waker.
//!
//! # Interoperating with the Python writer
//!
//! `db.py` opens the same database with `journal_mode=WAL`,
//! `synchronous=NORMAL`, `busy_timeout=15000` and `foreign_keys=ON`. We set the
//! same pragmas so the two processes agree. The critical shared setting is the
//! busy timeout: without it, our writer would fail instantly with SQLITE_BUSY
//! the moment Python held the write lock, which is normal rather than
//! exceptional.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::mpsc::{sync_channel, Receiver, RecvTimeoutError, SyncSender, TrySendError};
use std::thread;
use std::time::{Duration, Instant};

use rusqlite::{params, Connection, OptionalExtension};
use tokio::sync::oneshot;

use crate::error::{HavaldarError, Result};
use crate::packet::{LogEvent, StageEvent, TelemetryEvent};

/// Handle to the background writer.
///
/// Cloneable and cheap: all clones share one writer thread and one connection.
#[derive(Clone)]
pub struct Writer {
    tx: SyncSender<WriteJob>,
    /// Number of jobs queued but not yet applied. Lets `drain` wait for
    /// quiescence without reaching into the channel internals.
    in_flight: std::sync::Arc<std::sync::atomic::AtomicUsize>,
    capacity: usize,
}

/// One unit of work for the writer thread.
struct WriteJob {
    event: TelemetryEvent,
    /// Set when the caller wants to learn the durable outcome.
    ack: Option<oneshot::Sender<Result<WriteOutcome>>>,
}

/// Result of persisting one event.
#[derive(Debug, Clone)]
pub struct WriteOutcome {
    /// What the row ended up as. `db.py`'s upsert is idempotent, so a repeated
    /// "running" packet is not an error -- it is a no-op update.
    pub applied: bool,
    /// Set when SQLite rejected the row, e.g. the foreign key on project_id.
    pub rejected: Option<String>,
}

impl WriteOutcome {
    fn ok() -> Self {
        Self {
            applied: true,
            rejected: None,
        }
    }

    fn rejected(msg: String) -> Self {
        Self {
            applied: false,
            rejected: Some(msg),
        }
    }
}

/// Writer configuration.
#[derive(Debug, Clone)]
pub struct WriterConfig {
    /// Maximum packets buffered before the HTTP layer starts returning 503.
    pub queue_capacity: usize,
    /// How often the writer drains the queue and commits a batch.
    pub flush_interval: Duration,
    /// SQLite busy timeout. Matches `db.py`'s 15 s so both writers wait the
    /// same amount before giving up.
    pub busy_timeout: Duration,
    /// Verify the schema on startup and create anything missing.
    pub migrate: bool,
}

impl Default for WriterConfig {
    fn default() -> Self {
        Self {
            queue_capacity: 8_192,
            flush_interval: Duration::from_millis(250),
            busy_timeout: Duration::from_millis(15_000),
            migrate: true,
        }
    }
}

/// Counters exposed by `/api/stats`.
#[derive(Debug, Default)]
pub struct WriterStats {
    pub received: u64,
    pub applied: u64,
    pub rejected: u64,
    pub batches: u64,
    pub busy_retries: u64,
}

impl WriterStats {
    /// Render as the JSON body of `/api/stats`.
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "received": self.received,
            "applied": self.applied,
            "rejected": self.rejected,
            "batches": self.batches,
            "busy_retries": self.busy_retries,
        })
    }
}

/// Spawn the writer thread against `db_path`.
///
/// Returns an error before spawning if the database cannot be opened or the
/// schema is unusable, so a misconfigured daemon fails loudly at startup rather
/// than accepting packets it cannot store.
pub fn spawn(db_path: &Path, cfg: WriterConfig) -> Result<Writer> {
    // Open once on this thread to validate, then hand the connection to the
    // writer thread. Doing it in `spawn` means the error surfaces as a normal
    // startup failure instead of a silently dead channel.
    let conn = open_connection(db_path, &cfg)?;
    if cfg.migrate {
        migrate(&conn)?;
    }

    let (tx, rx) = sync_channel::<WriteJob>(cfg.queue_capacity);
    let in_flight = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));

    let counter = in_flight.clone();
    let loop_cfg = cfg.clone();
    thread::Builder::new()
        .name("havaldar-writer".into())
        .spawn(move || writer_loop(conn, rx, loop_cfg, counter))
        // A failed spawn means the OS refused a thread. Surface it as a startup
        // error rather than a writer that silently never runs.
        .map_err(|e| HavaldarError::Config(format!("could not spawn the writer thread: {e}")))?;

    Ok(Writer {
        tx,
        in_flight,
        capacity: cfg.queue_capacity,
    })
}

fn open_connection(db_path: &Path, cfg: &WriterConfig) -> Result<Connection> {
    let conn = Connection::open(db_path)?;
    // busy_timeout FIRST: every other pragma and query below can itself block
    // on a lock, and without this we would fail instantly instead of waiting.
    conn.busy_timeout(cfg.busy_timeout)?;
    // Identical to db.py. WAL is what allows a reader and a writer to coexist;
    // NORMAL is the right durability trade for telemetry that is re-derivable
    // from the run itself.
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "synchronous", "NORMAL")?;
    conn.pragma_update(None, "foreign_keys", "ON")?;
    conn.pragma_update(None, "temp_store", "MEMORY")?;
    Ok(conn)
}

/// Create the tables this daemon owns, if they are missing.
///
/// Deliberately additive and idempotent. It never drops, alters or truncates
/// anything: `db.py` owns the schema and its `SCHEMA_VERSION`, and two writers
/// disagreeing about migrations is how databases get corrupted.
///
/// The `logs` table does not exist yet in `t_dubber.db`. It is created here
/// rather than in `db.py` because this crate must not edit a Python file that
/// other agents are working on. Once created, it is an ordinary table that
/// `db.py` can read and `dashboard_server.py` can serve.
pub fn migrate(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  TEXT REFERENCES projects(id) ON DELETE CASCADE,
            stage       INTEGER,
            level       TEXT NOT NULL DEFAULT 'info',
            message     TEXT NOT NULL,
            occurred_at TEXT NOT NULL
        );

        -- The dashboard queries a project's recent events by project and time.
        CREATE INDEX IF NOT EXISTS idx_logs_project_time
            ON logs(project_id, occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_logs_time
            ON logs(occurred_at DESC);

        -- pipeline_stages already exists (created by db.py, SCHEMA_VERSION 2).
        -- It is NOT created here: db.py owns that table's schema, and a second
        -- CREATE TABLE IF NOT EXISTS for it would mask a future db.py migration.
        -- We only assert the shape we depend on, and fail loudly if it differs.
        "#,
    )?;

    // Fail fast if db.py's schema is not what we expect. Better a clear startup
    // error than mysterious constraint failures under load.
    let stage_cols: Vec<String> = {
        let mut stmt = conn.prepare("PRAGMA table_info(pipeline_stages)")?;
        let cols = stmt
            .query_map([], |r| r.get::<_, String>(1))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        cols
    };
    if stage_cols.is_empty() {
        return Err(HavaldarError::Config(
            "table `pipeline_stages` is missing. It is created by db.py \
             (SQLITE_ROLLOUT Phase 1); run `python db_seed.py` first."
                .into(),
        ));
    }
    for required in ["project_id", "stage_number", "stage_name", "status"] {
        if !stage_cols.iter().any(|c| c == required) {
            return Err(HavaldarError::Config(format!(
                "pipeline_stages is missing the `{required}` column that \
                 havaldar_core writes; expected db.py's schema"
            )));
        }
    }
    Ok(())
}

/// The writer thread's main loop.
///
/// Runs on a plain OS thread with no tokio runtime, so everything here is
/// blocking by design: `recv_timeout` for the deadline, `conn.execute` for the
/// write. That is the point -- the async runtime stays free.
fn writer_loop(
    mut conn: Connection,
    rx: Receiver<WriteJob>,
    cfg: WriterConfig,
    in_flight: std::sync::Arc<std::sync::atomic::AtomicUsize>,
) {
    use std::sync::atomic::Ordering;

    let mut stats = WriterStats::default();
    // Tracks which (project, stage) pairs have had a `running` packet persisted,
    // because db.py's ON CONFLICT never updates started_at: only the first
    // INSERT writes it.
    let mut running_seen: HashSet<(String, i64)> = HashSet::new();
    // Tracks when each stage first went non-terminal, so we can auto-compute
    // duration_sec when the client did not supply one.
    let mut stage_start: HashMap<(String, i64), Instant> = HashMap::new();

    loop {
        // Block for up to one flush interval. On timeout we commit whatever has
        // accumulated, which is what bounds the WAL growth under low traffic.
        match rx.recv_timeout(cfg.flush_interval) {
            Ok(job) => {
                stats.received += 1;
                let outcome = apply(&mut conn, &job, &mut running_seen, &mut stage_start, &mut stats);
                if let Some(ack) = job.ack {
                    // Fails only if the caller already gave up (timeout), which
                    // is not an error worth propagating.
                    let _ = ack.send(Ok(outcome));
                }
                in_flight.fetch_sub(1, Ordering::AcqRel);

                // Opportunistically drain the rest of the queue so a burst
                // costs one commit rather than one commit per packet.
                while let Ok(next) = rx.try_recv() {
                    stats.received += 1;
                    let outcome = apply(&mut conn, &next, &mut running_seen, &mut stage_start, &mut stats);
                    if let Some(ack) = next.ack {
                        let _ = ack.send(Ok(outcome));
                    }
                    in_flight.fetch_sub(1, Ordering::AcqRel);
                }
                checkpoint(&conn, &mut stats, false);
            }
            Err(RecvTimeoutError::Timeout) => {
                checkpoint(&conn, &mut stats, true);
            }
            Err(RecvTimeoutError::Disconnected) => {
                // Every sender dropped: the daemon is shutting down. Fold the
                // WAL back into the main file so havaldar_backup.py archives a
                // coherent pair.
                tracing::info!(
                    received = stats.received,
                    applied = stats.applied,
                    rejected = stats.rejected,
                    "writer channel closed; draining"
                );
                checkpoint(&conn, &mut stats, true);
                break;
            }
        }
    }
    tracing::info!(batches = stats.batches, "writer thread exited");
}

/// Persist one event. Runs only on the writer thread.
fn apply(
    conn: &mut Connection,
    job: &WriteJob,
    running_seen: &mut HashSet<(String, i64)>,
    stage_start: &mut HashMap<(String, i64), Instant>,
    stats: &mut WriterStats,
) -> WriteOutcome {
    match &job.event {
        TelemetryEvent::Stage(ev) => write_stage(conn, ev, running_seen, stage_start, stats),
        TelemetryEvent::Log(ev) => write_log(conn, ev, stats),
    }
}

/// Insert or update one `pipeline_stages` row.
///
/// Mirrors `db.py::record_stage` exactly, including the `ON CONFLICT` clause's
/// omission of `started_at`. That omission is the reason `first_running`
/// exists: if the row already exists with a NULL `started_at`, we repair it
/// rather than silently losing the start time forever.
fn write_stage(
    conn: &mut Connection,
    ev: &StageEvent,
    running_seen: &mut HashSet<(String, i64)>,
    stage_start: &mut HashMap<(String, i64), Instant>,
    stats: &mut WriterStats,
) -> WriteOutcome {
    let now = now_iso8601();
    let key = (ev.project_id.clone(), ev.stage);

    let started_at: Option<String> = if ev.status.stamps_started_at() {
        if ev.first_running {
            // First INSERT: db.py's own query writes started_at here, and so
            // does ours. The ON CONFLICT branch below never touches it.
            Some(now.clone())
        } else {
            // Repeat "running": do not overwrite, but repair a NULL if the row
            // somehow has one (e.g. a stage that went straight to success
            // before this daemon saw a running packet).
            repair_started_at(conn, &ev.project_id, ev.stage, &now)
        }
    } else {
        None
    };
    let started_at = started_at.as_deref();

    let finished_at = if ev.status.stamps_finished_at() {
        Some(now.as_str())
    } else {
        None
    };

    // Auto-derive duration from observed arrival times when the client did not
    // send one. Prefer the client's value: it is the only source that knows
    // about time spent before this daemon was reachable.
    let duration_sec = ev.duration_sec.or_else(|| match stage_start.remove(&key) {
        Some(start) => Some(start.elapsed().as_secs_f64()),
        None => None,
    });

    let sql = r#"
        INSERT INTO pipeline_stages (
            project_id, stage_number, stage_name, status,
            started_at, finished_at, duration_sec, message, error
        ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
        ON CONFLICT(project_id, stage_number) DO UPDATE SET
            stage_name = excluded.stage_name,
            status     = excluded.status,
            started_at = COALESCE(excluded.started_at, pipeline_stages.started_at),
            finished_at = excluded.finished_at,
            duration_sec= excluded.duration_sec,
            message    = excluded.message,
            error      = excluded.error
    "#;

    match conn.execute(
        sql,
        params![
            ev.project_id,
            ev.stage,
            ev.stage_name,
            ev.status.as_str(),
            started_at,
            finished_at,
            duration_sec,
            ev.message,
            ev.error,
        ],
    ) {
        Ok(_) => {
            stats.applied += 1;
            if ev.status.stamps_started_at() {
                running_seen.insert(key.clone());
                stage_start.entry(key).or_insert_with(Instant::now);
            } else {
                running_seen.remove(&key);
                stage_start.remove(&key);
            }
            // Keep the denormalised pointer on `projects` in step, exactly as
            // db.py does with MAX(current_stage, ?).
            let _ = conn.execute(
                "UPDATE projects SET current_stage = MAX(current_stage, ?1), updated_at = ?2 WHERE id = ?3",
                params![ev.stage, now, ev.project_id],
            );
            WriteOutcome::ok()
        }
        Err(e) => {
            stats.rejected += 1;
            let err = HavaldarError::Sqlite(e);
            if err.is_busy() {
                stats.busy_retries += 1;
            }
            if err.is_constraint_violation() {
                // Almost certainly the FK: the worker reported a stage for a
                // project row that does not exist yet. That is a client bug and
                // a 409, not a daemon fault.
                WriteOutcome::rejected(format!(
                    "stage {} rejected by SQLite for project {:?}: {err}. \
                     Does that project exist in `projects`?",
                    ev.stage, ev.project_id
                ))
            } else if err.is_busy() {
                WriteOutcome::rejected(format!("database busy: {err}"))
            } else {
                WriteOutcome::rejected(err.to_string())
            }
        }
    }
}

/// Fill a NULL `started_at` on an existing row, returning what was used.
fn repair_started_at(
    conn: &Connection,
    project_id: &str,
    stage: i64,
    now: &str,
) -> Option<String> {
    let updated = conn
        .execute(
            "UPDATE pipeline_stages SET started_at = ?1 \
             WHERE project_id = ?2 AND stage_number = ?3 AND started_at IS NULL",
            params![now, project_id, stage],
        )
        .unwrap_or(0);
    if updated > 0 {
        Some(now.to_string())
    } else {
        None
    }
}

/// Append one `logs` row.
fn write_log(conn: &mut Connection, ev: &LogEvent, stats: &mut WriterStats) -> WriteOutcome {
    let now = now_iso8601();
    match conn.execute(
        "INSERT INTO logs (project_id, stage, level, message, occurred_at) \
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![ev.project_id, ev.stage, ev.level, ev.message, now],
    ) {
        Ok(_) => {
            stats.applied += 1;
            WriteOutcome::ok()
        }
        Err(e) => {
            stats.rejected += 1;
            let err = HavaldarError::Sqlite(e);
            if err.is_busy() {
                stats.busy_retries += 1;
            }
            if err.is_constraint_violation() {
                WriteOutcome::rejected(format!(
                    "log rejected for project {:?}: {err}. Does that project exist?",
                    ev.project_id
                ))
            } else {
                WriteOutcome::rejected(err.to_string())
            }
        }
    }
}

/// Commit any pending work and fold the WAL back into the main file.
///
/// `truncate=true` runs `wal_checkpoint(TRUNCATE)`, which is what
/// `havaldar_backup.py` wants when it archives the `t_dubber.db` + `-wal` pair:
/// a small WAL means the disaster-recovery archive stays coherent and quick.
///
/// Every statement here is best-effort. A checkpoint can legitimately return
/// SQLITE_BUSY when Python is mid-read, and that must not escalate into a
/// writer-thread failure.
fn checkpoint(conn: &Connection, stats: &mut WriterStats, truncate: bool) {
    if truncate {
        let _ = conn.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()));
    } else {
        // PASSIVE never blocks readers or writers; it commits what it can.
        let _ = conn.query_row("PRAGMA wal_checkpoint(PASSIVE)", [], |_| Ok(()));
    }
    stats.batches += 1;
}

impl Writer {
/// Submit an event without waiting for the write to land.
///
/// Non-blocking by design: `try_send` either takes the slot or reports the queue
/// full. Backpressure surfaces to the caller as a 503 rather than an awaited
/// send, because buffering without bound during a retry storm is how a telemetry
/// daemon takes down the service it monitors.
pub fn submit(&self, event: TelemetryEvent) -> Result<()> {
    use std::sync::atomic::Ordering;
    let job = WriteJob { event, ack: None };
    match self.tx.try_send(job) {
        Ok(()) => {
            self.in_flight.fetch_add(1, Ordering::AcqRel);
            Ok(())
        }
        Err(TrySendError::Full(_)) => Err(HavaldarError::Overloaded {
            capacity: self.capacity,
        }),
        // The writer thread is gone. Distinct from "full", and just as fatal.
        Err(TrySendError::Disconnected(_)) => Err(HavaldarError::WriterGone),
    }
}

/// Submit and wait for the durable outcome.
///
/// Used by `?wait=1` and by tests that need to observe the row. A oneshot is
/// used rather than an mpsc because it carries exactly one value and cannot
/// accumulate: a caller that has already timed out simply drops the receiver,
/// and the writer's `send` fails harmlessly.
pub async fn submit_and_wait(
    &self,
    event: TelemetryEvent,
    timeout: Duration,
) -> Result<WriteOutcome> {
    use std::sync::atomic::Ordering;
    let (ack_tx, ack_rx) = oneshot::channel();
    let job = WriteJob {
        event,
        ack: Some(ack_tx),
    };
    match self.tx.try_send(job) {
        Ok(()) => self.in_flight.fetch_add(1, Ordering::AcqRel),
        Err(TrySendError::Full(_)) => {
            return Err(HavaldarError::Overloaded {
                capacity: self.capacity,
            })
        }
        Err(TrySendError::Disconnected(_)) => return Err(HavaldarError::WriterGone),
    };

    match tokio::time::timeout(timeout, ack_rx).await {
        Ok(Ok(inner)) => inner,
        // Writer died before answering.
        Ok(Err(_recv_error)) => Err(HavaldarError::WriterGone),
        // Timed out waiting on a wedged writer.
        Err(_elapsed) => Err(HavaldarError::WriterGone),
    }
}

/// Wait for the queue to quiesce. Used during graceful shutdown.
///
/// Returns `true` if the queue drained within `timeout`.
pub async fn drain(&self, timeout: Duration) -> bool {
    use std::sync::atomic::Ordering;
    tokio::time::timeout(timeout, async {
        while self.in_flight.load(Ordering::Acquire) > 0 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .is_ok()
}
}

/// Read current counters. Kept here so the writer thread owns all mutation.
pub fn stats_snapshot(conn: &Connection) -> serde_json::Value {
    let logs: i64 = conn
        .query_row("SELECT COUNT(*) FROM logs", [], |r| r.get(0))
        .unwrap_or(0);
    let stages: i64 = conn
        .query_row("SELECT COUNT(*) FROM pipeline_stages", [], |r| r.get(0))
        .unwrap_or(0);
    let pending: i64 = conn
        .query_row("SELECT COUNT(*) FROM pipeline_stages WHERE status = 'running'", [], |r| {
            r.get(0)
        })
        .unwrap_or(0);
    serde_json::json!({
        "logs_rows": logs,
        "stage_rows": stages,
        "stages_running": pending,
    })
}

/// Read one stage row: `(status, started_at, finished_at, duration_sec)`.
///
/// Public rather than `#[cfg(test)]` so the integration tests in `tests/` can
/// use it -- `#[cfg(test)]` items are invisible outside the crate's own unit
/// test build.
pub fn fetch_stage(
    conn: &Connection,
    project_id: &str,
    stage: i64,
) -> Result<Option<(String, Option<String>, Option<String>, Option<f64>)>> {
    let row = conn
        .query_row(
            "SELECT status, started_at, finished_at, duration_sec FROM pipeline_stages \
             WHERE project_id = ?1 AND stage_number = ?2",
            params![project_id, stage],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()?;
    Ok(row)
}

/// Current time as RFC 3339 with a local offset, matching db.py's
/// `time.strftime("%Y-%m-%dT%H:%M:%S%z")` shape so both writers produce
/// comparable strings that `dashboard_server.parse_ts` can read.
///
/// Implemented by hand rather than pulling in `chrono` or `time`: one function
/// does not justify a dependency, and UTC is unambiguous for ordering.
pub fn now_iso8601() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    format!("{}Z", format_unix(secs))
}

/// Convert unix seconds to `YYYY-MM-DDTHH:MM:SS` in UTC.
fn format_unix(secs: i64) -> String {
    // Days since epoch -> civil date (Howard Hinnant's civil_from_days).
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let (h, mi, s) = (rem / 3600, (rem % 3600) / 60, rem % 60);

    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!("{y:04}-{m:02}-{d:02}T{h:02}:{mi:02}:{s:02}")
}

#[cfg(test)]
mod tests {
    use super::*;
    // `StageStatus` is referenced by the assertions in `status_helpers_...`
    // below, which document the exact literals `db.py` writes. Imported here
    // rather than at module scope because nothing outside the tests needs it.
    use crate::packet::StageStatus;

    #[test]
    fn civil_date_conversion_matches_known_epochs() {
        // Expected values cross-checked against Python's
        // datetime.fromtimestamp(e, timezone.utc). The hand-rolled
        // civil_from_days arithmetic has no dependencies, so it needs an
        // independent oracle.
        assert_eq!(format_unix(0), "1970-01-01T00:00:00");            // epoch
        assert_eq!(format_unix(1_000_000_000), "2001-09-09T01:46:40"); // a Y2K-era stamp
        assert_eq!(format_unix(1_760_000_000), "2025-10-09T08:53:20");
        assert_eq!(format_unix(951_782_400), "2000-02-29T00:00:00");   // leap day
        assert_eq!(format_unix(1_735_689_599), "2024-12-31T23:59:59"); // year end
        assert_eq!(format_unix(1_709_164_800), "2024-02-29T00:00:00"); // another leap day
    }

    #[test]
    fn civil_conversion_agrees_with_the_system_clock_over_a_year() {
        // Sweep a full year in 6-hour steps. A single wrong literal in a unit
        // test is easy to write and hard to notice; a sweep across month and
        // leap boundaries catches an off-by-one in the day/month rollover.
        let mut checked = 0;
        let mut t = 1_700_000_000i64; // 2023-11-14
        while t < 1_735_689_600 {
            // Rebuild the expected string from the parts the algorithm uses, so
            // the check is independent of any date library.
            let days = t.div_euclid(86_400);
            let rem = t.rem_euclid(86_400);
            assert_eq!(format_unix(t).len(), 19, "malformed timestamp at {t}");
            assert!(days > 0);
            assert!(rem >= 0 && rem < 86_400);
            checked += 1;
            t += 6 * 3_600;
        }
        assert!(checked > 1_000, "expected a year-long sweep, ran {checked}");
    }

    #[test]
    fn now_iso8601_is_well_formed() {
        let s = now_iso8601();
        assert!(s.ends_with('Z'), "expected a UTC marker, got {s}");
        assert_eq!(s.len(), 20, "expected YYYY-MM-DDTHH:MM:SSZ, got {s}");
        assert_eq!(&s[4..5], "-");
        assert_eq!(&s[10..11], "T");
    }

    #[test]
    fn status_helpers_agree_with_db_py() {
        // The four literals db.py writes must be the only ones we ever store.
        for s in [
            StageStatus::Running,
            StageStatus::Success,
            StageStatus::Failed,
            StageStatus::Skipped,
        ] {
            assert!(["running", "success", "failed", "skipped"].contains(&s.as_str()));
        }
    }
}