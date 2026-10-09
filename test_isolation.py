"""test_isolation.py -- keep tests out of the production database.

WHY THIS EXISTS
---------------
These test scripts redirect ``app.PROJECTS_DIR`` into a scratch folder, so the
project *directories* land somewhere harmless. They never redirected
``db.DB_PATH``, so every ``db.upsert_project()`` wrote a real row into the real
``t_dubber.db``.

Measured consequence on this repository, 2026-10-08:

    projects rows          : 232
    test-fixture rows      : 200   (86%)
    distinct test fixtures :   4   (clip / clip2 / Some_YouTube_Video / restored)
    re-runs of each        :  47

So most of what looked like "the database is full of rubbish" was the test
suite writing to production, 47 times over. It also meant the suite could never
be run against a database that mattered, and it made the dashboard's numbers
meaningless.

USAGE
-----
Put this near the top of any test module that touches the database, before it
calls into app/db:

    import test_isolation
    test_isolation.use_scratch_db()

Call it at import time, not inside a test function, so a module-level fixture
cannot slip past it.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile

# Set once per process. Import order matters: this has to run before app is
# imported by the caller, or db.connect() may already have cached a connection.
_SCRATCH: str | None = None
# What ``db.DB_PATH`` pointed at before ``use_scratch_db()`` moved it. Kept so
# ``restore()`` undoes *its own* redirect instead of hardcoding the production
# path -- under pytest the conftest guard owns the redirect, and a restore() that
# jumps straight to production fights it (and hands the next test a live
# connection to the real database).
_PREVIOUS: str | None = None


def production_db_path() -> str:
    """Where the real database lives."""
    root = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, "t_dubber.db")


def use_scratch_db(prefix: str = "tdub_test_") -> str:
    """Point ``db.DB_PATH`` at a throwaway database. Idempotent per process.

    Returns the scratch path. The original ``db.DB_PATH`` is restored by
    :func:`restore`, which is mostly useful for a script that wants to inspect
    production afterwards without reimporting.
    """
    global _SCRATCH, _PREVIOUS
    if _SCRATCH is not None:
        return _SCRATCH

    scratch = os.path.join(tempfile.gettempdir(), prefix + str(os.getpid()))
    for suffix in ("", "-wal", "-shm"):
        candidate = scratch + suffix
        if os.path.exists(candidate):
            os.unlink(candidate)

    root = os.path.dirname(os.path.abspath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)

    import db

    _SCRATCH = scratch
    _PREVIOUS = db.DB_PATH
    db.DB_PATH = scratch
    # A connection cached against the old path would keep writing to it.
    if getattr(db._local, "conn", None) is not None:
        db._local.conn.close()
        db._local.conn = None
    db.connect()
    return scratch


def restore() -> None:
    """Point ``db.DB_PATH`` back at what it was before :func:`use_scratch_db`."""
    global _SCRATCH, _PREVIOUS
    import db

    if getattr(db._local, "conn", None) is not None:
        db._local.conn.close()
        db._local.conn = None
    db.DB_PATH = _PREVIOUS if _PREVIOUS is not None else production_db_path()
    _SCRATCH = None
    _PREVIOUS = None


def production_row_count(table: str = "projects") -> int:
    """How many rows the production database has right now.

    Used to prove that running the suite changes nothing. Returns ``-1`` when
    the database does not exist yet, so a caller can tell "empty" from "absent".
    """
    path = production_db_path()
    if not os.path.isfile(path):
        return -1
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return -1


def cleanup_scratch() -> None:
    """Remove the scratch database created by :func:`use_scratch_db`."""
    if _SCRATCH and os.path.isfile(_SCRATCH):
        shutil.rmtree(os.path.dirname(_SCRATCH), ignore_errors=True) if False else None
        for suffix in ("", "-wal", "-shm"):
            candidate = _SCRATCH + suffix
            if os.path.exists(candidate):
                try:
                    os.unlink(candidate)
                except OSError:
                    pass
