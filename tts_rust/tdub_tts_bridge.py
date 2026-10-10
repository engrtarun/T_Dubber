#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Drive ``tdub_tts`` and return what mazinger's Speak stage expects.

    from tdub_tts_bridge import TtsRunner
    runner = TtsRunner(resolve(), "/kaggle/working/models/VibeVoice-1.5B",
                       ref_audio="voice_sample.wav")
    runner.synthesize("Namaste, yeh ek test hai.", "seg-0000.wav")
    runner.synthesize_many([{"text": "..."}, {"text": "..."}], "out/")  # ONE model load

WHY THIS EXISTS
---------------
mazinger registers the ``rusttts`` engine as its torch-free, NON-cloning
TTS: the engine registry (``mazinger/mazinger/tts.py`` ->
``TTS_ENGINES["rusttts"]``) says ``clones=False``, and three separate
gates refuse a clone request before it can reach this bridge. So the
call that actually arrives here is
``TtsRunner(binary, model_dir).synthesize(text, ref_audio=None, out_wav)``
-- plain text in, WAV out, default VibeVoice voice. That call is the
contract this module speaks; everything else is defensive.

VibeVoice-1.5B is still the backend (it is the only any-tts backend
that CAN clone), so an explicit ``ref_audio=...`` does clone -- but
nothing in the mazinger path relies on that, which is why
``require_clone`` defaults to ``False``: the no-reference call is the
normal one, and defaulting the other way made every plain segment
raise ``TtsReferenceAudioError`` (a real integration bug, fixed
2026-10-10). Pass ``require_clone=True`` to get the old guard back --
"a clone was requested, so a missing reference is an error".

CPU-ONLY, LIKE THE BINARY IT DRIVES
-----------------------------------
The ``cuda`` feature of any-tts is never compiled in, and ``tdub_tts --device``
accepts exactly one value. Nothing here adds a flag that could change that.
The precedent is not theoretical: Kaggle ``test4_gotgVERSION`` spent 1067 s of
a 1258 s run pip-installing vLLM + PyTorch + CUDA and then died at ``import``
with ``ImportError: libcudart.so.13``.

ONE MODEL LOAD PER RUN, NOT PER SEGMENT
---------------------------------------
VibeVoice-1.5B is 1.5B parameters. ``load_model`` on it is not a thing to do
once per subtitle line. :meth:`TtsRunner.synthesize_many` writes a segment file,
runs the binary exactly once, and the binary loads the model once and loops.
``test_tts_bridge.py`` asserts the process count, because a regression here is
silent: it just gets slower, and then it does not finish.

NEVER RETURN AN EMPTY WAV
-------------------------
Same rule as ``whispercpp_bridge``: a zero-sample WAV that nobody checks is how
two hours of silence shipped once. Every path here either returns audio that was
verified non-empty on disk, or raises. ``allow_empty=True`` is the escape
hatch, and the binary still reports how many samples it produced.

CLI SURFACE
-----------
    tdub_tts --model <dir> --text <s> --out <wav> [options]
    tdub_tts --model <dir> --text-file <path> --out-dir <dir> [options]
    tdub_tts --model <dir> --probe --json
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "TDUB_TTS",
    "TtsError",
    "TtsBinaryMissing",
    "TtsUsageError",
    "TtsModelError",
    "TtsReferenceAudioError",
    "TtsSynthesisError",
    "TtsOutputError",
    "TtsEmptyAudioError",
    "TtsRunner",
    "available",
    "resolve",
    "search_dirs",
]

#: Canonical name of the binary this module drives.
TDUB_TTS = "tdub_tts"

#: Full path to a binary overrides everything else. Set by hand or by a Kaggle
#: dataset mount; a *directory* is accepted too and probed normally.
ENV_BINARY = "TDUBBER_TTS_BIN"

#: Default model directory, so a notebook preamble does not have to spell it out.
ENV_MODEL_DIR = "TDUBBER_VIBEVOICE_MODEL_DIR"

