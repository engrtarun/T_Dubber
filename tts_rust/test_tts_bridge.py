#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for the Rust TTS bridge -- no real binary, no weights.

    python tts_rust/test_tts_bridge.py
    python -m unittest discover -s tts_rust -v
    pytest tts_rust/test_tts_bridge.py -v

WHY A FAKE EXECUTABLE INSTEAD OF THE REAL ONE
---------------------------------------------
VibeVoice-1.5B is 1.5B parameters of safetensors and a CPU diffusion decode.
Testing against it would make this suite a multi-minute, multi-gigabyte test
that CI skips -- the same reason ``test_runtimes.py`` fakes ``llama-server``
rather than starting it.

What actually breaks in production is never the model maths, it is the
plumbing, and the plumbing is exactly what is faked here:

  * one invocation per segment instead of one per job -- the regression that
    does not fail, it just takes 400x longer and then does not finish;
  * a missing ``--ref-audio`` quietly producing a dub in the wrong voice;
  * a missing model directory discovered after the runner was constructed;
  * exit 0 with a JSON report and no WAV on disk, which is the silent-empty
    failure that dubbed two hours of silence once already;
  * stdout that is not the ``--json`` the caller asked for.

THE FAKES ARE REAL PROCESSES
----------------------------
``tdub_tts`` is replaced by a Python script launched through ``subprocess``,
mirroring ``test_runtimes.py``. Mocking ``subprocess.run`` would let the fake's
own argument parsing -- the thing under test -- be the thing that is fake.
On Windows the fake is additionally exposed through a ``.bat`` shim so that
CreateProcess, not just ``[python, script]``, is exercised; that is how the
real binary is invoked in production.
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
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tdub_tts_bridge  # noqa: E402
from tdub_tts_bridge import (  # noqa: E402
    TtsBinaryMissing,
    TtsEmptyAudioError,
    TtsError,
    TtsModelError,
    TtsOutputError,
    TtsReferenceAudioError,
    TtsRunner,
    TtsSynthesisError,
    TtsUsageError,
    available,
    resolve,
    search_dirs,
)

IS_WINDOWS = os.name == "nt"
PYTHON = sys.executable


# --------------------------------------------------------------------------- #
# Fake executable
# --------------------------------------------------------------------------- #

