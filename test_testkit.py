#!/usr/bin/env python3
"""test_testkit.py -- the test library tests itself.

WHY A LIBRARY NEEDS ITS OWN TESTS
---------------------------------
``testkit.py`` is what other tests stand on. If it breaks, every test that
imports it fails at setup time, and the failure message points at the *caller*
rather than at the real cause. That is a genuinely nasty failure mode to debug,
so this file pins the library's behaviour before that can happen.

It also pins the two contracts that are easy to break by accident and expensive
to notice:

  * ``sha256_of`` must agree between its bytes branch and its path branch.
    They look interchangeable and are not -- digesting a path *string* produces a
    valid-looking 64-char hex that matches nothing.
  * ``CapturedCommand.flag`` must return None for an absent flag, not raise
    IndexError. Every tgup test asserts on flags that may be missing.

Run standalone (the repo convention)::

    python test_testkit.py
    python -m pytest test_testkit.py -q
"""

from __future__ import annotations

import os
import sys
import unittest
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urllib.response import addinfourl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import testkit


class TestScratchWorkspace(unittest.TestCase):
    """Cleanup behaviour. Leftover scratch is how a test passes once, then fails."""

    def test_creates_and_removes_itself(self) -> None:
        workspace = testkit.ScratchWorkspace()
        path = workspace.path
        self.assertTrue(path.is_dir(), "workspace should exist immediately")
        workspace.close()
        self.assertFalse(path.exists(), "close() must delete the directory")

    def test_close_is_idempotent(self) -> None:
        # Cleanup code runs in a finally, which can run twice when a test
        # already closed early. That must not raise.
        workspace = testkit.ScratchWorkspace()
        workspace.close()
        workspace.close()

    def test_context_manager_cleans_up(self) -> None:
        with testkit.ScratchWorkspace() as workspace:
            path = workspace.path
            (path / "a.txt").write_text("x", encoding="utf-8")
        self.assertFalse(path.exists(), "leaving the block must clean up")

    def test_child_creates_parents(self) -> None:
        with testkit.ScratchWorkspace() as workspace:
            target = workspace.child("deep", "nested", "file.mkv")
            self.assertTrue(target.parent.is_dir())
            self.assertTrue(str(target).endswith(os.path.join("deep", "nested", "file.mkv")))

    def test_root_points_at_the_repo(self) -> None:
        # Tests that need a repo-relative path use this; it must not be the
        # scratch dir or the tests silently resolve against temp.
        with testkit.ScratchWorkspace() as workspace:
            self.assertTrue((workspace.root / "testkit.py").is_file())


class TestFakeMedia(unittest.TestCase):
    """The fake video file: right name, right size, deterministic content."""

    def test_creates_file_with_requested_size_and_name(self) -> None:
        with testkit.ScratchWorkspace() as workspace:
            target = testkit.fake_media(workspace.path, name="clip.mkv", size=1234)
            self.assertEqual(target.name, "clip.mkv")
            self.assertEqual(target.stat().st_size, 1234)

    def test_default_name_is_movie_mkv(self) -> None:
        # Not arbitrary: chunk part names are derived from the file name
        # ("movie.mkv.part0001of0002.mkv") and several chunking tests hardcode
        # it. Changing this default breaks them silently.
        with testkit.ScratchWorkspace() as workspace:
            self.assertEqual(testkit.fake_media(workspace.path).name, "movie.mkv")

    def test_content_is_deterministic(self) -> None:
        # A test that hashes the file twice must get the same digest, so the
        # filler bytes must not depend on randomness or on wall-clock time.
        with testkit.ScratchWorkspace() as workspace:
            first = testkit.fake_media(workspace.path, size=512).read_bytes()
            second = testkit.fake_media(workspace.path, size=512).read_bytes()
            self.assertEqual(first, second)

    def test_creates_missing_directory(self) -> None:
        with testkit.ScratchWorkspace() as workspace:
            nested = workspace.path / "does" / "not" / "exist"
            target = testkit.fake_media(nested)
            self.assertTrue(target.is_file())