#: stderr tail attached to every failure. Long enough to contain the actual
#: reason, short enough to not swamp the traceback -- same choice as
#: whispercpp_bridge, for the same reason.
STDERR_TAIL_CHARS = 30000

#: A canonical WAV header is 44 bytes. Anything at or below it is silence.
WAV_HEADER_BYTES = 44

_TTS_DIR = Path(__file__).resolve().parent          # tts_rust/
_REPO_DIR = _TTS_DIR.parent                          # repo root

#: Default per-segment timeout. VibeVoice is a 1.5B diffusion model on a CPU
#: with no GPU anywhere in the pipeline: minutes per segment is the expected
#: order of magnitude, and a timeout that assumes seconds just produces a
#: mystery failure 40 minutes into a job.
DEFAULT_TIMEOUT_SECS = 3600.0

#: Exit codes as defined by ``tdub_tts --help``. Mapping them to distinct
#: classes is the whole point of the binary having a contract: "it failed" is
#: not actionable, "the weights are missing" is.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MODEL_LOAD = 3
EXIT_REF_AUDIO = 4
EXIT_SYNTHESIS = 5
EXIT_WRITE = 6
EXIT_EMPTY_AUDIO = 7


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class TtsError(RuntimeError):
    """Anything that would otherwise become a silent empty WAV."""


class TtsBinaryMissing(TtsError):
    """tdub_tts itself could not be found or started."""


class TtsUsageError(TtsError):
    """Bad arguments -- exit 2. Nothing was loaded and nothing was written."""


class TtsModelError(TtsError):
    """Model weights missing or rejected -- exit 3. Retrying will not help."""


class TtsReferenceAudioError(TtsError):
    """Clone requested but no usable reference audio -- exit 4."""


class TtsSynthesisError(TtsError):
    """The model loaded and then failed to produce audio -- exit 5."""


class TtsOutputError(TtsError):
    """Audio was synthesized but never reached disk -- exit 6."""


class TtsEmptyAudioError(TtsError):
    """Zero samples returned -- exit 7. Never a success unless asked for."""


_EXIT_ERRORS: dict[int, type[TtsError]] = {
    EXIT_USAGE: TtsUsageError,
    EXIT_MODEL_LOAD: TtsModelError,
    EXIT_REF_AUDIO: TtsReferenceAudioError,
    EXIT_SYNTHESIS: TtsSynthesisError,
    EXIT_WRITE: TtsOutputError,
    EXIT_EMPTY_AUDIO: TtsEmptyAudioError,
}


def _error_for(returncode: int) -> type[TtsError]:
    """Typed error for *returncode*, falling back to the base class.

    An unmapped code is still a failure -- a crash inside the binary exits with
    something outside 2..=7 -- but it must not be mistaken for a modelled one."""
    return _EXIT_ERRORS.get(returncode, TtsError)


# --------------------------------------------------------------------------- #
# Binary resolution
# --------------------------------------------------------------------------- #

def _executable_names(stem: str) -> list[str]:
    """Platform spellings of *stem*, most specific first.

    ``.exe`` is probed before the bare name on Windows because a bare
    ``tdub_tts`` there could resolve through PATHEXT to something unrelated."""
    names = [stem]
    if os.name == "nt" and not stem.lower().endswith((".exe", ".bat", ".cmd")):
        names.insert(0, stem + ".exe")
    return names


def search_dirs(extra: Iterable[Path] = ()) -> list[Path]:
    """Directories to probe for ``tdub_tts``, in priority order.

    1. extra              -- test hook, FIRST on purpose
    2. ``TDUBBER_TTS_BIN``  -- operator override (mounted Kaggle dataset)
    3. ``tts_rust/bin/``    -- where a staged/cached copy is placed
    4. ``tts_rust/target/release/`` -- the plain ``cargo build --release`` output
    5. PATH

    *extra* goes first rather than last because of a specific, observed trap:
    once ``cargo build --release`` has actually run, a real ``tdub_tts.exe``
    sits in step 4, and a test that appends its fake there resolves the REAL
    binary instead -- so the suite passes on a bare machine and fails (or worse,
    silently passes for the wrong reason) on the machine that built it.

    The staging directory outranks the cargo target on purpose: a hand-placed
    build is the one an operator chose, and silently using some older local
    compile instead is the failure mode this ordering exists to prevent."""
    dirs: list[Path] = [d for d in extra if d]
    env = os.environ.get(ENV_BINARY, "").strip()
    if env:
        candidate = Path(env)
        # A full path to the binary itself wins outright -- no directory probe.
        if candidate.is_file():
            return [candidate.parent] + dirs
        dirs.extend(Path(part) for part in env.split(os.pathsep) if part)
    dirs.append(_TTS_DIR / "bin")
    dirs.append(_TTS_DIR / "target" / "release")
    return [d for d in dirs if d]