FAKE_TDUB_TTS = r'''
"""Stand-in for tdub_tts: parses the real flags and writes real WAVs.

Env knobs let one script play every role the binary can take:

  FAKE_COUNT=<path>    append one line per invocation (the load-once assertion)
  FAKE_ARGV=<path>     append the argv it received, one JSON list per line
  FAKE_MODE=ok         normal run: writes WAVs and a --json report
  FAKE_MODE=fail       non-zero exit with a reason on stderr (FAKE_EXIT picks it)
  FAKE_MODE=badjson    exit 0, valid-looking stdout that is not JSON
  FAKE_MODE=nojson     exit 0 and print nothing at all
  FAKE_MODE=silent     exit 0, well-formed report, but writes NO wav
  FAKE_MODE=headeronly exit 0, report, but writes a 44-byte header-only wav
  FAKE_MODE=partial    exit 0, report lists only some of the segments
"""
import json, os, struct, sys

argv = sys.argv[1:]

counter = os.environ.get("FAKE_COUNT")
if counter:
    with open(counter, "a", encoding="utf-8") as fh:
        fh.write("invoked\n")

dump = os.environ.get("FAKE_ARGV")
if dump:
    with open(dump, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\n")

MODE = os.environ.get("FAKE_MODE", "ok")
SAMPLE_RATE = 24000


def flag(name, default=None):
    for i, item in enumerate(argv):
        if item == name and i + 1 < len(argv):
            return argv[i + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return default


def has(name):
    return name in argv


def write_wav(path, samples):
    pcm = b"".join(struct.pack("<h", max(-32768, min(32767, int(s * 32767)))) for s in samples)
    with open(path, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt ")
        fh.write(struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16))
        fh.write(b"data" + struct.pack("<I", len(pcm)) + pcm)


def emit(report):
    sys.stdout.write(json.dumps(report))
    sys.exit(0)


if MODE == "fail":
    sys.stderr.write(
        "tdub_tts: could not load VibeVoice-1.5B from /models/VibeVoice-1.5B: "
        "Failed to load model weights: config.json not found\n")
    sys.exit(int(os.environ.get("FAKE_EXIT", "3")))

if MODE == "badjson":
    sys.stdout.write("tdub_tts: loading...\ntdub_tts: done\n")
    sys.exit(0)

if MODE == "nojson":
    sys.exit(0)

MODEL_DIR = flag("--model", "?")
MODEL = {"name": "VibeVoice-1.5B", "variant": "vibevoice",
         "parameters": 1500000000, "sample_rate": SAMPLE_RATE,
         "languages": ["auto", "multilingual"], "voices": []}

if has("--probe"):
    emit({"ok": True, "mode": "probe", "device": flag("--device", "cpu"),
          "model_path": MODEL_DIR, "model": MODEL, "load_ms": 4321, "warnings": []})

text_file = flag("--text-file")
if text_file:
    with open(text_file, "r", encoding="utf-8") as fh:
        segments = [line.rstrip("\n") for line in fh if line.strip()]
else:
    segments = [flag("--text", "")]

out_dir = flag("--out-dir")
prefix = flag("--out-prefix", "seg")
out = flag("--out")
records = []
for index, text in enumerate(segments):
    destination = out if out else os.path.join(out_dir, "%s-%04d.wav" % (prefix, index))
    if MODE != "silent":
        if MODE == "headeronly":
            with open(destination, "wb") as fh:
                fh.write(b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt ")
                fh.write(struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16))
                fh.write(b"data" + struct.pack("<I", 0))
        else:
            write_wav(destination, [0.1, -0.1] * (SAMPLE_RATE // 2))
    if MODE == "partial" and index > 0:
        continue
    records.append({"index": index, "text": text, "out": destination,
                    "sample_rate": SAMPLE_RATE, "samples": SAMPLE_RATE,
                    "duration_secs": 1.0, "bytes": 48044, "elapsed_ms": 1200})

emit({"ok": MODE != "partial", "mode": "synthesize", "device": "cpu",
      "model_path": MODEL_DIR, "model": MODEL, "reference_audio": None,
      "load_ms": 4321, "segments": records, "failures": [],
      "elapsed_ms": 1200 * len(records), "warnings": []})
'''


