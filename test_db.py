"""
Tests for the SQLite layer.

Covers the parts most likely to break in real use: concurrent access from the
UI thread and an upload worker, the ON CONFLICT upsert semantics, the rollback
path, and the historical data import.

Run with:  python test_db.py
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# These tests write projects, archives and parts. Without a redirect they land
# in the production t_dubber.db -- which is where 27 rows named p1/p2/p3,
# seed, w0-0..w3-4, lucifer-abc123-def456 and demo-0..2 came from.
import test_isolation

test_isolation.use_scratch_db(prefix="tdub_db_")

import db  # noqa: E402


def _use_temp_db(path):
    """Point db at a scratch file so the real database is never touched."""
    original = db.DB_PATH
    db.DB_PATH = path
    # `hasattr(_local, "conn")` is True even when the attribute was explicitly
    # set to None, which is what every other test module here does on teardown
    # (`db._local.conn = None`). Asking hasattr then calling .close() on None is
    # why the whole suite passed but `pytest test_db.py` ERRORED with 12 errors
    # once it ran after test_p0_direct.py / test_archive_roundtrip.py.
    # MEASURED 2026-10-09: `pytest test_db.py` alone = 12 passed; the same file
    # after a module that nulls the thread-local = 12 errors, 53 passed.
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
        db._local.conn = None
    return original


def _restore_db(original):
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
        del db._local.conn
    db.DB_PATH = original


@pytest.fixture(autouse=True)
def _scratch_db(scratch):
    """Give every test its own scratch database.

    The script runner (``main``) redirects the database once for
    the whole run; under pytest there is no runner, so each test
    would otherwise read and write the real t_dubber.db -- the
    stats assertions (``projects == 0``) prove the tests expect
    a fresh file. Same redirect, per test, so both runners agree.
    """
    original = _use_temp_db(str(Path(scratch) / "t_dubber.db"))
    yield
    _restore_db(original)


def test_schema_and_stats(scratch):
    print("\n[1] schema: all tables exist and start empty")
    conn = db.connect()
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected = {
        "projects", "telegram_archives", "telegram_parts", "media_metadata",
        "pipeline_stages", "quality_metrics", "run_errors", "voice_samples",
        "speakers", "job_queue", "kaggle_sweeper_logs", "channel_daily_quota",
    }
    assert expected <= tables, f"missing: {expected - tables}"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.stats()["projects"] == 0

    # WAL is not optional here: the UI reads while a worker writes.
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal", f"journal_mode is {mode}, expected wal"
    print(f"    {len(tables)} tables, version {db.SCHEMA_VERSION}, journal_mode=wal")


def test_project_upsert_is_idempotent(scratch):
    print("\n[2] projects: upsert twice does not duplicate or lose fields")
    manifest = {
        "project_id": "lucifer-abc123-def456",
        "title": "lucifer",
        "run_id": "def456",
        "state": "processing",
        "target_language": "Hindi",
        "source_kind": "upload",
        "source_video": "LuciferS01E13.mkv",
        "created_at": "2026-10-03T20:00:00+05:30",
    }
    db.upsert_project(manifest)
    db.upsert_project(manifest)
    assert db.stats()["projects"] == 1, db.stats()

    db.set_project_status(
        "lucifer-abc123-def456", "success", stage=7, kernel_id="user/worker"
    )
    row = db.get_project("lucifer-abc123-def456")
    assert row["status"] == "success"
    assert row["current_stage"] == 7
    assert row["kernel_id"] == "user/worker"

    # A later partial update must not blank the kernel id.
    db.upsert_project({**manifest, "state": "processing"})
    row = db.get_project("lucifer-abc123-def456")
    assert row["kernel_id"] == "user/worker", row["kernel_id"]
    print("    kernel_id survived a partial re-upsert (COALESCE works)")


def test_archive_parts_and_foreign_key(scratch):
    print("\n[3] archives: parts recorded and linked to their project")
    journal = {
        "filename": "movie.mkv",
        "file_path": "C:/tmp/movie.mkv",
        "size": 9_955_747_524,
        "channel": "@tgwebcloud1",
        "chunked": True,
        "chunk_count": 5,
        "state": "uploading",
        "message_id": 99,
        "message_link": "https://t.me/tgwebcloud1/99",
        "parts": [
            {
                "part": n,
                "message_id": 100 + n,
                "link": f"https://t.me/tgwebcloud1/{100 + n}",
                "offset": (n - 1) * 1_992_294_400,
                "size": 1_992_294_400,
                "sha256": f"{n:064x}",
            }
            for n in range(1, 6)
        ],
    }
    archive_id = db.upsert_archive(journal)
    db.upsert_project({"project_id": "p1", "title": "p1", "state": "processing"})
    db.attach_archive_to_project("p1", archive_id)

    parts = db.get_parts(archive_id)
    assert len(parts) == 5, len(parts)
    assert parts[0]["part_number"] == 1
    assert parts[-1]["sha256"].endswith("5")

    project = db.get_project("p1")
    assert project["backup_archive_id"] == archive_id

    # Re-upserting the same journal must update, not duplicate.
    journal["state"] = "complete"
    same_id = db.upsert_archive(journal)
    assert same_id == archive_id
    assert db.stats()["archives"] == 1
    assert len(db.get_parts(archive_id)) == 5

    found = db.find_archive(journal["filename"] + "@" + str(journal["size"]) + "@@tgwebcloud1",
                            "@tgwebcloud1")
    assert found and found["state"] == "complete"
    print("    5 parts recorded, upsert idempotent, foreign key set")


def test_stages_metrics_and_estimates(scratch):
    print("\n[4] stages and metrics: queryable, and drive timing estimates")
    db.upsert_project({"project_id": "p2", "title": "p2", "state": "processing"})
    for stage, name, secs in (
        (1, "Compress", 30.0),
        (2, "Bundle", 5.0),
        (3, "Dataset upload", 120.0),
    ):
        db.record_stage("p2", stage, name, "running")
        db.record_stage("p2", stage, name, "success", duration_sec=secs)

    db.record_stage("p2", 4, "Kernel push", "failed", error="HTTP 500")
    db.record_error("p2", 4, RuntimeError("HTTP 500"), retry_count=1)

    db.record_metric("p2", 7, "coverage", 0.94, passed=True)
    db.record_metric("p2", 7, "devanagari_ratio", 0.31, passed=True)
    db.record_metric("p2", 7, "wer", "Not measured")

    summary = db.project_summary("p2")
    assert len(summary["stages"]) == 4
    assert len(summary["metrics"]) == 3
    assert summary["project"]["current_stage"] == 4, summary["project"]["current_stage"]

    wer = [m for m in summary["metrics"] if m["metric_name"] == "wer"][0]
    assert wer["metric_value"] is None and wer["detail"] == "Not measured", dict(wer)

    coverage = [m for m in summary["metrics"] if m["metric_name"] == "coverage"][0]
    assert abs(coverage["metric_value"] - 0.94) < 1e-9
    assert coverage["passed"] == 1

    estimates = db.timing_estimates()
    names = [row["stage_name"] for row in estimates]
    assert "Compress" in names and "Kernel push" not in names, names
    print(f"    averages over {len(estimates)} successful stages, failures excluded")


def test_rollback_on_error(scratch):
    print("\n[5] transaction: a failure rolls back instead of half-writing")
    db.upsert_project({"project_id": "p3", "title": "p3", "state": "processing"})
    conn = db.connect()
    before = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]

    class Boom(Exception):
        pass

    try:
        with db._write(conn):
            conn.execute(
                "INSERT INTO projects (id, title, status) VALUES ('p4','p4','x')"
            )
            raise Boom("simulated mid-transaction failure")
    except Boom:
        pass

    after = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    assert after == before, f"expected rollback, went from {before} to {after}"
    assert db.get_project("p4") is None
    print("    the partial insert was rolled back")


def test_concurrent_writers(scratch):
    print("\n[6] concurrency: several threads writing at once must not deadlock")
    db.upsert_project({"project_id": "seed", "title": "seed", "state": "processing"})
    errors = []
    done = []

    def worker(index):
        try:
            for n in range(5):
                db.upsert_project({
                    "project_id": f"w{index}-{n}",
                    "title": f"w{index}-{n}",
                    "state": "processing",
                })
                db.record_stage(f"w{index}-{n}", 1, "Compress", "success", duration_sec=1.0)
            done.append(index)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"threads failed: {errors}"
    assert sorted(done) == [0, 1, 2, 3], done
    assert db.stats()["projects"] == 21, db.stats()["projects"]
    print(f"    4 threads x 10 writes = 40 operations, {len(done)} completed, no lock errors")


def test_migrate_existing_projects(scratch):
    print("\n[7] import: existing projects/*/project.json files land in the database")
    projects_dir = os.path.join(scratch, "projects")
    made = 0
    for name in ("lucifer-aaa-bbb", "weeknd-ccc-ddd"):
        folder = os.path.join(projects_dir, name)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "project.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "project_id": name,
                    "title": name.split("-")[0],
                    "state": "processing",
                    "target_language": "Hindi",
                    "telegram_backup": f"https://t.me/tgwebcloud1/{made + 20}",
                    "kernel_id": "engrtarun/dubber-worker-homura",
                },
                handle,
            )
        made += 1
    # Scratch directories must be skipped, not imported as projects.
    for junk in ("_inbox", "_restored"):
        folder = os.path.join(projects_dir, junk)
        os.makedirs(folder, exist_ok=True)

    original_dir = db.PROJECTS_DIR if hasattr(db, "PROJECTS_DIR") else None
    db.PROJECTS_DIR = projects_dir
    try:
        imported = db.import_existing_projects(projects_dir)
    finally:
        if original_dir is None:
            delattr(db, "PROJECTS_DIR")
        else:
            db.PROJECTS_DIR = original_dir

    assert imported == 2, f"imported {imported}, expected 2"
    rows = db.list_projects()
    ids = {r["id"] for r in rows}
    assert ids == {"lucifer-aaa-bbb", "weeknd-ccc-ddd"}, ids
    assert all(r["kernel_id"] == "engrtarun/dubber-worker-homura" for r in rows)
    print(f"    {imported} historical projects imported, _inbox/_restored skipped")


def test_channel_usage(scratch):
    print("\n[8] reporting: per-channel usage and totals")
    for channel in ("@tgwebcloud1", "@tgwebcloud2"):
        for n in (1, 2):
            db.upsert_archive({
                "filename": f"{channel}-{n}.mkv",
                "size": 1000 * n,
                "channel": channel,
                "chunked": n == 2,
                "chunk_count": n if n == 2 else 1,
                "state": "complete",
                "parts": [],
            })
    usage = db.channel_usage()
    by_channel = {row["channel"]: row for row in usage}
    assert set(by_channel) == {"@tgwebcloud1", "@tgwebcloud2"}, usage
    assert by_channel["@tgwebcloud1"]["uploads"] == 2
    assert by_channel["@tgwebcloud1"]["total_bytes"] == 3000
    assert by_channel["@tgwebcloud2"]["split"] == 1
    stats = db.stats()
    assert stats["archives"] == 4
    print("    " + ", ".join(
        f"{row['channel']}={row['total_bytes']}B/{row['uploads']}files"
        for row in usage
    ))


def test_sweeper_log_sync(scratch):
    print("\n[9] sweeper: JSONL audit trail drains into the database")
    audit_file = os.path.join(scratch, "space_sweeper_audit.jsonl")
    entries = [
        {"ts": "2026-10-03T03:00:01+00:00", "action": "delete",
         "kind": "dataset", "ref": "engrtarun/dubbing-input-001",
         "title": "Dubbing input 001", "bytes": 2_000_000_000},
        {"ts": "2026-10-03T03:00:02+00:00", "action": "delete",
         "kind": "kernel", "ref": "engrtarun/worker-homura-001",
         "title": "worker-homura run", "bytes": 500_000},
        # Torn line (e.g. sweeper was mid-append) must be preserved.
        '{"ts": "2026-10-03T03:00:03+00:00", "action": "delete", "kind":',
    ]
    with open(audit_file, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n" if isinstance(entry, dict) else entry + "\n")

    summary = db.sync_sweeper_logs(audit_file)
    assert summary == {"inserted": 2, "duplicates": 0, "kept_lines": 1}, summary

    logs = db.list_sweeper_logs()
    assert len(logs) == 2, logs
    assert logs[0]["ref"] == "engrtarun/worker-homura-001"  # newest first
    assert logs[1]["bytes_freed"] == 2_000_000_000
    assert db.stats()["sweeper_logs"] == 2

    # A second sync of the same data must be a no-op, not a double count.
    with open(audit_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entries[0]) + "\n")
    summary = db.sync_sweeper_logs(audit_file)
    assert summary["inserted"] == 0 and summary["duplicates"] == 1, summary
    assert db.stats()["sweeper_logs"] == 2

    # The torn line survives in the file for the next pass.
    with open(audit_file, "r", encoding="utf-8") as handle:
        leftover = handle.read()
    assert "action" in leftover and "engrtarun" not in leftover, leftover
    print(f"    drained 2 entries, idempotent re-sync, 1 torn line kept")


def test_channel_load_balancer(scratch):
    print("\n[10] balancer: round robin with a 50 GB-style daily quota")
    channels_file = os.path.join(scratch, "channels.json")
    with open(channels_file, "w", encoding="utf-8") as handle:
        json.dump({"channels": ["@tgwebcloud1", "@tgwebcloud2", "@tgwebcloud3"],
                   "strategy": "round_robin", "default_index": 0}, handle)

    original_quota = db.CHANNEL_DAILY_QUOTA_MB
    db.CHANNEL_DAILY_QUOTA_MB = 100  # 100 MB keeps the test fast
    original_channels = db.CHANNELS_FILE
    db.CHANNELS_FILE = channels_file
    try:
        # Fresh day starts at default_index and walks the roster in order.
        assert db.get_next_telegram_channel(40, channels_file, day="2026-01-01") == "@tgwebcloud1"
        assert db.get_next_telegram_channel(40, channels_file, day="2026-01-01") == "@tgwebcloud2"
        assert db.get_next_telegram_channel(40, channels_file, day="2026-01-01") == "@tgwebcloud3"
        # Wraps around: cloud1 has 40 MB used, 40 more still fits under 100.
        assert db.get_next_telegram_channel(40, channels_file, day="2026-01-01") == "@tgwebcloud1"

        # cloud1 is at 80 MB; 30 MB does not fit, so the balancer
        # skips it and lands on the next channel with room.
        assert db.get_next_telegram_channel(30, channels_file, day="2026-01-01") == "@tgwebcloud2"

        # Fill every channel to the ceiling; the balancer must refuse
        # rather than oversubscribe a channel.
        db.get_next_telegram_channel(20, channels_file, day="2026-01-01")  # cloud3 -> 60
        db.get_next_telegram_channel(30, channels_file, day="2026-01-01")  # cloud2 -> 100
        db.get_next_telegram_channel(20, channels_file, day="2026-01-01")  # cloud3 -> 80
        db.get_next_telegram_channel(20, channels_file, day="2026-01-01")  # cloud1 -> 100
        db.get_next_telegram_channel(20, channels_file, day="2026-01-01")  # cloud3 -> 100
        assert db.get_next_telegram_channel(1, channels_file, day="2026-01-01") is None

        # A new day resets every quota; an oversized video is never placed.
        assert db.get_next_telegram_channel(40, channels_file, day="2026-01-02") == "@tgwebcloud1"
        assert db.get_next_telegram_channel(101, channels_file, day="2026-01-02") is None

        # A failed upload gives its quota back.
        db.release_channel_quota("@tgwebcloud1", 40, day="2026-01-02")
        status = {row["channel"]: row for row in db.channel_quota_status(day="2026-01-02")}
        assert status["@tgwebcloud1"]["used_mb"] == 0.0, status
    finally:
        db.CHANNEL_DAILY_QUOTA_MB = original_quota
        db.CHANNELS_FILE = original_channels
    print("    6 channels rotate in order, quota enforced, failures refunded")


def test_channels_roster_shapes(scratch):
    print("\n[11] roster: dict shape and bare-list shape both load")
    dict_file = os.path.join(scratch, "dict.json")
    with open(dict_file, "w", encoding="utf-8") as handle:
        json.dump({"channels": ["@a", "@b"], "default_index": 1}, handle)
    list_file = os.path.join(scratch, "list.json")
    with open(list_file, "w", encoding="utf-8") as handle:
        json.dump(["x1", "x2", "x3"], handle)

    channels, index = db._load_channels(dict_file)
    assert channels == ["@a", "@b"] and index == 1, (channels, index)

    channels, index = db._load_channels(list_file)
    # Bare lists have no declared start, so they begin at 0.
    assert channels == ["x1", "x2", "x3"] and index == 0, (channels, index)

    # Names pass through untouched: tgup's trimAt() handles '@'.
    bare_file = os.path.join(scratch, "bare.json")
    with open(bare_file, "w", encoding="utf-8") as handle:
        json.dump(["tgwebcloud1", "@tgwebcloud2"], handle)
    channels, _ = db._load_channels(bare_file)
    assert channels == ["tgwebcloud1", "@tgwebcloud2"], channels

    # An empty roster is a deployment error, not a silent no-op.
    empty_file = os.path.join(scratch, "empty.json")
    with open(empty_file, "w", encoding="utf-8") as handle:
        json.dump([], handle)
    try:
        db._load_channels(empty_file)
        raise AssertionError("empty roster should raise")
    except ValueError:
        pass
    print("    dict, bare list, and '@'-mixing rosters all load")


def test_database_introspection(scratch):
    print("\n[12] introspection: the Database tab reads itself and explains itself")
    for n in range(3):
        db.upsert_project({
            "project_id": f"demo-{n}-aaa111",
            "title": f"demo {n}",
            "state": "processing",
            "target_language": "Hindi",
            "source_kind": "upload",
            "source_video": f"demo{n}.mkv",
            "created_at": "2026-10-05T10:00:00+05:30",
        })

    tables = db.table_names()
    assert "projects" in tables, "projects missing from the table list"
    assert not any(name.startswith("sqlite_") for name in tables), \
        "sqlite's own bookkeeping would show up as a browsable table"

    info = db.database_overview()
    assert info["path"] == db.DB_PATH, info["path"]
    assert info["journal_mode"].lower() == "wal", info["journal_mode"]
    assert info["schema_version"] == db.SCHEMA_VERSION
    assert info["row_counts"]["projects"] == 3, info["row_counts"]
    assert info["total_rows"] >= 3, info["total_rows"]

    # The whole point of the tab: a column is never just a name.
    unexplained = []
    for table in tables:
        schema = db.describe_table(table)
        if not schema["description"]:
            unexplained.append(table + " (table)")
        for column in schema["columns"]:
            if not column["help"]:
                unexplained.append(f"{table}.{column['name']}")
    assert not unexplained, f"no plain-English meaning for: {unexplained}"

    projects = db.describe_table("projects")
    assert projects["columns"][0]["name"] == "id" and projects["columns"][0]["pk"], \
        "projects.id should be flagged as the primary key"
    parts = db.describe_table("telegram_parts")
    assert any(k["ref_table"] == "telegram_archives" for k in parts["foreign_keys"]), \
        "telegram_parts should report its foreign key"

    page1 = db.query_table("projects", page=1, per_page=2)
    page2 = db.query_table("projects", page=2, per_page=2)
    assert page1["total"] == 3 and page1["pages"] == 2, (page1["total"], page1["pages"])
    assert len(page1["rows"]) == 2 and len(page2["rows"]) == 1
    assert len({r["id"] for r in page1["rows"] + page2["rows"]}) == 3, "pages overlap"

    hit = db.query_table("projects", "demo 1")
    assert hit["total"] == 1 and hit["rows"][0]["title"] == "demo 1", hit["total"]

    # LIKE metacharacters are escaped: '%' is text, not "match everything".
    assert db.query_table("projects", "%")["total"] == 0, "wildcard was not escaped"

    # The name is bound after being checked against sqlite_master, so an
    # injection attempt is refused rather than executed.
    for bad in ("projects; DROP TABLE projects", 'projects"', "sqlite_master", "nope"):
        try:
            db.query_table(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"table {bad!r} should have been refused")

    assert db.stats()["projects"] == 3, "a read path wrote to the database"
    print(f"    {len(tables)} tables, {info['total_rows']} rows, "
          f"every column explained, search + pagination verified")


# ---------------------------------------------------------------------------


def main():
    tests = [
        test_schema_and_stats,
        test_project_upsert_is_idempotent,
        test_archive_parts_and_foreign_key,
        test_stages_metrics_and_estimates,
        test_rollback_on_error,
        test_concurrent_writers,
        test_migrate_existing_projects,
        test_channel_usage,
        test_sweeper_log_sync,
        test_channel_load_balancer,
        test_channels_roster_shapes,
        test_database_introspection,
    ]

    original_db = db.DB_PATH
    failures = []

    for test in tests:
        scratch = tempfile.mkdtemp(prefix="tdb_")
        db_path = os.path.join(scratch, "test.db")
        try:
            _use_temp_db(db_path)
            test(scratch)
        except AssertionError as exc:
            failures.append((test.__name__, str(exc)))
            print(f"    FAIL: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, repr(exc)))
            print(f"    ERROR: {exc!r}")
        finally:
            _restore_db(original_db)
            shutil.rmtree(scratch, ignore_errors=True)

    print("\n" + "=" * 62)
    if failures:
        for name, message in failures:
            print(f"FAILED  {name}: {message}")
        print(f"{len(failures)}/{len(tests)} tests failed")
        return 1
    print(f"All {len(tests)} database tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