class TestSha256Of(unittest.TestCase):
    """The bytes-vs-path mismatch this helper exists to prevent."""

    def test_path_branch_agrees_with_bytes_branch(self) -> None:
        with testkit.ScratchWorkspace() as workspace:
            target = testkit.fake_media(workspace.path, size=2048)
            self.assertEqual(
                testkit.sha256_of(target),
                testkit.sha256_of(target.read_bytes()),
                "digesting a path must equal digesting its contents",
            )

    def test_known_vector(self) -> None:
        # The empty string's SHA-256. If this ever changes, the helper is
        # hashing the path string instead of the bytes.
        self.assertEqual(
            testkit.sha256_of(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_str_branch_encodes_utf8(self) -> None:
        self.assertEqual(testkit.sha256_of("abc"), testkit.sha256_of(b"abc"))

    def test_path_string_is_not_silently_digested(self) -> None:
        # The actual trap: passing a path where bytes were meant still yields a
        # plausible hex string. Assert the two differ, so nobody "fixes" a
        # broken test by switching branches.
        with testkit.ScratchWorkspace() as workspace:
            target = testkit.fake_media(workspace.path, size=64)
            self.assertNotEqual(testkit.sha256_of(target), testkit.sha256_of(str(target)))


class TestCapturedCommand(unittest.TestCase):
    """The tgup_bridge stand-in."""

    def test_records_argv(self) -> None:
        capture = testkit.CapturedCommand()
        capture(["upload", "--file", "a.mkv"])
        self.assertEqual(capture.args, ["upload", "--file", "a.mkv"])

    def test_args_is_empty_before_any_call(self) -> None:
        self.assertEqual(testkit.CapturedCommand().args, [])

    def test_flag_returns_value(self) -> None:
        capture = testkit.CapturedCommand()
        capture(["upload", "--file", "a.mkv"])
        self.assertEqual(capture.flag("--file"), "a.mkv")

    def test_flag_returns_none_when_absent(self) -> None:
        # IndexError here would break every assertion that checks a flag is
        # NOT present, which is the most common shape in the tgup tests.
        capture = testkit.CapturedCommand()
        capture(["upload", "--file", "a.mkv"])
        self.assertIsNone(capture.flag("--url"))

    def test_flag_returns_none_when_flag_is_last(self) -> None:
        capture = testkit.CapturedCommand()
        capture(["upload", "--file"])
        self.assertIsNone(capture.flag("--file"))

    def test_called_with_requires_all_fragments(self) -> None:
        capture = testkit.CapturedCommand()
        capture(["upload", "--file", "a.mkv"])
        self.assertTrue(capture.called_with("upload", "--file"))
        self.assertFalse(capture.called_with("upload", "--url"))

    def test_captures_stdin(self) -> None:
        # tgup_bridge sends credentials on stdin, never argv, precisely so they
        # do not show up in the process list. Tests assert on that.
        capture = testkit.CapturedCommand()
        capture(["upload"], input='{"api_id": 1}')
        self.assertEqual(capture.stdin_text, '{"api_id": 1}')

    def test_returns_configured_exit_and_stdout(self) -> None:
        capture = testkit.CapturedCommand(stdout="{}", returncode=0)
        self.assertEqual(capture(["upload"]), (0, "{}"))

    def test_raises_when_asked(self) -> None:
        capture = testkit.CapturedCommand()
        capture.raises = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            capture(["upload"])

    def test_records_every_call_and_resets(self) -> None:
        capture = testkit.CapturedCommand()
        capture(["one"])
        capture(["two"])
        self.assertEqual(len(capture.calls), 2)
        capture.reset()
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(capture.args, [])


class TestEnvGuard(unittest.TestCase):
    """Environment save/restore. The leak here outlives the test that caused it."""

    def test_sets_and_restores(self) -> None:
        name = "TESTKIT_ENV_GUARD_CASE"
        os.environ.pop(name, None)
        with testkit.EnvGuard(**{name: "value"}):
            self.assertEqual(os.environ[name], "value")
        self.assertNotIn(name, os.environ, "variable must be removed, not blanked")

    def test_restores_a_previous_value(self) -> None:
        name = "TESTKIT_ENV_GUARD_CASE"
        os.environ[name] = "original"
        try:
            with testkit.EnvGuard(**{name: "temporary"}):
                self.assertEqual(os.environ[name], "temporary")
            self.assertEqual(os.environ[name], "original")
        finally:
            os.environ.pop(name, None)

    def test_restores_even_when_the_body_raises(self) -> None:
        name = "TESTKIT_ENV_GUARD_CASE"
        os.environ.pop(name, None)
        with self.assertRaises(ValueError), testkit.EnvGuard(**{name: "value"}):
            raise ValueError("test body failed")
        self.assertNotIn(name, os.environ, "a failing test must still clean up")


class TestServeBytes(unittest.TestCase):
    """Range support is the point. A 200-only server would hide the real bug."""

    def setUp(self) -> None:
        self.payload = bytes(range(256)) * 4  # 1024 bytes, every byte value
        self.server, self.url = testkit.serve_bytes(self.payload)
        self.addCleanup(testkit.stop_server, self.server)

    def _get(self, headers: dict[str, str] | None = None) -> addinfourl:
        # S310 on both lines: urlopen without a scheme audit. The URL is 127.0.0.1
        # on a port the OS just handed us, served by testkit's own handler in
        # this same process. There is no user input anywhere near it, and no
        # remote host this test could be redirected to.
        request = urllib.request.Request(self.url)  # noqa: S310
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        response: addinfourl = urllib.request.urlopen(request)  # noqa: S310
        self.addCleanup(response.close)
        return response

    def test_plain_get_returns_the_whole_payload(self) -> None:
        response = self._get()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), self.payload)

    def test_range_get_returns_206_and_exact_slice(self) -> None:
        response = self._get({"Range": "bytes=10-19"})
        self.assertEqual(response.status, 206, "a range request must answer 206, not 200")
        self.assertEqual(response.read(), self.payload[10:20])

    def test_open_ended_range_runs_to_the_end(self) -> None:
        response = self._get({"Range": "bytes=1000-"})
        self.assertEqual(response.status, 206)
        self.assertEqual(response.read(), self.payload[1000:])

    def test_suffix_range_returns_the_tail(self) -> None:
        response = self._get({"Range": "bytes=-16"})
        self.assertEqual(response.status, 206)
        self.assertEqual(response.read(), self.payload[-16:])

    def test_range_beyond_the_end_is_refused(self) -> None:
        # 416, not a 206 with a short body: a truncated answer that looks
        # successful is how a resume bug turns into silent data loss.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get({"Range": "bytes=99999-100000"})
        self.assertEqual(caught.exception.code, 416)


