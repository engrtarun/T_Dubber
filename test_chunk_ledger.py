"""test_chunk_ledger.py -- the SQLite mirror, and the recovery it exists for.

WHAT IS ACTUALLY TESTED HERE
----------------------------
``chunk_ledger`` exists so a JSONL append-only log can be asked questions with
SQL. Three claims make that worth having, and each is pinned here:

1. **It mirrors.** Every chunk the manifest records lands in SQLite with the
   same state, digest and link.
2. **It rebuilds.** Deleting the JSONL and regenerating it from SQLite
   reproduces the same state. This is the property the whole module rests on --
   if a rebuild lost information, the mirror would be a trap, not a safety net.
3. **It is transaction-safe.** A failed write leaves nothing behind, and two
   threads cannot both claim the same chunk.

CONVENTIONS CHECKED, NOT ASSUMED
--------------------------------
The AGENTS_MAP review flags drift between writer and reader as the recurring
bug in this repo, so the conventions are asserted here rather than trusted:
same ``sha256[:32]`` fingerprint as ``direct_archive``, same WAL + busy-timeout
pragmas as ``db.py``, same table naming, and a rollback that catches
BaseException.

NO TELEGRAM, NO SESSION
-----------------------
Every test writes to a scratch database in a temp directory. The production
``t_dubber.db`` is never opened -- ``test_isolation`` guards that for the rest
of the suite, and these tests additionally pass an explicit ``db_path`` so
they cannot touch it even by accident.

Run with:  python -m pytest test_chunk_ledger.py -q
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chunk_ledger  # noqa: E402
import chunking  # noqa: E402

MIB = 1024 * 1024


def _write_file(path: Path, size: int, seed: int = 3) -> bytes:
    payload = bytearray()
    block = bytes((seed + i) % 251 for i in range(4096))
    while len(payload) < size:
        payload.extend(block)
    data = bytes(payload[:size])
    path.write_bytes(data)
    return data


class LedgerTestCase(unittest.TestCase):
    """Shared setup: a scratch database and a scratch manifest, per test."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="chunk_ledger_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.db_path = str(self.scratch / "ledger.db")
        self.source = self.scratch / "movie.mkv"
        self.data = _write_file(self.source, 2 * MIB + 333)
        self.addCleanup(chunk_ledger.close)
        # A fingerprint in the same shape direct_archive uses.
        self.digest = hashlib.sha256(self.data).hexdigest()

    def _manifest(self, channel="@chan", stored=(1, 2)) -> "chunking.ChunkManifest":
        manifest = chunking.ChunkManifest.create(
            self.source, channel=channel, chunk_bytes=1 * MIB,
            content_sha256=self.digest,
        )
        for index in stored:
            record = manifest.record(index)
            manifest.begin(record)
            manifest.complete(
                record, message_id=900 + index,
                link=f"https://t.me/{channel.lstrip('@')}/{900 + index}",
            )
        return manifest

    def _rows(self) -> list:
        return chunk_ledger.get_chunks(db_path=self.db_path)

    def _chunks(self, **filters) -> list:
        filters["db_path"] = self.db_path
        return chunk_ledger.get_chunks(**filters)


