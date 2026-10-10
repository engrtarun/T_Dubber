#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for the runtimes bridge -- no real binaries, no network.

    python cpp_accelerator/runtimes/test_runtimes.py
    python -m unittest discover -s cpp_accelerator/runtimes -v
    pytest cpp_accelerator/runtimes/test_runtimes.py -v

WHY FAKE EXECUTABLES INSTEAD OF THE REAL ONES
----------------------------------------------
llama-server needs a GGUF and 4 GB of RAM; whisper-cli needs a 150 MB model.
Both would make the suite a 10-minute, 4 GB test that CI skips. What actually
broke in production was never the model maths, it was the plumbing:

  * the wrong flag (``-m org/name`` instead of ``-hf``) which makes the server
    exit with a file-not-found message that reads like a missing weights file;
  * readiness polling that never saw a 200 and reported "did not start" with
    no reason attached;
  * a stop() that left worker children holding the port;
  * whisper.cpp writing no output and the pipeline dubbing silence for two
    hours;
  * a resolver that picked the wrong directory and silently used an old build.

None of those need a model. Each fake below reproduces one of them.

THE FAKES ARE REAL PROCESSES
---------------------------
``llama-server`` is replaced by a ``http.server`` script and ``whisper-cli`` by
a script that writes real ``.json``/``.srt`` files, both launched through
``subprocess``. Mocking ``subprocess.run`` would have let the fake's own
argument parsing -- the thing under test -- be the thing that is fake.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_runtimes  # noqa: E402
import llamacpp_bridge  # noqa: E402
import whispercpp_bridge  # noqa: E402
from llamacpp_bridge import (  # noqa: E402
    LlamaServer,
    LlamaServerError,
    LlamaServerTimeout,
    build_argv,
    build_openai_client,
    free_port,
)
from whispercpp_bridge import (  # noqa: E402
    WhisperCppError,
    WhisperCppRunner,
    convert_to_wav,
    parse_srt,
    parse_whisper_json,
)

IS_WINDOWS = os.name == "nt"
PYTHON = sys.executable


# --------------------------------------------------------------------------- #
# Fake executables
# --------------------------------------------------------------------------- #

FAKE_LLAMA_SERVER = r'''
"""Stand-in for llama-server: serves /v1/models and /v1/chat/completions.

Env knobs let one script play every role a startup can take:
  FAKE_FAIL=1      -> print garbage to stderr and exit non-zero (the vLLM-style
                      crash this migration exists to avoid)
  FAKE_HANG=1      -> sleep forever, serving nothing (a timeout, not a crash)
  FAKE_SLOW=<sec>  -> answer /v1/models only after a delay (readiness polling)
  FAKE_ARGV=<path> -> dump the argv it was given, so tests can assert the flags
"""
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer

argv = sys.argv[1:]
if os.environ.get("FAKE_ARGV"):
    with open(os.environ["FAKE_ARGV"], "w", encoding="utf-8") as fh:
        json.dump(argv, fh)

def flag(name, default=None):
    for i, item in enumerate(argv):
        if item == name and i + 1 < len(argv):
            return argv[i + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return default

if os.environ.get("FAKE_FAIL"):
    sys.stderr.write(
        "ggml_init: failed to initialize backend\n"
        "error loading model: unable to open model file\n"
        "libcudart.so.13: cannot open shared object file\n")
    sys.exit(3)

if os.environ.get("FAKE_HANG"):
    time.sleep(float(os.environ.get("FAKE_HANG_SECS", "120")))
    sys.exit(0)

HOST = flag("--host", "127.0.0.1")
PORT = int(flag("--port", "8080"))
MODEL = flag("--alias") or flag("-m") or flag("-hf") or "fake-model"
DELAY = float(os.environ.get("FAKE_SLOW", "0"))

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            if DELAY:
                time.sleep(DELAY)
            self._send(200, {"object": "list",
                             "data": [{"id": MODEL, "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/").endswith("/chat/completions"):
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length) or b"{}")
            self._send(200, {
                "id": "chatcmpl-fake", "object": "chat.completion",
                "model": req.get("model", MODEL),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": "OK"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 1,
                          "total_tokens": 8},
            })
        else:
            self._send(404, {"error": "not found"})

srv = HTTPServer((HOST, PORT), Handler)
print("llama-server listening on %s:%d, model %s" % (HOST, PORT, MODEL), flush=True)
try:
    srv.serve_forever()
except KeyboardInterrupt:
    pass
'''

FAKE_WHISPER_CLI = r'''
"""Stand-in for whisper-cli: writes a real .json and .srt next to -of.

Env knobs:
  FAKE_MODE=json   -> JSON only (a build without SRT output)
  FAKE_MODE=srt    -> SRT only (a build without -oj)
  FAKE_MODE=none   -> exit 0 and write nothing (the silent-empty failure mode)
  FAKE_MODE=garbage-> .json that is not JSON, plus noisy stderr
  FAKE_MODE=garbage_srt -> malformed .json but a valid .srt
  FAKE_MODE=empty  -> well-formed JSON whose only cue is blank text
  FAKE_FAIL=1      -> garbage on stderr, non-zero exit
  FAKE_ARGV=<path> -> dump argv
"""
import json, os, sys

argv = sys.argv[1:]
if os.environ.get("FAKE_ARGV"):
    with open(os.environ["FAKE_ARGV"], "w", encoding="utf-8") as fh:
        json.dump(argv, fh)

def flag(name):
    for i, item in enumerate(argv):
        if item == name and i + 1 < len(argv):
            return argv[i + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None

mode = os.environ.get("FAKE_MODE", "both")

if os.environ.get("FAKE_FAIL"):
    sys.stderr.write("whisper_init_state: failed to initialize whisper context\n"
                     "error loading model 'ggml-base.bin': file not found\n")
    sys.exit(2)

prefix = flag("-of")
if not prefix:
    sys.stderr.write("no -of given\n")
    sys.exit(64)

if mode == "garbage":
    with open(prefix + ".json", "w", encoding="utf-8") as fh:
        fh.write("<<<not json at all>>>")
    sys.stderr.write("warning: something odd happened\n")
elif mode == "empty":
    with open(prefix + ".json", "w", encoding="utf-8") as fh:
        json.dump({"transcription": [
            {"offsets": {"from": 0, "to": 10}, "text": "   "}]}, fh)
elif mode == "none":
    pass
else:
    if mode == "garbage_srt":
        with open(prefix + ".json", "w", encoding="utf-8") as fh:
            fh.write("<<<not json at all>>>")
        with open(prefix + ".srt", "w", encoding="utf-8", newline="\n") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\nrecovered\n")
        sys.exit(0)
    if mode in ("both", "json"):
        with open(prefix + ".json", "w", encoding="utf-8") as fh:
            json.dump({
                "transcription": [
                    {"timestamps": {"from": "00:00:00,000", "to": "00:00:02,500"},
                     "offsets": {"from": 0, "to": 2500},
                     "text": " Namaste, yeh ek test hai."},
                    {"timestamps": {"from": "00:00:02,500", "to": "00:00:05,000"},
                     "offsets": {"from": 2500, "to": 5000},
                     "text": " Doosra segment."},
                ],
            }, fh)
    if mode in ("both", "srt"):
        with open(prefix + ".srt", "w", encoding="utf-8", newline="\n") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:02,500\nNamaste, yeh ek test hai.\n\n"
                     "2\n00:00:02,500 --> 00:00:05,000\nDoosra segment.\n")
sys.exit(0)
'''