def make_exe(path: Path, body: str) -> Path:
    """Create a file the resolver accepts as executable.

    On Windows ``os.access(X_OK)`` is True for any existing file; on POSIX the
    x bit has to be set or the resolver rejects it -- which is itself worth
    proving rather than assuming."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if not IS_WINDOWS:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class TempCase(unittest.TestCase):
    """Isolated temp dir, clean env, and a throwaway VibeVoice-shaped model dir."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="td-tts-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._env = dict(os.environ)
        self.addCleanup(self._restore_env)

        # Neutralise the binary override so a real cargo build in
        # tts_rust/target/release/ cannot answer a question about the fake.
        for key in ("TDUBBER_TTS_BIN", "TDUBBER_VIBEVOICE_MODEL_DIR"):
            os.environ.pop(key, None)

        self.counter = self.tmp / "invocations.txt"
        self.argv_dump = self.tmp / "argv.jsonl"
        self.set_env(FAKE_COUNT=str(self.counter))

        self.fake = self._install_fake()
        self.model_dir = self.tmp / "VibeVoice-1.5B"
        self.model_dir.mkdir()
        for asset in ("config.json", "tokenizer.json"):
            (self.model_dir / asset).write_text("{}", encoding="utf-8")
        (self.model_dir / "model.safetensors").write_bytes(b"FAKE")
        self.ref = self.tmp / "voice_sample.wav"
        self.ref.write_bytes(b"RIFF" + b"\0" * 64)

    def _restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def set_env(self, **values) -> None:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _install_fake(self) -> Path:
        """Write the fake and return it under the platform's binary spelling.

        On Windows a ``.bat`` shim is used, exactly as ``test_runtimes.py`` does
        for ffmpeg: CreateProcess can only run a ``.bat`` through its own
        interpreter, and using it here means the tests exercise the same call
        path production uses."""
        script = self.tmp / "fake_tdub_tts.py"
        script.write_text(textwrap.dedent(FAKE_TDUB_TTS).lstrip(), encoding="utf-8")
        if IS_WINDOWS:
            shim = self.tmp / "tdub_tts.bat"
            shim.write_text(f'@"{PYTHON}" "{script}" %*\r\n', encoding="utf-8")
            return shim
        return make_exe(self.tmp / "tdub_tts",
                        f'#!/bin/sh\nexec "{PYTHON}" "{script}" "$@"\n')

    # -- assertions -------------------------------------------------------- #

    @property
    def invocations(self) -> int:
        if not self.counter.is_file():
            return 0
        return len([line for line in self.counter.read_text(encoding="utf-8").splitlines()
                    if line.strip()])

    @property
    def argv_runs(self) -> list[list[str]]:
        if not self.argv_dump.is_file():
            return []
        return [json.loads(line) for line in
                self.argv_dump.read_text(encoding="utf-8").splitlines() if line.strip()]

    def runner(self, **kwargs) -> TtsRunner:
        params = {"binary": str(self.fake), "model_dir": str(self.model_dir),
                  "ref_audio": str(self.ref)}
        params.update(kwargs)
        return TtsRunner(**params)


# --------------------------------------------------------------------------- #
# 1: the expensive-thing regression -- one load, many segments
# --------------------------------------------------------------------------- #

class OneLoadManySegmentsTests(TempCase):

    def test_many_segments_run_the_binary_exactly_once(self):
        """THE assertion. 60 segments must be one process, not 60.

        A regression here does not raise -- it just reloads 1.5B parameters 60
        times and turns a job that finishes into one that does not."""
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        segments = [{"text": f"Segment number {i}."} for i in range(60)]
        results = self.runner().synthesize_many(segments, self.tmp / "out")

        self.assertEqual(self.invocations, 1,
                         "the model must be loaded once for the whole batch")
        self.assertEqual(len(results), 60)
        self.assertEqual(len(self.argv_runs), 1)
        # One --text-file for all of them, and never a --text per segment.
        self.assertIn("--text-file", self.argv_runs[0])
        self.assertNotIn("--text", self.argv_runs[0])

    def test_one_invocation_per_call_not_one_per_segment(self):
        """Two single-segment calls are two invocations -- the batching is
        explicit, not something the runner does behind your back."""
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        runner = self.runner()
        runner.synthesize("Pehla.", self.tmp / "a.wav")
        runner.synthesize("Doosra.", self.tmp / "b.wav")
        self.assertEqual(self.invocations, 2)
        self.assertIn("--text", self.argv_runs[0])
        self.assertIn("--out", self.argv_runs[0])

    def test_segments_file_is_removed_and_outputs_land_in_out_dir(self):
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        results = self.runner().synthesize_many(
            ["Ek.", "Do.", "Teen."], self.tmp / "out", prefix="dub")
        text_file = Path(self.argv_runs[0][self.argv_runs[0].index("--text-file") + 1])
        self.assertFalse(text_file.exists(),
                         "the temp segment file must not be left behind")
        for index, record in enumerate(results):
            self.assertEqual(record["index"], index)
            self.assertTrue((self.tmp / "out" / f"dub-{index:04d}.wav").is_file())

    def test_plain_strings_and_text_mappings_are_both_accepted(self):
        results = self.runner().synthesize_many(
            ["string segment", {"text": "mapping segment"}], self.tmp / "out")
        self.assertEqual([r["text"] for r in results],
                         ["string segment", "mapping segment"])

    def test_report_missing_a_segment_is_an_error_not_a_short_list(self):
        """A --json report that quietly omits a segment would let a caller
        index into the results and get None where audio should be. The fake
        drops every index above 0, so segment 1 is the first one absent."""
        self.set_env(FAKE_MODE="partial")
        with self.assertRaises(TtsSynthesisError) as caught:
            self.runner().synthesize_many(["a", "b", "c"], self.tmp / "out")
        message = str(caught.exception)
        self.assertIn("segment 1", message)
        self.assertIn("absent from its --json report", message)
        self.assertIn("[0]", message)

    def test_blank_segment_is_rejected_rather_than_dropped(self):
        with self.assertRaises(TtsUsageError) as caught:
            self.runner().synthesize_many(["ok", "   "], self.tmp / "out")
        self.assertIn("blank", str(caught.exception))
        self.assertEqual(self.invocations, 0, "must fail before spawning")


