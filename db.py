"""
SQLite storage layer for T_Dubber.

One database holds every fact the project accumulates: which runs exist, where
each one is, which Telegram archive protects it, how far it got, and how it
scored. Video bytes stay on Telegram and files stay on disk; this stores only
what can be queried.

Design notes
------------
* **WAL + busy timeout.** The Gradio UI, the upload worker thread and a
  Kaggle reattach can all touch the database at once. SQLite's default rollback
  journal would raise "database is locked" under exactly that concurrency, so
  WAL mode and a generous busy timeout are mandatory here, not optional tuning.
* **Migrations are versioned.** A ``user_version`` pragma tracks the applied
  schema so an existing database upgrades in place instead of needing a wipe.
* **The Telegram session is deliberately untouched.** Telethon owns its own
  SQLite file and corrupts if another process writes to it concurrently.

This is Phase 1 of the rollout: the database is written alongside the existing
JSON files, which remain the source of truth until the cutover. Nothing here
changes current behaviour.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "t_dubber.db")

SCHEMA_VERSION = 1

# Stand-in for a channel that was never recorded. Keeps a partially-failed
# upload queryable instead of dropping it.
UNKNOWN_CHANNEL = "(unknown)"

_local = threading.local()

# Uploads write from a worker thread while the UI reads on the main thread.
# A long write would otherwise block the UI, so this is generous on purpose.
BUSY_TIMEOUT_MS = 15000


SCHEMA = """
-- One row per dubbing run.
CREATE TABLE IF NOT EXISTS projects (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    run_id              TEXT,
    source_kind         TEXT,
    source_url          TEXT,
    source_video        TEXT,
    source_size         INTEGER,
    target_language     TEXT,
    speaker_detection   INTEGER DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'processing',
    current_stage       INTEGER DEFAULT 0,
    kernel_id           TEXT,
    backup_archive_id   INTEGER REFERENCES telegram_archives(id) ON DELETE SET NULL,
    backup_link         TEXT,
    backup_error        TEXT,
    output_video        TEXT,
    report_file         TEXT,
    created_at          TEXT,
    updated_at          TEXT,
    completed_at        TEXT
);

-- Every upload this machine has made, source or output.
CREATE TABLE IF NOT EXISTS telegram_archives (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT NOT NULL,
    filename        TEXT NOT NULL,
    file_path       TEXT,
    file_size       INTEGER NOT NULL DEFAULT 0,
    channel         TEXT,
    chunked         INTEGER DEFAULT 0,
    chunk_count     INTEGER DEFAULT 0,
    chunk_size      INTEGER,
    manifest_msg_id INTEGER,
    manifest_link   TEXT,
    state           TEXT,
    error           TEXT,
    project_id      TEXT REFERENCES projects(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    -- Same bytes to the same channel is the same archive.
    UNIQUE (fingerprint, channel)
);

-- One row per uploaded part, so restore can be verified and queried.
CREATE TABLE IF NOT EXISTS telegram_parts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id      INTEGER NOT NULL REFERENCES telegram_archives(id) ON DELETE CASCADE,
    part_number     INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    message_link    TEXT,
    offset_bytes    INTEGER NOT NULL,
    size_bytes      INTEGER NOT NULL,
    sha256          TEXT,
    UNIQUE (archive_id, part_number)
);

-- Per-run metadata carried through from the source link or local file.
CREATE TABLE IF NOT EXISTS media_metadata (
    project_id      TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    page_url        TEXT,
    extractor       TEXT,
    uploader        TEXT,
    media_title     TEXT,
    duration_sec    REAL,
    thumbnail_url   TEXT,
    source_sha256   TEXT,
    resolved_at     TEXT
);

-- Stage-by-stage outcome, the basis for resume and for timing estimates.
CREATE TABLE IF NOT EXISTS pipeline_stages (
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    stage_number    INTEGER NOT NULL,
    stage_name      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    started_at      TEXT,
    finished_at     TEXT,
    duration_sec    REAL,
    message         TEXT,
    error           TEXT,
    PRIMARY KEY (project_id, stage_number)
);