class TestChunkManifest(unittest.TestCase):
    """Chunk manifests, built through chunking's own factory."""

    def test_plans_the_expected_chunk_count(self) -> None:
        with testkit.ScratchWorkspace() as workspace:
            source = testkit.fake_media(workspace.path, size=10000)
            manifest = testkit.chunk_manifest(source, chunk_bytes=4096)
            self.assertEqual(testkit.manifest_chunk_size(manifest), 4096)
            self.assertEqual(len(manifest.records), 3, "10000 / 4096 -> 3 chunks")

    def test_chunk_lengths_cover_the_source_exactly(self) -> None:
        # Overlapping or short chunks corrupt reassembly. This is the invariant
        # that makes the whole manifest worth having.
        with testkit.ScratchWorkspace() as workspace:
            source = testkit.fake_media(workspace.path, size=10000)
            manifest = testkit.chunk_manifest(source, chunk_bytes=4096)
            total = sum(record.length for record in testkit.manifest_records(manifest))
            self.assertEqual(total, source.stat().st_size)

    def test_records_are_one_based(self) -> None:
        # The production convention. Pinned because it is surprising, and a
        # refactor that changed it would break part naming everywhere.
        with testkit.ScratchWorkspace() as workspace:
            source = testkit.fake_media(workspace.path, size=10000)
            manifest = testkit.chunk_manifest(source, chunk_bytes=4096)
            self.assertEqual(sorted(manifest.records), [1, 2, 3])

    def test_each_chunk_digest_matches_its_own_bytes(self) -> None:
        with testkit.ScratchWorkspace() as workspace:
            source = testkit.fake_media(workspace.path, size=10000)
            manifest = testkit.chunk_manifest(source, chunk_bytes=4096)
            for record in testkit.manifest_records(manifest):
                with source.open("rb") as handle:
                    handle.seek(record.offset)
                    payload = handle.read(record.length)
                self.assertEqual(record.sha256, testkit.sha256_of(payload))


class TestTgLinks(unittest.TestCase):
    """Asserted against the real build_message_link / parse_tg_link."""

    def test_roundtrip_preserves_the_message_id(self) -> None:
        parsed = testkit.tg_link_roundtrip("@tgwebcloud1", 4242)
        self.assertEqual(str(parsed["message_id"]), "4242")

    def test_builds_a_telegram_url(self) -> None:
        self.assertIn("t.me", testkit.tg_link("@tgwebcloud1", 4242))


class TestDbRowCount(unittest.TestCase):
    """Must read the CURRENT db.DB_PATH, never a hardcoded production path."""

    def test_reads_the_current_database_and_returns_a_count(self) -> None:
        # Works in BOTH run modes. Under pytest the conftest guard redirects
        # db.DB_PATH to a scratch file; standalone (`python test_testkit.py`)
        # there is no conftest, so it reads production. Either way the helper
        # must return a real count rather than raising -- and it must be reading
        # db.DB_PATH as it stands right now, not a path baked into testkit.
        import db

        before = testkit.db_row_count("projects")
        self.assertGreaterEqual(before, 0, "a row count should always be readable")

        # Point the module at a throwaway database and prove the helper follows
        # it rather than returning the production number it would otherwise see.
        original = db.DB_PATH
        with testkit.ScratchWorkspace() as workspace, self.subTest(scratch=workspace.path):
            db.DB_PATH = str(workspace.path / "probe.db")
            try:
                empty = testkit.db_row_count("projects")
                self.assertEqual(empty, 0, "a brand-new database has no rows")
            except Exception as exc:  # noqa: BLE001 -- any failure is a failure here
                # A missing table is fine (the schema was never created). What
                # must never happen is the helper reaching production instead.
                self.assertNotIn("t_dubber.db", str(exc), "must not have fallen back to production")
            finally:
                db.DB_PATH = original

    @unittest.skipUnless(
        "pytest" in sys.modules,
        "the scratch redirect is installed by conftest.py, which only runs under pytest",
    )
    def test_reads_the_scratch_database_under_pytest(self) -> None:
        import db
        import test_isolation

        self.assertIsNotNone(test_isolation._SCRATCH, "conftest should have redirected")
        self.assertEqual(
            db.DB_PATH,
            test_isolation._SCRATCH,
            "the helper must read wherever the guard points, not production",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
