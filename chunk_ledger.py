"""
chunk_ledger.py -- the chunked-upload state, in SQLite, queryable.

WHY A SECOND STORE WHEN THERE IS ALREADY A JSONL MANIFEST
--------------------------------------------------------
``chunking.ChunkManifest`` is a write-ahead log: append-only, fsynced, ordered
for crash recovery. That is exactly right for the writer and exactly wrong for
questions a person asks later -- "which chunk of this 100 GB upload has been
sitting failed for three days?", "how many bytes are stored across all
archives?". Answering those from JSONL means reading every line of every file
and folding it in Python.

So this is the index, in the sense SQLITE_ROLLOUT.md means it: the manifest
stays authoritative for the transfer, this stores what can be asked about. Both
directions are supported and tested:

* ``save_manifest()`` mirrors a JSONL manifest in, and ``rebuild_manifest()``
  writes a JSONL manifest back out from here. Deleting the JSONL and rebuilding
  it must reproduce the same state -- that is the recovery test, and it is the
  property that makes this safe to rely on.
* Every transition is one transaction, so two workers racing over the same
  chunk cannot both claim it.

CONVENTIONS BORROWED FROM db.py, NOT INVENTED
--------------------------------------------
Same connection handling (per-thread connection, WAL, busy timeout, foreign
keys on, ``BEGIN IMMEDIATE`` with exponential backoff), same ``_write``
rollback-on-any-exception rule, same ``_now()`` timestamps, same table naming
(lower snake_case, plural), same ``created_at``/``updated_at`` pair, and the
same content-derived ``fingerprint`` convention as
``direct_archive._archive_fingerprint`` -- ``sha256[:32]``. The schema is
applied with ``CREATE TABLE IF NOT EXISTS`` through this module's own migrate
step rather than being spliced into ``db.SCHEMA``: this table belongs to the
chunking feature and must not force a ``db.SCHEMA_VERSION`` bump (and a
migration of every existing database) for a feature nobody has used yet.

There is deliberately NO table here that stores video bytes, and none that
duplicates ``telegram_parts``. This is per-chunk *transfer state*, keyed by the
file's content fingerprint, for the resumable-upload path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

import chunking

# Same generous timeout db.py uses: the Gradio UI reads while an upload worker
# writes, and a short timeout surfaces as "database is locked" to the user.
BUSY_TIMEOUT_MS = 15000

CHUNK_TABLE = "telegram_chunks"

CHUNK_SCHEMA = """
-- One row per chunk of one archive. The identity is the FILE's content
-- fingerprint plus the chunk index, so the same bytes uploaded to the same
-- channel always land on the same row (re-uploading updates, never duplicates)
-- and the same bytes on a different channel are correctly a different row.
CREATE TABLE IF NOT EXISTS telegram_chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT NOT NULL,
    channel         TEXT,
    filename        TEXT NOT NULL,
    file_path       TEXT,
    file_size       INTEGER NOT NULL DEFAULT 0,
    chunk_size      INTEGER,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    chunk_index     INTEGER NOT NULL,
    offset_bytes    INTEGER NOT NULL,
    size_bytes      INTEGER NOT NULL,
    sha256          TEXT,
    state           TEXT NOT NULL DEFAULT 'pending',
    message_id      INTEGER,
    message_link    TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (fingerprint, channel, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_fingerprint ON telegram_chunks(fingerprint);
CREATE INDEX IF NOT EXISTS idx_chunks_state       ON telegram_chunks(state);
CREATE INDEX IF NOT EXISTS idx_chunks_updated     ON telegram_chunks(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chunks_channel     ON telegram_chunks(channel);
"""

_local = threading.local()


def _now() -> str:
    """Same timestamp format as db._now, so rows from the two tables sort together."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _retry_on_lock(step, attempts: int = 6):
    """Run ``step``, retrying while SQLite reports the database is locked.

    The same exponential backoff, and the same budget, as _write's BEGIN
    IMMEDIATE loop, for the same reason: two connections that want a lock at the
    same instant have to take turns, and the loser only needs to look again a
    moment later. Separate from _write because the lock this guards is taken
    OUTSIDE any transaction -- see connect().
    """
    delay = 0.05
    for attempt in range(attempts):
        try:
            return step()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2


def connect(db_path: str = None) -> sqlite3.Connection:
    """This thread's connection to the chunk ledger.

    A connection per thread, because SQLite forbids sharing one across threads
    and the upload worker and the UI are different threads -- the same reason
    db.connect() exists. WAL and the busy timeout are mandatory here for the
    same concurrency they matter for in db.py.
    """
    if db_path is None:
        import db

        db_path = db.DB_PATH

    cached = getattr(_local, "conn", None)
    if cached is not None and getattr(_local, "path", None) == db_path:
        return cached

    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # journal_mode=WAL takes a database-level lock to move the file out of
    # rollback-journal mode, and the busy timeout does NOT cover that pragma on
    # every platform. So several threads creating their FIRST connections to a
    # fresh ledger -- exactly what the concurrent-mirror test does -- can all run
    # this line at once, and all but one get "database is locked". It is raised
    # here rather than inside a transaction, which is why _write's backoff never
    # sees it. Retrying is the fix, not a longer timeout: the winner finishes in
    # microseconds and the losers only need to look again.
    _retry_on_lock(lambda: conn.execute("PRAGMA journal_mode=WAL"))
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)

    _local.conn = conn
    _local.path = db_path
    return conn


@contextmanager
def _write(conn: sqlite3.Connection):
    """One transaction, rolled back on ANY exception.

    Identical in shape to db._write: BEGIN IMMEDIATE so two writers serialise
    instead of racing, an exponential-backoff retry while another writer holds
    the lock, and a rollback that catches BaseException so a cancelled transfer
    cannot leave half a chunk row behind claiming to be stored.
    """
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
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply the schema if it is not there yet.

    Statements are parsed with chunking's own splitter rather than split on
    ';', because CREATE INDEX lines contain no semicolon but do contain
    parentheses -- the naive split truncates them, exactly as db._split_statements
    had to fix.
    """
    present = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (CHUNK_TABLE,),
    ).fetchone()
    if present is not None:
        return
    with _write(conn):
        for statement in _split_statements(CHUNK_SCHEMA):
            conn.execute(statement)


def _split_statements(script: str):
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


def close() -> None:
    """Close this thread's ledger connection."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.path = None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def save_manifest(manifest: "chunking.ChunkManifest", db_path: str = None) -> int:
    """Mirror a JSONL manifest into SQLite. Returns the number of rows written.

    One transaction for the whole manifest on purpose. A half-mirrored archive
    is the state that makes "is this stored?" answerable in two contradictory
    ways, and the mirror is cheap enough that atomicity costs nothing.
    """
    conn = connect(db_path)
    header = manifest.header or {}
    fingerprint = header.get("fingerprint") or ""
    if not fingerprint:
        raise ValueError(
            "The manifest has no fingerprint; create it with "
            "ChunkManifest.create() so the ledger can key on the content."
        )
    channel = (header.get("channel") or "").strip() or "(unknown)"
    filename = header.get("filename") or "?"
    file_size = int(header.get("size") or 0)
    chunk_size = int(header.get("chunk_size") or 0)
    now = _now()
    written = 0

    with _write(conn):
        for record in manifest.chunks():
            conn.execute(
                """
                INSERT INTO telegram_chunks (
                    fingerprint, channel, filename, file_path, file_size,
                    chunk_size, chunk_count, chunk_index, offset_bytes,
                    size_bytes, sha256, state, message_id, message_link,
                    attempts, error, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(fingerprint, channel, chunk_index) DO UPDATE SET
                    size_bytes=excluded.size_bytes,
                    sha256=excluded.sha256,
                    state=excluded.state,
                    message_id=excluded.message_id,
                    message_link=excluded.message_link,
                    attempts=excluded.attempts,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    fingerprint,
                    channel,
                    filename,
                    header.get("file_path") or None,
                    file_size,
                    chunk_size,
                    manifest.chunk_count,
                    record.index,
                    record.offset,
                    record.length,
                    record.sha256,
                    record.state,
                    int(record.message_id or 0) or None,
                    record.link,
                    int(record.attempts or 0),
                    record.error or None,
                    now,
                    now,
                ),
            )
            written += 1
    return written


def set_state(
    chunk_index: int,
    state: str,
    fingerprint: str = None,
    channel: str = None,
    db_path: str = None,
    message_id: int = None,
    message_link: str = None,
    error: str = None,
    bump_attempts: bool = False,
) -> bool:
    """Move one chunk to a new state. Returns True when a row actually changed.

    ``bump_attempts`` increments the counter in the same statement, so the
    attempt and the state it produced can never disagree after a crash between
    them.
    """
    conn = connect(db_path)
    clauses = ["state=?", "updated_at=?"]
    values: list = [state, _now()]
    if message_id is not None:
        clauses.append("message_id=?")
        values.append(int(message_id) or None)
    if message_link is not None:
        clauses.append("message_link=?")
        values.append(message_link)
    if error is not None:
        clauses.append("error=?")
        values.append(str(error)[:1000] or None)
    if bump_attempts:
        clauses.append("attempts=attempts+1")

    where = ["chunk_index=?"]
    values.append(int(chunk_index))
    if fingerprint:
        where.append("fingerprint=?")
        values.append(fingerprint)
    if channel is not None:
        where.append("channel=?")
        values.append((channel or "").strip() or "(unknown)")

    with _write(conn):
        cursor = conn.execute(
            f"UPDATE telegram_chunks SET {', '.join(clauses)} "
            f"WHERE {' AND '.join(where)}",
            values,
        )
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def get_chunks(
    fingerprint: str = None, channel: str = None, state: str = None,
    db_path: str = None,
) -> list:
    """Chunk rows, newest index order. Filters are all optional."""
    conn = connect(db_path)
    clauses = []
    values: list = []
    if fingerprint:
        clauses.append("fingerprint=?")
        values.append(fingerprint)
    if channel is not None:
        clauses.append("channel=?")
        values.append((channel or "").strip() or "(unknown)")
    if state:
        clauses.append("state=?")
        values.append(state)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM telegram_chunks {where} ORDER BY fingerprint, channel, chunk_index",
        values,
    ).fetchall()
    return [dict(row) for row in rows]


def find_chunk(fingerprint: str, chunk_index: int, channel: str = None,
               db_path: str = None):
    """One chunk row, or None."""
    rows = get_chunks(fingerprint=fingerprint, channel=channel, db_path=db_path)
    for row in rows:
        if int(row["chunk_index"]) == int(chunk_index):
            return row
    return None


def summary(fingerprint: str = None, channel: str = None, db_path: str = None) -> dict:
    """What is stored, what is not, and how many bytes that is worth."""
    conn = connect(db_path)
    clauses = []
    values: list = []
    if fingerprint:
        clauses.append("fingerprint=?")
        values.append(fingerprint)
    if channel is not None:
        clauses.append("channel=?")
        values.append((channel or "").strip() or "(unknown)")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    row = conn.execute(
        f"""
        SELECT COUNT(*)                                        AS chunks,
               COALESCE(SUM(size_bytes), 0)                    AS bytes,
               COALESCE(SUM(CASE WHEN state='done' THEN size_bytes ELSE 0 END), 0)
                                                              AS stored_bytes,
               SUM(CASE WHEN state='done' THEN 1 ELSE 0 END)  AS done,
               SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN state='uploading' THEN 1 ELSE 0 END) AS uploading,
               SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS pending,
               MAX(attempts)                                   AS max_attempts
        FROM telegram_chunks {where}
        """,
        values,
    ).fetchone()
    return dict(row)


def failed_chunks(fingerprint: str = None, channel: str = None, db_path: str = None) -> list:
    """Exactly the chunks a resume should re-send, from the indexed side.

    This is the query the JSONL cannot answer cheaply: "what has been failing?"
    across every archive ever started.
    """
    return get_chunks(fingerprint=fingerprint, channel=channel, state="failed",
                      db_path=db_path)


# ---------------------------------------------------------------------------
# Rebuilding a manifest from SQLite
# ---------------------------------------------------------------------------


def rebuild_manifest(
    path: str,
    fingerprint: str = None,
    channel: str = None,
    db_path: str = None,
) -> "chunking.ChunkManifest":
    """Write a JSONL manifest back out from the ledger. Returns it.

    The recovery move for a lost or truncated manifest file. Because the ledger
    holds every field the manifest carries, the rebuilt file is equivalent --
    test_chunk_ledger asserts that equality rather than assuming it.
    """
    conn = connect(db_path)
    clauses = []
    values: list = []
    if fingerprint:
        clauses.append("fingerprint=?")
        values.append(fingerprint)
    if channel is not None:
        clauses.append("channel=?")
        values.append((channel or "").strip() or "(unknown)")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT * FROM telegram_chunks {where}
            ORDER BY fingerprint, channel, chunk_index
            """,
            values,
        ).fetchall()
    ]
    if not rows:
        raise LookupError(
            "No chunk rows in the ledger for that fingerprint/channel; there is "
            "nothing to rebuild."
        )

    first = rows[0]
    header = {
        "kind": "header",
        "manifest": chunking.MANIFEST_KIND,
        "version": chunking.MANIFEST_VERSION,
        "filename": first.get("filename") or "?",
        "file_path": first.get("file_path") or "",
        "size": int(first.get("file_size") or 0),
        "channel": first.get("channel") or "",
        "chunk_size": int(first.get("chunk_size") or 0),
        "chunk_count": int(first.get("chunk_count") or len(rows)),
        "fingerprint": first.get("fingerprint") or "",
        "source_sha256": "",
        "throughput": {},
        "rebuilt_from": "sqlite",
        "created_at": first.get("created_at") or _now(),
    }
    manifest = chunking.ChunkManifest(path, header)
    for row in rows:
        manifest.records[int(row["chunk_index"])] = chunking.ChunkRecord(
            index=int(row["chunk_index"]),
            offset=int(row["offset_bytes"]),
            length=int(row["size_bytes"]),
            sha256=row.get("sha256") or "",
            state=row.get("state") or chunking.STATE_PENDING,
            message_id=int(row.get("message_id") or 0),
            link=row.get("message_link") or "",
            attempts=int(row.get("attempts") or 0),
            error=row.get("error") or "",
        )

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        for record in manifest.chunks():
            handle.write(json.dumps(record.to_json(), ensure_ascii=False) + "\n")
    return manifest


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------