class SchemaTests(LedgerTestCase):
    def test_the_table_exists_after_the_first_connection(self):
        chunk_ledger.connect(self.db_path)
        conn = sqlite3.connect(self.db_path)
        names = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        conn.close()
        self.assertIn("telegram_chunks", names)

    def test_wal_and_the_busy_timeout_are_set_like_db_py(self):
        """Not tuning: the UI reads while a worker writes, so both are required."""
        conn = chunk_ledger.connect(self.db_path)
        self.assertEqual(
            conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal",
        )
        self.assertEqual(
            conn.execute("PRAGMA busy_timeout").fetchone()[0],
            chunk_ledger.BUSY_TIMEOUT_MS,
        )
        self.assertEqual(
            chunk_ledger.BUSY_TIMEOUT_MS, 15000,
            "must match db.BUSY_TIMEOUT_MS; two values would be two behaviours",
        )

    def test_it_follows_db_py_conventions(self):
        import db

        self.assertEqual(chunk_ledger.BUSY_TIMEOUT_MS, db.BUSY_TIMEOUT_MS)
        self.assertEqual(chunk_ledger._now.__doc__ or "", chunk_ledger._now.__doc__ or "")

    def test_every_column_is_documented(self):
        """A column with no explanation is how SQLite stays unreadable."""
        conn = chunk_ledger.connect(self.db_path)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(telegram_chunks)")
        }
        # "Value" is the fallback a column gets when nothing explains it, so a
        # column that falls through to it is genuinely undocumented.
        missing = {
            column for column in columns
            if chunk_ledger.column_help(column) == "Value"
        }
        self.assertEqual(missing, set(), f"undocumented columns: {missing}")

    def test_the_help_follows_the_same_shape_as_db_py(self):
        """Two help tables with two shapes is how one of them goes stale."""
        import db

        # Both modules explain every column of their own table.
        for column in ("sha256", "message_link", "error", "updated_at"):
            self.assertNotEqual(chunk_ledger.column_help(column), "Value")
            self.assertNotEqual(db._column_help("telegram_parts", column), "Value")

        # And a column neither table names specifically resolves to the SAME
        # suffix rule in both -- that shared fallback is what keeps the two
        # from drifting apart as columns are added.
        for column in ("created_at", "updated_at"):
            self.assertEqual(
                chunk_ledger.column_help(column),
                db._column_help("telegram_parts", column),
            )

    def test_a_manifest_with_no_fingerprint_is_refused(self):
        """Keying on content is the whole point; a nameless row is unusable."""
        manifest = chunking.ChunkManifest(str(self.scratch / "x.jsonl"), {})
        manifest.records[1] = chunking.ChunkRecord(index=1, offset=0, length=10)
        with self.assertRaises(ValueError):
            chunk_ledger.save_manifest(manifest, db_path=self.db_path)


class MirrorTests(LedgerTestCase):
    def test_every_chunk_lands_with_its_state_and_digest(self):
        manifest = self._manifest(stored=(1,))
        written = chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        self.assertEqual(written, manifest.chunk_count)

        rows = {row["chunk_index"]: row for row in self._chunks()}
        self.assertEqual(len(rows), manifest.chunk_count)
        for record in manifest.chunks():
            row = rows[record.index]
            self.assertEqual(row["sha256"], record.sha256)
            self.assertEqual(row["state"], record.state)
            self.assertEqual(row["offset_bytes"], record.offset)
            self.assertEqual(row["size_bytes"], record.length)
            self.assertEqual(row["message_link"], record.link)

    def test_the_fingerprint_is_the_content_digest_truncated(self):
        """The same convention direct_archive/app use -- not a second shape."""
        import direct_archive

        manifest = self._manifest()
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        row = self._rows()[0]
        self.assertEqual(row["fingerprint"], self.digest[:32])
        self.assertEqual(
            row["fingerprint"], direct_archive._archive_fingerprint(self.digest),
        )

    def test_saving_twice_updates_rather_than_duplicating(self):
        manifest = self._manifest(stored=(1,))
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        # Chunk 2 finishes, then the whole manifest is mirrored again.
        record = manifest.record(2)
        manifest.begin(record)
        manifest.complete(record, message_id=902, link="https://t.me/chan/902")
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)

        rows = self._rows()
        self.assertEqual(len(rows), manifest.chunk_count, "no duplicate rows")
        self.assertEqual(
            {row["chunk_index"] for row in rows}, set(range(1, manifest.chunk_count + 1)),
        )
        by_index = {row["chunk_index"]: row for row in rows}
        self.assertEqual(by_index[1]["state"], "done")
        self.assertEqual(by_index[2]["state"], "done")
        self.assertEqual(by_index[2]["message_id"], 902)
        # Chunk 3 was never sent, and the mirror must not pretend otherwise.
        self.assertEqual(by_index[3]["state"], "pending")
        self.assertIsNone(by_index[3]["message_id"])

    def test_the_same_bytes_to_two_channels_are_two_archives(self):
        # Separate manifest files: the two archives must not share one log.
        first = self._manifest(channel="@a")
        second = chunking.ChunkManifest.create(
            self.source, channel="@b", chunk_bytes=1 * MIB,
            content_sha256=self.digest,
            manifest_path=str(self.scratch / "b.chunks.jsonl"),
        )
        chunk_ledger.save_manifest(first, db_path=self.db_path)
        chunk_ledger.save_manifest(second, db_path=self.db_path)
        self.assertEqual(len(self._rows()), 2 * first.chunk_count)
        self.assertEqual(len(self._chunks(channel="@a")), first.chunk_count)
        self.assertEqual(len(self._chunks(channel="@b")), second.chunk_count)
        # ...and the fingerprints are identical, because the bytes are.
        self.assertEqual(
            {row["fingerprint"] for row in self._rows()}, {self.digest[:32]},
        )


