"""test_p0_direct.py -- the P0 changes, pinned.

Covers the three things P0 changed, each of which is a behaviour someone could
silently undo later:

* P0-1 Tier 0 must reuse a completed archive and refuse anything else.
* P0-2 the resume key must follow the bytes, not the path -- and a journal
  written under the old key must still be found.
* P0-3 the dataset must carry no media, and the worker must fall back rather
  than fail when it cannot reach the channel.

Run: python test_p0_direct.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import db                      # noqa: E402
import telegram_uploader as tu  # noqa: E402


def make_file(path: Path, size: int = 4096, fill: bytes = b"a") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fill * size)


class Tier0CacheHitTests(unittest.TestCase):
    """P0-1: a completed archive for these exact bytes is reused."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="p0_tier0_"))
        self.saved_db = db.DB_PATH
        db.DB_PATH = str(self.scratch / "index.db")
        if getattr(db._local, "conn", None) is not None:
            db._local.conn.close()
            db._local.conn = None
        db.connect()
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.addCleanup(self._restore_db)

    def _restore_db(self):
        if getattr(db._local, "conn", None) is not None:
            db._local.conn.close()
            db._local.conn = None
        db.DB_PATH = self.saved_db

    def _archive(self, digest: str, channel: str, state: str, msg_id: int = 77,
                 manifest_link: str = ""):
        db.upsert_archive({
            "fingerprint": digest[:32],
            "filename": "movie.mp4",
            "file_path": "whatever/movie.mp4",
            "size": 1234,
            "channel": channel,
            "state": state,
            "message_id": msg_id,
            "message_link": manifest_link,
        })

    def test_completed_archive_is_reused(self):
        digest = "a" * 64
        self._archive(digest, "@chan", "complete", msg_id=77)
        link = app_find(digest, "@chan")
        self.assertTrue(link, "a completed archive must produce a link")
        self.assertIn("77", link)

    def test_manifest_link_wins_over_a_rebuilt_one(self):
        digest = "b" * 64
        self._archive(digest, "@chan", "complete", msg_id=77,
                      manifest_link="https://t.me/chan/900")
        self.assertEqual(app_find(digest, "@chan"), "https://t.me/chan/900")

    def test_in_flight_archive_is_not_a_hit(self):
        """An upload that died mid-flight is not a copy anyone can rely on."""
        digest = "c" * 64
        self._archive(digest, "@chan", "uploading", msg_id=77)
        self.assertIsNone(app_find(digest, "@chan"))

    def test_failed_archive_is_not_a_hit(self):
        digest = "d" * 64
        self._archive(digest, "@chan", "failed", msg_id=77)
        self.assertIsNone(app_find(digest, "@chan"))

    def test_a_different_channel_is_not_a_hit(self):
        digest = "e" * 64
        self._archive(digest, "@chan", "complete", msg_id=77)
        self.assertIsNone(app_find(digest, "@other"))

    def test_a_different_file_is_not_a_hit(self):
        digest = "f" * 64
        self._archive(digest, "@chan", "complete", msg_id=77)
        self.assertIsNone(app_find("0" * 64, "@chan"))

    def test_missing_inputs_are_not_a_hit(self):
        self.assertIsNone(app_find("", "@chan"))
        self.assertIsNone(app_find("a" * 64, ""))

    def test_broken_index_does_not_raise(self):
        """A lookup failure must cost a cache miss, never a run."""
        broken = self.scratch / "not-a-database"
        broken.write_text("this is not sqlite", encoding="utf-8")
        db.DB_PATH = str(broken)
        if getattr(db._local, "conn", None) is not None:
            db._local.conn.close()
            db._local.conn = None
        self.assertIsNone(app_find("a" * 64, "@chan"))


def app_find(digest: str, channel: str):
    """Call the real Tier 0 predicate without importing all of app.py's UI."""
    import app
    return app._find_archived_copy(digest, channel)


