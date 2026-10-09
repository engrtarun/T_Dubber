"""`tgup --url` on the Python side: the argv contract, and a refusal to guess.

The Go side of the zero-disk upload is proved by Go tests (source_test.go) and
by hand against a real range server. What is proved HERE is the Python contract:
that ``--url`` reaches the binary, that the old ``--file`` argument list is
unchanged byte for byte, and that "both" or "neither" is refused instead of
guessed.

No Telegram, no session, no credentials: these tests never run the binary. They
pin the command line, which is the only thing Python owns.

Run with:  python -m pytest test_tgup_url.py -q
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import go_planner  # noqa: E402
import tgup_bridge  # noqa: E402


class CapturedCommand:
    """Stands in for tgup_bridge.run_command and remembers the argv it was given."""

    def __init__(self):
        self.args = []
        self.stdin_text = None

    def __call__(self, args, on_progress=None, stdin_text=None, on_human=None):
        self.args = list(args)
        self.stdin_text = stdin_text
        return 0, "", ""


class UrlArgvTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="tgup_url_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.saved_session = os.environ.get("TGUP_SESSION")
        os.environ["TGUP_SESSION"] = str(self.scratch / "tgup.session")
        self.addCleanup(self._restore_session)

        self.saved_refuse = tgup_bridge._refuse
        self.saved_run = tgup_bridge.run_command
        self.saved_result = tgup_bridge._result_from
        # _refuse() is the guard that keeps a headless worker from spending an
        # OTP. With it stubbed out these tests exercise the argv only.
        tgup_bridge._refuse = lambda *a, **k: ""
        tgup_bridge._result_from = lambda *a, **k: tgup_bridge.GoUploadResult(
            used_go=True, ok=True
        )
        self.capture = CapturedCommand()
        tgup_bridge.run_command = self.capture
        self.addCleanup(self._restore_bridge)

    def _restore_session(self):
        if self.saved_session is None:
            os.environ.pop("TGUP_SESSION", None)
        else:
            os.environ["TGUP_SESSION"] = self.saved_session

    def _restore_bridge(self):
        tgup_bridge._refuse = self.saved_refuse
        tgup_bridge.run_command = self.saved_run
        tgup_bridge._result_from = self.saved_result

    def _flag(self, name):
        args = self.capture.args
        return args[args.index(name) + 1] if name in args else None

    def test_url_is_passed_and_file_is_not(self):
        tgup_bridge.upload(
            url="https://host/movie.mp4",
            channel="@name",
            api_id=1,
            api_hash="h",
            concurrency=4,
        )
        self.assertEqual(self._flag("--url"), "https://host/movie.mp4")
        self.assertNotIn("--file", self.capture.args)

    def test_file_path_is_byte_identical_to_before(self):
        """--file must not change shape. Every existing caller depends on it."""
        tgup_bridge.upload(
            file=r"C:\videos\big.mkv",
            channel="@name",
            api_id=35578684,
            api_hash="hash",
            concurrency=4,
            caption="hi",
            thumbnail=r"C:\videos\thumb.jpg",
        )
        args = self.capture.args
        self.assertEqual(args[0], "upload")
        self.assertEqual(args[1], "--file")
        self.assertEqual(args[2], r"C:\videos\big.mkv")
        self.assertIn("--channel", args)
        self.assertIn("--credentials-stdin", args)
        self.assertIn("--caption", args)
        self.assertIn("--thumbnail", args)
        self.assertNotIn("--url", args)
        self.assertNotIn("--dry-run", args)
        # The api_id string coercion that stopped the bench failing on a parse
        # error must still reach the child process on stdin.
        self.assertIn('"api_id": 35578684', self.capture.stdin_text)

    def test_both_file_and_url_is_refused(self):
        result = tgup_bridge.upload(
            file=r"C:\videos\big.mkv",
            url="https://host/movie.mp4",
            channel="@name",
            api_id=1,
            api_hash="h",
        )
        self.assertFalse(result.ok)
        self.assertIn("not both", result.error)
        self.assertEqual(self.capture.args, [],
                         "nothing may be sent when the source is ambiguous")

    def test_neither_file_nor_url_is_refused(self):
        result = tgup_bridge.upload(channel="@name", api_id=1, api_hash="h")
        self.assertFalse(result.ok)
        self.assertIn("either file= or url=", result.error)
        self.assertEqual(self.capture.args, [])

    def test_dry_run_needs_no_credentials(self):
        """A dry run sends nothing, so it must not demand a session."""
        lines = []
        tgup_bridge.upload(
            url="https://host/movie.mp4",
            channel="",
            api_id=1,
            api_hash="",
            dry_run=True,
            on_human=lines.append,
        )
        self.assertIn("--dry-run", self.capture.args)
        self.assertEqual(self._flag("--channel"), "")

    def test_url_timeout_is_only_sent_when_asked_for(self):
        tgup_bridge.upload(url="https://host/m.mp4", channel="@n", api_id=1,
                           api_hash="h", url_timeout=12.5)
        self.assertEqual(self._flag("--url-timeout"), "12.5s")
        tgup_bridge.upload(url="https://host/m.mp4", channel="@n", api_id=1,
                           api_hash="h")
        self.assertNotIn("--url-timeout", self.capture.args)


class GoPlannerUrlTests(unittest.TestCase):
    def setUp(self):
        self.saved_upload = go_planner.tgup_bridge.upload
        self.seen = {}

        def fake_upload(**kwargs):
            self.seen = dict(kwargs)
            return tgup_bridge.GoUploadResult(
                used_go=True,
                ok=True,
                total_size=1234,
                chunk_count=2,
                source_sha256="deadbeef",
            )

        go_planner.tgup_bridge.upload = fake_upload
        self.addCleanup(self._restore)

    def _restore(self):
        go_planner.tgup_bridge.upload = self.saved_upload

    def test_upload_url_via_go_sends_the_url(self):
        out = go_planner.upload_url_via_go(
            "https://host/movie.mp4", api_id=1, api_hash="h", channel="@name"
        )
        self.assertEqual(self.seen["url"], "https://host/movie.mp4")
        self.assertEqual(self.seen["channel"], "@name")
        self.assertFalse(self.seen["dry_run"])
        self.assertEqual(out["source_sha256"], "deadbeef")
        self.assertTrue(out["chunked"])

    def test_dry_run_needs_no_channel_or_hash(self):
        go_planner.upload_url_via_go(
            "https://host/movie.mp4", api_id=1, dry_run=True
        )
        self.assertEqual(self.seen["channel"], "")
        self.assertEqual(self.seen["api_hash"], "")
        self.assertTrue(self.seen["dry_run"])

    def test_a_non_http_url_is_refused_before_any_command(self):
        with self.assertRaises(RuntimeError) as ctx:
            go_planner.upload_url_via_go(
                "ftp://host/movie.mp4", api_id=1, api_hash="h", channel="@n"
            )
        self.assertIn("http(s)", str(ctx.exception))
        self.assertEqual(self.seen, {}, "nothing may be sent for an unusable URL")

    def test_a_real_send_without_a_channel_is_refused(self):
        with self.assertRaises(RuntimeError):
            go_planner.upload_url_via_go(
                "https://host/movie.mp4", api_id=1, api_hash="h", channel=""
            )
        self.assertEqual(self.seen, {})


if __name__ == "__main__":
    unittest.main()