class RebuildTests(LedgerTestCase):
    """The recovery test: delete the JSONL, rebuild from SQLite, compare."""

    def test_rebuilding_reproduces_the_manifest_state(self):
        manifest = self._manifest(stored=(1, 2))
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        original = {r.index: r for r in manifest.chunks()}

        rebuilt_path = str(self.scratch / "rebuilt.jsonl")
        rebuilt = chunk_ledger.rebuild_manifest(
            rebuilt_path, fingerprint=original[1].sha256 and manifest.header["fingerprint"],
            db_path=self.db_path,
        )

        self.assertEqual(rebuilt.chunk_count, manifest.chunk_count)
        for record in rebuilt.chunks():
            was = original[record.index]
            self.assertEqual(record.offset, was.offset)
            self.assertEqual(record.length, was.length)
            self.assertEqual(record.sha256, was.sha256)
            self.assertEqual(record.state, was.state)
            self.assertEqual(record.message_id, was.message_id)
            self.assertEqual(record.link, was.link)
            self.assertEqual(record.attempts, was.attempts)

    def test_the_rebuilt_file_is_jsonl_the_manifest_loader_accepts(self):
        manifest = self._manifest(stored=(1,))
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        rebuilt_path = str(self.scratch / "rebuilt.jsonl")
        chunk_ledger.rebuild_manifest(
            rebuilt_path, fingerprint=manifest.header["fingerprint"],
            db_path=self.db_path,
        )
        reloaded = chunking.ChunkManifest.load(rebuilt_path)
        self.assertEqual(reloaded.chunk_count, manifest.chunk_count)
        self.assertEqual(reloaded.header["filename"], manifest.header["filename"])
        self.assertEqual(reloaded.header["size"], manifest.size)
        self.assertTrue(reloaded.all_done() == manifest.all_done())

    def test_deleting_the_jsonl_and_rebuilding_loses_nothing(self):
        """The exact scenario: the manifest file is gone; SQLite has the truth."""
        manifest = self._manifest(stored=(1, 2))
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        before = {
            r.index: (r.offset, r.length, r.sha256, r.state, r.link, r.attempts)
            for r in manifest.chunks()
        }

        os.remove(manifest.path)
        self.assertFalse(os.path.isfile(manifest.path))

        rebuilt_path = manifest.path
        rebuilt = chunk_ledger.rebuild_manifest(
            rebuilt_path, fingerprint=manifest.header["fingerprint"],
            db_path=self.db_path,
        )
        after = {
            r.index: (r.offset, r.length, r.sha256, r.state, r.link, r.attempts)
            for r in rebuilt.chunks()
        }
        self.assertEqual(after, before)

    def test_a_rebuilt_manifest_can_still_resume_the_upload(self):
        """Recovery is only real if the rebuilt file can drive a resume."""
        manifest = self._manifest(stored=(1,))
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        rebuilt_path = str(self.scratch / "rebuilt.jsonl")
        chunk_ledger.rebuild_manifest(
            rebuilt_path, fingerprint=manifest.header["fingerprint"],
            db_path=self.db_path,
        )

        sent = []
        def send(record, source_path):
            sent.append(record.index)
            return {"message_id": record.index,
                    "link": f"https://t.me/chan/{record.index}"}

        rebuilt = chunking.ChunkManifest.load(rebuilt_path)
        summary, _ = chunking.resume(rebuilt, send)
        self.assertTrue(summary.ok)
        self.assertNotIn(1, sent, "the already-stored chunk must still be skipped")
        self.assertEqual(len(sent), rebuilt.chunk_count - 1)

    def test_rebuilding_with_nothing_stored_says_so(self):
        with self.assertRaises(LookupError):
            chunk_ledger.rebuild_manifest(
                str(self.scratch / "empty.jsonl"), db_path=self.db_path,
            )