FAKE_FFMPEG = r'''
"""Stand-in for ffmpeg: writes a 16 kHz mono PCM16 WAV with a real header."""
import struct, sys

args = sys.argv[1:]
out = args[-1]
src = args[args.index("-i") + 1] if "-i" in args else None
if src and (src.endswith("bad.mp4") or src.endswith("missing.mp4")):
    sys.stderr.write("input.mp4: No such file or directory\\n")
    sys.exit(1)
if src and src.endswith(".empty"):
    with open(out, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt ")
    sys.exit(0)
rate = int(args[args.index("-ar") + 1]) if "-ar" in args else 16000
samples = 16000  # one second
pcm = b"".join(struct.pack("<h", 0) for _ in range(samples))
with open(out, "wb") as fh:
    fh.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt ")
    fh.write(struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
    fh.write(b"data" + struct.pack("<I", len(pcm)) + pcm)
sys.exit(0)
'''


def write_script(path: Path, source: str) -> Path:
    """Write a Python fake and return an argv prefix that runs it.

    A ``.py`` file is not directly executable on Windows and a shell script is
    not either, so the binary argument becomes ``[python, script]``. The bridge
    accepts an argv prefix precisely so that this -- and a wrapper script in
    production -- works without special-casing.
    """
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    return [PYTHON, str(path)]


def exe_name(base: str) -> str:
    """Platform spelling of a binary. The resolver probes ``.exe`` first on
    Windows but accepts both, so tests that place a file must place the name
    they then assert on."""
    return base + ".exe" if IS_WINDOWS else base


def make_exe(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    """Create a file that ``resolve()`` accepts as an executable.

    On Windows ``os.access(X_OK)`` is True for any existing file, so a plain
    file is enough; on POSIX the x bit has to be set or the resolver rejects it
    -- which is itself worth proving.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if not IS_WINDOWS:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class TempDirCase(unittest.TestCase):
    """Base class giving each test an isolated temp dir and a clean env.

    It also repoints ``build_runtimes.BIN_DIR`` at an empty directory. Without
    that, a developer who has actually installed llama-server finds every
    resolve/ensure test short-circuiting against the real binary in
    ``runtimes/bin/`` -- so the suite passes on a bare machine and fails on the
    machine that uses it, which is the worst possible split.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="td-runtimes-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._env = dict(os.environ)
        self.addCleanup(self._restore_env)

        original_bin_dir = build_runtimes.BIN_DIR
        isolated = self.tmp / "__empty_bin__"
        isolated.mkdir(parents=True, exist_ok=True)
        build_runtimes.BIN_DIR = isolated
        self.addCleanup(setattr, build_runtimes, "BIN_DIR", original_bin_dir)

    def _restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def set_env(self, **values) -> None:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# --------------------------------------------------------------------------- #
# 1-8: build_runtimes -- resolution order, offline no-op, digest verification
# --------------------------------------------------------------------------- #

class ResolveOrderTests(TempDirCase):

    def test_env_override_wins_over_everything(self):
        """TDUBBER_BIN_DIR is checked first so a mounted Kaggle dataset wins
        without copying 200 MB into /kaggle/working first."""
        override = self.tmp / "override"
        fallback = self.tmp / "fallback"
        make_exe(override / exe_name("llama-server"))
        make_exe(fallback / exe_name("llama-server"))
        self.set_env(TDUBBER_BIN_DIR=str(override))
        self.assertEqual(build_runtimes.resolve("llama-server"),
                         override / exe_name("llama-server"))

    def test_falls_back_through_dir_order(self):
        """Without the override, runtimes/bin is probed before a build dir.

        runtimes/bin comes first on purpose: it is the digest-verified copy, so
        preferring it means a stale local build cannot silently win."""
        second = self.tmp / "build"
        make_exe(second / exe_name("whisper-cli"))
        with tempfile.TemporaryDirectory() as scratch:
            patch_bin = Path(scratch) / "bin"
            make_exe(patch_bin / exe_name("whisper-cli"))
            self.set_env(TDUBBER_BIN_DIR=None)
            original = build_runtimes.BIN_DIR
            build_runtimes.BIN_DIR = patch_bin
            self.addCleanup(setattr, build_runtimes, "BIN_DIR", original)
            found = build_runtimes.resolve("whisper-cpp")   # legacy alias
            self.assertEqual(found, patch_bin / exe_name("whisper-cli"))

    def test_search_dirs_reports_priority(self):
        self.set_env(TDUBBER_BIN_DIR="X:/a" + os.pathsep + "Y:/b")
        dirs = build_runtimes.search_dirs()
        self.assertEqual(dirs[0], Path("X:/a"))
        self.assertEqual(dirs[1], Path("Y:/b"))
        self.assertIn(build_runtimes.BIN_DIR, dirs)

    def test_legacy_aliases_map_to_canonical(self):
        for alias in ("whisper-cpp", "main", "whisper", "WHISPER-CPP.EXE"):
            self.assertEqual(build_runtimes.canonical_name(alias), "whisper-cli")
        self.assertEqual(build_runtimes.canonical_name("llama-server.exe"),
                         "llama-server")

    def test_missing_binary_resolves_to_none(self):
        self.set_env(TDUBBER_BIN_DIR=None, PATH="")
        with tempfile.TemporaryDirectory() as scratch:
            original = build_runtimes.BIN_DIR
            build_runtimes.BIN_DIR = Path(scratch)
            self.addCleanup(setattr, build_runtimes, "BIN_DIR", original)
            self.assertIsNone(build_runtimes.resolve("llama-server"))

    def test_non_executable_file_is_not_accepted(self):
        """A non-executable hit would become a PermissionError inside the
        bridge, minutes into a run, instead of a clear MISSING line here."""
        if IS_WINDOWS:
            self.skipTest("Windows reports X_OK for every existing file")
        self.set_env(TDUBBER_BIN_DIR=None, PATH="")
        bin_dir = self.tmp / "bin"
        target = bin_dir / "llama-server"
        target.parent.mkdir(parents=True)
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o644)
        self.assertIsNone(build_runtimes.resolve("llama-server"))