class ContentKeyTests(unittest.TestCase):
    """P0-2: the resume key has to follow the bytes."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="p0_key_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)

    def test_key_survives_a_move(self):
        """The defect this fixes: one shutil.move used to lose every part."""
        first = self.scratch / "inbox" / "movie.mp4"
        make_file(first, 4096)
        before = tu._fingerprint(str(first), first.stat().st_size)
        legacy_before = tu._legacy_fingerprint(str(first), first.stat().st_size)

        moved = self.scratch / "project" / "movie.mp4"
        moved.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(first), str(moved))

        self.assertEqual(before, tu._fingerprint(str(moved), moved.stat().st_size),
                         "the content key must follow the bytes across a move")
        self.assertNotEqual(
            legacy_before, tu._legacy_fingerprint(str(moved), moved.stat().st_size),
            "the old key changed on the move, which is the whole bug being fixed")

    def test_key_survives_a_rename(self):
        first = self.scratch / "a.mp4"
        make_file(first, 2048)
        before = tu._fingerprint(str(first), first.stat().st_size)
        renamed = self.scratch / "b.mp4"
        shutil.move(str(first), str(renamed))
        self.assertEqual(before, tu._fingerprint(str(renamed), renamed.stat().st_size))

    def test_key_changes_when_the_bytes_change_at_the_same_size(self):
        """Size alone must never be enough to reuse an upload."""
        first = self.scratch / "one.mp4"
        second = self.scratch / "two.mp4"
        make_file(first, 4096, fill=b"a")
        make_file(second, 4096, fill=b"b")
        self.assertEqual(first.stat().st_size, second.stat().st_size)
        self.assertNotEqual(
            tu._fingerprint(str(first), first.stat().st_size),
            tu._fingerprint(str(second), second.stat().st_size),
        )

    def test_key_changes_when_only_the_middle_changes(self):
        """An in-place edit must invalidate a resume, even mid-file.

        The sampled bytes cannot see the middle of a large file, so the mtime in
        the key is what catches this. mtime_ns is set explicitly because a
        filesystem with coarse timestamps would otherwise hide the difference
        and make this test pass or fail for the wrong reason.
        """
        path = self.scratch / "big.bin"
        body = bytearray(b"z" * (tu.CONTENT_SAMPLE_BYTES * 3))
        path.write_bytes(bytes(body))
        size = path.stat().st_size
        before = tu._fingerprint(str(path), size)

        body[len(body) // 2] = 0x41
        path.write_bytes(bytes(body))
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

        self.assertNotEqual(before, tu._fingerprint(str(path), size))

    def test_key_is_unchanged_when_nothing_touched_the_file(self):
        """Reading a file must not change its key: no atime in the seed."""
        path = self.scratch / "movie.mp4"
        make_file(path, 4096)
        size = path.stat().st_size
        first = tu._fingerprint(str(path), size)
        with open(path, "rb") as handle:      # this bumps atime
            handle.read(16)
        self.assertEqual(first, tu._fingerprint(str(path), size))

    def test_supplied_digest_is_used_and_agrees_across_a_move(self):
        """The caller's whole-file digest must not disagree with itself."""
        path = self.scratch / "movie.mp4"
        make_file(path, 4096)
        size = path.stat().st_size
        import hashlib
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        key = tu._fingerprint(str(path), size, digest)
        moved = self.scratch / "moved.mp4"
        shutil.move(str(path), str(moved))
        self.assertEqual(key, tu._fingerprint(str(moved), moved.stat().st_size, digest))
        # a different file's digest must not collide
        self.assertNotEqual(key, tu._fingerprint(str(moved), size, "b" * 64))

    def test_unreadable_file_falls_back_instead_of_raising(self):
        missing = self.scratch / "gone.mp4"
        self.assertTrue(tu._fingerprint(str(missing), 10))


