#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Drive ``whisper-cli`` and return what mazinger's ASR stage expects.

    from whispercpp_bridge import WhisperCppRunner
    runner = WhisperCppRunner(resolve("whisper-cli"), "ggml-base.bin")
    result = runner.transcribe_wav("stage1.wav")   # -> wav is produced if needed
    result["text"], result["segments"], result["language"]

WHY THIS EXISTS
---------------
mazinger transcribes with faster-whisper today, which drags in ctranslate2 +
onnxruntime + a wheel set -- the same class of dependency that made the vLLM
install cost 1067 s and then crash. whisper.cpp is one static-ish binary and a
~150 MB model, so the stage becomes "download once, run forever".

NEVER RETURN AN EMPTY TRANSCRIPT
--------------------------------
The pipeline's cost is dominated by what happens *after* a bad transcript: an
empty ASR result still produces a two-hour dub of nothing, and every stage
downstream succeeds. So this module raises :class:`WhisperCppError` on a missing
output, an unparseable output, a non-zero exit, or a genuinely empty result
(pass ``allow_empty=True`` if silence really is the expected answer). Failure is
loud here so it is loud 20 minutes later instead of silent.

CLI SURFACE (whisper.cpp flags, all verified to exist upstream)
--------------------------------------------------------------
    whisper-cli -m <model> -f <wav> -oj -of <prefix> [-l <lang>] [-t <threads>]
               [--vad] [--vad-threshold <f>] [--max-len <n>]

``-oj`` writes ``<prefix>.json`` (structured) and whisper.cpp also writes
``<prefix>.srt``; both are parsed, JSON first, SRT as the fallback. Optional
flags are passed only when the caller asks -- an unknown flag aborts the binary
before it reads a single sample, and a hard-coded ``--vad`` that a given build
does not have would cost the whole stage.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    from build_runtimes import canonical_name, resolve as _resolve_runtime
except ImportError:  # pragma: no cover - only when copied out on its own
    canonical_name = None
    _resolve_runtime = None

__all__ = [
    "WHISPER_CLI",
    "WhisperCppError",
    "WhisperCppRunner",
    "available",
    "convert_to_wav",
    "parse_srt",
    "parse_whisper_json",
    "resolve",
]

#: Canonical name of the binary this module drives.
WHISPER_CLI = "whisper-cli"

#: stderr tail attached to every failure. Long enough to contain the actual
#: reason ("failed to load model", "unknown argument"), short enough to not
#: swamp the traceback.
STDERR_TAIL_CHARS = 30000

_SRT_TIME = re.compile(
    r"(?P<h>\d{1,3}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})"
)


class WhisperCppError(RuntimeError):
    """Anything that would otherwise become a silent empty transcript."""


# --------------------------------------------------------------------------- #
# Binary resolution
# --------------------------------------------------------------------------- #

def resolve(name: str = WHISPER_CLI) -> str | None:
    """Absolute path to a whisper.cpp binary, or ``None``.

    Accepts the legacy names ``whisper-cpp`` and ``main``; they map onto the
    same canonical ``whisper-cli``."""
    if _resolve_runtime is None:
        return None
    path = _resolve_runtime(name)
    return str(path) if path else None


def available(name: str = WHISPER_CLI) -> bool:
    return resolve(name) is not None


# --------------------------------------------------------------------------- #
# Timestamp parsing
# --------------------------------------------------------------------------- #