class DigestAndOfflineTests(TempDirCase):

    def _place(self, name: str, body: str = "binary-v1\n") -> Path:
        return make_exe(self.tmp / "bin" / exe_name(name), body)

    def test_offline_hit_is_a_noop(self):
        """The warm run: the file and its digest already agree, so nothing is
        downloaded, nothing is rewritten, and exactly one RUNTIME_OK is printed."""
        path = self._place("llama-server")
        manifest = self.tmp / "bin" / "MANIFEST.sha256"
        build_runtimes.manifest_write({path.name: build_runtimes.sha256_file(path)}, manifest)
        before = manifest.read_bytes()
        stdout = _capture(lambda: build_runtimes.ensure(
            "llama-server", offline=True, out_dir=self.tmp / "bin"))
        self.assertEqual(stdout.count("RUNTIME_OK"), 1)
        self.assertIn(str(path), stdout)
        self.assertEqual(manifest.read_bytes(), before)

    def test_unpinned_hit_is_accepted_with_a_note(self):
        """A hand-built binary is usable; refusing to run it would make the
        resolver useless on Windows where no upstream zip exists."""
        path = self._place("llama-server")
        stdout = _capture(lambda: build_runtimes.ensure(
            "llama-server", offline=True, out_dir=self.tmp / "bin"))
        self.assertIn("RUNTIME_OK", stdout)
        self.assertIn("unpinned", stdout)

    def test_digest_mismatch_is_reported_not_silently_run(self):
        path = self._place("llama-server")
        manifest = self.tmp / "bin" / "MANIFEST.sha256"
        build_runtimes.manifest_write({path.name: "0" * 64}, manifest)
        stdout = _capture(lambda: build_runtimes.ensure(
            "llama-server", offline=True, out_dir=self.tmp / "bin"))
        self.assertIn("RUNTIME_ERROR", stdout)
        self.assertIn("mismatch", stdout)

    def test_missing_binary_offline_prints_missing(self):
        self.set_env(TDUBBER_BIN_DIR=None, PATH="")
        with tempfile.TemporaryDirectory() as scratch:
            original = build_runtimes.BIN_DIR
            build_runtimes.BIN_DIR = Path(scratch)
            self.addCleanup(setattr, build_runtimes, "BIN_DIR", original)
            stdout = _capture(lambda: build_runtimes.ensure(
                "llama-server", offline=True, out_dir=self.tmp / "empty"))
            self.assertIn("RUNTIME_MISSING", stdout)

    def test_manifest_roundtrip_and_garbage_tolerance(self):
        path = self._place("llama-server")
        manifest = self.tmp / "bin" / "MANIFEST.sha256"
        digest = build_runtimes.sha256_file(path)
        build_runtimes.manifest_write({path.name: digest}, manifest)
        self.assertEqual(build_runtimes.manifest_read(manifest), {path.name: digest})
        with open(manifest, "a", encoding="utf-8") as fh:
            fh.write("# a comment\nnot-a-digest line\n")
        self.assertEqual(build_runtimes.manifest_read(manifest), {path.name: digest})

    def test_downloaded_file_is_digest_verified_and_recorded(self):
        payload = b"#!/bin/sh\necho fake\n"
        import hashlib
        digest = hashlib.sha256(payload).hexdigest()
        dest_dir = self.tmp / "fetched"
        path = build_runtimes.ensure(
            "llama-server", offline=False, out_dir=dest_dir,
            urls={"llama-server": "https://example.invalid/llama-server.bin"},
            digests={"llama-server": digest},
            opener=lambda _url: payload,
        )
        self.assertIsNotNone(path)
        self.assertEqual(build_runtimes.sha256_file(path), digest)
        recorded = build_runtimes.manifest_read(dest_dir / "MANIFEST.sha256")
        self.assertEqual(recorded[path.name], digest)

    def test_download_with_wrong_digest_is_refused(self):
        """An unverified file must never become a runnable binary -- an
        interrupted download used to leave a half file that the next offline
        run happily reported as RUNTIME_OK."""
        stdout = _capture(lambda: build_runtimes.ensure(
            "llama-server", offline=False, out_dir=self.tmp / "bad",
            urls={"llama-server": "https://example.invalid/llama-server.bin"},
            digests={"llama-server": "f" * 64},
            opener=lambda _url: b"#!/bin/sh\necho evil\n",
        ))
        self.assertIn("RUNTIME_ERROR", stdout)
        self.assertIn("sha256 mismatch", stdout)
        self.assertFalse((self.tmp / "bad" / "llama-server").exists())

    def test_zip_payload_is_digest_checked_before_extraction(self):
        """The pinned digest describes the zip, so it has to be verified
        BEFORE the archive is opened -- verifying only the extracted member
        would let a tampered archive through."""
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("whisper-cli", "MZ evil")
        stdout = _capture(lambda: build_runtimes.ensure(
            "whisper-cli", offline=False, out_dir=self.tmp / "z",
            urls={"whisper-cli": "https://example.invalid/w.zip"},
            digests={"whisper-cli": "a" * 64},
            opener=lambda _url: buf.getvalue(),
        ))
        self.assertIn("sha256 mismatch", stdout)
        self.assertNotIn("RUNTIME_OK", stdout)
        self.assertFalse((self.tmp / "z" / "whisper-cli").exists())

    def test_mismatched_hit_refetches_instead_of_printing_ok_twice(self):
        """The contract is exactly one status line per binary. A cell that
        stops at the first RUNTIME_OK must never end up running the bad file."""
        stale = self._place("llama-server", body="stale\n")
        manifest = self.tmp / "bin" / "MANIFEST.sha256"
        build_runtimes.manifest_write({stale.name: "1" * 64}, manifest)
        stdout = _capture(lambda: build_runtimes.ensure(
            "llama-server", offline=False, out_dir=self.tmp / "bin",
            urls={"llama-server": "https://example.invalid/llama-server.bin"},
            opener=lambda _url: b"#!/bin/sh\necho fresh\n",
        ))
        self.assertEqual(stdout.count("RUNTIME_OK"), 1)
        self.assertNotIn("RUNTIME_ERROR", stdout)
        fresh = stale  # the refetch must land ON the stale file, not beside it
        self.assertEqual(fresh.read_text(encoding="utf-8"), "#!/bin/sh\necho fresh\n")
        # the new digest has to replace the stale one, or the next run refetches
        recorded = build_runtimes.manifest_read(manifest)
        self.assertEqual(recorded[fresh.name], build_runtimes.sha256_file(fresh))

    def test_downloaded_binary_is_executable_afterwards(self):
        """A raw download lands non-executable; resolve() refuses such a file,
        so without the chmod the next warm run reports the binary we just
        fetched as MISSING."""
        import hashlib
        payload = b"#!/bin/sh\necho fake\n"
        build_runtimes.ensure(
            "llama-server", offline=False, out_dir=self.tmp / "raw",
            urls={"llama-server": "https://example.invalid/llama-server.bin"},
            digests={"llama-server": hashlib.sha256(payload).hexdigest()},
            opener=lambda _url: payload,
        )
        # Named by the spec, NOT by the URL asset name -- a file called
        # `llama-server.bin` is one no resolver will ever find.
        fetched = self.tmp / "raw" / exe_name("llama-server")
        self.assertTrue(fetched.is_file())
        if IS_WINDOWS:
            self.skipTest("Windows reports X_OK for every existing file")
        self.assertTrue(os.access(fetched, os.X_OK))

    def test_dll_companions_are_extracted_alongside_the_exe(self):
        """The Windows llama.cpp build is a DLL farm. Extracting only
        llama-server.exe yields a resolved, digest-verified binary that dies
        with STATUS_DLL_NOT_FOUND (exit 3221225781) and logs NOTHING, because
        it dies before main(). Verified against release b11539."""
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("build/bin/Release/llama-server.exe", "MZ")
            archive.writestr("build/bin/Release/llama-server-impl.dll", "dll")
            archive.writestr("build/bin/Release/ggml.dll", "dll")
            archive.writestr("build/bin/Release/libomp.dll", "dll")
            archive.writestr("build/bin/Release/LICENSE", "text")
        stdout = _capture(lambda: build_runtimes.ensure(
            "llama-server", offline=False, out_dir=self.tmp / "dlls",
            urls={"llama-server":
                  "https://example.invalid/llama-b11539-bin-win-cpu-x64.zip"},
            opener=lambda _url: buf.getvalue(),
        ))
        self.assertIn("RUNTIME_OK", stdout)
        for dll in ("llama-server-impl.dll", "ggml.dll", "libomp.dll"):
            self.assertTrue((self.tmp / "dlls" / dll).is_file(), dll)
        self.assertFalse((self.tmp / "dlls" / "LICENSE").exists())

    def test_tar_companions_are_extracted_too(self):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as archive:
            for name, body in (("build/bin/llama-server", b"ELF"),
                               ("build/bin/libllama.so", b"SO"),
                               ("build/bin/README.md", b"docs")):
                info = tarfile.TarInfo(name)
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
        _capture(lambda: build_runtimes.ensure(
            "llama-server", offline=False, out_dir=self.tmp / "tardlls",
            urls={"llama-server":
                  "https://example.invalid/llama-b11539-bin-ubuntu-x64.tar.gz"},
            opener=lambda _url: buf.getvalue(),
        ))
        self.assertTrue((self.tmp / "tardlls" / "llama-server").is_file())
        self.assertTrue((self.tmp / "tardlls" / "libllama.so").is_file())

    def test_path_lookup_never_resolves_an_alias(self):
        """Windows resolves `main` through PATHEXT to
        C:\\Windows\\System32\\main.CPL -- a Control Panel applet, not a
        speech recogniser. Observed live: whisper-cli resolved to main.CPL and
        the download tests short-circuited against a system file."""
        if not IS_WINDOWS:
            self.skipTest("PATHEXT aliasing is a Windows behaviour")
        with tempfile.TemporaryDirectory() as scratch:
            original = build_runtimes.BIN_DIR
            build_runtimes.BIN_DIR = Path(scratch)
            self.addCleanup(setattr, build_runtimes, "BIN_DIR", original)
            self.set_env(TDUBBER_BIN_DIR=None)
            found = build_runtimes.resolve("whisper-cli")
            if found is not None:
                self.assertNotIn("main.CPL", str(found).upper())
                self.assertIn("whisper", str(found).lower())

    def test_whisper_aliases_are_all_probed_in_a_directory(self):
        """whisper.cpp renamed its CLI to whisper-whisper-cli; a build
        directory may hold any of the three spellings, so canonical-only
        probing finds none of them."""
        with tempfile.TemporaryDirectory() as scratch:
            original = build_runtimes.BIN_DIR
            build_runtimes.BIN_DIR = Path(scratch)
            self.addCleanup(setattr, build_runtimes, "BIN_DIR", original)
            self.set_env(TDUBBER_BIN_DIR=None, PATH="")
            for spelling in ("whisper-cli", "whisper-whisper-cli", "main"):
                target = Path(scratch) / exe_name(spelling)
                make_exe(target)
                self.assertEqual(build_runtimes.resolve("whisper-cli"), target,
                                 f"alias {spelling} not resolved")
                target.unlink()

    def test_canonical_name_beats_an_alias_that_sorts_first(self):
        """whisper.cpp ships main.exe (a deprecation shim that exits 1) next to
        whisper-cli.exe, and main sorts first in the archive. A first-match
        extractor installs the shim under the correct filename, so the failure
        looks like a code bug instead of a packaging one. Found live: the
        extracted whisper-cli.exe had main.exe's bytes."""
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("Release/main.exe", "DEPRECATION-SHIM")
            archive.writestr("Release/whisper-cli.exe", "REAL-CLI")
            archive.writestr("Release/whisper.dll", "dll")
        _capture(lambda: build_runtimes.ensure(
            "whisper-cli", offline=False, out_dir=self.tmp / "canon",
            urls={"whisper-cli": "https://example.invalid/whisper-bin-x64.zip"},
            opener=lambda _url: buf.getvalue(),
        ))
        installed = self.tmp / "canon" / "whisper-cli.exe"
        self.assertTrue(installed.is_file())
        self.assertEqual(installed.read_text(encoding="utf-8"), "REAL-CLI")

    def test_alias_is_used_when_the_canonical_name_is_absent(self):
        """An older archive may ship only main.exe; the alias is what makes it
        usable at all."""
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("Release/main.exe", "REAL-CLI")
        _capture(lambda: build_runtimes.ensure(
            "whisper-cli", offline=False, out_dir=self.tmp / "alias",
            urls={"whisper-cli": "https://example.invalid/w.zip"},
            opener=lambda _url: buf.getvalue(),
        ))
        self.assertEqual(
            (self.tmp / "alias" / "whisper-cli.exe").read_text(encoding="utf-8"),
            "REAL-CLI")

    def test_each_project_installs_into_its_own_directory(self):
        """llama.cpp and whisper.cpp ship colliding DLL names (ggml.dll,
        llama.dll, ggml-base.dll, ggml-cpu-*.dll) at different ABI versions.
        One shared directory means the second install overwrites the first and
        the other binary dies at load time with WinError 216 -- before main(),
        so with no log output at all. Verified live on Windows."""
        with tempfile.TemporaryDirectory() as scratch:
            original = build_runtimes.BIN_DIR
            build_runtimes.BIN_DIR = Path(scratch)
            self.addCleanup(setattr, build_runtimes, "BIN_DIR", original)
            self.set_env(TDUBBER_BIN_DIR=None, PATH="")

            payload = b"#!/bin/sh\necho fake\n"
            for name in ("llama-server", "whisper-cli"):
                build_runtimes.ensure(
                    name, offline=False,
                    urls={name: f"https://example.invalid/{name}.bin"},
                    opener=lambda _url: payload,
                )
            for project in ("llama", "whisper"):
                self.assertTrue((Path(scratch) / project).is_dir(), project)
                self.assertTrue(
                    (Path(scratch) / project / "MANIFEST.sha256").is_file(), project)
            # and the shared root stays empty of DLLs
            self.assertFalse((Path(scratch) / exe_name("llama-server")).exists())
            self.assertFalse((Path(scratch) / exe_name("whisper-cli")).exists())

    def test_zip_member_is_extracted_by_basename(self):
        """Upstream moves files between build/bin and build/bin/Release
        between releases; a hard-coded path inside the zip breaks on the next
        tag."""
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("build/bin/Release/whisper-cli.exe", "MZ fake")
        stdout = _capture(lambda: build_runtimes.ensure(
            "whisper-cli", offline=False, out_dir=self.tmp / "zipped",
            urls={"whisper-cli": "https://example.invalid/w.zip"},
            opener=lambda _url: buf.getvalue(),
        ))
        self.assertIn("RUNTIME_OK", stdout)
        self.assertTrue((self.tmp / "zipped" / "whisper-cli.exe").exists())

    def test_targz_member_is_extracted(self):
        """llama.cpp ships .tar.gz for Linux, not .zip. A zip-only extractor
        makes the Linux fetch fail with a confusing ReadError on a correct
        download -- verified against ggml-org/llama.cpp release b11539."""
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as archive:
            payload = b"#!/bin/sh\necho llama\n"
            info = tarfile.TarInfo("build/bin/llama-server")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        stdout = _capture(lambda: build_runtimes.ensure(
            "llama-server", offline=False, out_dir=self.tmp / "tarred",
            urls={"llama-server":
                  "https://example.invalid/llama-b11539-bin-ubuntu-x64.tar.gz"},
            opener=lambda _url: buf.getvalue(),
        ))
        self.assertIn("RUNTIME_OK", stdout)
        self.assertTrue((self.tmp / "tarred" / "llama-server").is_file())

    def test_archive_suffixes_cover_upstream(self):
        """Both projects disagree about the extension; accepting only one is a
        bug waiting for the other release."""
        self.assertIn(".zip", build_runtimes.ARCHIVE_SUFFIXES)
        self.assertIn(".tar.gz", build_runtimes.ARCHIVE_SUFFIXES)

    def test_llama_asset_patterns_prefer_the_cpu_build(self):
        """A CUDA 13 build would reproduce the exact `libcudart.so.13`
        failure that cost 1067 s, so no CPU pattern may name a CUDA build."""
        spec = next(s for s in build_runtimes.BINARIES if s.name == "llama-server")
        for pattern in spec.asset_patterns:
            self.assertNotIn("cuda", pattern)
            self.assertNotIn("rocm", pattern)

    def test_asset_patterns_are_ordered_for_the_host_platform(self):
        """Both projects publish every platform on every release, so matching
        a Windows zip on Linux yields a .exe the resolver finds but the kernel
        cannot run."""
        spec = next(s for s in build_runtimes.BINARIES if s.name == "llama-server")
        linux = build_runtimes.preferred_patterns(spec, "linux")
        windows = build_runtimes.preferred_patterns(spec, "win32")
        mac = build_runtimes.preferred_patterns(spec, "darwin")
        self.assertIn("ubuntu", linux[0])
        self.assertIn("win", windows[0])
        self.assertIn("macos", mac[0])
        self.assertEqual(sorted(linux), sorted(spec.asset_patterns))