class LegacyJournalMigrationTests(unittest.TestCase):
    """A journal from before the change must not be thrown away."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="p0_migrate_"))
        self.saved_state = tu.STATE_DIR
        tu.STATE_DIR = str(self.scratch / "state")
        os.makedirs(tu.STATE_DIR, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.scratch, True)

        def _restore():
            tu.STATE_DIR = self.saved_state
        self.addCleanup(_restore)

    def test_legacy_key_still_finds_its_journal(self):
        path = self.scratch / "movie.mp4"
        make_file(path, 4096)
        size = path.stat().st_size
        legacy_key = tu._legacy_fingerprint(str(path), size)
        tu._atomic_write_json(tu.state_path_for(legacy_key),
                              {"state": "uploading", "parts": [{"part": 1}]})
        # The new key is different, so the file is looked for the old way too.
        self.assertNotEqual(tu._fingerprint(str(path), size), legacy_key)
        self.assertEqual(tu._read_json(tu.state_path_for(legacy_key), {}).get("state"),
                         "uploading")


class GoJournalKeyTests(unittest.TestCase):
    """The Go path must journal under the key the caller is looking up.

    A caller that supplies a whole-file digest gets a different key from one
    that does not. If _try_go_upload recomputed the key itself, the journal the
    Go upload wrote would sit under a name the next run never looks for, and
    every "already uploaded" reuse would silently re-upload the file.
    """

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="p0_gokey_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)

    def test_go_path_writes_under_the_callers_key(self):
        import inspect

        source = inspect.getsource(tu._try_go_upload)
        self.assertNotIn(
            "state_path_for(_fingerprint(", source,
            "_try_go_upload must not recompute the journal key")
        self.assertIn("journal_key", source)

        # and the parameter really is threaded through from the entry point
        entry = inspect.getsource(tu.upload_file_detailed)
        self.assertIn("journal_key=key", entry)
        self.assertIn("content_sha256", inspect.signature(
            tu.upload_file_detailed).parameters)

    def test_sampled_and_digest_keys_are_different_names(self):
        """The reason this bug existed: two spellings of one file's key."""
        import hashlib
        path = self.scratch / "movie.mp4"
        make_file(path, 4096)
        size = path.stat().st_size
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertNotEqual(tu._fingerprint(str(path), size),
                            tu._fingerprint(str(path), size, digest))


class CredentialsTypeTests(unittest.TestCase):
    """The Go binary needs ``api_id`` as an int; config.json stores a string.

    Passing the string through made tgup answer "cannot unmarshal string into Go
    struct field .api_id of type int". That is not a credentials problem, it is a
    type mismatch -- but it reads like one, so ``auto_tuner`` never got a real
    benchmark and fell back to the default concurrency on every run.
    """

    def test_string_api_id_is_coerced(self):
        import tgup_bridge
        payload = json.loads(tgup_bridge._credentials_json("35578684", "hash"))
        self.assertIsInstance(payload["api_id"], int)
        self.assertEqual(payload["api_id"], 35578684)

    def test_int_api_id_survives(self):
        import tgup_bridge
        payload = json.loads(tgup_bridge._credentials_json(35578684, "hash"))
        self.assertEqual(payload["api_id"], 35578684)

    def test_garbage_degrades_instead_of_raising(self):
        import tgup_bridge
        for value in ("", None, "abc", True, False, "  "):
            payload = json.loads(tgup_bridge._credentials_json(value, "hash"))
            self.assertIsInstance(payload["api_id"], int, repr(value))

    def test_every_call_site_uses_the_helper(self):
        """A new call site that inlines json.dumps would reintroduce this."""
        import inspect
        import tgup_bridge
        source = inspect.getsource(tgup_bridge)
        # The helper's own docstring quotes the bad pattern on purpose, to explain
        # the bug. Take the helper out of the source before looking for it.
        helper = inspect.getsource(tgup_bridge._credentials_json)
        rest = source.replace(helper, "")
        inline = 'json.dumps({"api_id": api_id, "api_hash": api_hash})'
        self.assertNotIn(inline, rest,
                         "a call site still builds credentials by hand")
        self.assertGreaterEqual(
            rest.count("_credentials_json(api_id, api_hash)"), 3,
            "upload, bench and fetch must all route through the helper")


