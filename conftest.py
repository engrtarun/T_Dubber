"""Pytest glue for the repo's standalone test scripts.

test_flow_order.py (and any other script that asks for a ``scratch``
argument) is written unittest-style: each test function takes a
``scratch`` parameter that is a plain **string** directory path.
pytest has no built-in fixture with that contract, so collecting the
file under pytest used to fail with ``fixture 'scratch' not found``
-- even though the script runs perfectly on its own
(``python test_flow_order.py`` -> 7/7 pass, exit=0, MEASURED).

This conftest fills that gap: ``scratch`` is a fresh temp directory
(a string, not a Path) per test, cleaned up afterwards.
"""

import os
import shutil
import tempfile

import pytest


@pytest.fixture
def scratch():
    path = tempfile.mkdtemp(prefix="pytest_scratch_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def state():
    """Scratch state dir for test_tg_cloud.py (self-cleans after each test)."""
    path = tempfile.mkdtemp(prefix="tgcloud_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _scratch_database_for_every_test():
    """No test may ever write to the real ``t_dubber.db`` -- not just the ones
    that remembered to ask for it.

    Measured 2026-10-09: ``test_p0_direct.py`` and ``test_flow_order.py`` each
    pass on their own and each leave ``projects`` unchanged (delta 0), yet run
    together they wrote **4 rows** into production. Cause: both modules redirect
    ``db.DB_PATH`` once at *import* time (``test_isolation.use_scratch_db()``,
    which is idempotent per process), so under pytest there is no per-test
    redirect at all -- and ``test_p0_direct.py::test_helper_redirects_and_can_
    restore`` calls ``test_isolation.restore()`` in its ``finally``, which points
    ``db.DB_PATH`` back at production *while* test_flow_order still believes it
    owns a scratch redirect. Every row written after that landed in the real DB.

    Order dependence like this is exactly what a green board hides, so the
    redirect lives here, in one place, for every test: setup points ``db.DB_PATH``
    at a fresh throwaway database, teardown closes the cached connection and puts
    the production path back.

    Verify with ``count(*)``, never with the file's mtime -- SQLite is in WAL
    mode, so writes land in ``-wal`` and the main file only changes on checkpoint.
    """
    import db
    import test_isolation

    production = test_isolation.production_db_path()
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
        db._local.conn = None
    scratch_dir = tempfile.mkdtemp(prefix="pytest_db_")
    db.DB_PATH = os.path.join(scratch_dir, "t_dubber.db")
    # Publish the redirect through test_isolation as well, so a module that
    # called use_scratch_db() at import time still gets an honest answer if it
    # asks again mid-test. use_scratch_db() is idempotent per process, so
    # without this it would hand back a path that is no longer the active one.
    # The remembered "previous" path is production, not whatever a module-level
    # import happened to leave behind: after a test the guard must hand the next
    # test the real thing back, never another suite's scratch file.
    test_isolation._PREVIOUS = production
    test_isolation._SCRATCH = db.DB_PATH

    # The upload journals need the same treatment as the database.
    #
    # telegram_uploader.STATE_DIR is one shared directory at the repo root
    # (.tg_uploads), and a journal is keyed by CONTENT FINGERPRINT. Two tests
    # that use the same bytes therefore share one journal -- and a journal in
    # state `complete` short-circuits the upload ("already archived"), so a later
    # test silently loses the very event it is asserting on. That is how
    # test_flow_order's zero-disk refusal test failed once out of six runs while
    # passing in isolation and in every pair: a shared file the database
    # redirect could not see.
    #
    # Measured 2026-10-09: after this guard, six consecutive full-suite runs left
    # .tg_uploads untouched and the suite was green every time.
    journal_dir = None
    journal_owner = None
    try:
        import telegram_uploader

        journal_owner = telegram_uploader
        journal_dir = os.path.join(scratch_dir, "tg_uploads")
        telegram_uploader.STATE_DIR = journal_dir
    except Exception:  # noqa: BLE001 - a missing optional import must not stop the suite
        journal_owner = None

    try:
        yield
    finally:
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
            db._local.conn = None
        db.DB_PATH = production
        test_isolation._PREVIOUS = None
        test_isolation._SCRATCH = None
        if journal_owner is not None and journal_dir is not None:
            journal_owner.STATE_DIR = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), ".tg_uploads"
            )
        shutil.rmtree(scratch_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _restore_patched_app_attributes():
    """Keep monkeypatching inside the test that did it.

    test_flow_order.py's tests swap ``app.*`` handlers and rely
    on its script runner to restore them between tests (its
    ``finally`` block puts every original back). Under pytest
    there is no runner, so the first test that patches an app
    attribute leaks the fake into every test after it --
    MEASURED: test_real_backup_short_circuits_for_telegram_
    sources saw test 4's fake_backup stub, yielded no lines,
    and asserted against an empty log while the same test
    passed under ``python test_flow_order.py``.

    This fixture snapshots the app module around every test
    and puts it back: the same contract the script runner
    had, for the whole suite.
    """
    import app
    snapshot = dict(vars(app))
    yield
    live = vars(app)
    for name in list(live):
        if name not in snapshot:
            delattr(app, name)
        elif live[name] is not snapshot[name]:
            setattr(app, name, snapshot[name])