# --------------------------------------------------------------------------- #
# 9-16: llamacpp_bridge -- argv, readiness, failure reporting, lifecycle
# --------------------------------------------------------------------------- #

class ArgvTests(TempDirCase):

    def test_local_path_uses_dash_m(self):
        model = self.tmp / "homura-2b-q4.gguf"
        model.write_bytes(b"GGUF")
        argv = build_argv("llama-server", str(model), port=9000)
        self.assertIn("-m", argv)
        self.assertEqual(argv[argv.index("-m") + 1], str(model))
        self.assertNotIn("-hf", argv)

    def test_hf_repo_uses_dash_hf(self):
        """-m with a repo id fails with a file-not-found message that reads
        like missing weights; -hf with a path fails too. This is the flag
        choice that made the swap look harder than it was."""
        argv = build_argv("llama-server", "IndexTeam/Index-Homura-2B-GGUF", port=9000)
        self.assertIn("-hf", argv)
        self.assertEqual(argv[argv.index("-hf") + 1],
                         "IndexTeam/Index-Homura-2B-GGUF")
        self.assertNotIn("-m", argv)

    def test_repo_with_quant_suffix_is_passed_through(self):
        argv = build_argv("llama-server", "org/repo:Q4_K_M", port=9000)
        self.assertEqual(argv[argv.index("-hf") + 1], "org/repo:Q4_K_M")

    def test_missing_bare_gguf_is_treated_as_a_path(self):
        """A typo in a filename must stay a typo, not become a Hub lookup."""
        argv = build_argv("llama-server", "typo.gguf", port=9000)
        self.assertIn("-m", argv)
        self.assertNotIn("-hf", argv)

    def test_windows_path_is_not_mistaken_for_a_repo(self):
        """`C:/models/homura.gguf` contains a slash but is a local file.
        Sending it to -hf turns a typo into a confusing download -- and this
        was observed live: llama-server got -hf for a plain missing path."""
        for path in (r"C:/nonexistent/nope.gguf",
                     r"C:\models\homura.gguf",
                     "/kaggle/working/models/homura.gguf",
                     "./homura.gguf",
                     "../models/homura.gguf"):
            argv = build_argv("llama-server", path, port=9000)
            self.assertIn("-m", argv, path)
            self.assertNotIn("-hf", argv, path)

    def test_real_repo_ids_still_use_dash_hf(self):
        for repo in ("org/repo", "IndexTeam/Index-Homura-2B-GGUF", "org/repo:Q4_K_M"):
            argv = build_argv("llama-server", repo, port=9000)
            self.assertIn("-hf", argv, repo)
            self.assertNotIn("-m", argv, repo)

    def test_alias_for_a_local_path_is_the_file_stem(self):
        """--alias must be something a client can actually send as the model
        name; a full path is neither stable nor meaningful."""
        model = self.tmp / "homura-2b-q4_k_m.gguf"
        model.write_bytes(b"GGUF")
        argv = build_argv("llama-server", str(model), port=9000)
        self.assertEqual(argv[argv.index("--alias") + 1], "homura-2b-q4_k_m")

    def test_ctx_parallel_threads_and_no_jinja(self):
        argv = build_argv("llama-server", "org/repo", port=9000,
                          ctx=16384, threads=8, parallel=2)
        self.assertEqual(argv[argv.index("-c") + 1], "16384")
        self.assertEqual(argv[argv.index("-np") + 1], "2")
        self.assertEqual(argv[argv.index("-t") + 1], "8")
        # --jinja aborts an older llama.cpp before it binds a port.
        self.assertNotIn("--jinja", argv)
        self.assertNotIn("--max-model-len", argv)

    def test_alias_is_added_so_the_client_model_name_resolves(self):
        argv = build_argv("llama-server", "org/repo", port=9000)
        self.assertEqual(argv[argv.index("--alias") + 1], "org/repo")

    def test_extra_args_are_appended_last(self):
        argv = build_argv("llama-server", "org/repo", port=9000,
                          extra_args=["-ngl", "99"])
        self.assertEqual(argv[-2:], ["-ngl", "99"])