# --------------------------------------------------------------------------- #
# 2: failures surface, loudly, with the binary's own words
# --------------------------------------------------------------------------- #

class FailureSurfaceTests(TempCase):

    def test_nonzero_exit_surfaces_stderr(self):
        self.set_env(FAKE_MODE="fail", FAKE_EXIT="3")
        with self.assertRaises(TtsModelError) as caught:
            self.runner().synthesize("Hello.", self.tmp / "a.wav")
        message = str(caught.exception)
        self.assertIn("exit 3", message)
        self.assertIn("Failed to load model weights", message)
        self.assertIn("config.json not found", message)

    def test_exit_code_selects_the_typed_error(self):
        """Each binary exit code maps to its own class. 'It failed' is not
        actionable; 'the reference clip is unusable' is."""
        expected = {
            2: TtsUsageError,
            3: TtsModelError,
            4: TtsReferenceAudioError,
            5: TtsSynthesisError,
            6: TtsOutputError,
            7: TtsEmptyAudioError,
        }
        for code, klass in expected.items():
            with self.subTest(code=code):
                self.set_env(FAKE_MODE="fail", FAKE_EXIT=str(code))
                with self.assertRaises(klass):
                    self.runner().synthesize("Hello.", self.tmp / f"{code}.wav")

    def test_unmapped_exit_code_still_fails(self):
        """A crash inside the binary exits outside 2..=7. That is not success,
        and it must not be dressed up as a modelled failure."""
        self.set_env(FAKE_MODE="fail", FAKE_EXIT="3221225781")
        with self.assertRaises(TtsError):
            self.runner().synthesize("Hello.", self.tmp / "a.wav")

    def test_exit_zero_with_a_wav_nobody_wrote_is_still_a_failure(self):
        """Exit 0 + a clean --json report + no file on disk. This is the shape
        of the two-hours-of-silence bug, wearing a different hat."""
        self.set_env(FAKE_MODE="silent")
        with self.assertRaises(TtsEmptyAudioError) as caught:
            self.runner().synthesize("Hello.", self.tmp / "a.wav")
        self.assertIn("header-only", str(caught.exception))

    def test_header_only_wav_is_rejected(self):
        """44 bytes is a valid file containing no sound. Accepting it is how a
        segment goes missing without anything turning red."""
        self.set_env(FAKE_MODE="headeronly")
        with self.assertRaises(TtsEmptyAudioError):
            self.runner().synthesize("Hello.", self.tmp / "a.wav")

    def test_unparseable_stdout_on_exit_zero_is_an_error(self):
        self.set_env(FAKE_MODE="badjson")
        with self.assertRaises(TtsSynthesisError) as caught:
            self.runner().synthesize("Hello.", self.tmp / "a.wav")
        self.assertIn("not parseable", str(caught.exception))

    def test_absent_stdout_on_exit_zero_is_an_error(self):
        self.set_env(FAKE_MODE="nojson")
        with self.assertRaises(TtsSynthesisError):
            self.runner().synthesize("Hello.", self.tmp / "a.wav")

    def test_missing_binary_is_named(self):
        runner = self.runner(binary=str(self.tmp / "no-such-binary-xyz"))
        with self.assertRaises(TtsBinaryMissing) as caught:
            runner.synthesize("Hello.", self.tmp / "a.wav")
        self.assertIn("no-such-binary-xyz", str(caught.exception))

    def test_empty_text_is_rejected_before_spawning(self):
        with self.assertRaises(TtsUsageError):
            self.runner().synthesize("   ", self.tmp / "a.wav")
        self.assertEqual(self.invocations, 0)

    def test_a_gpu_device_is_refused_at_construction(self):
        """The CPU-only rule is enforced in Python too, so no future caller can
        pass device='cuda' and quietly reintroduce the stack this project is
        removing."""
        with self.assertRaises(TtsUsageError) as caught:
            self.runner(device="cuda")
        self.assertIn("libcudart.so.13", str(caught.exception))