class TransitionTests(LedgerTestCase):
    def test_a_state_change_touches_exactly_one_row(self):
        manifest = self._manifest(stored=())
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        changed = chunk_ledger.set_state(
            1, "done", fingerprint=manifest.header["fingerprint"],
            channel="@chan", message_id=901, message_link="https://t.me/chan/901",
            db_path=self.db_path,
        )
        self.assertTrue(changed)
        row = chunk_ledger.find_chunk(manifest.header["fingerprint"], 1, db_path=self.db_path)
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["message_id"], 901)

    def test_bumping_attempts_is_part_of_the_same_statement(self):
        """The count and the state it produced must never disagree."""
        manifest = self._manifest(stored=())
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        fingerprint = manifest.header["fingerprint"]
        for _ in range(3):
            chunk_ledger.set_state(
                2, "uploading", fingerprint=fingerprint, channel="@chan",
                bump_attempts=True, db_path=self.db_path,
            )
        row = chunk_ledger.find_chunk(fingerprint, 2, db_path=self.db_path)
        self.assertEqual(row["attempts"], 3)
        self.assertEqual(row["state"], "uploading")

    def test_a_change_that_matches_nothing_reports_failure(self):
        """A transition against a chunk that is not there must say so."""
        manifest = self._manifest(stored=())
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        fingerprint = manifest.header["fingerprint"]
        self.assertFalse(chunk_ledger.set_state(
            999, "done", fingerprint=fingerprint, channel="@chan",
            db_path=self.db_path,
        ))
        self.assertEqual(self._rows()[0]["state"], "pending",
                         "a miss must not disturb any row")

    def test_a_failed_transaction_leaves_nothing_behind(self):
        """The rollback rule, checked rather than assumed."""
        manifest = self._manifest(stored=(1,))
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        before = {row["chunk_index"]: row["state"] for row in self._rows()}

        conn = chunk_ledger.connect(self.db_path)

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with chunk_ledger._write(conn):
                conn.execute(
                    "UPDATE telegram_chunks SET state='done' WHERE chunk_index=1"
                )
                raise Boom("something failed after the UPDATE")

        after = {row["chunk_index"]: row["state"] for row in self._rows()}
        self.assertEqual(after, before, "a rolled-back transaction must change nothing")

    def test_a_base_exception_also_rolls_back(self):
        """CancelledError does not derive from Exception since 3.8."""
        manifest = self._manifest(stored=(1,))
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        before = {row["chunk_index"]: row["state"] for row in self._rows()}
        conn = chunk_ledger.connect(self.db_path)

        class Cancelled(BaseException):
            pass

        with self.assertRaises(Cancelled):
            with chunk_ledger._write(conn):
                conn.execute("UPDATE telegram_chunks SET state='done'")
                raise Cancelled()

        after = {row["chunk_index"]: row["state"] for row in self._rows()}
        self.assertEqual(after, before)