class LlamaServerLifecycleTests(TempDirCase):

    def _fake(self, **env) -> list[str]:
        prefix = write_script(self.tmp / "fake_llama_server.py", FAKE_LLAMA_SERVER)
        for key, value in env.items():
            self.set_env(**{key: value})
        return prefix

    def test_start_polls_until_ready_and_stop_releases_the_port(self):
        fake = self._fake(FAKE_SLOW="1.0")
        log = self.tmp / "server.log"
        server = LlamaServer(fake, "org/repo", port=free_port(),
                             log_path=log, startup_timeout=30, poll_interval=0.2)
        started = time.monotonic()
        server.start()
        self.assertGreaterEqual(time.monotonic() - started, 1.0)  # really polled
        self.assertEqual(server.base_url,
                         f"http://127.0.0.1:{server.port}/v1")
        self.assertTrue(server.is_running())
        server.stop()
        self.assertFalse(server.is_running())
        self.assertTrue(log.is_file())

    def test_crash_reports_exit_code_and_log_tail(self):
        """The vLLM run burned 1067 s and died on one import error line. An
        error message with the log tail in it is the difference between a 20
        minute debug round and a 20 second one."""
        fake = self._fake(FAKE_FAIL="1")
        log = self.tmp / "crash.log"
        server = LlamaServer(fake, "org/repo", port=free_port(), log_path=log,
                             startup_timeout=15, poll_interval=0.1)
        with self.assertRaises(LlamaServerError) as caught:
            server.start()
        message = str(caught.exception)
        self.assertIn("exited with code 3", message)
        self.assertIn("libcudart.so.13", message)
        self.assertIn(str(log), message)
        server.stop()   # must be safe after a failed start

    def test_timeout_reports_log_tail(self):
        fake = self._fake(FAKE_HANG="1", FAKE_HANG_SECS="120")
        log = self.tmp / "hang.log"
        server = LlamaServer(fake, "org/repo", port=free_port(), log_path=log,
                             startup_timeout=1.5, poll_interval=0.2)
        with self.assertRaises(LlamaServerTimeout) as caught:
            server.start()
        self.assertIn("did not answer /v1/models", str(caught.exception))
        self.assertIn(str(log), str(caught.exception))
        server.stop()

    def test_context_manager_stops_on_exception(self):
        fake = self._fake()
        server = LlamaServer(fake, "org/repo", port=free_port(),
                             log_path=self.tmp / "ctx.log")
        with self.assertRaises(RuntimeError):
            with server:
                self.assertTrue(server.is_running())
                raise RuntimeError("boom")
        self.assertFalse(server.is_running())

    def test_stop_is_idempotent(self):
        fake = self._fake()
        server = LlamaServer(fake, "org/repo", port=free_port(),
                             log_path=self.tmp / "idem.log")
        server.start()
        server.stop()
        server.stop()
        self.assertIsNone(server.process)

    def test_port_zero_is_resolved_before_launch(self):
        """`--port 0` is "any free port" to llama-server, which binds one it
        never reports back -- so every /v1/models probe then 404s and the
        server looks dead while it is serving. Observed live: the log said
        "listening on http://127.0.0.1:3402" while we polled a different port."""
        fake = self._fake()
        server = LlamaServer(fake, "org/repo", port=0,
                             log_path=self.tmp / "p0.log")
        self.assertEqual(server.port, 0)
        server.start()
        self.addCleanup(server.stop)
        self.assertNotEqual(server.port, 0)
        self.assertGreater(server.port, 0)
        self.assertTrue(server.is_running())

    def test_busy_port_is_replaced_not_reused(self):
        """The previous cell's server may still hold 8000. A bind error would
        look like a llama.cpp bug; a silent port swap just works."""
        fake = self._fake()
        holder = LlamaServer(fake, "org/repo", port=free_port(),
                             log_path=self.tmp / "holder.log")
        holder.start()
        self.addCleanup(holder.stop)
        second = LlamaServer(fake, "org/repo", port=holder.port,
                             log_path=self.tmp / "second.log")
        second.start()
        self.addCleanup(second.stop)
        self.assertNotEqual(second.port, holder.port)