# --------------------------------------------------------------------------- #
# 3: model directory and reference audio, checked before a 1.5B load
# --------------------------------------------------------------------------- #

class PreflightTests(TempCase):

    def test_missing_model_dir_fails_loudly_at_construction(self):
        """A missing snapshot must be named before a job is claimed, not after
        an hour of scheduling."""
        with self.assertRaises(TtsModelError) as caught:
            TtsRunner(str(self.fake), self.tmp / "no-such-model-dir")
        message = str(caught.exception)
        self.assertIn("model directory not found", message)
        self.assertIn("VibeVoice-1.5B", message)
        self.assertIn("TDUBBER_VIBEVOICE_MODEL_DIR", message)
        self.assertEqual(self.invocations, 0)

    def test_model_dir_that_is_a_file_is_rejected(self):
        stray = self.tmp / "model.txt"
        stray.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(TtsModelError):
            TtsRunner(str(self.fake), stray)

    def test_missing_ref_audio_is_rejected_when_a_clone_is_requested(self):
        """THE clone guard. A clone WAS requested (require_clone=True);
        if the reference clip is absent the stage must fail, not
        quietly dub in a default one. Without the flag the call is
        plain TTS and may proceed -- that default is pinned below."""
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        runner = self.runner(ref_audio=None, require_clone=True)
        with self.assertRaises(TtsReferenceAudioError) as caught:
            runner.synthesize("Hello.", self.tmp / "a.wav")
        message = str(caught.exception)
        self.assertIn("no reference audio", message)
        self.assertIn("wrong", message)
        self.assertEqual(self.invocations, 0,
                         "must fail before a 1.5B parameter model load")

    def test_ref_audio_omitted_for_many_segments_is_also_rejected(self):
        with self.assertRaises(TtsReferenceAudioError):
            self.runner(ref_audio=None, require_clone=True).synthesize_many(
                ["a", "b"], self.tmp / "out")
        self.assertEqual(self.invocations, 0)

    def test_ref_audio_path_that_does_not_exist_is_named(self):
        runner = self.runner()
        with self.assertRaises(TtsReferenceAudioError) as caught:
            runner.synthesize("Hello.", self.tmp / "a.wav",
                              ref_audio=self.tmp / "ghost.wav")
        self.assertIn("ghost.wav", str(caught.exception))

    def test_require_ref_audio_flag_reaches_the_binary(self):
        """The binary enforces the same rule. Belt and braces, so the guarantee
        survives a caller that reaches tdub_tts directly."""
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        self.runner(require_clone=True).synthesize("Hello.", self.tmp / "a.wav")
        self.assertIn("--require-ref-audio", self.argv_runs[0])
        self.assertIn("--ref-audio", self.argv_runs[0])

    def test_clone_may_be_waived_deliberately(self):
        """require_clone=False is the documented escape hatch for plain TTS,
        and it must actually work -- otherwise nobody would ever turn it on."""
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        runner = self.runner(ref_audio=None, require_clone=False)
        runner.synthesize("Hello.", self.tmp / "a.wav")
        self.assertNotIn("--require-ref-audio", self.argv_runs[0])
        self.assertNotIn("--ref-audio", self.argv_runs[0])