def _seconds_from_timestamp(value: Any, unit: str = "ms") -> float | None:
    """Accept every timestamp shape whisper.cpp has ever emitted.

    ``00:00:02,500`` (SRT), ``00:00:02.500`` (some builds), ``2500`` (offsets
    milliseconds) and ``2.5`` (already seconds). Guessing wrong here puts every
    subtitle cue in the wrong place, which is worse than a parse error because
    nothing looks broken.

    *unit* is explicit because the two number conventions disagree by 1000x:
    whisper.cpp's ``offsets`` are milliseconds, an OpenAI-shaped ``start`` is
    seconds. A float cannot be told apart from an int by value alone, so the
    caller has to say which convention it is reading."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if unit == "ms" else float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return float(text) / 1000.0 if unit == "ms" else float(text)
    match = _SRT_TIME.search(text)
    if match:
        ms = match.group("ms").ljust(3, "0")
        return (int(match.group("h")) * 3600 + int(match.group("m")) * 60
                + int(match.group("s")) + int(ms) / 1000.0)
    try:
        return float(text)
    except ValueError:
        return None


def parse_srt(text: str) -> list[dict[str, Any]]:
    """Parse SRT into mazinger's segment shape.

    Hand-rolled rather than done with a library: SRT is a fixed, tiny grammar
    and adding a dependency to replace 40 lines would recreate the exact problem
    this migration exists to remove."""
    segments: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        arrow = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if arrow is None:
            continue
        left, _, right = lines[arrow].partition("-->")
        start = _seconds_from_timestamp(left)
        end = _seconds_from_timestamp(right.split()[0] if right.split() else right)
        body = " ".join(line.strip() for line in lines[arrow + 1:]).strip()
        if start is None or end is None:
            continue
        segments.append({"start": float(start), "end": float(end), "text": body})
    return segments


def parse_whisper_json(payload: Any) -> tuple[str, list[dict[str, Any]], str]:
    """Return ``(text, segments, language)`` from a whisper.cpp ``-oj`` file.

    The JSON layout has changed twice upstream -- a top-level ``transcription``
    array in current builds, ``segments`` in a couple of forks, a bare list in
    others -- so all three are accepted. Every field is optional; what is not
    optional is that an unrecognised shape raises instead of returning nothing.
    """
    if isinstance(payload, list):
        rows, language, text = payload, "", ""
    elif isinstance(payload, dict):
        rows = payload.get("transcription") or payload.get("segments") or []
        result = payload.get("result")
        if isinstance(result, dict):
            language = str(result.get("language", "") or "")
        else:
            language = str(payload.get("language", "") or "")
        text = str(payload.get("text", "") or "")
    else:
        raise WhisperCppError(
            f"unexpected whisper.cpp JSON root of type {type(payload).__name__}"
        )
    if not isinstance(rows, list):
        raise WhisperCppError("whisper.cpp JSON 'transcription' is not a list")

    segments: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        offsets = row.get("offsets") or {}
        stamps = row.get("timestamps") or {}
        start = _seconds_from_timestamp(
            offsets.get("from") if isinstance(offsets, dict) else None)
        end = _seconds_from_timestamp(
            offsets.get("to") if isinstance(offsets, dict) else None)
        if start is None:
            start = _seconds_from_timestamp(
                stamps.get("from") if isinstance(stamps, dict) else None)
        if end is None:
            end = _seconds_from_timestamp(
                stamps.get("to") if isinstance(stamps, dict) else None)
        if start is None:
            start = _seconds_from_timestamp(row.get("start"), unit="s")
        if end is None:
            end = _seconds_from_timestamp(row.get("end"), unit="s")
        body = str(row.get("text", "") or "").strip()
        if start is None or end is None:
            # A cue with no timing is unusable for re-segmentation; keeping it
            # would silently collapse every subtitle to zero length.
            continue
        segments.append({"start": float(start), "end": float(end), "text": body})

    if not text:
        text = " ".join(seg["text"] for seg in segments).strip()
    return text, segments, language


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #

def convert_to_wav(
    source: str | Path,
    dest: str | Path | None = None,
    *,
    ffmpeg: str = "ffmpeg",
    sample_rate: int = 16000,
    timeout: float = 1800.0,
) -> Path:
    """Decode *source* to 16 kHz mono PCM16 WAV.

    whisper.cpp only reads WAV, and the pipeline hands us mp4/mkv. 16 kHz mono
    is what the model was trained on; anything else resampled later costs
    accuracy, and whisper.cpp's own resampler is a poor one."""
    source = Path(source)
    if not source.is_file():
        raise WhisperCppError(f"input audio not found: {source}")
    if dest is None:
        dest = source.with_suffix(".16k.wav")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-vn",
        "-ac", "1", "-ar", str(int(sample_rate)),
        "-c:a", "pcm_s16le", str(dest),
    ]
    try:
        result = subprocess.run(  # noqa: S603
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise WhisperCppError(
            f"{ffmpeg} not found on PATH; cannot decode {source.name}. "
            "Install ffmpeg or hand transcribe_wav() an already-decoded WAV."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WhisperCppError(
            f"ffmpeg timed out after {timeout:.0f}s on {source}"
        ) from exc
    if result.returncode != 0 or not dest.is_file():
        raise WhisperCppError(
            f"ffmpeg failed on {source} (exit {result.returncode})\n"
            f"{(result.stderr or '')[-STDERR_TAIL_CHARS:]}"
        )
    if dest.stat().st_size <= 44:  # 44 = canonical WAV header, nothing else
        raise WhisperCppError(f"ffmpeg produced an empty WAV for {source}")
    return dest


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

class WhisperCppRunner:
    """One whisper.cpp invocation, returning mazinger's transcript shape."""

    def __init__(
        self,
        binary: str | Sequence[str],
        model_path: str | Path,
        language: str | None = None,
        threads: int | None = None,
        *,
        ffmpeg: str = "ffmpeg",
        timeout: float = 3600.0,
        extra_args: Iterable[str] = (),
    ) -> None:
        self.binary = binary
        self.model_path = Path(model_path)
        self.language = language
        self.threads = threads
        self.ffmpeg = ffmpeg
        self.timeout = float(timeout)
        self.extra_args = list(extra_args)
        if not self.model_path.is_file():
            raise WhisperCppError(
                f"whisper.cpp model not found: {self.model_path}. "
                "Download e.g. ggml-base.bin once; do not rebuild it per run."
            )

    # -- command ---------------------------------------------------------- #

    def build_command(
        self,
        wav_path: Path,
        out_prefix: Path,
        *,
        language: str | None = None,
        threads: int | None = None,
        vad: bool = False,
        vad_threshold: float | None = None,
        max_len: int | None = None,
    ) -> list[str]:
        """The exact ``whisper-cli`` command line. No invented flags.

        ``-oj`` (output JSON) and ``-of <prefix>`` are what make the output
        parseable at all; without ``-of`` the files land in CWD under the audio
        file's stem and two jobs in the same directory overwrite each other."""
        argv = [self.binary] if isinstance(self.binary, str) else list(self.binary)
        argv += ["-m", str(self.model_path), "-f", str(wav_path), "-oj",
                 "-of", str(out_prefix)]
        lang = language if language is not None else self.language
        if lang:
            argv += ["-l", str(lang)]
        threads_used = threads if threads is not None else self.threads
        if threads_used:
            argv += ["-t", str(int(threads_used))]
        if vad:
            argv.append("--vad")
        if vad_threshold is not None:
            argv += ["--vad-threshold", str(vad_threshold)]
        if max_len is not None:
            argv += ["--max-len", str(int(max_len))]
        argv += [str(arg) for arg in self.extra_args]
        return argv

    # -- transcription ---------------------------------------------------- #

    def transcribe_wav(
        self,
        wav_path: str | Path,
        *,
        language: str | None = None,
        threads: int | None = None,
        out_dir: str | Path | None = None,
        vad: bool = False,
        vad_threshold: float | None = None,
        max_len: int | None = None,
        allow_empty: bool = False,
        cleanup: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Transcribe *wav_path* (any audio file: non-WAV is decoded first).

        Returns ``{"text": str, "segments": [...], "language": str}``.
        """
        wav_path = Path(wav_path)
        if wav_path.suffix.lower() != ".wav":
            wav_path = convert_to_wav(wav_path, ffmpeg=self.ffmpeg)
        if not wav_path.is_file():
            raise WhisperCppError(f"input audio not found: {wav_path}")

        work_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="whispercpp-"))
        work_dir.mkdir(parents=True, exist_ok=True)
        # A unique prefix per call: the output filename is derived from it, and
        # a fixed one makes two concurrent jobs (or a retry) clobber each other.
        out_prefix = work_dir / f"{wav_path.stem}-{uuid.uuid4().hex[:8]}"
        command = self.build_command(
            wav_path, out_prefix, language=language, threads=threads,
            vad=vad, vad_threshold=vad_threshold, max_len=max_len,
        )
        try:
            result = subprocess.run(  # noqa: S603
                [str(part) for part in command],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.timeout if timeout is None else float(timeout), check=False,
            )
        except FileNotFoundError as exc:
            raise WhisperCppError(
                f"whisper.cpp binary not found: {command[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise WhisperCppError(
                f"whisper-cli timed out after {exc.timeout:.0f}s on {wav_path.name}"
                f"\n--- stderr tail ---\n{(exc.stderr or '')[-STDERR_TAIL_CHARS:]}"
            ) from exc

        if result.returncode != 0:
            raise WhisperCppError(
                f"whisper-cli failed on {wav_path.name} (exit {result.returncode})\n"
                f"--- stderr tail ---\n{(result.stderr or '')[-STDERR_TAIL_CHARS:]}"
            )

        try:
            return self._collect(out_prefix, language=language,
                                 allow_empty=allow_empty, cleanup=cleanup,
                                 work_dir=work_dir)
        finally:
            if cleanup and out_dir is None:
                _rmtree_quiet(work_dir)

    def _collect(
        self,
        out_prefix: Path,
        *,
        language: str | None,
        allow_empty: bool,
        cleanup: bool,
        work_dir: Path,
    ) -> dict[str, Any]:
        """JSON first, SRT second.

        JSON carries offsets in milliseconds and is what re-segmentation needs;
        SRT is the human-readable fallback that has been in whisper.cpp since
        day one, so a build that drops ``-oj`` still yields a usable transcript
        rather than an exception with no output to show."""
        json_path = Path(str(out_prefix) + ".json")
        srt_path = Path(str(out_prefix) + ".srt")
        detected = ""
        text, segments = "", []

        if json_path.is_file():
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
                text, segments, detected = parse_whisper_json(payload)
            except (json.JSONDecodeError, WhisperCppError) as exc:
                if srt_path.is_file():
                    segments = parse_srt(srt_path.read_text(encoding="utf-8", errors="replace"))
                    text = " ".join(seg["text"] for seg in segments).strip()
                else:
                    raise WhisperCppError(
                        f"unreadable whisper.cpp JSON at {json_path}: {exc}"
                    ) from exc
        elif srt_path.is_file():
            segments = parse_srt(srt_path.read_text(encoding="utf-8", errors="replace"))
            text = " ".join(seg["text"] for seg in segments).strip()
        else:
            raise WhisperCppError(
                f"whisper-cli wrote no .json and no .srt next to {out_prefix} "
                f"(exit 0). Directory now holds: "
                f"{sorted(p.name for p in work_dir.glob(str(out_prefix.name) + '*'))}"
            )

        if not text and not allow_empty:
            raise WhisperCppError(
                f"whisper-cli produced an empty transcript for "
                f"{json_path.name if json_path.is_file() else srt_path.name}. "
                "Refusing to return it: the pipeline would dub silence for hours."
            )
        return {
            "text": text,
            "segments": segments,
            "language": detected or (language if language is not None else self.language) or "",
        }


def _rmtree_quiet(path: Path) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass