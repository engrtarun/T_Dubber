"""Remove the two test-fixture project rows, with a real backup first.

``projects`` is HISTORY (Tarun decided 22:25) -- so genuine runs are never
deleted. These two are not genuine runs: ``test_db.py`` upserts them as
fixtures, and neither has a project directory:

    lucifer-aaa-bbb     <- test_db.py line ~242, literal id
    weeknd-ccc-ddd      <- same

The backup uses sqlite's online backup API rather than a file copy, because
the database is in WAL mode: copying the .db alone would miss everything still
sitting in the -wal file.
"""

import os
import shutil
import sqlite3
import sys

import db

TARGETS = ("lucifer-aaa-bbb", "weeknd-ccc-ddd")
BACKUP = os.path.join(os.environ.get("TEMP", "."), "t_dubber.db.before2rows")

conn = db.connect()

rows = conn.execute(
    "select id, title, status, created_at from projects where id in (?, ?)",
    TARGETS,
).fetchall()
print("targets found:", len(rows))
for row in rows:
    print("  ", tuple(row))

if len(rows) != len(TARGETS):
    print("ABORT: expected 2 fixture rows, found", len(rows))
    sys.exit(1)

# Online backup: consistent even with the -wal file in play.
dest = sqlite3.connect(BACKUP)
with dest:
    conn.backup(dest)
dest.close()
print("backup written:", BACKUP, os.path.getsize(BACKUP), "bytes")

before = conn.execute("select count(*) from projects").fetchone()[0]
with db._write(conn):
    for pid in TARGETS:
        conn.execute("delete from pipeline_stages where project_id=?", (pid,))
        conn.execute("delete from projects where id=?", (pid,))
after = conn.execute("select count(*) from projects").fetchone()[0]
left = conn.execute(
    "select count(*) from projects where id in (?, ?)", TARGETS
).fetchone()[0]
dangling = conn.execute(
    "select count(*) from projects where backup_archive_id is not null "
    "and backup_archive_id not in (select id from telegram_archives)"
).fetchone()[0]

print(f"projects {before} -> {after} (delta {after - before})")
print("targets still present:", left)
print("dangling FK:", dangling)