# --------------------------------------------------------------------------- #
# 3b: the calls mazinger actually makes (rusttts engine, clones=False)
# --------------------------------------------------------------------------- #

class MazingerInteropTests(TempCase):
    """``_RustTTSWrapper``'s real call shapes, from
    ``mazinger/mazinger/tts.py``.

    The rusttts engine is registered ``clones=False`` and refuses a
    reference at three gates, so the wrapper's plain-TTS calls carry
    no reference clip. ``require_clone`` therefore defaults to False:
    defaulting it to True made every plain segment raise
    ``TtsReferenceAudioError`` -- a real integration bug, fixed
    2026-10-10. These tests pin the fixed behaviour, and the
    batch shapes the wrapper sends."""

    def test_plain_two_argument_synthesize_without_a_reference(self):
        """``TtsRunner(binary, model_dir).synthesize(text, out_wav)`` --
        the exact shape ``_RustTTSWrapper._call_synthesize`` sends
        when no voice sample was supplied."""
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        record = TtsRunner(str(self.fake), str(self.model_dir)).synthesize(
            "Namaste.", self.tmp / "plain.wav")
        self.assertGreater(record["samples"], 0)
        self.assertTrue((self.tmp / "plain.wav").is_file())
        argv = self.argv_runs[0]
        self.assertNotIn("--ref-audio", argv)
        self.assertNotIn("--require-ref-audio", argv)
        self.assertEqual(self.invocations, 1)

    def test_items_mode_moves_each_wav_to_its_own_destination(self):
        """``synthesize_many(items=[(text, ref_audio, out_wav), ...])``
        -- ``_RustTTSWrapper.synthesize_batch``'s shape. One
        invocation, one model load, per-item destinations outside
        the scratch directory the binary wrote to."""
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        destinations = [self.tmp / "dub" / "one.wav",
                        self.tmp / "dub" / "two.wav"]
        results = TtsRunner(str(self.fake), str(self.model_dir)).synthesize_many(
            items=[("Pehla.", None, destinations[0]),
                   ("Doosra.", None, destinations[1])])
        self.assertEqual(self.invocations, 1,
                         "the batch must be one process, one model load")
        self.assertEqual([r["text"] for r in results], ["Pehla.", "Doosra."])
        for path in destinations:
            self.assertTrue(path.is_file(), f"{path} must exist")
        argv = self.argv_runs[0]
        self.assertIn("--text-file", argv)
        self.assertNotIn(str(destinations[0].parent), argv)

    def test_positional_item_triples_are_detected_without_a_keyword(self):
        """mazinger's kwarg mapping cannot always name the parameters,
        so it falls back to ``many([(text, ref_audio, out_wav), ...])``."""
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        results = TtsRunner(str(self.fake), str(self.model_dir)).synthesize_many(
            [("Ek.", None, self.tmp / "a.wav"),
             ("Do.", None, self.tmp / "b.wav")])
        self.assertEqual(self.invocations, 1)
        self.assertEqual([r["text"] for r in results], ["Ek.", "Do."])
        self.assertTrue((self.tmp / "a.wav").is_file())
        self.assertTrue((self.tmp / "b.wav").is_file())

    def test_one_shared_reference_for_a_whole_batch(self):
        """A direct caller MAY clone in a batch: every item naming the
        same clip means it is decoded once for the whole run."""
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        results = TtsRunner(str(self.fake), str(self.model_dir)).synthesize_many(
            items=[("Ek.", self.ref, self.tmp / "a.wav"),
                   ("Do.", self.ref, self.tmp / "b.wav")])
        self.assertEqual(self.invocations, 1)
        argv = self.argv_runs[0]
        self.assertIn("--ref-audio", argv)
        self.assertEqual(argv[argv.index("--ref-audio") + 1], str(self.ref))

    def test_items_disagreeing_on_reference_audio_fail_loudly(self):
        """Two different clips in one batch would mean one is decoded
        and the other silently ignored -- a wrong-voice dub. Refuse."""
        other = self.tmp / "other_voice.wav"
        other.write_bytes(b"RIFF" + b"\0" * 64)
        with self.assertRaises(TtsReferenceAudioError) as caught:
            TtsRunner(str(self.fake), str(self.model_dir)).synthesize_many(
                items=[("Ek.", self.ref, self.tmp / "a.wav"),
                       ("Do.", other, self.tmp / "b.wav")])
        self.assertIn("different reference clips", str(caught.exception))
        self.assertEqual(self.invocations, 0)

    def test_per_call_language_beats_the_runner_level_language(self):
        """mazinger sends the segment's language per call; the
        constructor-level knob must not silently win."""
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        self.runner(language="hi").synthesize(
            "Hello.", self.tmp / "a.wav", language="bn")
        argv = self.argv_runs[0]
        self.assertIn("--language", argv)
        self.assertEqual(argv[argv.index("--language") + 1], "bn")