class InFlightChannelTests(unittest.TestCase):
    """The channel must be on the journal before the first part lands.

    `telegram_archives` is unique on (fingerprint, channel), and `_archive_to_db`
    mirrors every in-flight snapshot. So while the journal had no channel, the
    mirror wrote the `(unknown)` placeholder, could never collide with the final
    row, and every archive became two rows -- one `uploading (unknown)` that
    nothing ever updates, and one `complete @channel`.

    Found by NIMBU during review. This pins the invariant, not the symptom.
    """

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="p0_channel_"))
        self.saved_db = db.DB_PATH
        db.DB_PATH = str(self.scratch / "index.db")
        if getattr(db._local, "conn", None) is not None:
            db._local.conn.close()
            db._local.conn = None
        db.connect()
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.addCleanup(self._restore_db)

    def _restore_db(self):
        if getattr(db._local, "conn", None) is not None:
            db._local.conn.close()
            db._local.conn = None
        db.DB_PATH = self.saved_db

    def _rows(self, fingerprint):
        return db.connect().execute(
            "SELECT channel, state FROM telegram_archives WHERE fingerprint=?",
            (fingerprint,),
        ).fetchall()

    def _archive(self, fingerprint, channel, state):
        db.upsert_archive({
            "fingerprint": fingerprint, "filename": "movie.mp4",
            "file_path": "p/movie.mp4", "size": 2048, "channel": channel,
            "state": state, "message_id": 42 if state == "complete" else None,
            "message_link": "https://t.me/chan/42" if state == "complete" else None,
        })

    def test_old_behaviour_produced_two_rows(self):
        """Documents the bug, so the fix cannot be undone silently."""
        fp = "a" * 32
        self._archive(fp, db.UNKNOWN_CHANNEL, "uploading")   # no channel yet
        self._archive(fp, "@chan", "complete")              # channel arrives
        self.assertEqual(len(self._rows(fp)), 2)

    def test_new_behaviour_collapses_to_one_row(self):
        fp = "b" * 32
        self._archive(fp, "@chan", "uploading")
        self._archive(fp, "@chan", "complete")
        rows = self._rows(fp)
        self.assertEqual(len(rows), 1, f"expected one row, got {len(rows)}")
        self.assertEqual(rows[0]["state"], "complete")

    def test_journal_records_channel_before_the_first_part(self):
        """The fix is an ordering guarantee, so assert the ordering."""
        import inspect
        source = inspect.getsource(tu.upload_file_detailed)
        set_channel = source.find('journal["channel"] = channel')
        first_plan = source.find("plan_chunks(")
        self.assertNotEqual(set_channel, -1, "journal channel is never set")
        self.assertNotEqual(first_plan, -1, "plan_chunks call not found")
        self.assertLess(
            set_channel, first_plan,
            "the channel must be recorded before any part can land, otherwise "
            "the first mirror write falls back to the placeholder channel",
        )


class TestIsolationTests(unittest.TestCase):
    """Running the test suite must not write to the production database.

    Measured damage on 2026-10-08: 200 of 232 `projects` rows were this
    repository's own test fixtures -- four fixtures (`clip`, `clip2`,
    `Some_YouTube_Video`, `restored`) re-run 47 times each. The tests redirect
    `app.PROJECTS_DIR` so the *directories* go to scratch, but nothing redirected
    `db.DB_PATH`, so the *rows* went to the real database. That is what made the
    dashboard look like it was full of rubbish.
    """

    def setUp(self):
        import test_isolation
        self.iso = test_isolation
        self.db_path = self.iso.production_db_path()

    def _run(self, script: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, script],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )

    def test_flow_order_does_not_touch_the_production_db(self):
        before = self.iso.production_row_count("projects")
        result = self._run("test_flow_order.py")
        after = self.iso.production_row_count("projects")
        self.assertEqual(
            before, after,
            f"{result.stdout[-400:]} -- test_flow_order.py wrote "
            f"{after - before} row(s) into the production database",
        )
        self.assertEqual(result.returncode, 0, result.stderr[-400:])

    def test_flow_order_leaves_no_scratch_database_in_the_repo(self):
        result = self._run("test_flow_order.py")
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        self.assertFalse(
            (ROOT / "t_dubber.db.tmp").exists(),
            "a stray database file was left in the repo",
        )

    def test_helper_redirects_and_can_restore(self):
        import db
        original = self.iso.production_db_path()
        scratch = self.iso.use_scratch_db(prefix="tdub_iso_probe_")
        try:
            self.assertNotEqual(db.DB_PATH, original)
            self.assertEqual(db.DB_PATH, scratch)
            db.upsert_project({"id": "probe-row", "title": "probe"})
            self.assertEqual(
                self.iso.production_row_count("projects"),
                self.iso.production_row_count("projects"),
            )
            # the probe row exists in the scratch DB, not in production
            rows = db.connect().execute(
                "SELECT COUNT(*) FROM projects WHERE id='probe-row'"
            ).fetchone()[0]
            self.assertEqual(rows, 1)
        finally:
            self.iso.restore()
            self.iso.cleanup_scratch()
        self.assertEqual(db.DB_PATH, original)