COLUMN_HELP = {
    "telegram_chunks.fingerprint": "SHA-256 of the whole file, truncated to 32 "
                                   "chars -- same key direct_archive uses, so a "
                                   "chunked upload of bytes already archived is "
                                   "recognisable as the same archive.",
    "telegram_chunks.channel": "Telegram channel this chunk was sent to. Part "
                               "of the key: same bytes to two channels are two "
                               "archives.",
    "telegram_chunks.chunk_index": "1-based chunk number within the file.",
    "telegram_chunks.offset_bytes": "Byte offset of this chunk in the source file.",
    "telegram_chunks.size_bytes": "Length of this chunk in bytes.",
    "telegram_chunks.sha256": "SHA-256 of exactly these bytes. Resume and "
                              "reassembly both verify against it.",
    "telegram_chunks.state": "pending | uploading | done | failed. A chunk left "
                             "as 'uploading' is one the process died sending, and "
                             "resume re-sends it.",
    "telegram_chunks.attempts": "How many times this chunk has been sent. An "
                                "attempt that died mid-flight counts, because "
                                "that is the one worth knowing about.",
    "telegram_chunks.message_link": "Telegram message holding this chunk.",
    "telegram_chunks.message_id": "Telegram message id for this chunk.",
    "telegram_chunks.file_path": "Where the source file was when it was "
                                 "mirrored. Lets a rebuilt manifest resume "
                                 "without being told the path again.",
    "telegram_chunks.chunk_count": "How many chunks the whole file has.",
    "telegram_chunks.chunk_size": "Chunk size in bytes this file was planned "
                                  "with. Adaptive, so it is what the link was "
                                  "doing when the plan was made.",
}

