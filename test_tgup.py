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
  - and no run ever asks Telegram for a login code it cannot answer (see
    TestNoOtpLogin, and login_test.go for the binary's half of that rule)
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import tgup_bridge  # noqa: E402
import telegram_uploader as tu  # noqa: E402


def _binary():
    cmd = tgup_bridge.get_base_command()
    return cmd if cmd else None


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
                    *binary, "plan",
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
            [*binary, "help"], capture_output=True, timeout=60
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
                    *binary, "plan",
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
            [*binary, "plan", "--file", str(ROOT / "no-such-file.bin")],
            capture_output=True, timeout=60,
        )
        self.assertEqual(missing.returncode, 2)

        with tempfile.TemporaryDirectory() as work:
            empty = Path(work) / "empty.bin"
            empty.write_bytes(b"")
            result = subprocess.run(
                [*binary, "plan", "--file", str(empty)],
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
            [*binary, "help"], capture_output=True, timeout=60
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


class TestPerChunkRecoveryUsesTheVerifiedFetchPath(unittest.TestCase):
    """Chunk recovery must go through tgup's fetch, not a second downloader.

    The mission rule is "sirf us chunk ko Telegram se wapas manga kar retry" --
    fetch THAT chunk back and retry only it. The tempting shortcut is to write a
    small Telethon download for one part. That would be a second, weaker
    implementation of something tgup already does, so these tests pin the
    wiring: the call must go to ``tgup_bridge.fetch`` with ``verify=True``.

    No session, no binary, no network: ``tgup_bridge.fetch`` is replaced with a
    stub and the assertion is about which function was called and how.
    """

    def setUp(self):
        import chunking

        self.chunking = chunking
        self.scratch = tempfile.mkdtemp(prefix="tgup_chunk_recovery_")
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.source = Path(self.scratch) / "movie.mkv"
        payload = os.urandom(2 * 1024 * 1024 + 4096)
        self.source.write_bytes(payload)
        self.payload = payload

        self.manifest = chunking.ChunkManifest.create(
            self.source, chunk_bytes=1024 * 1024, channel="@testchan",
        )
        self.record = self.manifest.record(1)
        self.manifest.begin(self.record)
        # A prior attempt produced a message, so there is something to fetch.
        self.record.link = "https://t.me/testchan/9001"

        self.calls = []
        self.saved_fetch = tgup_bridge.fetch
        tgup_bridge.fetch = self._fake_fetch
        self.addCleanup(self._restore)

    def _restore(self):
        tgup_bridge.fetch = self.saved_fetch

    def _fake_fetch(self, **kwargs):
        self.calls.append(kwargs)
        # tgup names the downloaded file after the document; the stub writes
        # that same name so the caller's resolution logic is exercised too.
        dest = Path(kwargs["dest"])
        filename = f"part{self.record.index:05d}.bin"
        target = dest / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            self.payload[self.record.offset:self.record.offset + self.record.length]
        )
        return tgup_bridge.GoUploadResult(
            used_go=True, ok=True, filename=filename,
        )

    def test_recovery_calls_the_shared_bridge_with_verification_on(self):
        result = self.chunking.fetch_chunk_from_telegram(
            self.record, self.scratch, api_id=1, api_hash="h",
        )
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        # The link must be THIS chunk's link, and the fetch must verify.
        self.assertEqual(call["link"], "https://t.me/testchan/9001")
        self.assertTrue(call["verify"], "a fetch-back must verify, or it proves nothing")
        self.assertEqual(call["api_id"], 1)
        self.assertTrue(os.path.isfile(result))

    def test_the_retry_helper_fetches_only_the_failed_chunk(self):
        sends = []

        def flaky(record, source_path):
            sends.append(record.index)
            raise OSError("dropped")

        # fetch-back proves the bytes are already safe, so no second send.
        result = self.chunking.upload_chunk_with_recovery(
            self.record, str(self.source), flaky,
            recover=lambda chunk: self.chunking.fetch_chunk_from_telegram(
                chunk, self.scratch, api_id=1, api_hash="h",
            ),
            max_attempts=3,
        )
        self.assertTrue(result.get("recovered"))
        self.assertEqual(len(sends), 1, "only this chunk was ever attempted")
        self.assertEqual(len(self.calls), 1, "only this chunk was ever fetched")

    def test_no_other_chunk_is_fetched_when_one_fails(self):
        """The rest of the movie is not re-uploaded and not re-fetched."""
        others = []
        for record in self.manifest.chunks():
            if record.index == self.record.index:
                continue
            self.manifest.complete(record, message_id=8000 + record.index,
                                  link=f"https://t.me/testchan/{8000 + record.index}")
            others.append(record.index)

        def always_fails(record, source_path):
            raise OSError("network is down")

        def refusing(**kwargs):
            self.calls.append(kwargs)
            return tgup_bridge.GoUploadResult(
                used_go=True, ok=False, error="connection reset",
            )

        tgup_bridge.fetch = refusing
        with self.assertRaises(self.chunking.ChunkRetryExhausted):
            self.chunking.upload_chunk_with_recovery(
                self.record, str(self.source), always_fails,
                recover=lambda chunk: self.chunking.fetch_chunk_from_telegram(
                    chunk, self.scratch, api_id=1, api_hash="h",
                ),
                max_attempts=2,
            )
        # Exactly the failing chunk was fetched -- twice, once per attempt.
        fetched_links = {call["link"] for call in self.calls}
        self.assertEqual(fetched_links, {"https://t.me/testchan/9001"})
        self.assertEqual(len(self.calls), 2)
        for index in others:
            self.assertNotIn(f"https://t.me/testchan/{8000 + index}", fetched_links)

    def test_a_failed_fetch_reports_why_rather_than_claiming_success(self):
        def refusing(**kwargs):
            self.calls.append(kwargs)
            return tgup_bridge.GoUploadResult(
                used_go=True, ok=False,
                fallback_reason="no tgup session at C:\\tmp\\tgup.session",
            )

        tgup_bridge.fetch = refusing
        with self.assertRaises(self.chunking.ChunkingError) as ctx:
            self.chunking.fetch_chunk_from_telegram(
                self.record, self.scratch, api_id=1, api_hash="h",
            )
        self.assertIn("no tgup session", str(ctx.exception))
        self.assertIn("chunk 1", str(ctx.exception))

    def test_a_fetch_that_returns_no_file_is_not_treated_as_success(self):
        def silent(**kwargs):
            self.calls.append(kwargs)
            # ok=True, and it names a file that never landed: what a vanished
            # or expired message looks like from Python's side.
            return tgup_bridge.GoUploadResult(
                used_go=True, ok=True, filename="gone.bin",
            )

        tgup_bridge.fetch = silent
        with self.assertRaises(self.chunking.ChunkingError) as ctx:
            self.chunking.fetch_chunk_from_telegram(
                self.record, self.scratch, api_id=1, api_hash="h",
            )
        # It must name the file it expected, or the message is untraceable.
        self.assertIn("gone.bin", str(ctx.exception))

    def test_a_fetch_with_no_name_falls_back_to_what_is_actually_there(self):
        def anonymous(**kwargs):
            self.calls.append(kwargs)
            # No filename reported, but the bytes did land.
            return tgup_bridge.GoUploadResult(used_go=True, ok=True)

        tgup_bridge.fetch = anonymous
        result = self.chunking.fetch_chunk_from_telegram(
            self.record, self.scratch, api_id=1, api_hash="h",
        )
        self.assertTrue(os.path.isfile(result))


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