class ChatTests(TempDirCase):

    def test_openai_shaped_response(self):
        fake = write_script(self.tmp / "srv.py", FAKE_LLAMA_SERVER)
        server = LlamaServer(fake, "org/repo", port=free_port(),
                             log_path=self.tmp / "chat.log")
        with server:
            result = server.chat([{"role": "user", "content": "Say OK"}])
        self.assertEqual(result["model"], "org/repo")
        self.assertEqual(result["choices"][0]["message"]["content"], "OK")
        self.assertEqual(result["usage"]["total_tokens"], 8)


class EndpointTests(TempDirCase):

    def test_build_openai_client_from_server(self):
        fake = write_script(self.tmp / "srv.py", FAKE_LLAMA_SERVER)
        server = LlamaServer(fake, "org/repo", port=free_port(),
                             log_path=self.tmp / "e.log")
        endpoint = build_openai_client(server=server)
        self.assertEqual(endpoint.base_url, f"http://127.0.0.1:{server.port}/v1")
        self.assertEqual(endpoint.api_key, "EMPTY")
        self.assertEqual(endpoint.model, "org/repo")

    def test_build_openai_client_needs_a_target(self):
        with self.assertRaises(ValueError):
            build_openai_client()

    def test_available_and_resolve_do_not_raise_when_absent(self):
        """These are called from a notebook preamble; raising there would hide
        the real error behind an import-time explosion."""
        llamacpp_bridge.resolve("definitely-not-installed-xyz")
        whispercpp_bridge.resolve("definitely-not-installed-xyz")
        self.assertIsInstance(llamacpp_bridge.available("definitely-not-installed-xyz"), bool)
        self.assertIsInstance(whispercpp_bridge.available("definitely-not-installed-xyz"), bool)


# --------------------------------------------------------------------------- #
# 17-24: whispercpp_bridge -- parsing, errors, flags
# --------------------------------------------------------------------------- #

class WhisperParserTests(unittest.TestCase):

    def test_parse_whisper_json_offsets_are_milliseconds(self):
        payload = {"transcription": [
            {"offsets": {"from": 0, "to": 2500}, "text": " hello"},
            {"offsets": {"from": 2500, "to": 5000}, "text": " world"},
        ]}
        text, segments, language = parse_whisper_json(payload)
        self.assertEqual([s["start"] for s in segments], [0.0, 2.5])
        self.assertEqual([s["end"] for s in segments], [2.5, 5.0])
        self.assertEqual(text, "hello world")
        self.assertEqual(language, "")

    def test_parse_whisper_json_falls_back_to_timestamp_strings(self):
        payload = {"transcription": [
            {"timestamps": {"from": "00:01:02,250", "to": "00:01:04,000"},
             "text": " cue"},
        ]}
        _, segments, _ = parse_whisper_json(payload)
        self.assertAlmostEqual(segments[0]["start"], 62.25, places=3)
        self.assertAlmostEqual(segments[0]["end"], 64.0, places=3)

    def test_parse_whisper_json_accepts_segments_key_and_language(self):
        text, segments, language = parse_whisper_json(
            {"segments": [{"start": 1.5, "end": 2.5, "text": "x"}], "language": "hi"})
        self.assertEqual(language, "hi")
        self.assertEqual(segments[0]["start"], 1.5)

    def test_parse_whisper_json_rejects_unknown_shape(self):
        with self.assertRaises(WhisperCppError):
            parse_whisper_json(42)

    def test_parse_srt(self):
        srt = ("1\n00:00:00,000 --> 00:00:02,500\nNamaste, yeh ek test hai.\n\n"
               "2\n00:00:02,500 --> 00:00:05,000\nDoosra segment.\n")
        segments = parse_srt(srt)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["end"], 2.5)
        self.assertEqual(segments[1]["text"], "Doosra segment.")

    def test_parse_srt_skips_malformed_blocks(self):
        self.assertEqual(parse_srt("nonsense\n\n00:00:00,000 --> x\n"), [])
        self.assertEqual(parse_srt(""), [])

    def test_timestamp_shapes(self):
        f = whispercpp_bridge._seconds_from_timestamp
        self.assertAlmostEqual(f("00:00:02,500"), 2.5)
        self.assertAlmostEqual(f("00:00:02.500"), 2.5)
        self.assertAlmostEqual(f(2500), 2.5)
        self.assertAlmostEqual(f("2.5"), 2.5)
        self.assertIsNone(f(None))
        self.assertIsNone(f(""))


class WhisperRunnerTests(TempDirCase):

    def _runner(self, *, ffmpeg: bool = True, **env) -> WhisperCppRunner:
        model = self.tmp / "ggml-base.bin"
        model.write_bytes(b"FAKEWHISPERMODEL")
        prefix = write_script(self.tmp / "fake_whisper_cli.py", FAKE_WHISPER_CLI)
        for key, value in env.items():
            self.set_env(**{key: value})
        # The real ffmpeg may well be installed on the dev box; defaulting to it
        # would make this suite test ffmpeg, not the bridge.
        return WhisperCppRunner(prefix, model, language="hi", threads=4,
                                ffmpeg=str(self._fake_ffmpeg()) if ffmpeg else "ffmpeg")

    def _wav(self) -> Path:
        # convert_to_wav refuses a missing input, which is the behaviour the
        # production path relies on -- so the fixture has to exist first.
        source = self.tmp / "movie.mkv"
        source.write_bytes(b"fake container")
        convert_to_wav(source, self.tmp / "movie.wav", ffmpeg=str(self._fake_ffmpeg()))
        return self.tmp / "movie.wav"

    def _fake_ffmpeg(self) -> Path:
        script = self.tmp / "fake_ffmpeg.py"
        if not script.is_file():
            write_script(script, FAKE_FFMPEG)
        if IS_WINDOWS:
            # CreateProcess can only run a .bat through its own interpreter, so
            # the fake is exposed under a .bat name with the x bit irrelevant.
            shim = self.tmp / "fake_ffmpeg.bat"
            shim.write_text(f'@"PYTHON" "{script}" %*\r\n'.replace("PYTHON", PYTHON),
                            encoding="utf-8")
            return shim
        return make_exe(self.tmp / "fake_ffmpeg",
                        f'#!/bin/sh\nexec "{PYTHON}" "{script}" "$@"\n')

    def test_transcribe_returns_mazinger_shape(self):
        self._runner()
        wav = self._wav()
        result = WhisperCppRunner(
            write_script(self.tmp / "w.py", FAKE_WHISPER_CLI),
            self.tmp / "ggml-base.bin", language="hi", threads=4,
        ).transcribe_wav(wav)
        self.assertEqual(result["language"], "hi")
        self.assertEqual(len(result["segments"]), 2)
        self.assertEqual(result["segments"][0]["start"], 0.0)
        self.assertEqual(result["segments"][1]["end"], 5.0)
        self.assertIn("Namaste", result["text"])
        for segment in result["segments"]:
            self.assertEqual(set(segment), {"start", "end", "text"})
            self.assertIsInstance(segment["start"], float)
            self.assertIsInstance(segment["end"], float)

    def test_srt_only_build_still_produces_a_transcript(self):
        """A build without -oj output must degrade to SRT, not raise: SRT has
        the same timing information re-segmentation needs."""
        self.set_env(FAKE_MODE="srt")
        runner = self._runner()
        result = runner.transcribe_wav(self._wav())
        self.assertEqual(len(result["segments"]), 2)
        self.assertIn("Doosra segment.", result["text"])

    def test_json_only_build_works(self):
        self.set_env(FAKE_MODE="json")
        runner = self._runner()
        result = runner.transcribe_wav(self._wav())
        self.assertEqual(result["segments"][1]["start"], 2.5)

    def test_unparseable_json_falls_back_to_srt(self):
        """A build that writes malformed JSON but a valid SRT must degrade to
        SRT rather than raise: SRT carries the same timings re-segmentation
        needs, so the stage is still worth running."""
        self.set_env(FAKE_MODE="garbage_srt")
        runner = self._runner()
        result = runner.transcribe_wav(self._wav())
        self.assertEqual(result["text"], "recovered")
        self.assertEqual(result["segments"][0]["end"], 1.0)

    def test_unparseable_json_without_srt_raises(self):
        """The fallback has to end somewhere; with no SRT to fall back to,
        a clear error beats an empty transcript."""
        self.set_env(FAKE_MODE="garbage")
        runner = self._runner()
        with self.assertRaises(WhisperCppError) as caught:
            runner.transcribe_wav(self._wav())
        self.assertIn("unreadable whisper.cpp JSON", str(caught.exception))

    def test_no_output_raises_instead_of_returning_empty(self):
        """Exit 0 with no output is the exact shape of the failure that dubbed
        two hours of silence."""
        self.set_env(FAKE_MODE="none")
        runner = self._runner()
        with self.assertRaises(WhisperCppError) as caught:
            runner.transcribe_wav(self._wav())
        self.assertIn("wrote no .json", str(caught.exception))

    def test_empty_transcript_raises_unless_allowed(self):
        """An exit-0 run that produced only whitespace is still a failure: the
        pipeline would dub silence for hours and every stage after would
        report success."""
        self.set_env(FAKE_MODE="empty")
        runner = self._runner()
        with self.assertRaises(WhisperCppError) as caught:
            runner.transcribe_wav(self._wav())
        self.assertIn("empty transcript", str(caught.exception))
        allowed = runner.transcribe_wav(self._wav(), allow_empty=True)
        self.assertEqual(allowed["text"], "")
        self.assertEqual(len(allowed["segments"]), 1)

    def test_nonzero_exit_includes_stderr_tail(self):
        self.set_env(FAKE_FAIL="1")
        runner = self._runner()
        with self.assertRaises(WhisperCppError) as caught:
            runner.transcribe_wav(self._wav())
        message = str(caught.exception)
        self.assertIn("exit 2", message)
        self.assertIn("failed to initialize whisper context", message)

    def test_argv_uses_only_real_flags(self):
        model = self.tmp / "ggml-base.bin"
        model.write_bytes(b"M")
        wav = self.tmp / "a.wav"
        prefix = self.tmp / "out"
        runner = WhisperCppRunner("whisper-cli", model, language="hi", threads=4)
        argv = runner.build_command(wav, prefix)
        self.assertEqual(argv[:8], ["whisper-cli", "-m", str(model), "-f",
                                    str(wav), "-oj", "-of", str(prefix)])
        self.assertEqual(argv[argv.index("-l") + 1], "hi")
        self.assertEqual(argv[argv.index("-t") + 1], "4")
        self.assertNotIn("--vad", argv)          # optional flags stay optional
        self.assertNotIn("--max-len", argv)

    def test_optional_flags_are_added_when_requested(self):
        model = self.tmp / "ggml-base.bin"
        model.write_bytes(b"M")
        runner = WhisperCppRunner("whisper-cli", model)
        argv = runner.build_command(self.tmp / "a.wav", self.tmp / "o",
                                    vad=True, max_len=30)
        self.assertIn("--vad", argv)
        self.assertEqual(argv[argv.index("--max-len") + 1], "30")

    def test_missing_model_fails_at_construction(self):
        """A missing 150 MB model should be named before a job is claimed,
        not after an hour of scheduling."""
        with self.assertRaises(WhisperCppError) as caught:
            WhisperCppRunner("whisper-cli", self.tmp / "absent.bin")
        self.assertIn("model not found", str(caught.exception))

    def test_non_wav_input_is_converted_first(self):
        self.set_env(FAKE_MODE="json")
        runner = self._runner()
        (self.tmp / "clip.mp4").write_bytes(b"fake mp4")
        result = runner.transcribe_wav(self.tmp / "clip.mp4")
        self.assertEqual(len(result["segments"]), 2)
        self.assertTrue((self.tmp / "clip.16k.wav").exists())

    def test_argv_dump_proves_the_flags_really_reached_the_process(self):
        dump = self.tmp / "argv.json"
        self.set_env(FAKE_ARGV=str(dump), FAKE_MODE="json")
        runner = self._runner()
        runner.transcribe_wav(self._wav())
        argv = json.loads(dump.read_text(encoding="utf-8"))
        self.assertIn("-oj", argv)
        self.assertEqual(argv[argv.index("-t") + 1], "4")
        self.assertTrue(argv[argv.index("-f") + 1].endswith(".wav"))