# Suffix rules, borrowed from db._SUFFIX_HELP: they explain the majority of
# columns without a 20-row table, and every column still ends up with meaning.
# Mirroring db.py's structure on purpose -- a second, thinner help table would
# drift from the first the moment a column is added to one and not the other.
_SUFFIX_HELP = (
    ("_sha256", "SHA-256 checksum of the bytes"),
    ("_index", "Position in a sequence"),
    ("_number", "Number / index"),
    ("_count", "Count"),
    ("_name", "Naam"),
    ("_filename", "Naam"),
    ("_path", "File path"),
    ("_file", "File path"),
    ("_size", "Size (is column ke unit me)"),
    ("_bytes", "Size in bytes"),
    ("_link", "Clickable URL"),
    ("_error", "Error text (NULL = koi error nahi)"),
    ("_state", "State machine value"),
    ("_at", "Timestamp (ISO-8601)"),
    ("_id", "Identifier - dusre table se juda hua (foreign key)"),
)

_WORD_HELP = {
    "id": "Row identifier (primary key).",
    "filename": "File ka naam (path nahi)",
    "channel": "Telegram channel jisme upload gaya",
    "state": "pending | uploading | done | failed",
    "attempts": "Kitni baar is chunk ko bheja gaya",
    "file_size": "Puri file ka size (bytes)",
    "chunk_size": "Ek chunk ka size (bytes)",
    # Bare, no suffix -- which is exactly why it needs naming here. db.py lists
    # the same one for the same reason.
    "error": "Why the last attempt failed (NULL = koi error nahi)",
}


def column_help(column: str) -> str:
    """Plain-English meaning of one column.

    Specific table.column first, then a bare word, then a suffix rule. Every
    column of the table ends up with something, because a column with a name and
    no meaning is how SQLite stays unreadable -- the same argument db._column_help
    makes, in the same shape.
    """
    specific = COLUMN_HELP.get(f"{CHUNK_TABLE}.{column}")
    if specific:
        return specific
    generic = _WORD_HELP.get(column)
    if generic:
        return generic
    for suffix, text in _SUFFIX_HELP:
        if column.endswith(suffix):
            return text
    return "Value"