def resolve(name: str = TDUB_TTS, extra: Iterable[Path] = ()) -> str | None:
    """Absolute path to the ``tdub_tts`` executable, or ``None``.

    Returns ``None`` rather than raising: this is called from a notebook
    preamble, and an import-time explosion there hides the real error behind
    a worse one."""
    if name == TDUB_TTS:
        env = os.environ.get(ENV_BINARY, "").strip()
        direct = Path(env)
        if direct.is_file() and os.access(direct, os.X_OK):
            return str(direct)

    names = _executable_names(name)
    for directory in search_dirs(extra):
        try:
            for candidate in names:
                path = Path(directory) / candidate
                if path.is_file() and os.access(path, os.X_OK):
                    return str(path)
        except OSError:
            continue

    for candidate in names:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def available(name: str = TDUB_TTS) -> bool:
    """Whether the binary can be found. Never raises."""
    return resolve(name) is not None


# --------------------------------------------------------------------------- #
# WAV sanity
# --------------------------------------------------------------------------- #

def wav_is_silent(path: str | Path) -> bool:
    """Whether *path* is missing or carries no audio samples.

    Header-only counts as silent. A 44-byte WAV is a valid file that contains
    no sound, which is exactly the shape that a half-finished dub takes."""
    path = Path(path)
    try:
        return not path.is_file() or path.stat().st_size <= WAV_HEADER_BYTES
    except OSError:
        return True


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def _is_tts_item(item: Any) -> bool:
    """Is *item* a mazinger batch item: ``(text, ref_audio, out_wav)``?

    A three-element tuple/list whose first element is the text, whose
    second is a reference path or ``None``, and whose third is the
    destination path. A plain string is a *segment*, not an item, and a
    string of length three is not a tuple -- the type check is what keeps
    the two call shapes apart.
    """
    if isinstance(item, (str, bytes)) or not isinstance(item, (tuple, list)):
        return False
    if len(item) != 3:
        return False
    text, reference, destination = item
    return (
        isinstance(text, str)
        and (reference is None or isinstance(reference, (str, os.PathLike)))
        and isinstance(destination, (str, os.PathLike))
    )


def _is_tts_items(segments: Sequence[Any]) -> bool:
    """Does *segments* carry the mazinger batch shape, not text segments?"""
    return bool(segments) and all(_is_tts_item(item) for item in segments)


