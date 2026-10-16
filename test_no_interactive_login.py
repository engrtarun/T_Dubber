"""The app must NEVER stop for an interactive Telegram OTP prompt.

Regression suite for the Gradio-worker hang: ``app.py`` runs uploads on worker
threads with no console, and ``client.start()`` sends the OTP to the phone
FIRST and only then asks for it with ``input()`` -- by the time the prompt
appeared, the code was already spent and the thread was blocked forever.

What this file pins down:

1. ``_get_client`` raises ``TelegramCloudError`` when the session is missing,
   WITHOUT ever calling ``start()`` (which would send a wasted OTP).
2. Every ``.start(phone=...)`` reachable from the app carries a
   ``code_callback`` that raises instead of prompting (telethon only swallows
   ``telethon.errors.*`` around its code prompt).
3. ``_bootstrap`` (``python telegram_uploader.py``) refuses on a non-TTY
   before any prompt; ``login_once.py`` refuses before contacting Telegram at
   all unless a ``--code-file`` was given.
4. ``ffprobe``/``ffmpeg`` are launched with ``stdin=DEVNULL`` and video-frame
   thumbnails land in ``%TEMP%`` (never in the project tree) and are deleted
   in a ``finally``.
5. ``prune_upload_journals`` keeps the newest journals, never deletes the
   in-flight one, and is idempotent.

Everything runs against FAKE telethon modules injected into ``sys.modules``:
no network, no real session file, stdlib-only (CI-safe).

Run with:  python -m pytest test_no_interactive_login.py -q
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any

import telegram_uploader as tg

import testkit

ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
#  Fake telethon plumbing
# ---------------------------------------------------------------------------


class FakeMessage:
    """What telethon's send_file returns that this code actually reads."""

    def __init__(self, message_id: int = 101) -> None:
        self.id = message_id
        self.media = None


class FakeClient:
    """A telethon client stand-in that records -- never performs -- everything."""

    def __init__(
        self,
        session: Any = None,
        api_id: Any = None,
        api_hash: Any = None,
    ) -> None:
        self.session = session
        self.api_id = api_id
        self.api_hash = api_hash
        self.authorized = True
        self.connected = True
        self.start_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.disconnect_calls = 0
        self.sent: list[dict[str, Any]] = []
        self.send_error: Exception | None = None
        self.sent_code: str | None = None

    def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    def is_user_authorized(self) -> bool:
        return self.authorized

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    def start(self, *args: Any, **kwargs: Any) -> Any:
        # The whole point: nothing in the app path may ever reach start().
        self.start_calls.append((args, kwargs))
        raise AssertionError("start() must never be called from the app path")

    def send_file(self, entity: Any, file: Any = None, **kwargs: Any) -> FakeMessage:
        if self.send_error is not None:
            raise self.send_error
        thumb = kwargs.get("thumb")
        thumb_bytes = None
        if thumb is not None:
            with open(thumb, "rb") as handle:
                thumb_bytes = handle.read()
        if hasattr(file, "read"):
            while file.read(65536):
                pass
        self.sent.append(
            {
                "entity": entity,
                "thumb": thumb,
                "thumb_bytes": thumb_bytes,
                "caption": kwargs.get("caption"),
            }
        )
        return FakeMessage(100 + len(self.sent))


def _module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