-- Quality metrics, one row per measurement rather than a JSON blob, so trends
-- can be queried with plain SQL.
CREATE TABLE IF NOT EXISTS quality_metrics (
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    stage_number    INTEGER NOT NULL,
    metric_name     TEXT NOT NULL,
    metric_value    REAL,
    passed          INTEGER,
    detail          TEXT,
    measured_at     TEXT NOT NULL,
    PRIMARY KEY (project_id, stage_number, metric_name)
);

-- Structured error log: which stage failed, why, and how many times.
CREATE TABLE IF NOT EXISTS run_errors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT REFERENCES projects(id) ON DELETE CASCADE,
    stage_number    INTEGER,
    error_type      TEXT,
    message         TEXT,
    retry_count     INTEGER DEFAULT 0,
    occurred_at     TEXT NOT NULL
);

-- Reusable voice references, global across runs.
CREATE TABLE IF NOT EXISTS voice_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    sample_path     TEXT NOT NULL UNIQUE,
    language        TEXT DEFAULT 'Hindi',
    gender          TEXT,
    description     TEXT,
    created_at      TEXT NOT NULL
);

-- Detected speakers per run, mapped onto a voice sample.
CREATE TABLE IF NOT EXISTS speakers (
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    speaker_label       TEXT NOT NULL,
    voice_sample_path   TEXT,
    voice_sample_id     INTEGER REFERENCES voice_samples(id) ON DELETE SET NULL,
    segment_count       INTEGER,
    PRIMARY KEY (project_id, speaker_label)
);