class FfmpegTests(TempDirCase):

    def _fake_ffmpeg(self) -> Path:
        script = self.tmp / "ff.py"
        write_script(script, FAKE_FFMPEG)
        if IS_WINDOWS:
            shim = self.tmp / "ff.bat"
            shim.write_text(f'@"PYTHON" "{script}" %*\r\n'.replace("PYTHON", PYTHON),
                            encoding="utf-8")
            return shim
        return make_exe(self.tmp / "ff", f'#!/bin/sh\nexec "{PYTHON}" "{script}" "$@"\n')

    def test_convert_produces_a_16k_mono_wav(self):
        (self.tmp / "in.mp4").write_bytes(b"x")
        out = convert_to_wav(self.tmp / "in.mp4", self.tmp / "out.wav",
                             ffmpeg=str(self._fake_ffmpeg()))
        data = out.read_bytes()
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(data[8:12], b"WAVE")
        self.assertGreater(len(data), 44)

    def test_convert_failure_names_the_stderr(self):
        (self.tmp / "bad.mp4").write_bytes(b"x")
        with self.assertRaises(WhisperCppError) as caught:
            convert_to_wav(self.tmp / "bad.mp4", self.tmp / "o.wav",
                           ffmpeg=str(self._fake_ffmpeg()))
        self.assertIn("ffmpeg failed", str(caught.exception))

    def test_empty_output_is_rejected(self):
        (self.tmp / "e.empty").write_bytes(b"x")
        with self.assertRaises(WhisperCppError) as caught:
            convert_to_wav(self.tmp / "e.empty", self.tmp / "o.wav",
                           ffmpeg=str(self._fake_ffmpeg()))
        self.assertIn("empty WAV", str(caught.exception))

    def test_missing_input_is_named(self):
        with self.assertRaises(WhisperCppError):
            convert_to_wav(self.tmp / "nope.mp4")

    def test_missing_ffmpeg_gives_an_actionable_message(self):
        (self.tmp / "in.mp4").write_bytes(b"x")
        with self.assertRaises(WhisperCppError) as caught:
            convert_to_wav(self.tmp / "in.mp4", self.tmp / "o.wav",
                           ffmpeg="ffmpeg-does-not-exist-xyz")
        self.assertIn("not found on PATH", str(caught.exception))


# --------------------------------------------------------------------------- #

class _Capture:
    """Redirect stdout for the duration of a call. Printing the contract lines
    to a file would defeat the point of testing the contract."""

    def __init__(self) -> None:
        import io
        self._io = io
        self.buffer = io.StringIO()
        self._old = None

    def __enter__(self):
        self._old = sys.stdout
        sys.stdout = self.buffer
        return self

    def __exit__(self, *exc):
        sys.stdout = self._old
        return False

    @property
    def text(self) -> str:
        return self.buffer.getvalue()


def _capture(fn, *args, **kwargs) -> str:
    with _Capture() as cap:
        fn(*args, **kwargs)
    return cap.text


if __name__ == "__main__":
    unittest.main(verbosity=2)