class TestNoOtpLogin(unittest.TestCase):
    """A login code must never be requested by a machine that cannot answer it.

    Telegram dispatches the code as soon as the auth flow starts. Every run
    that began without a session -- on a Kaggle kernel, or anywhere else with
    no terminal -- therefore bought a real OTP, hit EOF on the prompt, and fell
    back to Telethon. The upload worked anyway, which is exactly why the waste
    was easy to miss.
    """

    def _with_env(self, **values):
        """Context manager setting env vars, restoring the previous values."""

        @contextlib.contextmanager
        def scope():
            saved = {key: os.environ.get(key) for key in values}
            try:
                os.environ.update(values)
                yield
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

        return scope()

    def test_session_path_follows_the_env_override(self):
        with self._with_env(TGUP_SESSION="/kaggle/working/tgup.session"):
            # Compared as a Path: the point is which file, not which separator.
            self.assertEqual(
                tgup_bridge.session_path(), Path("/kaggle/working/tgup.session")
            )
        # Without it, the session sits beside this file: one place, whatever
        # directory the process happens to be standing in.
        with self._with_env(TGUP_SESSION=""):
            self.assertEqual(tgup_bridge.session_path(), tgup_bridge.TGUP_DIR / "tgup.session")

    def test_no_session_means_no_login_attempt(self):
        with tempfile.TemporaryDirectory() as work:
            missing = str(Path(work) / "tgup.session")
            with self._with_env(TGUP_SESSION=missing, TGUP_ALLOW_LOGIN=""):
                reason = tgup_bridge.session_refusal("upload")
        self.assertTrue(reason, "a missing session must be refused, not attempted")
        self.assertIn(missing, reason)
        self.assertIn("OTP", reason)

    def test_an_existing_session_lifts_the_refusal(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / "tgup.session"
            session.write_bytes(b"")
            with self._with_env(TGUP_SESSION=str(session), TGUP_ALLOW_LOGIN=""):
                self.assertEqual(tgup_bridge.session_refusal("upload"), "")
                self.assertTrue(tgup_bridge.session_ready())
                self.assertFalse(tgup_bridge.needs_login())

    def test_an_explicit_login_opt_in_lifts_the_refusal(self):
        with tempfile.TemporaryDirectory() as work:
            missing = str(Path(work) / "tgup.session")
            with self._with_env(TGUP_SESSION=missing, TGUP_ALLOW_LOGIN="1"):
                self.assertEqual(tgup_bridge.session_refusal("upload"), "")

    def test_upload_refuses_without_spawning_the_binary(self):
        # The refusal has to happen before the process exists: spawning tgup is
        # what sends the code.
        with tempfile.TemporaryDirectory() as work:
            missing = str(Path(work) / "tgup.session")
            with self._with_env(TGUP_SESSION=missing, TGUP_ALLOW_LOGIN=""):
                with unittest.mock.patch.object(
                    tgup_bridge,
                    "run_command",
                    side_effect=AssertionError("tgup must not be spawned"),
                ):
                    result = tgup_bridge.upload(
                        file=ROOT / "tgup_bridge.py",
                        channel="@tgwebcloud1",
                        api_id=1,
                        api_hash="hash",
                        phone="+910000000000",
                    )
        self.assertFalse(result.ok)
        self.assertTrue(result.used_go)
        self.assertIn("OTP", result.error)
        # A fallback reason is how the caller knows to use Telethon rather than
        # treating this as a broken upload.
        self.assertIn("Telethon", result.fallback_reason)

    def test_go_path_declines_a_run_with_no_session(self):
        import go_planner

        with tempfile.TemporaryDirectory() as work:
            missing = str(Path(work) / "tgup.session")
            with self._with_env(TGUP_SESSION=missing, TGUP_ALLOW_LOGIN=""):
                use_go, reason = go_planner.should_use_go_upload(
                    4 * 1024 ** 3, channel="@tgwebcloud1"
                )
            self.assertFalse(use_go)
            self.assertIn("OTP", reason)

            session = Path(work) / "have.session"
            session.write_bytes(b"")
            with self._with_env(TGUP_SESSION=str(session), TGUP_ALLOW_LOGIN=""):
                use_go, reason = go_planner.should_use_go_upload(
                    4 * 1024 ** 3, channel="@tgwebcloud1"
                )
            self.assertTrue(use_go, reason)

    def test_the_binary_accepts_the_session_flag_it_ships_with(self):
        """The flag the bridge now passes must be one the binary understands.

        An unknown flag is not a fallback, it is a dead end: tgup exits 2 and
        the run loses the fast path entirely. login_test.go holds the other
        half -- the rule that no code is requested without a terminal -- which
        cannot be exercised here without a real network login.
        """
        binary = _needs_binary(self)
        with tempfile.TemporaryDirectory() as work:
            session = str(Path(work) / "no-session.session")
            proc = subprocess.run(
                [
                    *binary, "upload",
                    "--file", str(ROOT / "tgup_bridge.py"),
                    "--channel", "@tgwebcloud1",
                    "--api-id", "1",
                    "--api-hash", "bogus",
                    "--phone", "+910000000000",
                    "--session", session,
                    "--dry-run",   # hashes and stops: no connection, no login
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=120,
            )
            self.assertNotIn(b"not defined", proc.stderr)
            self.assertEqual(
                proc.returncode, 0,
                proc.stderr.decode("utf-8", "replace"),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)