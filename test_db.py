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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db


def _use_temp_db(path):
    """Point db at a scratch file so the real database is never touched."""
    original = db.DB_PATH
    db.DB_PATH = path
    for thread_attr in (db._local,):
        if hasattr(thread_attr, "conn"):
            thread_attr.conn.close()
            del thread_attr.conn
    return original


def _restore_db(original):
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
        del db._local.conn
    db.DB_PATH = original


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
        "speakers", "job_queue",
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