# --------------------------------------------------------------------------- #
# 4: the --json contract
# --------------------------------------------------------------------------- #

class JsonReportTests(TempCase):

    def test_probe_parses_and_reports_the_model(self):
        """The feasibility check: one process, one load, real metadata."""
        report = self.runner().probe()
        self.assertTrue(report["ok"])
        self.assertEqual(report["mode"], "probe")
        self.assertEqual(report["device"], "cpu")
        self.assertEqual(report["model"]["name"], "VibeVoice-1.5B")
        self.assertEqual(report["model"]["sample_rate"], 24000)
        self.assertEqual(self.invocations, 1)

    def test_probe_does_not_require_a_reference_clip(self):
        """--probe is about the model. Demanding a voice sample to find out
        whether the weights load would be backwards."""
        report = self.runner(ref_audio=None).probe()
        self.assertTrue(report["ok"])

    def test_synthesize_returns_a_usable_segment_record(self):
        record = self.runner().synthesize("Namaste.", self.tmp / "a.wav")
        self.assertEqual(record["sample_rate"], 24000)
        self.assertGreater(record["samples"], 0)
        self.assertTrue((self.tmp / "a.wav").is_file())
        self.assertIn("report", record)

    def test_json_is_always_requested(self):
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        self.runner().synthesize("Hello.", self.tmp / "a.wav")
        self.assertIn("--json", self.argv_runs[0])

    def test_device_cpu_is_always_passed(self):
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        self.runner().synthesize("Hello.", self.tmp / "a.wav")
        argv = self.argv_runs[0]
        self.assertIn("--device", argv)
        self.assertEqual(argv[argv.index("--device") + 1], "cpu")

    def test_optional_flags_are_passed_only_when_configured(self):
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        plain = self.runner()
        plain.synthesize("Hello.", self.tmp / "a.wav")
        for absent in ("--language", "--instruct", "--max-tokens", "--seed",
                       "--keep-going", "--allow-empty"):
            self.assertNotIn(absent, self.argv_runs[-1], absent)

        tuned = self.runner(language="hi", instruct="calm narration",
                            max_tokens=2048, keep_going=True)
        tuned.synthesize("Hello.", self.tmp / "b.wav")
        argv = self.argv_runs[-1]
        self.assertEqual(argv[argv.index("--language") + 1], "hi")
        self.assertEqual(argv[argv.index("--instruct") + 1], "calm narration")
        self.assertEqual(argv[argv.index("--max-tokens") + 1], "2048")
        self.assertIn("--keep-going", argv)


# --------------------------------------------------------------------------- #
# 5: resolution
# --------------------------------------------------------------------------- #