class FakeTelegram:
    """Install fake telethon modules into sys.modules; restore on exit.

    Works with or without the real telethon installed: whatever was there
    before is put back, so no other test can observe the swap. The factory
    must RETURN a client (it is used as ``TelegramClient(...)``), so the
    object ``_get_client`` hands back is the very one the test inspects.
    """

    def __init__(self, client_factory: Any = None) -> None:
        self.client_factory = client_factory
        self.created: list[FakeClient] = []
        self._saved: dict[str, types.ModuleType | None] = {}

    def _new_client(self, session: Any, api_id: Any, api_hash: Any) -> FakeClient:
        client = FakeClient(session, api_id, api_hash)
        self.created.append(client)
        return client

    def __enter__(self) -> FakeTelegram:
        inner = self.client_factory or self._new_client

        def TelegramClient(session: Any, api_id: Any, api_hash: Any) -> Any:
            return inner(session, api_id, api_hash)

        class FloodWaitError(Exception):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args)
                self.seconds = kwargs.get("seconds", 0)

        fakes = {
            "telethon": _module("telethon"),
            "telethon.sync": _module("telethon.sync", TelegramClient=TelegramClient),
            "telethon.errors": _module("telethon.errors", FloodWaitError=FloodWaitError),
            "telethon.tl": _module("telethon.tl"),
            "telethon.tl.types": _module(
                "telethon.tl.types",
                DocumentAttributeFilename=lambda *a, **k: ("filename", a, k),
                DocumentAttributeVideo=lambda *a, **k: ("video", a, k),
            ),
        }
        for name, mod in fakes.items():
            self._saved[name] = sys.modules.get(name)
            sys.modules[name] = mod
        return self

    def __exit__(self, *exc: object) -> None:
        for name, original in self._saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class FakeStdin:
    """A stdin stand-in with a controllable TTY answer."""

    def __init__(self, is_tty: bool = False) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty

    def read(self, *args: Any) -> bytes:
        raise AssertionError("nothing may read stdin in a headless run")

    def readline(self, *args: Any) -> str:
        raise AssertionError("nothing may read stdin in a headless run")


def patch_stdin(is_tty: bool) -> Any:
    saved = sys.stdin
    sys.stdin = FakeStdin(is_tty)
    return lambda: setattr(sys, "stdin", saved)


# ---------------------------------------------------------------------------
#  1. _get_client refuses instead of prompting
# ---------------------------------------------------------------------------


class GetClientNeverPromptsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = testkit.ScratchWorkspace(prefix="no_otp_")
        self.addCleanup(self.work.close)
        self.addCleanup(setattr, tg._client_cache, "client", None)
        tg._client_cache.client = None

        # Any-typed indirection: _get_client is a legacy untyped def, and
        # calling it from annotated test methods would be an untyped call.
        self._tg_get_client: Any = tg._get_client

        saved_lock = tg.hold_session_lock
        tg.hold_session_lock = lambda *a, **k: True
        self.addCleanup(setattr, tg, "hold_session_lock", saved_lock)

    @staticmethod
    def _unauthorized_factory(created: list[FakeClient]) -> Any:
        def factory(session: Any, api_id: Any, api_hash: Any) -> FakeClient:
            client = FakeClient(session, api_id, api_hash)
            client.authorized = False
            created.append(client)
            return client

        return factory

    def test_unauthorized_with_phone_raises_without_starting(self) -> None:
        created: list[FakeClient] = []
        with FakeTelegram(self._unauthorized_factory(created)):
            try:
                self._tg_get_client("12345", "hash", "+10000000000")
            except tg.TelegramCloudError as exc:
                message = str(exc)
            else:
                self.fail("_get_client must raise when the session is invalid")
        client = created[0]
        self.assertEqual(client.start_calls, [], "start() would spend an OTP")
        self.assertEqual(client.disconnect_calls, 1, "client must be closed")
        self.assertIn("login_once.py", message)
        self.assertIn("never asks for codes interactively", message)

    def test_unauthorized_without_phone_keeps_the_old_message(self) -> None:
        created: list[FakeClient] = []
        with FakeTelegram(self._unauthorized_factory(created)):
            try:
                self._tg_get_client("12345", "hash", None)
            except tg.TelegramCloudError as exc:
                message = str(exc)
            else:
                self.fail("_get_client must raise when the session is invalid")
        # Verbatim: app.py's error cards and the user's muscle memory both
        # point at this exact sentence.
        self.assertEqual(
            message,
            "Telegram session missing or expired. Run "
            "`python telegram_uploader.py` in a terminal once and complete "
            "the OTP login.",
        )

    def test_authorized_session_is_returned_and_cached(self) -> None:
        with FakeTelegram() as fake:
            client = self._tg_get_client("12345", "hash", "+10000000000")
        self.assertIs(client, fake.created[0])
        self.assertEqual(client.start_calls, [])
        self.assertIs(tg._client_cache.client, client)

    def test_broken_cache_entry_is_replaced_not_fatal(self) -> None:
        broken = FakeClient()
        broken.authorized = False
        tg._client_cache.client = broken
        with FakeTelegram() as fake:
            client = self._tg_get_client("12345", "hash", "+10000000000")
        self.assertIs(client, fake.created[0])
        self.assertEqual(broken.disconnect_calls, 1)