class TtsRunner:
    """One loaded model, many segments.

    The runner does not keep the model resident -- a subprocess cannot -- but
    it keeps the *invocation* to one, which is the part that costs. Construct
    it once per dubbing job and call :meth:`synthesize_many`."""

    def __init__(
        self,
        binary: str | Sequence[str],
        model_dir: str | Path,
        *,
        ref_audio: str | Path | None = None,
        require_clone: bool = False,
        language: str | None = None,
        instruct: str | None = None,
        max_tokens: int | None = None,
        device: str = "cpu",
        timeout: float = DEFAULT_TIMEOUT_SECS,
        keep_going: bool = False,
        allow_empty: bool = False,
        extra_args: Iterable[str] = (),
    ) -> None:
        # `require_clone` defaults to FALSE because of the call that actually
        # arrives from mazinger: the rusttts engine is registered clones=False,
        # its wrapper refuses a reference at three gates, and
        # `TtsRunner(binary, model_dir).synthesize(text, ref_audio=None,
        # out_wav)` must work. Defaulting to True made every plain segment
        # raise TtsReferenceAudioError -- a real integration bug, fixed
        # 2026-10-10. An explicit ref_audio still clones (VibeVoice supports
        # it); this only changes what a MISSING reference means. Pass True to
        # get "a clone was requested, so a missing reference is an error" back.
        self.binary = binary
        self.model_dir = Path(model_dir)
        self.ref_audio = Path(ref_audio) if ref_audio else None
        self.require_clone = bool(require_clone)
        self.language = language
        self.instruct = instruct
        self.max_tokens = max_tokens
        self.device = device
        self.timeout = float(timeout)
        self.keep_going = bool(keep_going)
        self.allow_empty = bool(allow_empty)
        self.extra_args = list(extra_args)

        if self.device != "cpu":
            raise TtsUsageError(
                f"device {self.device!r} is not available. tdub_tts is CPU-only by "
                "construction -- no CUDA feature is compiled in, so there is no "
                "GPU backend to fall back to. Passing a GPU here would only "
                "recreate the libcudart.so.13 failure that cost 1067 s."
            )
        if not self.model_dir.is_dir():
            raise TtsModelError(
                f"VibeVoice model directory not found: {self.model_dir}. "
                "Expected a microsoft/VibeVoice-1.5B snapshot (config.json, "
                f"tokenizer.json, model*.safetensors). Set {ENV_MODEL_DIR} or "
                "pass model_dir explicitly."
            )

    # -- argv -------------------------------------------------------------- #

    def _argv_prefix(self) -> list[str]:
        return [self.binary] if isinstance(self.binary, str) else list(self.binary)

    def build_command(
        self,
        *,
        text: str | None = None,
        text_file: Path | None = None,
        out: Path | None = None,
        out_dir: Path | None = None,
        out_prefix: str = "seg",
        ref_audio: Path | None = None,
        language: str | None = None,
        probe: bool = False,
    ) -> list[str]:
        """The exact ``tdub_tts`` command line. No invented flags.

        ``--json`` is always passed: the stdout report is what turns "it exited
        0" into "it wrote 4 WAVs totalling 19.3 s of audio". Optional flags
        stay optional so that a build which lacks one is not aborted by it.

        ``language`` overrides the runner-level ``language`` for this one
        call; mazinger passes the segment's language per call, and a
        constructor-only knob would silently dub every segment in the
        first language it saw.
        """
        argv = self._argv_prefix()
        argv += ["--model", str(self.model_dir), "--device", self.device, "--json"]

        if probe:
            argv.append("--probe")
            return argv

        if text is not None:
            argv += ["--text", text]
        if text_file is not None:
            argv += ["--text-file", str(text_file)]
        if out is not None:
            argv += ["--out", str(out)]
        if out_dir is not None:
            argv += ["--out-dir", str(out_dir), "--out-prefix", out_prefix]

        if ref_audio is not None:
            argv += ["--ref-audio", str(ref_audio)]
        if self.require_clone:
            # Belt and braces: the Python side already refuses to spawn without
            # a reference, but the flag makes the guarantee survive a future
            # caller that reaches the binary directly.
            argv.append("--require-ref-audio")
        effective_language = language if language else self.language
        if effective_language:
            argv += ["--language", effective_language]
        if self.instruct:
            argv += ["--instruct", self.instruct]
        if self.max_tokens:
            argv += ["--max-tokens", str(int(self.max_tokens))]
        if self.keep_going:
            argv.append("--keep-going")
        if self.allow_empty:
            argv.append("--allow-empty")
        argv += [str(arg) for arg in self.extra_args]
        return argv

    # -- execution --------------------------------------------------------- #

    def _resolve_reference(self, ref_audio: str | Path | None) -> Path | None:
        """Pick the reference clip and validate it BEFORE spawning.

        A missing clip is an exit-4 failure the binary would report only after
        loading 1.5B parameters. On a dubbing box that is minutes of wasted
        time to learn something a stat() call already knew."""
        chosen = Path(ref_audio) if ref_audio else self.ref_audio
        if chosen is None:
            if self.require_clone:
                raise TtsReferenceAudioError(
                    "a voice clone was requested but no reference audio was given. "
                    "Pass ref_audio=... to the runner or to synthesize(), or "
                    "construct it with require_clone=False if a default voice is "
                    "genuinely intended. Refusing to guess: a dub in the wrong "
                    "voice is worse than a failed stage."
                )
            return None
        if not chosen.is_file():
            raise TtsReferenceAudioError(
                f"reference audio not found: {chosen}. Voice cloning needs a real "
                "clip -- 3-10 s of clean single-speaker WAV or MP3."
            )
        return chosen

    def _execute(self, argv: Sequence[str], timeout: float | None = None) -> dict[str, Any]:
        """Run the binary and return its parsed ``--json`` report.

        Raises the typed error for the exit code on any non-zero exit, with the
        binary's stderr attached. There is no code path that returns a report
        for a failed run."""
        command = [str(part) for part in argv]
        try:
            result = subprocess.run(  # noqa: S603
                command,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.timeout if timeout is None else float(timeout),
                check=False,
            )
        except FileNotFoundError as exc:
            raise TtsBinaryMissing(
                f"tdub_tts binary not found: {command[0]}\n"
                f"Build it with `cargo build --release` in {str(_TTS_DIR)!r}, "
                f"or set {ENV_BINARY}."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TtsSynthesisError(
                f"tdub_tts timed out after {exc.timeout:.0f}s\n"
                f"--- command ---\n{' '.join(command)}\n"
                f"--- stderr tail ---\n{(exc.stderr or '')[-STDERR_TAIL_CHARS:]}"
            ) from exc

        if result.returncode != 0:
            raise _error_for(result.returncode)(
                f"tdub_tts failed (exit {result.returncode})\n"
                f"--- command ---\n{' '.join(command)}\n"
                f"--- stderr tail ---\n{(result.stderr or '')[-STDERR_TAIL_CHARS:]}"
            )

        # Exit 0 is necessary, not sufficient. A binary that prints nothing and
        # writes nothing has still failed, and reporting success here is the
        # silent-empty-WAV bug wearing a different hat.
        try:
            report = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise TtsSynthesisError(
                f"tdub_tts exited 0 but its --json stdout was not parseable: {exc}\n"
                f"--- stdout ---\n{(result.stdout or '')[-STDERR_TAIL_CHARS:]}"
            ) from exc
        if not isinstance(report, dict):
            raise TtsSynthesisError(
                "tdub_tts --json stdout was "
                f"{type(report).__name__}, expected a JSON object"
            )
        return report

    # -- public API -------------------------------------------------------- #

    def probe(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Load the model and report its metadata without synthesizing.

        This is the feasibility check: it answers "does this box have the RAM,
        the weights and a working CPU backend" in one process, and it costs one
        load instead of one load per segment.

        No reference clip is required: ``--probe`` is about the model, and
        ``build_command`` returns before ``--require-ref-audio`` is appended."""
        return self._execute(self.build_command(probe=True), timeout=timeout)

    def synthesize(
        self,
        text: str,
        out_wav: str | Path,
        *,
        ref_audio: str | Path | None = None,
        language: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Speak *text* into *out_wav*.

        ``language`` is the per-call override mazinger sends (an ISO code
        such as ``"hi"``); it beats the constructor-level ``language``.

        Returns the binary's JSON segment record. Raises rather than returning
        an empty WAV."""
        text = (text or "").strip()
        if not text:
            raise TtsUsageError("refusing to synthesize empty text")
        reference = self._resolve_reference(ref_audio)
        out_wav = Path(out_wav)
        out_wav.parent.mkdir(parents=True, exist_ok=True)

        report = self._execute(
            self.build_command(text=text, out=out_wav, ref_audio=reference,
                               language=language),
            timeout=timeout,
        )
        segments = report.get("segments") or []
        if len(segments) != 1:
            raise TtsSynthesisError(
                f"expected exactly one segment from a single-text run, got "
                f"{len(segments)}"
            )
        self._verify_wav(out_wav, segments[0])
        return dict(segments[0], report=report)

    def synthesize_many(
        self,
        segments: Sequence[Any] | None = None,
        out_dir: str | Path | None = None,
        *,
        items: Sequence[Any] | None = None,
        prefix: str = "seg",
        ref_audio: str | Path | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """Speak many segments with **one** invocation and **one** model load.

        This is the reason the binary exists in this shape. Two call shapes
        are accepted, and which one is used is decided by the arguments,
        not by a flag:

        * **items** -- ``[(text, ref_audio, out_wav), ...]``: the mazinger
          batch contract. Every item names its own destination, so the
          segment files the binary writes into a scratch directory are
          moved to their item paths afterwards. This is the shape
          ``_RustTTSWrapper.synthesize_batch`` sends.
        * **segments** -- strings or mappings carrying a ``text`` key,
          written as ``<out_dir>/<prefix>-NNNN.wav``.

        Blank segments are rejected rather than skipped: a caller that built a
        list of 400 and got 399 back has a bug in the caller, and the binary
        silently dropping them would hide it."""
        if items is not None:
            return self._synthesize_items(items, ref_audio=ref_audio,
                                          prefix=prefix, timeout=timeout)
        if segments is not None and _is_tts_items(segments):
            # The positional fallback mazinger uses when its keyword mapping
            # cannot name the parameters: `many([(text, ref, out), ...])`.
            return self._synthesize_items(segments, ref_audio=ref_audio,
                                          prefix=prefix, timeout=timeout)
        if segments is None or out_dir is None:
            raise TtsUsageError(
                "synthesize_many needs either items=[(text, ref_audio, "
                "out_wav), ...] or (segments, out_dir)"
            )
        rows = self._coerce_segments(segments)
        if not rows:
            raise TtsUsageError("no segments to synthesize")
        reference = self._resolve_reference(ref_audio)

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        work_dir = Path(tempfile.mkdtemp(prefix="tdub-tts-"))
        try:
            text_file = work_dir / "segments.txt"
            # One line per segment, written with an explicit newline so a file
            # with no trailing newline does not lose its last segment.
            text_file.write_text(
                "\n".join(text for _, text in rows) + "\n", encoding="utf-8"
            )
            argv = self.build_command(
                text_file=text_file, out_dir=out_dir, out_prefix=prefix,
                ref_audio=reference,
            )
            # The per-segment timeout scales: a 400-segment dub on CPU is a
            # multi-hour job and a flat 1 h cap would kill it at segment ~40.
            budget = timeout if timeout is not None else self.timeout * len(rows)
            report = self._execute(argv, timeout=budget)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        by_index = {int(s.get("index", -1)): s for s in report.get("segments") or []}
        results: list[dict[str, Any]] = []
        for index, text in rows:
            record = by_index.get(index)
            if record is None:
                raise TtsSynthesisError(
                    f"tdub_tts reported success but segment {index} "
                    f"(\"{text[:60]}\") is absent from its --json report. "
                    f"It did report these indices: {sorted(by_index)}"
                )
            destination = out_dir / f"{prefix}-{index:04d}.wav"
            self._verify_wav(destination, record)
            results.append(dict(record, text=text, out=str(destination)))
        return results

    def _synthesize_items(
        self,
        items: Sequence[Any],
        *,
        ref_audio: str | Path | None,
        prefix: str,
        timeout: float | None,
    ) -> list[dict[str, Any]]:
        """The mazinger batch contract: one load, per-item destinations.

        The binary writes ``<prefix>-NNNN.wav`` into a scratch directory
        (segment *i* of the text file is segment *i* of *items*), and each
        file is moved to its item's destination while the scratch directory
        still exists. One invocation, one model load -- the alternative, one
        invocation per item, reloads a 1.5B checkpoint per subtitle line.
        """
        rows: list[tuple[int, str, Path]] = []
        for position, item in enumerate(items):
            if not _is_tts_item(item):
                raise TtsUsageError(
                    f"item {position} is not a (text, ref_audio, out_wav) "
                    f"triple: {item!r}"
                )
            text, reference, destination = item
            text = str(text or "").strip()
            if not text:
                raise TtsUsageError(
                    f"item {position} is blank; refusing to drop it silently "
                    "(the caller built the list, the caller should know)"
                )
            rows.append((position, text, Path(destination)))
        if not rows:
            raise TtsUsageError("no segments to synthesize")

        # One invocation decodes ONE reference clip, so the items must
        # agree: every present reference the same path, or none at all.
        # (The mazinger path never sends one -- its rusttts engine refuses
        # clone requests at three gates -- but a direct caller might.)
        references = {
            str(item[1]) for item in items
            if isinstance(item, (tuple, list)) and len(item) == 3 and item[1]
        }
        if len(references) > 1:
            raise TtsReferenceAudioError(
                "batch items carry different reference clips "
                f"({sorted(references)}); one invocation decodes exactly one. "
                "Clone a batch item-by-item instead, or pass one shared "
                "ref_audio= for the whole batch."
            )
        item_reference = references.pop() if references else None
        # An explicit kwarg wins over whatever the items carry.
        reference = self._resolve_reference(ref_audio or item_reference)

        work_dir = Path(tempfile.mkdtemp(prefix="tdub-tts-"))
        try:
            text_file = work_dir / "segments.txt"
            text_file.write_text(
                "\n".join(text for _, text, _ in rows) + "\n",
                encoding="utf-8",
            )
            scratch = work_dir / "wav"
            scratch.mkdir()
            argv = self.build_command(
                text_file=text_file, out_dir=scratch, out_prefix=prefix,
                ref_audio=reference,
            )
            budget = timeout if timeout is not None else self.timeout * len(rows)
            report = self._execute(argv, timeout=budget)

            by_index = {
                int(s.get("index", -1)): s for s in report.get("segments") or []
            }
            results: list[dict[str, Any]] = []
            for index, text, destination in rows:
                record = by_index.get(index)
                if record is None:
                    raise TtsSynthesisError(
                        f"tdub_tts reported success but item {index} "
                        f"(\"{text[:60]}\") is absent from its --json report. "
                        f"It did report these indices: {sorted(by_index)}"
                    )
                source = scratch / f"{prefix}-{index:04d}.wav"
                destination.parent.mkdir(parents=True, exist_ok=True)
                # shutil.move, not os.replace: the scratch directory and the
                # destination may be on different filesystems.
                shutil.move(str(source), str(destination))
                self._verify_wav(destination, record)
                results.append(dict(record, text=text, out=str(destination)))
            return results
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    # -- helpers ----------------------------------------------------------- #

    @staticmethod
    def _coerce_segments(segments: Sequence[Any]) -> list[tuple[int, str]]:
        rows: list[tuple[int, str]] = []
        for position, segment in enumerate(segments):
            if isinstance(segment, Mapping):
                text = str(segment.get("text") or "")
            else:
                text = str(segment)
            text = text.strip()
            if not text:
                raise TtsUsageError(
                    f"segment {position} is blank; refusing to drop it silently "
                    "(the caller built the list, the caller should know)"
                )
            rows.append((position, text))
        return rows

    def _verify_wav(self, path: Path, record: Mapping[str, Any]) -> None:
        """Assert the WAV exists and actually contains audio.

        The binary already refuses to write a silent one, so this is the second
        lock on the same door -- it catches a truncated write, a full disk, and
        a stale file from a previous run that the current one never replaced."""
        if wav_is_silent(path):
            raise TtsEmptyAudioError(
                f"tdub_tts reported {record.get('samples', '?')} samples for "
                f"{path}, but that file is missing or header-only. Refusing to "
                "return it: the pipeline would dub silence for hours and every "
                "stage after this one would report success."
            )


def default_model_dir() -> Path | None:
    """Model directory from ``TDUBBER_VIBEVOICE_MODEL_DIR``, or ``None``."""
    env = os.environ.get(ENV_MODEL_DIR, "").strip()
    return Path(env) if env else None