-- Batch queue. depends_on_id lets episodes run in sequence inside one session.
CREATE TABLE IF NOT EXISTS job_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    priority        INTEGER NOT NULL DEFAULT 100,
    scheduled_at    TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'queued',
    depends_on_id   INTEGER REFERENCES job_queue(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_status    ON projects(status);
CREATE INDEX IF NOT EXISTS idx_projects_updated   ON projects(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_projects_created   ON projects(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_archives_channel   ON telegram_archives(channel);
CREATE INDEX IF NOT EXISTS idx_archives_state     ON telegram_archives(state);
CREATE INDEX IF NOT EXISTS idx_parts_archive      ON telegram_parts(archive_id);
CREATE INDEX IF NOT EXISTS idx_stages_project     ON pipeline_stages(project_id, stage_number);
CREATE INDEX IF NOT EXISTS idx_metrics_project    ON quality_metrics(project_id);
CREATE INDEX IF NOT EXISTS idx_errors_project     ON run_errors(project_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_queue_status       ON job_queue(status, priority);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def connect() -> sqlite3.Connection:
    """Return this thread's connection, creating and migrating it on first use.

    A connection per thread avoids SQLite's "objects created in a thread can
    only be used in that thread" rule. The Gradio event loop and the upload
    worker are different threads.
    """
    cached = getattr(_local, "conn", None)
    if cached is not None:
        return cached

    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    _local.conn = conn
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    # executescript() issues its own COMMIT before running, which would close
    # the transaction underneath us and make the outer COMMIT fail. So the DDL
    # runs on its own, then the version stamp is written separately.
    with _write(conn):
        for statement in _split_statements(SCHEMA):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _split_statements(script: str):
    """Split a DDL script into individual statements.

    Naive splitting on ';' would break the CREATE INDEX statements that contain
    no semicolons but do contain parentheses and commas, so parse with the
    sqlite3 helper instead of guessing.
    """
    statement = ""
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        statement += stripped + "\n"
        if sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        yield statement


@contextmanager
def _write(conn: sqlite3.Connection):
    """Run a write transaction, retrying briefly if another writer holds it."""
    delay = 0.05
    for attempt in range(6):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 5:
                raise
            time.sleep(delay)
            delay *= 2
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        # Roll back on any failure, including CancelledError, so a half-written
        # project row cannot be left visible.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def close() -> None:
    """Close this thread's connection."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def upsert_project(project: dict) -> None:
    """Create or refresh a project row from a manifest dict."""
    conn = connect()
    project_id = project.get("project_id") or project.get("id")
    if not project_id:
        raise ValueError("project dict has no project_id")
    now = _now()
    with _write(conn):
        conn.execute(
            """
            INSERT INTO projects (
                id, title, run_id, source_kind, source_url, source_video,
                source_size, target_language, speaker_detection, status,
                kernel_id, backup_link, backup_error, output_video,
                report_file, created_at, updated_at, completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                source_kind=excluded.source_kind,
                source_url=excluded.source_url,
                target_language=excluded.target_language,
                speaker_detection=excluded.speaker_detection,
                status=excluded.status,
                kernel_id=COALESCE(excluded.kernel_id, projects.kernel_id),
                backup_link=COALESCE(excluded.backup_link, projects.backup_link),
                backup_error=excluded.backup_error,
                output_video=excluded.output_video,
                report_file=excluded.report_file,
                updated_at=excluded.updated_at,
                completed_at=excluded.completed_at
            """,
            (
                project_id,
                project.get("title") or project_id,
                project.get("run_id"),
                project.get("source_kind"),
                project.get("source_url"),
                project.get("source_video"),
                project.get("source_size"),
                project.get("target_language"),
                1 if project.get("speaker_detection") else 0,
                project.get("state") or project.get("status") or "processing",
                project.get("kernel_id"),
                project.get("telegram_backup"),
                project.get("telegram_backup_error"),
                project.get("output_video"),
                project.get("report_file"),
                project.get("created_at") or now,
                now,
                project.get("completed_at"),
            ),
        )


def set_project_status(project_id: str, status: str, stage: int = None, **fields) -> None:
    """Update status and optionally stage plus any extra column."""
    conn = connect()
    assignments = ["status=?", "updated_at=?"]
    values = [status, _now()]
    if stage is not None:
        assignments.append("current_stage=?")
        values.append(stage)
    for column, value in fields.items():
        if value is not None and column in {
            "kernel_id", "backup_link", "backup_error",
            "output_video", "report_file", "completed_at",
        }:
            assignments.append(f"{column}=?")
            values.append(value)
    values.append(project_id)
    with _write(conn):
        conn.execute(
            f"UPDATE projects SET {', '.join(assignments)} WHERE id=?", values
        )


def get_project(project_id: str):
    conn = connect()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return dict(row) if row else None


def list_projects(status=None, limit: int = 100) -> list:
    conn = connect()
    if status:
        rows = conn.execute(
            "SELECT * FROM projects WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Archives and parts
# ---------------------------------------------------------------------------


def upsert_archive(journal: dict) -> int:
    """Insert or refresh an archive row from a telegram_uploader journal.

    Returns the archive id. A journal that never recorded its channel (because
    the upload died before the first part landed) is stored under a placeholder
    rather than being rejected; it simply will not be discoverable by channel
    queries.
    """
    conn = connect()
    fingerprint = journal.get("fingerprint") or _fingerprint_from_journal(journal)
    channel = (journal.get("channel") or "").strip() or UNKNOWN_CHANNEL
    now = _now()
    with _write(conn):
        conn.execute(
            """
            INSERT INTO telegram_archives (
                fingerprint, filename, file_path, file_size, channel, chunked,
                chunk_count, chunk_size, manifest_msg_id, manifest_link,
                state, error, project_id, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(fingerprint, channel) DO UPDATE SET
                state=excluded.state,
                chunked=excluded.chunked,
                chunk_count=excluded.chunk_count,
                manifest_msg_id=excluded.manifest_msg_id,
                manifest_link=excluded.manifest_link,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (
                fingerprint,
                journal.get("filename") or "?",
                journal.get("file_path"),
                int(journal.get("size") or 0),
                channel,
                1 if journal.get("chunked") else 0,
                int(journal.get("chunk_count") or 0),
                int(journal.get("manifest", {}).get("chunk_size") or 0),
                journal.get("message_id"),
                journal.get("message_link"),
                journal.get("state"),
                journal.get("error"),
                journal.get("project_id"),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM telegram_archives WHERE fingerprint=? AND channel=?",
            (fingerprint, channel),
        ).fetchone()
        archive_id = row["id"]

        for part in journal.get("parts") or []:
            conn.execute(
                """
                INSERT INTO telegram_parts (
                    archive_id, part_number, message_id, message_link,
                    offset_bytes, size_bytes, sha256
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(archive_id, part_number) DO UPDATE SET
                    message_id=excluded.message_id,
                    message_link=excluded.message_link,
                    sha256=excluded.sha256
                """,
                (
                    archive_id,
                    int(part.get("part", 0)),
                    int(part.get("message_id", 0)),
                    part.get("link"),
                    int(part.get("offset", 0)),
                    int(part.get("size", 0)),
                    part.get("sha256"),
                ),
            )
    return archive_id


def _fingerprint_from_journal(journal: dict) -> str:
    return journal.get("fingerprint") or (
        f"{journal.get('filename')}@{journal.get('size')}@{journal.get('channel')}"
    )


def attach_archive_to_project(project_id: str, archive_id: int) -> None:
    """Set the foreign key linking a run to the archive that protects it."""
    conn = connect()
    with _write(conn):
        conn.execute(
            "UPDATE projects SET backup_archive_id=?, updated_at=? WHERE id=?",
            (archive_id, _now(), project_id),
        )
        conn.execute(
            "UPDATE telegram_archives SET project_id=? WHERE id=?",
            (project_id, archive_id),
        )


def find_archive(fingerprint: str, channel: str):
    """Look up a completed archive by content fingerprint, for cache hits."""
    conn = connect()
    row = conn.execute(
        "SELECT * FROM telegram_archives WHERE fingerprint=? AND channel=?",
        (fingerprint, channel),
    ).fetchone()
    return dict(row) if row else None


def get_parts(archive_id: int) -> list:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM telegram_parts WHERE archive_id=? ORDER BY part_number",
        (archive_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Metadata, stages, metrics, errors
# ---------------------------------------------------------------------------


def upsert_media_metadata(project_id: str, meta: dict) -> None:
    conn = connect()
    with _write(conn):
        conn.execute(
            """
            INSERT INTO media_metadata (
                project_id, page_url, extractor, uploader, media_title,
                duration_sec, thumbnail_url, source_sha256, resolved_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(project_id) DO UPDATE SET
                page_url=excluded.page_url,
                extractor=excluded.extractor,
                uploader=excluded.uploader,
                media_title=excluded.media_title,
                duration_sec=excluded.duration_sec,
                thumbnail_url=excluded.thumbnail_url,
                source_sha256=excluded.source_sha256,
                resolved_at=excluded.resolved_at
            """,
            (
                project_id, meta.get("page_url"), meta.get("extractor"),
                meta.get("uploader"), meta.get("title"), meta.get("duration"),
                meta.get("thumbnail"), meta.get("sha256"), _now(),
            ),
        )


def record_stage(
    project_id: str,
    stage_number: int,
    stage_name: str,
    status: str,
    message: str = None,
    error: str = None,
    duration_sec: float = None,
) -> None:
    conn = connect()
    now = _now()
    with _write(conn):
        conn.execute(
            """
            INSERT INTO pipeline_stages (
                project_id, stage_number, stage_name, status, started_at,
                finished_at, duration_sec, message, error
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(project_id, stage_number) DO UPDATE SET
                status=excluded.status,
                finished_at=excluded.finished_at,
                duration_sec=excluded.duration_sec,
                message=excluded.message,
                error=excluded.error
            """,
            (
                project_id, int(stage_number), stage_name, status,
                now if status == "running" else None,
                now if status in ("success", "failed", "skipped") else None,
                duration_sec, message, error,
            ),
        )
        # Keep the project's stage pointer in step, so the UI can show progress
        # without joining tables.
        conn.execute(
            """
            UPDATE projects SET current_stage=MAX(current_stage, ?), updated_at=?
            WHERE id=?
            """,
            (int(stage_number), now, project_id),
        )


def record_metric(
    project_id: str,
    stage_number: int,
    name: str,
    value,
    passed=None,
    detail: str = None,
) -> None:
    """Store a numeric quality metric. Non-numeric values are kept as text."""
    conn = connect()
    numeric = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
    elif isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            detail = detail or value
    with _write(conn):
        conn.execute(
            """
            INSERT INTO quality_metrics (
                project_id, stage_number, metric_name, metric_value,
                passed, detail, measured_at
            ) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(project_id, stage_number, metric_name) DO UPDATE SET
                metric_value=excluded.metric_value,
                passed=excluded.passed,
                detail=excluded.detail,
                measured_at=excluded.measured_at
            """,
            (
                project_id, int(stage_number), name, numeric,
                None if passed is None else (1 if passed else 0),
                detail, _now(),
            ),
        )


def record_error(project_id: str, stage_number, error, retry_count: int = 0) -> None:
    conn = connect()
    with _write(conn):
        conn.execute(
            """
            INSERT INTO run_errors (
                project_id, stage_number, error_type, message, retry_count,
                occurred_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                project_id, stage_number, type(error).__name__, str(error)[:4000],
                retry_count, _now(),
            ),
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def project_summary(project_id: str) -> dict:
    """One object with a run, its archive, its stages and its metrics."""
    conn = connect()
    project = get_project(project_id)
    if project is None:
        return {}
    stages = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM pipeline_stages WHERE project_id=? ORDER BY stage_number",
            (project_id,),
        ).fetchall()
    ]
    metrics = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM quality_metrics WHERE project_id=? ORDER BY stage_number",
            (project_id,),
        ).fetchall()
    ]
    parts = []
    if project.get("backup_archive_id"):
        parts = get_parts(project["backup_archive_id"])
    meta = conn.execute(
        "SELECT * FROM media_metadata WHERE project_id=?", (project_id,)
    ).fetchone()

    return {
        "project": project,
        "stages": stages,
        "metrics": metrics,
        "parts": parts,
        "media": dict(meta) if meta else {},
    }


def import_existing_projects(projects_dir: str) -> int:
    """Import historical ``project.json`` files into the database.

    Used once when adopting the database on a machine that already has runs on
    disk. Scratch directories are skipped, and a project that is already present
    is left alone rather than overwritten, so re-running is safe.
    """
    if not os.path.isdir(projects_dir):
        return 0
    conn = connect()
    imported = 0
    for project_id in sorted(os.listdir(projects_dir)):
        # _inbox and _restored hold link downloads and archive rebuilds, not runs.
        if project_id.startswith("_"):
            continue
        manifest_path = os.path.join(projects_dir, project_id, "project.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, json.JSONDecodeError):
            # A run interrupted mid-write can leave a truncated file; skip it
            # rather than failing the whole import.
            continue
        if not manifest.get("project_id"):
            continue
        existing = conn.execute(
            "SELECT 1 FROM projects WHERE id=?", (manifest["project_id"],)
        ).fetchone()
        if existing:
            continue
        try:
            upsert_project(manifest)
            imported += 1
        except sqlite3.Error:
            continue
    return imported


def channel_usage() -> list:
    """Bytes and file counts per channel, for the multi-channel dashboard."""
    conn = connect()
    rows = conn.execute(
        """
        SELECT channel,
               COUNT(*)                        AS uploads,
               COALESCE(SUM(file_size), 0)     AS total_bytes,
               SUM(CASE WHEN state='complete' THEN 1 ELSE 0 END) AS complete,
               SUM(CASE WHEN state='failed'   THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN chunked=1        THEN 1 ELSE 0 END) AS split,
               MAX(updated_at)                 AS last_activity
        FROM telegram_archives
        GROUP BY channel
        ORDER BY total_bytes DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def timing_estimates() -> list:
    """Average stage duration across successful runs, for ETA estimates."""
    conn = connect()
    rows = conn.execute(
        """
        SELECT stage_number, stage_name,
               COUNT(*)        AS samples,
               AVG(duration_sec) AS avg_sec,
               MIN(duration_sec) AS min_sec,
               MAX(duration_sec) AS max_sec
        FROM pipeline_stages
        WHERE status='success' AND duration_sec IS NOT NULL
        GROUP BY stage_number
        ORDER BY stage_number
        """
    ).fetchall()
    return [dict(row) for row in rows]


def stats() -> dict:
    conn = connect()
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM projects)                                    AS projects,
          (SELECT COUNT(*) FROM projects WHERE status='success')             AS succeeded,
          (SELECT COUNT(*) FROM projects WHERE status='failed')              AS failed,
          (SELECT COUNT(*) FROM telegram_archives)                           AS archives,
          (SELECT COALESCE(SUM(file_size),0) FROM telegram_archives)         AS archived_bytes,
          (SELECT COUNT(*) FROM telegram_parts)                              AS parts
        """
    ).fetchone()
    return dict(row)


if __name__ == "__main__":
    conn = connect()
    print("database :", DB_PATH)
    print("version  :", conn.execute("PRAGMA user_version").fetchone()[0])
    print("tables   :", len(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()))
    print(json.dumps(stats(), indent=2))