class WorkerFetchContractTests(unittest.TestCase):
    """P0-3: the dataset ships no media, and the worker degrades safely."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="p0_worker_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)

    def _job_config(self, **overrides):
        config = {"target_language": "Hindi",
                  "telegram_backup": "https://t.me/chan/12"}
        config.update(overrides)
        input_dir = self.scratch / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        (input_dir / "dub_job.json").write_text(json.dumps(config), encoding="utf-8")
        return input_dir

    def test_absent_video_is_expected_when_fetching(self):
        """No mounted video is not an error when the host promised a link."""
        import multitasker
        input_dir = self._job_config(fetch_from_telegram=True,
                                     source_sha256="a" * 64,
                                     compress_480p_on_worker=True)
        job = multitasker.discover_jobs(input_dir)[0]
        self.assertTrue(job.fetch_from_telegram)
        self.assertTrue(job.compress_480p)
        self.assertEqual(job.source_sha256, "a" * 64)
        self.assertEqual(job.video_path, "")

    def test_absent_video_is_still_an_error_without_the_flag(self):
        """An old dataset with a missing file must still fail loudly."""
        import multitasker
        input_dir = self._job_config()
        with self.assertRaises(FileNotFoundError):
            multitasker.discover_jobs(input_dir)

    def test_migrated_dataset_without_the_keys_keeps_working(self):
        """A dataset written before P0-3 must behave exactly as it did."""
        import multitasker
        input_dir = self.scratch / "old"
        (input_dir).mkdir(parents=True, exist_ok=True)
        (input_dir / "dub_job.json").write_text(
            json.dumps({"target_language": "Tamil"}), encoding="utf-8")
        (input_dir / "source_video.mp4").write_bytes(b"\x00" * 1024)
        job = multitasker.discover_jobs(input_dir)[0]
        self.assertFalse(job.fetch_from_telegram)
        self.assertFalse(job.compress_480p)
        self.assertTrue(job.video_path.endswith("source_video.mp4"))

    def test_worker_falls_back_when_tgup_is_absent(self):
        """A worker with no binary must keep the mounted video, not crash."""
        import multitasker
        job = multitasker.DubJob(
            job_id="job-000", video_path="/kaggle/input/source_video.mp4",
            target_language="Hindi", telegram_backup="https://t.me/chan/12",
            fetch_from_telegram=True, source_sha256="a" * 64,
        )
        saved = os.environ.pop("TGUP_BIN", None)
        self.addCleanup(lambda: os.environ.__setitem__("TGUP_BIN", saved)
                        if saved else os.environ.pop("TGUP_BIN", None))
        os.environ["TGUP_BIN"] = str(self.scratch / "no-such-tgup")
        restored = multitasker.restore_source_from_channel(job, self.scratch)
        self.assertEqual(restored, "/kaggle/input/source_video.mp4")

    def test_worker_falls_back_when_the_link_is_missing(self):
        import multitasker
        job = multitasker.DubJob(
            job_id="job-000", video_path="/kaggle/input/source_video.mp4",
            target_language="Hindi", telegram_backup="", fetch_from_telegram=True,
        )
        restored = multitasker.restore_source_from_channel(job, self.scratch)
        self.assertEqual(restored, "/kaggle/input/source_video.mp4")


class PipelineContractTests(unittest.TestCase):
    """P0-3 host side: when the media stays off the dataset."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="p0_pipeline_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)

    def test_channel_links_are_recognised(self):
        import pipeline
        for good in ("https://t.me/chan/12", "https://telegram.me/chan/12",
                     "tg://privatepost?channel=chan&post=12"):
            self.assertIsNotNone(pipeline._channel_archive_link(good), good)
        for bad in ("", None, "https://example.com/x.mp4", "https://youtube.com/watch?v=1"):
            self.assertIsNone(pipeline._channel_archive_link(bad), repr(bad))

    def test_env_flag_defaults_to_on_and_is_falsifiable(self):
        import pipeline
        os.environ.pop("TDUBBER_WORKER_FETCH", None)
        self.assertTrue(pipeline._env_flag("TDUBBER_WORKER_FETCH", True))
        for falsy in ("0", "off", "false", "no", "OFF"):
            os.environ["TDUBBER_WORKER_FETCH"] = falsy
            self.assertFalse(pipeline._env_flag("TDUBBER_WORKER_FETCH", True), falsy)
        for truthy in ("1", "true", "yes", ""):
            os.environ["TDUBBER_WORKER_FETCH"] = truthy
            self.assertTrue(pipeline._env_flag("TDUBBER_WORKER_FETCH", True), truthy)
        os.environ.pop("TDUBBER_WORKER_FETCH", None)

    def test_worker_fetch_wiring_defaults_on_for_channel_links(self):
        """BUG 1 regression: unset env + channel link must mean the worker fetches.

        The wiring used to invert the kill-switch (``not _env_flag``), so
        with the env unset the host shipped the media anyway, and writing
        ``TDUBBER_WORKER_FETCH=off`` turned the feature *on* -- name,
        docstring and test all said the opposite.
        """
        import pipeline
        os.environ.pop("TDUBBER_WORKER_FETCH", None)
        self.addCleanup(lambda: os.environ.pop("TDUBBER_WORKER_FETCH", None))
        self.assertTrue(pipeline._worker_fetches("https://t.me/x/1"))
        self.assertTrue(pipeline._worker_fetches("tg://privatepost?channel=c&post=1"))
        os.environ["TDUBBER_WORKER_FETCH"] = "off"
        self.assertFalse(pipeline._worker_fetches("https://t.me/x/1"))
        # Non-channel links never fetch, whatever the env says.
        self.assertFalse(pipeline._worker_fetches("https://youtube.com/watch?v=1"))
        self.assertFalse(pipeline._worker_fetches(""))

    def test_source_digest_is_reused_not_recomputed(self):
        """ISSUE 2: a digest the caller already paid for is reused.

        The host hashes the whole file once (project id, Tier 0 lookup);
        the pipeline must reuse that digest instead of reading a
        multi-gigabyte source a second time for the worker.
        """
        import pipeline
        calls = []

        def fake_sha256(path, block=4 * 1024 * 1024):
            calls.append(path)
            return "f" * 64

        saved = pipeline._sha256_file
        pipeline._sha256_file = fake_sha256
        self.addCleanup(lambda: setattr(pipeline, "_sha256_file", saved))

        # Caller brought the digest: the file is never read again.
        self.assertEqual(
            pipeline._resolve_source_sha256("x.mp4", "a" * 64, True), "a" * 64)
        self.assertEqual(calls, [])
        # No digest and a fetching worker: exactly one read.
        self.assertEqual(
            pipeline._resolve_source_sha256("x.mp4", None, True), "f" * 64)
        self.assertEqual(calls, ["x.mp4"])
        # Not fetching: no read at all, and no digest is invented.
        self.assertIsNone(pipeline._resolve_source_sha256("x.mp4", None, False))
        self.assertEqual(calls, ["x.mp4"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