class ResolveTests(TempCase):

    def test_extra_search_dir_wins_over_a_real_cargo_build(self):
        """extra goes FIRST. Once `cargo build --release` has run, a real
        tdub_tts.exe sits in tts_rust/target/release/ and a test that appended
        its fake there would be testing the real binary by accident."""
        staging = self.tmp / "staging"
        staged = make_exe(staging / ("tdub_tts.exe" if IS_WINDOWS else "tdub_tts"),
                          "#!/bin/sh\nexit 0\n")
        found = resolve(extra=[staging])
        self.assertIsNotNone(found)
        self.assertEqual(Path(found), staged)

    def test_env_override_accepts_a_full_path_to_the_binary(self):
        self.set_env(TDUBBER_TTS_BIN=str(self.fake))
        found = resolve()
        self.assertIsNotNone(found)
        self.assertEqual(Path(found), self.fake)
        self.assertTrue(available())

    def test_search_dirs_are_ordered_operator_first(self):
        self.set_env(TDUBBER_TTS_BIN=str(self.tmp / "a") + os.pathsep + str(self.tmp / "b"))
        dirs = search_dirs(extra=[self.tmp / "hook"])
        self.assertEqual(dirs[0], self.tmp / "hook")
        self.assertIn(self.tmp / "a", dirs)
        self.assertIn(self.tmp / "b", dirs)
        self.assertIn(HERE / "target" / "release", dirs)

    def test_absent_binary_resolves_to_none_without_raising(self):
        """resolve() and available() are called from a notebook preamble; an
        exception there hides the real error behind a worse one."""
        self.set_env(TDUBBER_TTS_BIN=str(self.tmp / "empty_dir"))
        (self.tmp / "empty_dir").mkdir()
        original = os.environ.get("PATH", "")
        self.set_env(PATH="")
        try:
            found = resolve("definitely-not-installed-xyz", extra=[self.tmp / "empty_dir"])
            self.assertIsNone(found)
            self.assertIsInstance(available("definitely-not-installed-xyz"), bool)
        finally:
            self.set_env(PATH=original)

    def test_wav_is_silent_helper(self):
        good = self.tmp / "good.wav"
        good.write_bytes(b"RIFF" + b"\0" * 200)
        header = self.tmp / "header.wav"
        header.write_bytes(b"RIFF" + b"\0" * 40)
        self.assertFalse(tdub_tts_bridge.wav_is_silent(good))
        self.assertTrue(tdub_tts_bridge.wav_is_silent(header))
        self.assertTrue(tdub_tts_bridge.wav_is_silent(self.tmp / "absent.wav"))


# --------------------------------------------------------------------------- #
# 6: the fake really is a real process
# --------------------------------------------------------------------------- #

class FakeProcessTests(TempCase):

    def test_argv_really_reached_the_process(self):
        """Proves the fake parsed the real command line rather than the test
        asserting against its own construction of it."""
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        self.runner().synthesize("Hello.", self.tmp / "a.wav")
        self.assertEqual(len(self.argv_runs), 1)
        argv = self.argv_runs[0]
        self.assertEqual(argv[argv.index("--model") + 1], str(self.model_dir))
        self.assertEqual(argv[argv.index("--out") + 1], str(self.tmp / "a.wav"))
        self.assertEqual(argv[argv.index("--ref-audio") + 1], str(self.ref))

    @unittest.skipUnless(IS_WINDOWS, "the .bat shim is a Windows mechanism")
    def test_bat_shim_means_createprocess_is_exercised(self):
        """The fake runs through cmd.exe, the same way the real tdub_tts.exe
        will. A [python, script] prefix would not catch a quoting bug in the
        .bat handling."""
        self.assertTrue(str(self.fake).endswith(".bat"))
        self.set_env(FAKE_ARGV=str(self.argv_dump))
        self.runner().synthesize("Hello.", self.tmp / "a.wav")
        self.assertEqual(self.invocations, 1)

    def test_the_binary_is_runnable_as_an_argv_prefix_too(self):
        """The bridge accepts an argv prefix, which is how a wrapper script
        works without special-casing."""
        script = self.tmp / "fake_tdub_tts.py"
        runner = self.runner(binary=[PYTHON, str(script)])
        record = runner.synthesize("Hello.", self.tmp / "a.wav")
        self.assertGreater(record["samples"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)