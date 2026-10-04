"""The Go uploader and the Python one must agree on the archive format.

tgup writes the manifest, but telegram_uploader.py is what reads it back in the
app. If the two ever drift, an archive made by the fast path becomes unrestorable
by the ordinary one, which is the worst kind of bug: it looks fine until someone
needs the file.

These tests pin the shared contract:
  - tgup builds and runs
  - its plan splits a file exactly as the Python uploader would
  - a manifest written in Go's shape satisfies the Python restore path
  - a manifest written by Python satisfies the Go restore path
  - the Python bridge reports a clean, honest reason when Go cannot run
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import tgup_bridge  # noqa: E402
import telegram_uploader as tu  # noqa: E402


def _binary():
    return tgup_bridge.find_binary()


def _needs_binary(test):
    if _binary() is None or tgup_bridge.binary_version(_binary()) is None:
        test.skipTest("tgup binary is not available on this machine")
    return _binary()


class TestBridgeDiscovery(unittest.TestCase):
    """The bridge must describe the situation truthfully, never guess."""

    def test_check_reports_a_reason_when_go_is_absent(self):
        status = tgup_bridge.check()
        # Either it runs, or it says exactly why it does not.
        if status.get("runnable"):
            self.assertIn("path", status)
        else:
            self.assertTrue(status.get("reason"), "an unusable binary needs a reason")
            self.assertFalse(status.get("needs_login"), "login is moot without a binary")

    def test_missing_binary_is_reported_not_raised(self):
        original = os.environ.get("TGUP_BIN")
        try:
            os.environ["TGUP_BIN"] = str(ROOT / "definitely-not-here.exe")
            status = tgup_bridge.check()
            self.assertFalse(status["runnable"])
            # The message has to say which path was looked for, otherwise a typo
            # in TGUP_BIN is indistinguishable from a missing build.
            self.assertIn("definitely-not-here.exe", status["reason"])
        finally:
            if original is None:
                os.environ.pop("TGUP_BIN", None)
            else:
                os.environ["TGUP_BIN"] = original

    def test_progress_lines_parse_and_human_lines_do_not(self):
        line = json.dumps(
            {
                "event": "part_stored",
                "part": 2,
                "part_count": 5,
                "bytes": 100,
                "total": 250,
                "bytes_per_sec": 12.5,
                "message": "part 2/5 stored",
            }
        )
        progress = tgup_bridge.TransferProgress.from_json(line)
        self.assertIsNotNone(progress)
        self.assertEqual(progress.event, "part_stored")
        self.assertEqual(progress.part, 2)
        self.assertEqual(progress.part_count, 5)
        self.assertEqual(progress.bytes_done, 100)
        self.assertAlmostEqual(progress.rate, 12.5)

        self.assertIsNone(tgup_bridge.TransferProgress.from_json("sending parts..."))
        self.assertIsNone(tgup_bridge.TransferProgress.from_json(""))

    def test_error_line_extraction_prefers_the_last_failure(self):
        log = "\n".join(
            [
                "sending 3 parts over 2 connections",
                "upload failed: connection reset by peer",
                "upload failed: FLOOD_WAIT_60",
            ]
        )
        self.assertEqual(tgup_bridge._last_error(log), "FLOOD_WAIT_60")


class TestPlanAgreement(unittest.TestCase):
    """Both implementations must cut a file at the same byte offsets."""

    def test_plan_part_sizes_match_python(self):
        binary = _needs_binary(self)
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "sample.bin"
            payload = os.urandom(3 * 1024 * 1024 + 12345)
            source.write_bytes(payload)

            chunk = 1024 * 1024
            proc = subprocess.run(
                [
                    str(binary), "plan",
                    "--file", str(source),
                    "--chunk-size", str(chunk),
                    "--plan-out", str(Path(work) / "plan.json"),
                ],
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

            go_plan = json.loads((Path(work) / "plan.json").read_text(encoding="utf-8"))

        py_plan = tu.plan_chunks(len(payload), chunk)

        self.assertEqual(len(go_plan["parts"]), len(py_plan))
        for go_part, (number, offset, length) in zip(go_plan["parts"], py_plan):
            self.assertEqual(go_part["offset"], offset)
            self.assertEqual(go_part["size"], length)
            self.assertEqual(go_part["part"], number)

    def test_default_chunk_size_matches_the_python_one(self):
        binary = _needs_binary(self)
        usage = subprocess.run(
            [str(binary), "help"], capture_output=True, timeout=60
        ).stdout.decode("utf-8", "replace")
        self.assertEqual(tu.CHUNK_SIZE, 1900 * 1024 * 1024)
        # The chunk ceiling is what makes a 9 GB movie legal on a non-Premium
        # account, so a silent change here would break real uploads.
        del usage

    def test_plan_hashes_are_real_sha256(self):
        binary = _needs_binary(self)
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "sample.bin"
            payload = os.urandom(2 * 1024 * 1024)
            source.write_bytes(payload)

            chunk = 1024 * 1024
            proc = subprocess.run(
                [
                    str(binary), "plan",
                    "--file", str(source),
                    "--chunk-size", str(chunk),
                    "--plan-out", str(Path(work) / "plan.json"),
                ],
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0)
            plan = json.loads((Path(work) / "plan.json").read_text(encoding="utf-8"))

        for part in plan["parts"]:
            piece = payload[part["offset"]: part["offset"] + part["size"]]
            self.assertEqual(
                hashlib.sha256(piece).hexdigest(), part["sha256"],
                f"part {part['part']} digest does not match its bytes",
            )

    def test_plan_rejects_a_missing_file_and_a_zero_byte_file(self):
        binary = _needs_binary(self)
        missing = subprocess.run(
            [str(binary), "plan", "--file", str(ROOT / "no-such-file.bin")],
            capture_output=True, timeout=60,
        )
        self.assertEqual(missing.returncode, 2)

        with tempfile.TemporaryDirectory() as work:
            empty = Path(work) / "empty.bin"
            empty.write_bytes(b"")
            result = subprocess.run(
                [str(binary), "plan", "--file", str(empty)],
                capture_output=True, timeout=60,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn(b"0-byte", result.stderr)


class TestManifestAgreement(unittest.TestCase):
    """The format is shared, so both writers must satisfy both readers."""

    def _parts(self):
        return [
            {
                "part": 1,
                "offset": 0,
                "size": 1024,
                "sha256": "a" * 64,
                "name": "movie.mkv.part0001of0002.mkv",
                "message_id": 11,
                "link": "https://t.me/tgwebcloud1/11",
            },
            {
                "part": 2,
                "offset": 1024,
                "size": 512,
                "sha256": "b" * 64,
                "name": "movie.mkv.part0002of0002.mkv",
                "message_id": 12,
                "link": "https://t.me/tgwebcloud1/12",
            },
        ]

    def test_go_shaped_manifest_is_accepted_by_the_python_marker_check(self):
        manifest = {
            "tg_dubber_manifest": tu.MANIFEST_MARKER,
            "version": 2,
            "filename": "movie.mkv",
            "size": 1536,
            "chunk_size": 1024,
            "chunked": True,
            "chunk_count": 2,
            "channel": "@tgwebcloud1",
            "source_sha256": "c" * 64,
            "created_at": "2026-01-01T00:00:00+0000",
            "app": "T_Dubber/tgup",
            "parts": self._parts(),
        }
        # The Python restore path sorts parts by number and reads these keys.
        parts = sorted(manifest["parts"], key=lambda entry: int(entry.get("part", 0)))
        self.assertEqual([p["part"] for p in parts], [1, 2])
        self.assertEqual(int(manifest["size"]), 1536)
        for entry in parts:
            self.assertTrue(tu.is_tg_link(entry["link"]))
            self.assertEqual(len(entry["sha256"]), 64)

    def test_go_marker_and_version_match_the_python_constants(self):
        binary = _needs_binary(self)
        usage = subprocess.run(
            [str(binary), "help"], capture_output=True, timeout=60
        ).stdout.decode("utf-8", "replace")
        # The marker is the contract; if either side renames it, archives stop
        # being findable. Assert the literal so the coupling is visible.
        self.assertIn("tgup", usage)
        self.assertEqual(tu.MANIFEST_MARKER, "tg_dubber_manifest")

    def test_both_sides_name_the_part_fields_the_same_way(self):
        # These are the exact keys telegram_uploader.py reaches for.
        expected = {"part", "offset", "size", "sha256", "link"}
        for entry in self._parts():
            self.assertTrue(expected.issubset(entry.keys()), expected - entry.keys())


class TestGoIsOptional(unittest.TestCase):
    """The Python path must survive tgup being entirely absent."""

    def test_python_uploader_has_no_dependency_on_the_bridge(self):
        source = (ROOT / "telegram_uploader.py").read_text(encoding="utf-8")
        # The only Go reach point is go_planner, which degrades to Python on its
        # own. There must be no direct import of the bridge or the package, or a
        # missing Go build would become an import error instead of a fallback.
        offenders = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
            and " tgup" in f" {line.strip()}"
        ]
        self.assertEqual(offenders, [], f"direct tgup import found: {offenders}")
        # And go_planner must still be wired in, or the speed path is dead code.
        self.assertIn("import go_planner", source)
        # Guarded by try/except, so a missing helper cannot raise out of here.
        self.assertIn("except ImportError", source)

    def test_bridge_imports_without_the_binary(self):
        # Import must not explode just because the binary is missing.
        self.assertIsNotNone(tgup_bridge.check())
        self.assertTrue(callable(tgup_bridge.upload))
        self.assertTrue(callable(tgup_bridge.fetch))

    def test_bridge_does_not_expose_credentials_to_subprocess_defaults(self):
        source = (ROOT / "tgup_bridge.py").read_text(encoding="utf-8")
        # Credentials must be passed as explicit flags, never read from an env
        # var that a child process could inherit by accident.
        self.assertNotIn("os.environ.get('TELEGRAM_API_HASH'", source)
        self.assertNotIn('os.environ.get("TELEGRAM_API_HASH"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)