# ---------------------------------------------------------------------------
#  2. prune_upload_journals
# ---------------------------------------------------------------------------


def _write_journal(directory: str, name: str, state: str = "complete", age_days: int = 0) -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"state": state}, handle)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


class PruneUploadJournalsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = testkit.ScratchWorkspace(prefix="prune_")
        self.addCleanup(self.work.close)
        self.state_dir = str(self.work.child("state"))
        os.makedirs(self.state_dir, exist_ok=True)

        saved = tg.STATE_DIR
        tg.STATE_DIR = self.state_dir
        self.addCleanup(setattr, tg, "STATE_DIR", saved)

    def _names(self) -> list[str]:
        return sorted(name for name in os.listdir(self.state_dir) if name.endswith(".json"))

    def test_keeps_newest_and_prunes_the_rest(self) -> None:
        for index in range(205):
            _write_journal(self.state_dir, f"j{index:04d}.json")
        removed = tg.prune_upload_journals(max_age_days=365, max_files=200)
        self.assertEqual(removed, 5)
        self.assertEqual(len(self._names()), 200)
        # The five oldest are the ones that went.
        self.assertNotIn("j0000.json", self._names())
        self.assertIn("j0204.json", self._names())

    def test_age_rule_prunes_even_inside_the_newest_N(self) -> None:
        fresh = _write_journal(self.state_dir, "fresh.json")
        old = _write_journal(self.state_dir, "old.json", age_days=45)
        removed = tg.prune_upload_journals(max_age_days=30, max_files=200)
        self.assertEqual(removed, 1)
        self.assertTrue(os.path.exists(fresh))
        self.assertFalse(os.path.exists(old))

    def test_in_flight_journal_is_never_deleted(self) -> None:
        uploading = _write_journal(self.state_dir, "uploading.json", state="uploading", age_days=90)
        removed = tg.prune_upload_journals(max_age_days=30, max_files=0)
        self.assertEqual(removed, 0)
        self.assertTrue(os.path.exists(uploading))

    def test_keep_path_is_protected(self) -> None:
        keep = _write_journal(self.state_dir, "keep.json", age_days=90)
        gone = _write_journal(self.state_dir, "gone.json", age_days=90)
        removed = tg.prune_upload_journals(max_age_days=30, max_files=0, keep_path=keep)
        self.assertEqual(removed, 1)
        self.assertTrue(os.path.exists(keep))
        self.assertFalse(os.path.exists(gone))

    def test_underscore_scratch_is_left_alone(self) -> None:
        scratch = _write_journal(self.state_dir, "_manifest_probe.json", age_days=90)
        removed = tg.prune_upload_journals(max_age_days=30, max_files=0)
        self.assertEqual(removed, 0)
        self.assertTrue(os.path.exists(scratch))

    def test_unreadable_journal_is_prunable(self) -> None:
        broken = os.path.join(self.state_dir, "broken.json")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        os.utime(broken, (0, 0))
        removed = tg.prune_upload_journals(max_age_days=30, max_files=0)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(broken))

    def test_second_run_changes_nothing(self) -> None:
        for index in range(205):
            _write_journal(self.state_dir, f"j{index:04d}.json")
        tg.prune_upload_journals(max_age_days=365, max_files=200)
        first = self._names()
        removed = tg.prune_upload_journals(max_age_days=365, max_files=200)
        self.assertEqual(removed, 0)
        self.assertEqual(self._names(), first)

    def test_missing_state_dir_is_a_no_op(self) -> None:
        tg.STATE_DIR = os.path.join(str(self.work.path), "does-not-exist")
        self.assertEqual(tg.prune_upload_journals(), 0)