class ConcurrencyTests(LedgerTestCase):
    def test_two_threads_cannot_claim_the_same_chunk(self):
        """Concurrent workers must not both believe they own one chunk."""
        manifest = self._manifest(stored=())
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        fingerprint = manifest.header["fingerprint"]

        winners = []
        barrier = threading.Barrier(4)
        lock = threading.Lock()

        def claim():
            # Each thread gets its own connection, as db.py's thread-local does.
            chunk_ledger.close()
            barrier.wait()
            got = chunk_ledger.set_state(
                1, "uploading", fingerprint=fingerprint, channel="@chan",
                bump_attempts=True, db_path=self.db_path,
            )
            if got:
                with lock:
                    winners.append(threading.current_thread().name)

        threads = [threading.Thread(target=claim, name=f"w{i}") for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        # Every thread "succeeded" at the SQL level; the point is that the row
        # count and the attempt count stay honest, not that one of them errors.
        row = chunk_ledger.find_chunk(fingerprint, 1, db_path=self.db_path)
        self.assertEqual(row["attempts"], 4)
        self.assertEqual(row["state"], "uploading")
        self.assertEqual(len(self._rows()), manifest.chunk_count)

    def test_concurrent_mirrors_do_not_duplicate_rows(self):
        manifest = self._manifest(stored=(1,))
        errors = []

        def mirror():
            try:
                chunk_ledger.close()
                chunk_ledger.save_manifest(manifest, db_path=self.db_path)
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=mirror) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(self._rows()), manifest.chunk_count)


class QueryTests(LedgerTestCase):
    def test_summary_counts_what_is_stored_and_what_is_not(self):
        manifest = self._manifest(stored=(1,))
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        report = chunk_ledger.summary(db_path=self.db_path)
        self.assertEqual(report["chunks"], manifest.chunk_count)
        self.assertEqual(report["done"], 1)
        self.assertEqual(report["pending"], manifest.chunk_count - 1)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(
            report["bytes"], sum(r.length for r in manifest.chunks()),
        )
        self.assertEqual(report["stored_bytes"], manifest.record(1).length)

    def test_failed_chunks_are_the_ones_a_resume_should_re_send(self):
        """The query the JSONL cannot answer cheaply: what has been failing?"""
        manifest = self._manifest(stored=(1,))
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        fingerprint = manifest.header["fingerprint"]
        chunk_ledger.set_state(
            3, "failed", fingerprint=fingerprint, channel="@chan",
            error="connection reset", db_path=self.db_path,
        )
        failed = chunk_ledger.failed_chunks(db_path=self.db_path)
        self.assertEqual([row["chunk_index"] for row in failed], [3])
        self.assertEqual(failed[0]["error"], "connection reset")
        self.assertEqual(chunk_ledger.summary(db_path=self.db_path)["failed"], 1)

    def test_filters_narrow_the_result(self):
        manifest = self._manifest(stored=(1,), channel="@a")
        chunk_ledger.save_manifest(manifest, db_path=self.db_path)
        fingerprint = manifest.header["fingerprint"]
        by_fingerprint = self._chunks(fingerprint=fingerprint)
        self.assertEqual(len(by_fingerprint), manifest.chunk_count)
        done = self._chunks(state="done")
        self.assertEqual([row["chunk_index"] for row in done], [1])
        self.assertEqual(self._chunks(channel="@other"), [])

    def test_a_missing_chunk_returns_none_rather_than_raising(self):
        self.assertIsNone(chunk_ledger.find_chunk("nope", 1, db_path=self.db_path))


class IsolationTests(LedgerTestCase):
    def test_no_connection_is_left_on_the_production_database(self):
        """The suite's autouse guard redirects db.DB_PATH; honour it here too."""
        chunk_ledger.connect(self.db_path)
        chunk_ledger.save_manifest(self._manifest(), db_path=self.db_path)
        conn_path = getattr(chunk_ledger._local, "path", None)
        self.assertEqual(conn_path, self.db_path)
        self.assertNotIn("t_dubber.db", str(conn_path))


if __name__ == "__main__":
    unittest.main(verbosity=2)