# ---------------------------------------------------------------------------
#  3. login_once.py / _bootstrap refuse on a non-TTY
# ---------------------------------------------------------------------------


class LoginOnceNonTtyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = testkit.ScratchWorkspace(prefix="login_")
        self.addCleanup(self.work.close)
        self._saved_modules: dict[str, types.ModuleType | None] = {}
        self._input_calls: list[str] = []

        def _record_input(prompt: str = "") -> str:
            self._input_calls.append(prompt)
            raise AssertionError("input() must not be called without a TTY")

        real_input = builtins.input
        # Any-typed indirection: builtins.input's declared signature does not
        # accept the kwarg-less mock exactly as typed.
        mock_input: Any = _record_input
        builtins.input = mock_input
        self.addCleanup(setattr, builtins, "input", real_input)
        self.addCleanup(patch_stdin(False))

    def _install(self, name: str, mod: types.ModuleType) -> None:
        self._saved_modules[name] = sys.modules.get(name)
        sys.modules[name] = mod

    def tearDown(self) -> None:
        for name, original in self._saved_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    def test_refuses_before_touching_telegram(self) -> None:
        import login_once

        def _tripwire(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Telegram must not be contacted on a non-TTY")

        self._install("app", _module("app", load_tg_config=_tripwire))
        self._install("telethon", _module("telethon", TelegramClient=_tripwire))

        code = login_once.main([])
        self.assertEqual(code, 1)
        self.assertEqual(self._input_calls, [])

    def test_code_file_lifts_the_guard_and_eof_is_not_retried(self) -> None:
        import login_once

        code_file = self.work.child("otp.txt")
        code_file.write_text("12345", encoding="utf-8")

        self._install(
            "app",
            _module("app", load_tg_config=lambda: ("12345", "hash", "+1000", "@chan")),
        )

        calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        class _TelegramClient(FakeClient):
            def start(self, *args: Any, **kwargs: Any) -> Any:
                calls.append((args, kwargs))
                # read_code must have been handed to telethon, and with a
                # --code-file it must be able to answer without a console.
                self.sent_code = kwargs["code_callback"]()
                raise EOFError("simulated closed console")

        self._install("telethon", _module("telethon", TelegramClient=_TelegramClient))

        code = login_once.main(["--code-file", str(code_file)])
        self.assertEqual(code, 1, "EOFError must stop the run with an error")
        self.assertEqual(len(calls), 1, "no retries after EOFError")
        self.assertIn("password", calls[0][1])
        self.assertIsNotNone(calls[0][1].get("password"))
        self.assertEqual(self._input_calls, [])

    def test_bootstrap_exits_on_non_tty_before_prompting(self) -> None:
        import telegram_uploader

        try:
            telegram_uploader._bootstrap()
        except SystemExit as exc:
            self.assertEqual(exc.code, 1)
        else:
            self.fail("_bootstrap must exit(1) on a non-TTY")
        self.assertEqual(self._input_calls, [])

    def test_tty_code_callback_raises_on_non_tty(self) -> None:
        import telegram_uploader

        with self.assertRaises(tg.TelegramCloudError):
            telegram_uploader._tty_code_callback()
        self.assertEqual(self._input_calls, [])

    def test_tty_code_callback_reads_on_a_real_tty(self) -> None:
        import telegram_uploader

        real_input = builtins.input
        builtins.input = lambda prompt="": " 12345 "
        self.addCleanup(setattr, builtins, "input", real_input)
        self.addCleanup(patch_stdin(True))
        self.assertEqual(telegram_uploader._tty_code_callback(), "12345")


# ---------------------------------------------------------------------------
#  4. Thumbnail scratch space + prune wiring (integration, all fakes)
# ---------------------------------------------------------------------------


class ThumbnailTempDirTests(unittest.TestCase):
    THUMB_BYTES = b"\xff\xd8\xff\xe0 fake-jpeg-bytes"

    def setUp(self) -> None:
        self.work = testkit.ScratchWorkspace(prefix="thumb_")
        self.addCleanup(self.work.close)

        self.state_dir = str(self.work.child("state"))
        os.makedirs(self.state_dir, exist_ok=True)
        saved_state = tg.STATE_DIR
        tg.STATE_DIR = self.state_dir
        self.addCleanup(setattr, tg, "STATE_DIR", saved_state)

        saved_digests = tg._plan_digests_from_go
        tg._plan_digests_from_go = lambda *a, **k: {}
        self.addCleanup(setattr, tg, "_plan_digests_from_go", saved_digests)

        saved_sleep = time.sleep
        time.sleep = lambda seconds: None
        self.addCleanup(setattr, time, "sleep", saved_sleep)

        self.extracted_to: list[str] = []

        def _fake_extract(path: str, out_path: str) -> str:
            self.extracted_to.append(out_path)
            with open(out_path, "wb") as handle:
                handle.write(self.THUMB_BYTES)
            return out_path

        saved_probe = tg._probe_video
        saved_extract = tg._extract_video_thumb
        tg._probe_video = lambda path: (12, 640, 480)
        tg._extract_video_thumb = _fake_extract
        self.addCleanup(setattr, tg, "_probe_video", saved_probe)
        self.addCleanup(setattr, tg, "_extract_video_thumb", saved_extract)

        self.prune_calls: list[dict[str, Any]] = []
        saved_prune = tg.prune_upload_journals

        def _record_prune(**kwargs: Any) -> int:
            self.prune_calls.append(kwargs)
            return 0

        recorder: Any = _record_prune
        tg.prune_upload_journals = recorder
        self.addCleanup(setattr, tg, "prune_upload_journals", saved_prune)

        self.source = testkit.fake_media(self.work.path, "movie.mp4", size=4096)

    def _run_upload(self, fake: FakeClient) -> Any:
        saved_get_client = tg._get_client
        tg._get_client = lambda *a, **k: fake
        self.addCleanup(setattr, tg, "_get_client", saved_get_client)
        with FakeTelegram():
            uploader: Any = tg.upload_file_detailed
            return uploader(
                str(self.source),
                "12345",
                "hash",
                "+10000000000",
                "@testchan",
                use_go=False,
                go_concurrency=1,
            )

    def test_thumbnail_lands_in_temp_with_the_old_basename_and_is_deleted(self) -> None:
        fake = FakeClient()
        journal = self._run_upload(fake)

        self.assertEqual(journal["state"], "complete")
        self.assertEqual(len(self.extracted_to), 1)
        out_path = self.extracted_to[0]

        # Same basename as the pre-fix scheme, but outside the project tree.
        self.assertEqual(os.path.basename(out_path), "movie.t_dub_thumb.jpg")
        temp_root = os.path.realpath(tempfile.gettempdir())
        self.assertTrue(
            os.path.realpath(out_path).startswith(temp_root + os.sep),
            f"{out_path} must live under {temp_root}",
        )
        self.assertFalse(
            os.path.realpath(out_path).startswith(os.path.realpath(str(ROOT)) + os.sep),
            "thumbnails must never be written into the project directory",
        )

        # The exact bytes Telegram received are the ones the extractor wrote.
        self.assertEqual(fake.sent[0]["thumb"], out_path)
        self.assertEqual(fake.sent[0]["thumb_bytes"], self.THUMB_BYTES)

        # And the scratch directory is gone once the upload returned.
        self.assertFalse(os.path.exists(os.path.dirname(out_path)))

    def test_thumbnail_scratch_is_deleted_even_when_upload_fails(self) -> None:
        fake = FakeClient()
        fake.send_error = RuntimeError("network died")

        saved_retries = tg.PART_UPLOAD_RETRIES
        tg.PART_UPLOAD_RETRIES = 1
        self.addCleanup(setattr, tg, "PART_UPLOAD_RETRIES", saved_retries)

        with self.assertRaises(RuntimeError):
            self._run_upload(fake)

        self.assertEqual(len(self.extracted_to), 1)
        out_path = self.extracted_to[0]
        self.assertFalse(
            os.path.exists(os.path.dirname(out_path)),
            "the finally block must clean up after a failure too",
        )

        journals = [
            name
            for name in os.listdir(self.state_dir)
            if name.endswith(".json") and not name.startswith("_")
        ]
        self.assertEqual(len(journals), 1)
        with open(os.path.join(self.state_dir, journals[0]), encoding="utf-8") as h:
            self.assertEqual(json.load(h)["state"], "failed")

    def test_prune_is_called_with_the_live_journal_protected(self) -> None:
        fake = FakeClient()
        self._run_upload(fake)
        self.assertEqual(len(self.prune_calls), 1)
        keep = self.prune_calls[0].get("keep_path")
        self.assertIsNotNone(keep, "the just-written journal must be protected")
        assert keep is not None
        self.assertTrue(os.path.exists(keep))


# ---------------------------------------------------------------------------
#  5. Static guarantees -- the shape of the source itself
# ---------------------------------------------------------------------------


def _calls(node: ast.AST) -> list[ast.Call]:
    found: list[ast.Call] = []
    for item in ast.walk(node):
        if isinstance(item, ast.Call):
            found.append(item)
    return found


def _name_of(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


class SourceShapeTests(unittest.TestCase):
    """Guards that survive refactors: they read the SOURCE, not behaviour."""

    def _parse(self, filename: str) -> ast.Module:
        path = ROOT / filename
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_every_phone_start_carries_a_refusing_code_callback(self) -> None:
        for filename in ("telegram_uploader.py", "login_once.py"):
            tree = self._parse(filename)
            phone_starts = [
                call
                for call in _calls(tree)
                if _name_of(call) == "start" and _keyword(call, "phone") is not None
            ]
            self.assertTrue(phone_starts, f"expected a start(phone=...) in {filename}")
            for call in phone_starts:
                self.assertIsNotNone(
                    _keyword(call, "code_callback"),
                    f"{filename}:{call.lineno} start(phone=...) without "
                    "code_callback -- that is the interactive-prompt hazard",
                )

    def test_get_client_contains_no_start_call_at_all(self) -> None:
        tree = self._parse("telegram_uploader.py")
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_get_client":
                starts = [call for call in _calls(node) if _name_of(call) == "start"]
                self.assertEqual(starts, [], "_get_client must never call start()")
                return
        self.fail("_get_client not found")

    def test_ffprobe_and_ffmpeg_get_devnull_stdin(self) -> None:
        tree = self._parse("telegram_uploader.py")
        sealed = 0
        for call in _calls(tree):
            if _name_of(call) != "run":
                continue
            binaries = [
                item.value
                for arg in call.args
                if isinstance(arg, ast.List)
                for item in arg.elts
                if isinstance(item, ast.Constant)
                and isinstance(item.value, str)
                and item.value in ("ffprobe", "ffmpeg")
            ]
            if not binaries:
                continue
            stdin = _keyword(call, "stdin")
            assert stdin is not None, (
                f"{binaries[0]} at line {call.lineno} inherits stdin -- "
                "a worker thread can block on it"
            )
            assert isinstance(stdin, ast.Attribute)
            self.assertEqual(stdin.attr, "DEVNULL")
            sealed += 1
        self.assertEqual(sealed, 2, "expected one ffprobe and one ffmpeg call")

    def test_binary_version_seals_stdin(self) -> None:
        tree = self._parse("tgup_bridge.py")
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "binary_version":
                runs = [call for call in _calls(node) if _name_of(call) == "run"]
                self.assertTrue(runs)
                for call in runs:
                    stdin = _keyword(call, "stdin")
                    assert stdin is not None, "binary_version must seal stdin"
                    assert isinstance(stdin, ast.Attribute)
                    self.assertEqual(stdin.attr, "DEVNULL")
                return
        self.fail("binary_version not found")

    def test_qr_login_has_no_interactive_reads(self) -> None:
        tree = self._parse("tg_qr_login.py")
        offenders = [call for call in _calls(tree) if _name_of(call) in ("input", "getpass")]
        self.assertEqual(offenders, [], "tg_qr_login.py must not read from the console")


if __name__ == "__main__":
    unittest.main()
