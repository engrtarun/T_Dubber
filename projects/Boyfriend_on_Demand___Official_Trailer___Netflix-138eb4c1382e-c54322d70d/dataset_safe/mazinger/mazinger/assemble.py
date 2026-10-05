"""Time-align TTS segments and assemble the final dubbed audio track."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from collections import deque
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import Any, Callable, Iterable, Iterator

import numpy as np
import soundfile as sf
from tqdm.auto import tqdm

from mazinger.utils import get_audio_duration

log = logging.getLogger(__name__)

TARGET_SR = 24_000

# Extra silence appended after *original_duration* so the last segment is
# never hard-clipped.  Shared by both the Rust and the Python path.
TAIL_PAD_SEC = 2.0

# Segments prepared (loaded, tempo-stretched, trimmed) in parallel by
# assemble_timeline.  Most of that time is spent waiting on ffmpeg.
ASSEMBLE_WORKERS = min(8, os.cpu_count() or 1)

# Formats _load_and_resample reads without ffmpeg.  Lossy formats always go
# through ffmpeg: decoders disagree on encoder-delay padding.
_DIRECT_FORMATS = ("WAV", "WAVEX", "RF64", "FLAC", "AIFF")


def _load_and_resample(wav_path: str, target_sr: int) -> np.ndarray:
    """Load an audio file as mono float32 at *target_sr*.

    Lossless mono files already at *target_sr* (the TTS segments) are read
    directly, which is ~100× faster than starting ffmpeg and gives the same
    samples; anything else is converted with ffmpeg.
    """
    try:
        info = sf.info(wav_path)
    except Exception as exc:  # noqa: BLE001 — not a format soundfile reads
        log.debug("soundfile cannot probe %s: %s", wav_path, exc)
        info = None
    if (info is not None and info.samplerate == target_sr and info.channels == 1
            and info.format in _DIRECT_FORMATS):
        data, _ = sf.read(wav_path, dtype="float32")
        return data

    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", wav_path,
            "-ar", str(target_sr), "-ac", "1", "-f", "f32le", "-",
        ],
        capture_output=True,
        check=True,
    )
    return np.frombuffer(result.stdout, dtype=np.float32)


def _tempo_stretch(
    wav_path: str,
    factor: float,
    out_path: str,
    sr: int,
) -> np.ndarray:
    """Change playback speed by *factor* using the ffmpeg ``atempo`` filter.

    ``factor > 1`` speeds up, ``factor < 1`` slows down.
    """
    if not (factor > 0) or not np.isfinite(factor):
        raise ValueError(f"tempo factor must be a positive finite number, got {factor!r}")
    filters: list[str] = []
    remaining = factor
    while remaining > 100.0:
        filters.append("atempo=100.0")
        remaining /= 100.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.6f}")

    subprocess.run(
        [
            "ffmpeg", "-y", "-i", wav_path,
            "-filter:a", ",".join(filters),
            "-ar", str(sr), "-ac", "1", out_path,
        ],
        capture_output=True,
        check=True,
    )
    data, _ = sf.read(out_path, dtype="float32")
    return data


def _fade(
    audio: np.ndarray,
    sr: int,
    fade_in_ms: int = 15,
    fade_out_ms: int = 50,
) -> np.ndarray:
    """Apply a raised-cosine (Hann) fade-in/out for natural-sounding edges.

    Short fade-in (default 15 ms) prevents clicks; longer fade-out
    (default 50 ms) mirrors how speech naturally trails off.
    """
    audio = audio.copy()
    n = len(audio)

    fi = min(int(sr * fade_in_ms / 1000), n // 2)
    if fi >= 2:
        # Hann fade-in: 0.5 * (1 - cos(pi * t))  — starts gentle, ends steep
        ramp_in = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, fi))).astype(np.float32)
        audio[:fi] *= ramp_in

    fo = min(int(sr * fade_out_ms / 1000), n // 2)
    if fo >= 2:
        ramp_out = 0.5 * (1.0 + np.cos(np.linspace(0.0, np.pi, fo))).astype(np.float32)
        audio[-fo:] *= ramp_out

    return audio


def _rms_energy(audio: np.ndarray, frame_len: int) -> np.ndarray:
    """Compute per-frame RMS energy (non-overlapping windows)."""
    n_frames = len(audio) // frame_len
    if n_frames == 0:
        return np.array([0.0], dtype=np.float32)
    trimmed = audio[: n_frames * frame_len].reshape(n_frames, frame_len)
    return np.sqrt(np.mean(trimmed ** 2, axis=1))


def _find_last_silence(audio: np.ndarray, sr: int, budget_samps: int,
                        silence_thresh_db: float = -40.0) -> int:
    """Find the last silence boundary before *budget_samps*.

    Returns a sample index where the audio can be safely trimmed without
    cutting through voiced speech.  Falls back to the lowest-energy frame
    in the search range to minimise audible cuts.
    """
    frame_len = int(sr * 0.02)  # 20 ms frames
    energy = _rms_energy(audio, frame_len)
    thresh = 10 ** (silence_thresh_db / 20.0)
    budget_frame = min(budget_samps // frame_len, len(energy))

    # Search backwards over 80% of the budget range (not just 50%)
    search_floor = max(int(budget_frame * 0.2), 1)

    # Walk backwards from the budget boundary to find a silent frame
    for i in range(budget_frame - 1, search_floor, -1):
        if energy[i] < thresh:
            return (i + 1) * frame_len

    # No silence found — fall back to the lowest-energy frame in the range
    # so we at least cut at the quietest point rather than at an arbitrary
    # boundary that may be mid-vowel.
    search_region = energy[search_floor:budget_frame]
    if len(search_region) > 0:
        min_idx = int(np.argmin(search_region)) + search_floor
        return (min_idx + 1) * frame_len

    return budget_samps


def _speech_density(audio: np.ndarray, sr: int,
                     silence_thresh_db: float = -40.0) -> float:
    """Fraction of frames containing voiced speech (0.0–1.0)."""
    frame_len = int(sr * 0.02)
    energy = _rms_energy(audio, frame_len)
    thresh = 10 ** (silence_thresh_db / 20.0)
    if len(energy) == 0:
        return 1.0
    return float(np.mean(energy >= thresh))


def _ordered_map(
    pool: Executor, fn: Callable, items: Iterable, window: int,
) -> Iterator:
    """``pool.map(fn, items)`` with at most *window* results in flight.

    Results come back in order; memory stays bounded however many items
    there are (``Executor.map`` submits everything up front).
    """
    pending: deque = deque()
    try:
        for item in items:
            pending.append(pool.submit(fn, item))
            if len(pending) >= window:
                yield pending.popleft().result()
        while pending:
            yield pending.popleft().result()
    finally:
        for fut in pending:
            fut.cancel()


# Whole-timeline scans run over blocks of this many samples.  A 2 h timeline
# is ~690 MB; full-array temporaries (np.abs, np.nonzero's int64 indices)
# would multiply its peak memory several times over.
_SCAN_BLOCK = 1 << 20


def _last_nonzero(a: np.ndarray) -> int:
    """Index of the last non-zero element of *a*, or ``-1`` if all zero."""
    for hi in range(len(a), 0, -_SCAN_BLOCK):
        lo = max(0, hi - _SCAN_BLOCK)
        nz = np.flatnonzero(a[lo:hi])
        if len(nz):
            return lo + int(nz[-1])
    return -1


def _peak_abs(a: np.ndarray) -> float:
    """``np.max(np.abs(a))`` without a full-size temporary."""
    peak = 0.0
    for lo in range(0, len(a), _SCAN_BLOCK):
        block = a[lo:lo + _SCAN_BLOCK]
        peak = max(peak, float(block.max()), -float(block.min()))
    return peak


# ---------------------------------------------------------------------------
# Rust stitcher (fast path)
# ---------------------------------------------------------------------------
# The Rust ``stitcher`` mixes the timeline natively: one pre-sized buffer,
# voice segments overlaid at their SRT times, and the background ducked
# under them.  It is several times faster and holds far less memory than the
# numpy path, which is why it is preferred whenever the binary is present.
#
# Binary discovery, in order:
#   1. ``STITCHER_BIN``           — explicit path (or a bare name on PATH)
#   2. ``<repo>/stitcher/target/release/stitcher[.exe]``
#   3. ``stitcher`` on PATH
#
# ``MAZINGER_STITCHER`` overrides the choice:
#   ``off`` / ``0`` / ``false`` — never use Rust (force the numpy path)
#   anything else set          — use Rust when available
_STITCHER_ENV = "MAZINGER_STITCHER"
_STITCHER_BIN_ENV = "STITCHER_BIN"
_FALSEY = ("0", "off", "false", "no", "never")

_stitcher_binary_cache: dict[str, Any] = {}


def _repo_root() -> str:
    """The workspace root holding ``mazinger/``, ``stitcher/`` and ``cpp_accelerator/``.

    ``__file__`` is ``<root>/mazinger/mazinger/assemble.py``, so this is
    three parent hops up.  Exactly three: a fourth lands on the *parent* of
    the workspace and every bundled binary silently stops being found.
    """
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _stitcher_binary() -> str | None:
    """Locate the Rust ``stitcher`` binary, or ``None`` when unavailable.

    The lookup is cached because it runs once per timeline build, and the
    answer cannot change while the process is alive.
    """
    if "path" in _stitcher_binary_cache:
        return _stitcher_binary_cache["path"]

    candidates: list[str] = []
    override = os.environ.get(_STITCHER_BIN_ENV, "").strip()
    if override:
        # An explicit override that does not exist is reported, never
        # silently replaced by a different binary: that surprise costs an
        # afternoon of "why is the output wrong".
        if os.path.isfile(override):
            candidates.append(override)
        else:
            resolved = shutil.which(override)
            if resolved:
                candidates.append(resolved)
            else:
                log.warning(
                    "%s=%s does not exist; ignoring it.", _STITCHER_BIN_ENV, override
                )

    repo_root = _repo_root()
    exe = "stitcher.exe" if sys.platform == "win32" else "stitcher"
    for build in ("release", "debug"):
        candidate = os.path.join(repo_root, "stitcher", "target", build, exe)
        if os.path.isfile(candidate):
            candidates.append(candidate)

    on_path = shutil.which("stitcher")
    if on_path:
        candidates.append(on_path)

    found = candidates[0] if candidates else None
    if found:
        log.debug("Rust stitcher: %s", found)
    _stitcher_binary_cache["path"] = found
    return found


def _rust_stitcher_wanted(use_rust: bool | None) -> bool:
    """Decide whether the Rust path may be used for this call."""
    setting = os.environ.get(_STITCHER_ENV, "").strip().lower()
    if setting in _FALSEY:
        if use_rust:
            log.info("Rust stitcher disabled by %s; using the numpy path.", _STITCHER_ENV)
        return False
    if use_rust is False:
        return False
    return True


def _segment_window(
    seg: dict, seg_index: int, valid: list[dict],
    original_duration: float,
) -> tuple[float, float] | None:
    """The widest time window a segment may occupy, in seconds.

    These are the numpy path's *hard trim* boundaries, so both engines cut
    the audio in the same places:

    * not the last segment — up to the next segment's start.  The numpy
      path computes ``max_len = next_start_samp - start_samp`` there and
      trims anything longer; ``segment_gap_ms`` only feeds its tempo
      budget, never the trim, so it is deliberately not applied here.
    * the last segment — up to the end of the timeline, which is
      ``original_duration + TAIL_PAD_SEC``, matching the numpy path's
      ``budget + tail_pad`` allowance.

    The window is an upper bound, not a target: audio shorter than it plays
    in full, so making it as large as is safe never trims more than
    necessary.  ``None`` means no room at all, which the numpy path treats
    as a zero-length trim.
    """
    start = seg["start"]
    if seg_index + 1 >= len(valid):
        limit = original_duration + TAIL_PAD_SEC
    else:
        limit = valid[seg_index + 1]["start"]
    window = limit - start
    if window <= 0:
        return None
    return start, limit


def _write_timeline_json(
    segment_info: list[dict],
    original_duration: float,
    output_path: str,
    *,
    background_audio: str,
    background_volume: float,
    json_path: str,
) -> dict | None:
    """Write the ``timeline.json`` the Rust binary consumes.

    Returns the manifest, or ``None`` when there is nothing to mix — an
    empty segment list would only produce a silent file, and the numpy
    path is cheaper for that case.
    """
    segments: list[dict] = []
    skipped = 0
    # The window of each segment depends on its *neighbours*, so the list
    # must be filtered and sorted first and indexed in that order —
    # otherwise "is this the last segment?" is answered against the wrong
    # list and the final segment gets a budget measured from its neighbour.
    valid = sorted(
        (s for s in segment_info if s.get("wav_path")),
        key=lambda s: s["start"],
    )
    for seg_index, seg in enumerate(valid):
        window = _segment_window(seg, seg_index, valid, original_duration)
        if window is None:
            skipped += 1
            continue
        start, end = window
        segments.append({
            "start": round(start, 6),
            "end": round(end, 6),
            "file": os.path.abspath(seg["wav_path"]),
        })

    if not segments:
        return None

    manifest = {
        "duration": round(original_duration + TAIL_PAD_SEC, 6),
        "background_audio": background_audio or "",
        "background_volume": float(background_volume),
        "segments": segments,
        "output": os.path.abspath(output_path),
    }
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    log.debug(
        "Rust timeline: %d segment(s), %d skipped, %.2fs, bg=%s vol=%.2f -> %s",
        len(segments), skipped, manifest["duration"],
        background_audio or "(none)", background_volume, json_path,
    )
    return manifest


def _assemble_audio_with_rust(
    segment_info: list[dict],
    original_duration: float,
    output_path: str,
    *,
    sample_rate: int = TARGET_SR,
    background_audio: str | None = None,
    background_volume: float = 1.0,
    use_rust: bool | None = None,
) -> str | None:
    """Assemble the timeline with the Rust ``stitcher`` binary.

    Writes a ``timeline.json``, runs ``stitcher <json>`` and returns
    *output_path* on success.  Every other outcome — the binary missing,
    a sample rate it does not support, a non-zero exit, a truncated or
    missing result — returns ``None`` after logging why, and the caller
    falls back to :func:`_assemble_timeline_python`.

    There is no ``segment_gap_ms`` here on purpose: the binary places a
    segment at its start time and stops it at the next segment's start,
    exactly where the numpy path trims.  The gap only shapes the numpy
    tempo budget.

    Parameters:
        segment_info:      Segment dicts from :func:`mazinger.tts.synthesize_segments`.
        original_duration: Duration of the original audio in seconds.
        output_path:       Where to write the final WAV.
        sample_rate:       Must be 24 kHz: the binary mixes at that rate.
        background_audio:  Optional background stem to mix under the voice,
                           ducked to 50% wherever voice is present.
        background_volume: Gain for that stem (0.0–1.0).
        use_rust:          ``True``/``False`` force the choice, ``None``
                           defers to :data:`_STITCHER_ENV` and auto-detection.

    Returns:
        *output_path* on success, otherwise ``None``.
    """
    if not _rust_stitcher_wanted(use_rust):
        return None
    if sample_rate != TARGET_SR:
        # Only 24 kHz is wired up in the binary; another rate would need a
        # resample on either side, so leave it to the numpy path.
        log.debug(
            "Rust stitcher skipped: sample_rate=%d (binary mixes at %d).",
            sample_rate, TARGET_SR,
        )
        return None

    binary = _stitcher_binary()
    if binary is None:
        log.debug("Rust stitcher not built; using the numpy path.")
        return None

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # The manifest is scratch: keep it beside the output so relative WAV
    # paths in a hand-written timeline still resolve, and delete it after.
    json_path = os.path.join(
        os.path.dirname(os.path.abspath(output_path)),
        f".stitcher_timeline.{os.getpid()}.json",
    )
    manifest = None
    try:
        manifest = _write_timeline_json(
            segment_info, original_duration, output_path,
            background_audio=background_audio or "",
            background_volume=background_volume,
            json_path=json_path,
        )
        if manifest is None:
            log.info("Nothing to stitch with Rust; using the numpy path.")
            return None

        # A stale file from a crashed run must not be mistaken for success.
        if os.path.exists(output_path):
            os.remove(output_path)

        log.info(
            "Stitching with the Rust engine (%d segment(s), %.2fs)",
            len(manifest["segments"]), manifest["duration"],
        )
        result = subprocess.run(
            [binary, json_path],
            capture_output=True, text=True,
        )
        # The binary logs its steps to stderr; surface them at debug level
        # so the UI can show the Rust: Merging progress without spamming
        # a normal run.
        for line in (result.stderr or "").splitlines():
            if line.strip():
                log.debug("stitcher: %s", line.strip())

        if result.returncode != 0:
            log.warning(
                "Rust stitcher exited %s; falling back to the numpy path.\n%s",
                result.returncode,
                "\n".join((result.stderr or "").splitlines()[-10:]),
            )
            return None

        # Trust the file, not the exit code: a zero exit with a missing or
        # empty file is a silent failure, and a half-written WAV would be
        # far worse than falling back.
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            log.warning("Rust stitcher produced no output; using the numpy path.")
            return None

        produced = get_audio_duration(output_path)
        if produced is None or produced <= 0:
            log.warning(
                "Rust stitcher output is unreadable (%s); using the numpy path.",
                output_path,
            )
            return None
        if abs(produced - manifest["duration"]) > 0.5:
            log.warning(
                "Rust stitcher wrote %.2fs but the timeline is %.2fs; "
                "using the numpy path.", produced, manifest["duration"],
            )
            return None

        log.info("Timeline assembled by Rust: %.2fs -> %s", produced, output_path)
        return output_path
    except Exception as exc:  # noqa: BLE001 — speed must never fail a run
        log.warning("Rust stitcher unavailable (%s); using the numpy path.", exc)
        return None
    finally:
        try:
            os.remove(json_path)
        except OSError:
            pass


def assemble_timeline(
    segment_info: list[dict],
    original_duration: float,
    output_path: str,
    *,
    sample_rate: int = TARGET_SR,
    speed_threshold: float = 0.05,
    min_speed_ratio: float = 0.82,
    target_fill: float = 0.92,
    tempo_mode: str = "auto",
    fixed_tempo: float | None = None,
    max_tempo: float = 1.5,
    crossfade_ms: int = 50,
    segment_gap_ms: int = 50,
    use_rust: bool | None = None,
    background_audio: str | None = None,
    background_volume: float = 1.0,
) -> str:
    """Assemble per-segment TTS WAVs into a single time-aligned audio file.

    Prefers the Rust ``stitcher`` binary and falls back to the numpy
    implementation when it is unavailable or fails, so callers never need
    to know which engine ran.  Both produce the same WAV.

    The numpy path applies a smart tempo pass; the Rust path does not.
    Set ``tempo_mode="off"`` when passing ``background_audio``, because the
    binary mixes that stem in and a fixed tempo on top of it is rarely
    what a caller wants.

    Parameters:
        segment_info:      List of dicts from :func:`mazinger.tts.synthesize_segments`.
        original_duration: Duration of the original audio in seconds.
        output_path:       Where to write the final WAV.
        sample_rate:       Target sample rate.
        speed_threshold:   Fractional tolerance before a short segment is slowed
                           down.  Overflows are always sped up, since the
                           overflowing part would otherwise be trimmed.
        min_speed_ratio:   Hard floor for slowdown (default 0.82 = max ~22% slower).
                           Below this speech starts sounding unnatural.
        target_fill:       Target fraction of the time window to fill when
                           slowing down (default 0.92). A value < 1.0 leaves a
                           small natural gap instead of stretching to the edge.
        tempo_mode:        ``auto`` — speed up overflows AND slow down short
                           segments toward *target_fill* (default);
                           ``off`` — no tempo adjustment;
                           ``dynamic`` — same as auto (legacy alias);
                           ``fixed`` — apply *fixed_tempo* to every segment.
        fixed_tempo:       Tempo rate applied when ``tempo_mode="fixed"``.
        max_tempo:         Upper speed limit for dynamic/auto mode (default 1.5).
        crossfade_ms:      Fade-in/out at segment edges (default 50).
        segment_gap_ms:    Silence gap reserved between segments (default 50).
        use_rust:          ``True``/``False`` force or forbid the Rust engine,
                           ``None`` (default) auto-detects.
        background_audio:  Background stem to mix under the voice (Rust only).
                           Leaving it ``None`` keeps the output voice-only, so
                           :func:`post_process` mixes the background as before
                           and the stem is never applied twice.
        background_volume: Gain for that stem (0.0–1.0, default 1.0).

    Returns:
        The *output_path*.
    """
    if _rust_stitcher_wanted(use_rust) and _stitcher_binary() is not None:
        if tempo_mode and tempo_mode != "off":
            log.warning(
                "Rust stitcher selected: tempo_mode=%r will not be applied "
                "(the binary does not implement tempo stretching)", tempo_mode,
            )
        done = _assemble_audio_with_rust(
            segment_info, original_duration, output_path,
            sample_rate=sample_rate,
            background_audio=background_audio,
            background_volume=background_volume,
            use_rust=use_rust,
        )
        if done is not None:
            return done

    return _assemble_timeline_python(
        segment_info, original_duration, output_path,
        sample_rate=sample_rate,
        speed_threshold=speed_threshold,
        min_speed_ratio=min_speed_ratio,
        target_fill=target_fill,
        tempo_mode=tempo_mode,
        fixed_tempo=fixed_tempo,
        max_tempo=max_tempo,
        crossfade_ms=crossfade_ms,
        segment_gap_ms=segment_gap_ms,
    )


def _assemble_timeline_python(
    segment_info: list[dict],
    original_duration: float,
    output_path: str,
    *,
    sample_rate: int = TARGET_SR,
    speed_threshold: float = 0.05,
    min_speed_ratio: float = 0.82,
    target_fill: float = 0.92,
    tempo_mode: str = "auto",
    fixed_tempo: float | None = None,
    max_tempo: float = 1.5,
    crossfade_ms: int = 50,
    segment_gap_ms: int = 50,
) -> str:
    """Assemble per-segment TTS WAVs into a single time-aligned audio file.

    Smart tempo approach:
      1. Place each segment at its SRT start time.
      2. If a segment overflows its time slot → tempo-stretch up to *max_tempo*.
      3. If a segment is shorter than its slot → slow it down just enough
         to reach *target_fill* of the window (default 92%), capping at
         *min_speed_ratio* (default 0.82×) so speech never sounds
         unnaturally slow.
      4. If it *still* overflows after the cap → trim at the quietest
         point and apply a long fade-out to mask the cut.

    The *target_fill* parameter prevents the algorithm from trying to fill
    100% of the window — a small natural gap is left.  The *min_speed_ratio*
    acts as a hard floor: segments that would need more aggressive slowdown
    are left partially unfilled rather than distorted.

    Parameters:
        segment_info:      List of dicts from :func:`mazinger.tts.synthesize_segments`.
        original_duration: Duration of the original audio in seconds.
        output_path:       Where to write the final WAV.
        sample_rate:       Target sample rate.
        speed_threshold:   Fractional tolerance before a short segment is slowed
                           down.  Overflows are always sped up, since the
                           overflowing part would otherwise be trimmed.
        min_speed_ratio:   Hard floor for slowdown (default 0.82 = max ~22% slower).
                           Below this speech starts sounding unnatural.
        target_fill:       Target fraction of the time window to fill when
                           slowing down (default 0.92). A value < 1.0 leaves a
                           small natural gap instead of stretching to the edge.
        tempo_mode:        ``auto`` — speed up overflows AND slow down short
                           segments toward *target_fill* (default);
                           ``off`` — no tempo adjustment;
                           ``dynamic`` — same as auto (legacy alias);
                           ``fixed`` — apply *fixed_tempo* to every segment.
        fixed_tempo:       Tempo rate applied when ``tempo_mode="fixed"``.
        max_tempo:         Upper speed limit for dynamic/auto mode (default 1.5).
        crossfade_ms:      Fade-in/out at segment edges (default 50).
        segment_gap_ms:    Silence gap reserved between segments (default 50).

    Returns:
        The *output_path*.
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # Allow a small tail so the last segment is never hard-clipped.
    tail_pad_sec = TAIL_PAD_SEC
    total_samples = int((original_duration + tail_pad_sec) * sample_rate)
    timeline = np.zeros(total_samples, dtype=np.float32)

    stats = {"sped_up": 0, "slowed_down": 0, "ok": 0, "skipped": 0, "trimmed": 0}
    overflow_total = 0.0

    valid_segs = [s for s in segment_info if s.get("wav_path") is not None]
    valid_segs.sort(key=lambda s: s["start"])

    def prepare(seg_i: int) -> tuple[int, np.ndarray, str, float] | None:
        """Steps 1–2 for one segment: load, tempo-stretch, trim and fade.

        Depends only on the segment and the next one's start, so segments
        are prepared in parallel; placing them (step 3) stays in order.
        Returns ``(start_samp, audio, outcome, trimmed_secs)``, or ``None``
        for an empty segment.
        """
        seg = valid_segs[seg_i]
        raw_audio = _load_and_resample(seg["wav_path"], sample_rate)
        actual_dur = len(raw_audio) / sample_rate
        if actual_dur <= 0:
            return None
        trimmed_secs = 0.0

        target_dur = seg["target_dur"]
        start_samp = int(seg["start"] * sample_rate)

        # Budget = available time window for dynamic tempo decisions.
        # Uses the real gap to the next segment so tempo-stretch targets
        # the actual available space (the original behavior).
        is_last = (seg_i + 1 >= len(valid_segs))
        if not is_last:
            next_start = valid_segs[seg_i + 1]["start"]
            budget_dur = max(next_start - seg["start"] - segment_gap_ms / 1000, target_dur)
        else:
            budget_dur = max(original_duration - seg["start"], target_dur)

        if budget_dur <= 0:
            log.warning("Segment %s (%s) has no time budget; skipping tempo",
                        seg.get("idx"), seg.get("wav_path"))
            return None

        speed_ratio = actual_dur / budget_dur

        # -- Step 1: tempo-stretch if needed ------------------------------
        if tempo_mode == "fixed" and fixed_tempo is not None:
            base, ext = os.path.splitext(seg["wav_path"])
            stretched_path = f"{base}_stretched{ext}"
            audio = _tempo_stretch(seg["wav_path"], fixed_tempo, stretched_path, sample_rate)
            outcome = "sped_up"

        elif tempo_mode in ("auto", "dynamic"):
            if speed_ratio > 1.0:
                # Segment overflows — speed it up.  Even a tiny overflow is
                # stretched: left as-is it would be trimmed off below.
                effective_ratio = min(speed_ratio, max_tempo)
                stretched_path = seg["wav_path"].replace(".wav", "_stretched.wav")
                audio = _tempo_stretch(seg["wav_path"], effective_ratio, stretched_path, sample_rate)
                outcome = "sped_up"
            elif speed_ratio < 1.0 - speed_threshold:
                # Segment is shorter than its slot — slow it down toward
                # target_fill of the window.  This avoids trying to fill
                # 100% (which would need aggressive slowdown) while still
                # closing most of the gap.
                #
                # needed_ratio = fill / target_fill  (the atempo value
                # that would make TTS fill exactly target_fill of the window).
                # Clamped to min_speed_ratio so speech never sounds drunk.
                fill = speed_ratio              # current fill fraction
                needed_ratio = fill / target_fill  # e.g. 0.80/0.92 = 0.87
                effective_ratio = max(needed_ratio, min_speed_ratio)

                # Only bother stretching if the correction is meaningful
                if effective_ratio < 1.0 - speed_threshold:
                    slowed_path = seg["wav_path"].replace(".wav", "_slowed.wav")
                    audio = _tempo_stretch(seg["wav_path"], effective_ratio, slowed_path, sample_rate)
                    outcome = "slowed_down"
                    log.debug(
                        "Seg %s: slowed %.2fx (%.1fs → %.1fs, "
                        "fill %.0f%% → %.0f%% of %.1fs window)",
                        seg["idx"], effective_ratio, actual_dur,
                        len(audio) / sample_rate,
                        fill * 100, min(actual_dur / effective_ratio / budget_dur, 1.0) * 100,
                        budget_dur,
                    )
                else:
                    audio = raw_audio
                    outcome = "ok"
            else:
                audio = raw_audio
                outcome = "ok"
        else:
            # tempo_mode == "off"
            audio = raw_audio
            outcome = "ok"

        # -- Step 2: handle overflow after stretch -------------------------
        if is_last:
            # Last segment: trim only if it exceeds the generous tail pad.
            clip_samps = int((budget_dur + tail_pad_sec) * sample_rate)
            if len(audio) > clip_samps:
                trim_at = _find_last_silence(audio, sample_rate, clip_samps)
                trimmed_secs = (len(audio) - trim_at) / sample_rate
                audio = audio[:trim_at]
                if trimmed_secs > 0.2:
                    log.warning(
                        "Seg %s (last): trimmed %.2fs to fit budget+pad %.2fs",
                        seg["idx"], trimmed_secs, budget_dur + tail_pad_sec,
                    )
                audio = _fade(audio, sample_rate, fade_in_ms=15, fade_out_ms=crossfade_ms)
            else:
                audio = _fade(audio, sample_rate, fade_in_ms=15, fade_out_ms=200)
        else:
            # Non-last segments: cap the spill so we *never* get two voices
            # speaking at the same time.  A small overlap (≤ segment_gap_ms)
            # is harmless and blends naturally; anything larger is trimmed
            # at the quietest point with a fade-out (same logic as the
            # last-segment branch).  This protects the listener from
            # cascading TTS overruns when a translation is much longer
            # than the source slot or when ``--max-tempo`` can't squeeze
            # the audio to fit.
            next_start_samp = int(valid_segs[seg_i + 1]["start"] * sample_rate)
            # Maximum samples we may write into the timeline starting at
            # start_samp without colliding with the next segment.
            max_len = max(0, next_start_samp - start_samp)

            if len(audio) > max_len:
                overflow_secs = trimmed_secs = (len(audio) - max_len) / sample_rate
                trim_at = _find_last_silence(audio, sample_rate, max_len)
                # Guarantee no overlap even if no silence was found.
                trim_at = min(trim_at, max_len)
                audio = audio[:trim_at]
                if overflow_secs > 0.2:
                    log.warning(
                        "Seg %s: trimmed %.2fs to prevent overlapping the "
                        "next segment (budget %.2fs, audio after stretch "
                        "%.2fs). Source SRT entry is likely too long for "
                        "its time slot — consider re-running with "
                        "--force-reset, or lower --duration-budget / raise "
                        "--max-tempo.",
                        seg["idx"], overflow_secs,
                        budget_dur, len(raw_audio) / sample_rate,
                    )
                audio = _fade(audio, sample_rate, fade_in_ms=15, fade_out_ms=120)
            else:
                audio = _fade(audio, sample_rate, fade_in_ms=15, fade_out_ms=50)

        return start_samp, audio, outcome, trimmed_secs

    workers = max(1, min(ASSEMBLE_WORKERS, len(valid_segs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        prepared = _ordered_map(pool, prepare, range(len(valid_segs)), window=4 * workers)
        for result in tqdm(prepared, total=len(valid_segs), desc="Aligning"):
            if result is None:
                stats["skipped"] += 1
                continue
            start_samp, audio, outcome, trimmed_secs = result
            stats[outcome] += 1
            if trimmed_secs > 0:
                stats["trimmed"] += 1
                overflow_total += trimmed_secs

            # -- Step 3: paste at SRT start time --------------------------
            end_samp = min(start_samp + len(audio), total_samples)
            seg_len = end_samp - start_samp
            if seg_len > 0:
                timeline[start_samp:end_samp] += audio[:seg_len]

    stats["skipped"] += len(segment_info) - len(valid_segs)

    # Trim tail padding — keep up to the last placed sample or original
    # duration, whichever is longer, plus a small cushion for the fade.
    orig_samples = int(original_duration * sample_rate)
    # Find actual last non-zero sample (= where audio content ends)
    last_nz = _last_nonzero(timeline)
    content_end = last_nz + 1 if last_nz >= 0 else orig_samples

    # Apply a gentle fade-out at the actual content boundary so the
    # listener doesn't hear a hard cut when the last segment finishes.
    # The fade ramps down the *audio content*, not trailing silence.
    tail_fade_ms = 300
    tail_fade_samps = min(int(sample_rate * tail_fade_ms / 1000), content_end // 2)
    if tail_fade_samps >= 2:
        ramp = 0.5 * (1.0 + np.cos(np.linspace(0.0, np.pi, tail_fade_samps))).astype(np.float32)
        timeline[content_end - tail_fade_samps:content_end] *= ramp

    # Keep a tiny silence cushion (100 ms) after the content fade-out
    # so the ending doesn't feel abrupt, then trim.
    cushion = int(sample_rate * 0.1)
    placed_end = max(content_end + cushion, orig_samples)
    placed_end = min(placed_end, total_samples)
    timeline = timeline[:placed_end]

    peak = _peak_abs(timeline)
    if peak > 1.0:
        log.info("Normalising peak %.2f to 1.0", peak)
        timeline /= peak

    sf.write(output_path, timeline, sample_rate)

    if overflow_total > 0.1:
        log.warning(
            "Total overflow: %.2fs of TTS audio was trimmed to fit the timeline. "
            "Consider reducing translation word count (--duration-budget) "
            "or increasing --max-tempo.",
            overflow_total,
        )

    log.info(
        "Timeline assembled: %.2fs | sped_up=%d slowed=%d ok=%d trimmed=%d skipped=%d",
        len(timeline) / sample_rate,
        stats["sped_up"], stats["slowed_down"], stats["ok"],
        stats["trimmed"], stats["skipped"],
    )
    return output_path


def _loudness_or_none(path: str) -> float | None:
    """Integrated loudness (LUFS) of an audio file via ffmpeg, or ``None``."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", path,
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )
    import json as _json, re as _re
    m = _re.search(r'\{[^}]+"input_i"[^}]+\}', result.stderr, _re.DOTALL)
    if m:
        try:
            value = float(_json.loads(m.group())["input_i"])
        except (ValueError, KeyError):
            log.warning("Loudness JSON parse failed for %s", path)
            return None
        return value if np.isfinite(value) else None
    if result.returncode != 0:
        log.warning("ffmpeg loudness probe failed for %s (exit %s)", path, result.returncode)
    else:
        log.warning("No loudness JSON in ffmpeg output for %s", path)
    return None


def _measure_loudness(path: str) -> float:
    """Return integrated loudness (LUFS) of an audio file via ffmpeg."""
    value = _loudness_or_none(path)
    if value is None:
        log.warning("Loudness measurement failed for %s; assuming -24 LUFS", path)
        return -24.0
    return value


def measure_loudness_cached(audio_path: str, cache_path: str) -> float:
    """Integrated loudness of *audio_path*, kept in *cache_path* (JSON).

    Measuring takes about a minute per hour of audio, and the source of a
    project never changes, so the value is reused while *audio_path* keeps
    the size and modification time it was measured with.  A failed
    measurement (the -24 LUFS fallback) is not cached.
    """
    import json

    st = os.stat(audio_path)
    stamp = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cached = json.load(fh)
        if cached.get("source") == stamp:
            return float(cached["integrated_lufs"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass

    value = _loudness_or_none(audio_path)
    if value is None:
        return -24.0
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    tmp = f"{cache_path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"source": stamp, "integrated_lufs": value}, fh)
    os.replace(tmp, cache_path)
    log.info("Source loudness cached: %.1f LUFS (%s)", value, cache_path)
    return value


# Demucs runs over blocks of the source so memory stays flat on long videos:
# the separated stems of a whole 2 h file would need ~10 GB of RAM.  Each
# block is decoded with extra context on both sides, which is separated and
# then discarded so block seams are inaudible.
DEMUCS_BLOCK_SEC = 300.0
DEMUCS_CONTEXT_SEC = 5.0


def _decode_audio(audio_path: str, sr: int, channels: int,
                  start: float = 0.0, duration: float | None = None) -> np.ndarray:
    """Decode a range of *audio_path* to float32 ``(samples, channels)`` via ffmpeg."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", audio_path, "-ar", str(sr), "-ac", str(channels), "-f", "f32le", "-"]
    result = subprocess.run(cmd, capture_output=True, check=True)
    return np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, channels)


def _load_demucs(device: str | None) -> tuple[object, str]:
    """Load htdemucs and return ``(model, device)``."""
    import torch
    from demucs.pretrained import get_model

    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    model = get_model("htdemucs")
    model.to(device)
    model.eval()
    return model, device


def _unload_demucs(model: object, device: str) -> None:
    import gc
    del model
    gc.collect()
    if str(device).startswith("cuda"):
        import torch
        torch.cuda.empty_cache()


def _demucs_background_block(
    model, block: np.ndarray, sr: int, *,
    device: str, segment: float | None, overlap: float,
) -> np.ndarray:
    """Separate one ``(samples, channels)`` block; return mono background at *sr*."""
    import torch
    import torchaudio
    from demucs.apply import apply_model

    wav = torch.from_numpy(np.ascontiguousarray(block.T))
    with torch.no_grad():
        # split=True runs the model over `segment`-second windows moved to
        # *device* one at a time, so GPU memory does not grow with the block.
        sources = apply_model(
            model, wav[None], device=device, split=True,
            segment=segment, overlap=overlap, progress=False,
        )
    stems = sources[0].cpu().numpy()  # (stems, channels, samples)
    vocals_idx = model.sources.index("vocals")
    bg = (stems.sum(axis=0) - stems[vocals_idx]).mean(axis=0).astype(np.float32)
    if model.samplerate != sr:
        bg = torchaudio.functional.resample(
            torch.from_numpy(bg), model.samplerate, sr,
        ).numpy()
    return bg


def _extract_background_demucs(
    audio_path: str, out_path: str, sr: int, *,
    device: str | None = None,
    segment: float | None = None,
    overlap: float = 0.25,
    block_sec: float = DEMUCS_BLOCK_SEC,
    context_sec: float = DEMUCS_CONTEXT_SEC,
) -> None:
    model, device = _load_demucs(device)
    try:
        total = get_audio_duration(audio_path)
        n_blocks = max(1, int(np.ceil(total / block_sec)))
        log.info(
            "Extracting background with demucs on %s (%.0fs in %d block(s))",
            device, total, n_blocks,
        )
        with sf.SoundFile(out_path, "w", samplerate=sr, channels=1, format="WAV") as out:
            for b in tqdm(range(n_blocks), desc="Separating", disable=n_blocks == 1):
                core_start = b * block_sec
                is_last = b == n_blocks - 1
                core_end = total if is_last else (b + 1) * block_sec
                read_start = max(0.0, core_start - context_sec)
                read_end = None if is_last else core_end + context_sec

                block = _decode_audio(
                    audio_path, model.samplerate, model.audio_channels,
                    start=read_start,
                    duration=None if read_end is None else read_end - read_start,
                )
                if not len(block):
                    log.warning("ffmpeg returned no audio for block %d of %s; background stem will be short", b, audio_path)
                    break
                bg = _demucs_background_block(
                    model, block, sr, device=device, segment=segment, overlap=overlap,
                )
                # Slice positions come from absolute times so rounding never
                # accumulates across blocks.
                offset = round(core_start * sr) - round(read_start * sr)
                if is_last:
                    core = bg[offset:]
                else:
                    n = round(core_end * sr) - round(core_start * sr)
                    core = bg[offset:offset + n]
                    if len(core) < n:
                        core = np.pad(core, (0, n - len(core)))
                out.write(core)
    finally:
        _unload_demucs(model, device)


def _extract_background(audio_path: str, out_path: str, sr: int = TARGET_SR) -> str:
    """Extract non-vocal background from *audio_path*.

    Uses demucs (htdemucs model) for high-quality source separation, block
    by block (see :data:`DEMUCS_BLOCK_SEC`) and on the GPU when available.
    Falls back to spectral masking via librosa when demucs is unavailable.
    """
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    try:
        _extract_background_demucs(audio_path, out_path, sr)
    except Exception as exc:
        log.info("Demucs unavailable (%s), using spectral masking fallback", exc)
        import librosa
        y, _ = librosa.load(audio_path, sr=sr, mono=True)
        S = librosa.stft(y)
        H, P = librosa.decompose.hpss(np.abs(S), kernel_size=31, margin=4.0)
        mask = P / (H + P + 1e-10)
        bg = librosa.istft(S * mask, length=len(y))
        sf.write(out_path, bg, sr)
    return out_path


def background_cache_path(audio_path: str, sr: int = TARGET_SR) -> str:
    """Return the cache path of the background stem for *audio_path*.

    ``source/audio.mp3`` → ``source/background.<sr>.wav``.
    """
    return os.path.join(os.path.dirname(audio_path) or ".", f"background.{sr}.wav")


def extract_background_cached(
    audio_path: str,
    cache_path: str | None = None,
    sr: int = TARGET_SR,
) -> str:
    """Return a background stem for *audio_path*, extracting it only when needed.

    The stem at *cache_path* (default: :func:`background_cache_path`) is
    reused when it is at least as new as *audio_path*; otherwise it is
    re-extracted.  The file is replaced atomically, so a concurrent reader
    never sees a partial stem.
    """
    cache_path = cache_path or background_cache_path(audio_path, sr)
    try:
        fresh = (
            os.path.getsize(cache_path) > 0
            and os.path.getmtime(cache_path) >= os.path.getmtime(audio_path)
        )
    except OSError:
        fresh = False
    if fresh:
        log.info("Reusing cached background stem: %s", cache_path)
        return cache_path

    tmp_path = f"{os.path.splitext(cache_path)[0]}.{os.getpid()}.part.wav"
    try:
        _extract_background(audio_path, tmp_path, sr=sr)
        os.replace(tmp_path, cache_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    log.info("Background stem cached: %s", cache_path)
    return cache_path


# ---------------------------------------------------------------------------
# C++ normalizer (fast path)
# ---------------------------------------------------------------------------
# ``cpp_accelerator/normalizer`` is a dependency-free C++20 CLI that applies a
# per-sample noise gate and a linear gain to a 16-bit PCM WAV while streaming
# it in 64 KiB blocks, so a feature-length track costs ~128 KB of RAM instead
# of a full in-memory float copy.  It never resamples and never changes the
# frame count.
#
#   normalizer <input.wav> <output.wav> <noise_threshold> <target_gain>
#
# The contract it implements, which the Python fallback reproduces exactly:
#
#     gate_level = threshold * 32767
#     sample     = (abs(sample) < gate_level) ? 0 : sample
#     sample     = clamp(round_nearest_even(sample * gain), -32768, 32767)
#
# Binary discovery, in order:
#   1. ``NORMALIZER_BIN``   — explicit path (or a bare name on PATH)
#   2. ``cpp_accelerator/build/normalizer[.exe]``         (single-config)
#   3. ``cpp_accelerator/build/<Config>/normalizer[.exe]`` (multi-config/MSVC)
#   4. ``cpp_accelerator/normalizer[.exe]``               (in-place build)
#   5. ``normalizer`` on PATH
#
# ``MAZINGER_NORMALIZER`` overrides the choice:
#   ``off`` / ``0`` / ``false`` — never use C++ (force the Python path)
#   anything else set          — use C++ when available
NORMALIZER_THRESHOLD = 0.02   # -34 dBFS: removes room hiss, keeps speech
NORMALIZER_GAIN = 1.8         # the documented example gain (+5.1 dB)

_NORMALIZER_ENV = "MAZINGER_NORMALIZER"
_NORMALIZER_BIN_ENV = "NORMALIZER_BIN"

# Formats the C++ tool accepts.  It parses RIFF by hand and has no float or
# 24-bit reader, so anything else is a guaranteed exit 1 — cheaper to decline
# here than to discover in a subprocess.
_NORMALIZER_FORMATS = ("WAV",)
_NORMALIZER_SUBTYPES = ("PCM_16",)

_normalizer_binary_cache: dict[str, Any] = {}


def _normalizer_binary() -> str | None:
    """Locate the C++ ``normalizer`` binary, or ``None`` when unavailable.

    Cached for the same reason as :func:`_stitcher_binary`: the answer
    cannot change while the process is alive, and discovery walks the disk.
    """
    if "path" in _normalizer_binary_cache:
        return _normalizer_binary_cache["path"]

    candidates: list[str] = []
    override = os.environ.get(_NORMALIZER_BIN_ENV, "").strip()
    if override:
        # A broken override is reported, never silently swapped for a
        # different binary — that costs an afternoon of "why is the output
        # not what I asked for".
        if os.path.isfile(override):
            candidates.append(override)
        else:
            resolved = shutil.which(override)
            if resolved:
                candidates.append(resolved)
            else:
                log.warning(
                    "%s=%s does not exist; ignoring it.", _NORMALIZER_BIN_ENV, override
                )

    root = _repo_root()
    exe = "normalizer.exe" if sys.platform == "win32" else "normalizer"
    for build in (
        ("cpp_accelerator", "build", exe),
        ("cpp_accelerator", "build", "Release", exe),
        ("cpp_accelerator", "build", "RelWithDebInfo", exe),
        ("cpp_accelerator", exe),
    ):
        candidate = os.path.join(root, *build)
        if os.path.isfile(candidate):
            candidates.append(candidate)

    on_path = shutil.which("normalizer")
    if on_path:
        candidates.append(on_path)

    found = candidates[0] if candidates else None
    if found:
        log.debug("C++ normalizer: %s", found)
    _normalizer_binary_cache["path"] = found
    return found


def _cpp_normalizer_wanted(use_cpp: bool | None) -> bool:
    """Decide whether the C++ path may be used for this call."""
    setting = os.environ.get(_NORMALIZER_ENV, "").strip().lower()
    if setting in _FALSEY:
        if use_cpp:
            log.info(
                "C++ normalizer disabled by %s; using the Python path.", _NORMALIZER_ENV
            )
        return False
    if use_cpp is False:
        return False
    return True


def _normalizer_supported(path: str) -> tuple[bool, Any | None]:
    """Cheap pre-flight: can ``normalizer`` read *path* at all?

    Returns ``(supported, info)``.  Declining before spawning the binary
    keeps an unsupported file from costing a subprocess and a stack of
    stderr that says exactly what the check already knew.
    """
    try:
        info = sf.info(path)
    except Exception as exc:  # noqa: BLE001 — unreadable input is not our problem here
        log.debug("Cannot probe %s: %s", path, exc)
        return False, None

    if info.format not in _NORMALIZER_FORMATS or info.subtype not in _NORMALIZER_SUBTYPES:
        log.debug(
            "C++ normalizer skipped: %s is %s/%s (needs 16-bit PCM WAV).",
            path, info.format, info.subtype,
        )
        return False, info
    if not (1 <= info.channels <= 8):
        log.debug("C++ normalizer skipped: %s has %d channels (needs 1..8).",
                  path, info.channels)
        return False, info
    if not (1000 <= info.samplerate <= 768_000):
        log.debug("C++ normalizer skipped: implausible rate %d Hz in %s.",
                  info.samplerate, path)
        return False, info
    return True, info


def _normalize_audio_with_cpp(
    input_path: str,
    output_path: str,
    *,
    threshold: float = NORMALIZER_THRESHOLD,
    gain: float = NORMALIZER_GAIN,
    use_cpp: bool | None = None,
) -> str | None:
    """Gate and gain *input_path* with the C++ ``normalizer`` binary.

    Writes a manifest-free, four-argument invocation::

        normalizer <input> <staged_output> <threshold> <gain>

    and returns *output_path* only once the result has proved itself.  Every
    other outcome — binary missing, unsupported format, non-zero exit, an
    empty or wrong-sized file, any exception — returns ``None`` after logging
    why, and the caller falls back to :func:`_normalize_audio_python`.

    The tool refuses ``input == output``, so the result is always staged in a
    temp sibling and moved into place only after validation.  That also makes
    an in-place call (``output_path == input_path``) safe.

    Parameters:
        input_path:   WAV to read.
        output_path:  Where to write the cleaned file.
        threshold:    Gate threshold as a fraction of full scale, 0.0–1.0.
        gain:         Linear gain multiplier applied to surviving samples.
        use_cpp:      ``True``/``False`` force the choice, ``None`` defers to
                      :data:`_NORMALIZER_ENV` and auto-detection.

    Returns:
        *output_path* on success, otherwise ``None``.
    """
    if not _cpp_normalizer_wanted(use_cpp):
        return None
    if not (0.0 <= threshold <= 1.0):
        log.warning("Normalizer threshold must be in [0, 1], got %r.", threshold)
        return None
    if not (gain >= 0.0):
        log.warning("Normalizer gain must not be negative, got %r.", gain)
        return None

    binary = _normalizer_binary()
    if binary is None:
        log.debug("C++ normalizer not built; using the Python path.")
        return None

    supported, info = _normalizer_supported(input_path)
    if not supported:
        return None

    out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    os.makedirs(out_dir, exist_ok=True)

    # Staged beside the output so relative paths behave and the final move
    # is an atomic rename on the same volume.
    tmp_path = os.path.join(
        out_dir, f".{os.path.basename(output_path)}.{os.getpid()}.norm.tmp.wav"
    )
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    try:
        log.info(
            "Normalising with the C++ engine: threshold=%.3f gain=%.2f -> %s",
            threshold, gain, output_path,
        )
        result = subprocess.run(
            [binary, input_path, tmp_path, f"{threshold:.6f}", f"{gain:.6f}"],
            capture_output=True, text=True,
        )
        # The binary prints a summary on stdout and warnings on stderr;
        # surface both at debug level so a UI can show the real engine
        # talking without spamming an ordinary run.
        for line in (result.stdout or "").splitlines() + (result.stderr or "").splitlines():
            if line.strip():
                log.debug("normalizer: %s", line.strip())

        if result.returncode != 0:
            log.warning(
                "C++ normalizer exited %s; using the Python path.\n%s",
                result.returncode,
                "\n".join((result.stderr or "").splitlines()[-8:]),
            )
            return None

        # Trust the file, not the exit code: a zero exit with no output is a
        # silent failure, and a half-written WAV is worse than falling back.
        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            log.warning("C++ normalizer produced no output; using the Python path.")
            return None

        out_info = sf.info(tmp_path)
        # The tool is defined to preserve the frame count exactly; anything
        # else means we are not looking at our own output.
        if out_info.frames != info.frames:
            log.warning(
                "C++ normalizer wrote %d frames but the input has %d; "
                "using the Python path.", out_info.frames, info.frames,
            )
            return None
        if out_info.samplerate != info.samplerate or out_info.channels != info.channels:
            log.warning(
                "C++ normalizer changed the format to %d Hz/%d ch (input %d Hz/%d ch); "
                "using the Python path.", out_info.samplerate, out_info.channels,
                info.samplerate, info.channels,
            )
            return None

        os.replace(tmp_path, output_path)
        log.info(
            "Audio cleaned by C++: %.2fs, %d ch, threshold=%.3f gain=%.2f -> %s",
            info.duration, info.channels, threshold, gain, output_path,
        )
        return output_path
    except Exception as exc:  # noqa: BLE001 — speed must never fail a run
        log.warning("C++ normalizer unavailable (%s); using the Python path.", exc)
        return None
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _normalize_audio_python(
    input_path: str,
    output_path: str,
    *,
    threshold: float = NORMALIZER_THRESHOLD,
    gain: float = NORMALIZER_GAIN,
) -> str:
    """Native numpy fallback implementing the C++ tool's contract exactly.

    Same gate, same rounding, same saturation — so a machine without a
    compiler still produces the same audio as one running the binary.  The
    trade-off is memory: this reads the whole file, which is precisely what
    the streaming C++ path exists to avoid.

    Raises:
        Exception: propagated to :func:`normalize_audio`, which treats a
            failed fallback as "no cleaning" rather than a failed run.
    """
    data, sample_rate = sf.read(input_path, dtype="int16")

    # float32 throughout: the C++ kernel computes `threshold * 32767.0f` and
    # `nearbyint(sample * gain)` in single precision, and matching the width
    # is what makes the two engines agree sample for sample.
    gate_level = np.float32(threshold) * np.float32(32767.0)
    raw = data.astype(np.float32, copy=False)
    is_noise = np.abs(raw) < gate_level
    gated = np.where(is_noise, np.float32(0.0), raw)
    # np.rint is round-half-to-even, which is what nearbyint() does under the
    # default FP rounding mode.
    scaled = np.rint(gated * np.float32(gain))
    clamped = np.clip(scaled, np.float32(-32768.0), np.float32(32767.0))
    out = clamped.astype(np.int16).reshape(data.shape)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    # format= is explicit: the callers hand us paths like ``x.gate.wav``, and
    # a path without a recognised extension would otherwise make soundfile
    # guess and fail.
    sf.write(output_path, out, sample_rate, format="WAV", subtype="PCM_16")
    log.info(
        "Audio cleaned by Python: %.2fs, threshold=%.3f gain=%.2f -> %s",
        data.shape[0] / sample_rate, threshold, gain, output_path,
    )
    return output_path


def normalize_audio(
    input_path: str,
    output_path: str | None = None,
    *,
    threshold: float = NORMALIZER_THRESHOLD,
    gain: float = NORMALIZER_GAIN,
    use_cpp: bool | None = None,
) -> str | None:
    """Noise-gate and level *input_path*, preferring the C++ engine.

    This is the entry point for the audio-cleaning stage: callers never need
    to know whether the binary or numpy produced the file.

    The signal chain, identical in both engines::

        gate: every sample below ``threshold`` of full scale becomes silence
        gain: surviving samples are scaled by ``gain`` and saturated, never
              wrapped, at the 16-bit rails

    Parameters:
        input_path:   Audio to clean.  May equal *output_path*.
        output_path:  Where to write the result; defaults to *input_path*.
        threshold:    Gate threshold, 0.0–1.0 (0.02 = -34 dBFS = hiss removal).
                      0.0 disables the gate.
        gain:         Linear gain (1.8 = +5.1 dB).  1.0 gates without
                      changing the level.
        use_cpp:      ``True``/``False`` force the engine, ``None`` auto-detects.

    Returns:
        The output path, or ``None`` when neither engine could do the job —
        which callers treat as "leave the audio alone".  This function never
        raises: a cleaning pass must not be able to fail a dub run.
    """
    if output_path is None:
        output_path = input_path
    try:
        done = _normalize_audio_with_cpp(
            input_path, output_path, threshold=threshold, gain=gain, use_cpp=use_cpp
        )
        if done is not None:
            return done
        return _normalize_audio_python(
            input_path, output_path, threshold=threshold, gain=gain
        )
    except Exception as exc:  # noqa: BLE001 — never break the pipeline
        log.warning(
            "Audio cleaning skipped (%s); passing %s through unchanged.",
            exc, input_path,
        )
        return None


def post_process(
    dubbed_path: str,
    original_audio: str,
    output_path: str,
    *,
    loudness_match: bool = True,
    mix_background: bool = True,
    background_volume: float = 0.15,
    background_cache: str | None = None,
    loudness_cache: str | None = None,
    noise_gate: bool = True,
    gate_threshold: float = NORMALIZER_THRESHOLD,
    gate_gain: float = 1.0,
) -> str:
    """Apply noise gating, loudness normalisation and background mixing.

    The three stages run in that order, and the order matters: the gate
    strips the hiss *before* ``loudnorm`` measures the file, otherwise
    loudness matching lifts the noise floor along with the speech.

    Parameters:
        dubbed_path:       Path to the assembled TTS audio.
        original_audio:    Path to the original source audio.
        output_path:       Where to write the processed result.
        loudness_match:    Match dubbed loudness to the original.
        mix_background:    Extract and mix background from original.
        background_volume: Gain multiplier for the background layer (0.0–1.0).
        background_cache:  Where to keep the extracted background stem so
                           later calls reuse it (see
                           :func:`extract_background_cached`).  When
                           ``None``, the stem is re-extracted every time
                           into ``background.wav`` beside *output_path*.
        loudness_cache:    JSON file keeping the loudness of *original_audio*
                           so later calls skip measuring it again (see
                           :func:`measure_loudness_cached`).
        noise_gate:        Remove the hiss baked into the TTS output before
                           anything else looks at the file (see
                           :func:`normalize_audio`).  Uses the C++ binary
                           when it is built and numpy otherwise, and if
                           neither can do it the audio is passed through
                           untouched rather than failing the run.
        gate_threshold:    Gate threshold, 0.0–1.0 (0.02 = -34 dBFS).
        gate_gain:         Gain applied by the gate stage.  Defaults to
                           ``1.0`` — gate only, level untouched — because
                           *this* function is what owns loudness, via
                           ``loudness_match``.  Pass ``NORMALIZER_GAIN``
                           (1.8) if you want the C++ tool's documented
                           example lift as well; with ``loudness_match``
                           on it is re-levelled anyway.
    """
    if not loudness_match and not mix_background and not noise_gate:
        if dubbed_path != output_path:
            shutil.copy2(dubbed_path, output_path)
        return output_path

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    work = dubbed_path

    # -- noise gate (C++ fast path, Python fallback) ---------------------
    if noise_gate:
        gate_path = output_path + ".gate.wav"
        gated = normalize_audio(
            work, gate_path, threshold=gate_threshold, gain=gate_gain
        )
        if gated is not None:
            work = gated
        # else: keep the ungated audio. A cleaning pass is an optimisation
        # and must never cost the run its output.

    # -- loudness matching ------------------------------------------------
    if loudness_match:
        if loudness_cache:
            target_lufs = measure_loudness_cached(original_audio, loudness_cache)
        else:
            target_lufs = _measure_loudness(original_audio)
        target_lufs = max(target_lufs, -30.0)  # safety floor
        norm_path = output_path + ".norm.wav"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", work,
                 "-af", f"loudnorm=I={target_lufs:.1f}:TP=-1.5:LRA=11",
                 "-ar", str(TARGET_SR), "-ac", "1", norm_path],
                capture_output=True, check=True, timeout=300,
            )
        except subprocess.CalledProcessError as exc:
            log.error("ffmpeg loudness match failed: %s", (exc.stderr or "")[-2000:])
            raise
        except subprocess.TimeoutExpired:
            log.error("ffmpeg loudness match timed out after 300s")
            raise
        work = norm_path
        log.info("Loudness matched to %.1f LUFS", target_lufs)

    # -- background mixing ------------------------------------------------
    if mix_background:
        if background_cache:
            bg_path = extract_background_cached(original_audio, background_cache, sr=TARGET_SR)
        else:
            bg_path = os.path.join(os.path.dirname(output_path), "background.wav")
            _extract_background(original_audio, bg_path, sr=TARGET_SR)
            log.info("Background audio saved: %s", bg_path)

        dur_dub = get_audio_duration(work)
        mix_path = output_path + ".mix.wav"
        filt = (
            f"[1:a]atrim=0:{dur_dub:.3f},asetpts=PTS-STARTPTS[bg];"
            f"[0:a][bg]amix=inputs=2:duration=first:weights=1 {background_volume:.2f}:normalize=0[out]"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", work, "-i", bg_path,
             "-filter_complex", filt, "-map", "[out]",
             "-ar", str(TARGET_SR), "-ac", "1", mix_path],
            capture_output=True, check=True,
        )
        work = mix_path
        log.info("Mixed background at volume %.0f%%", background_volume * 100)

    # -- move final result into place ------------------------------------
    if work != output_path:
        shutil.move(work, output_path)

    # cleanup temp files (background.wav is kept for inspection)
    for suffix in (".norm.wav", ".mix.wav", ".gate.wav"):
        tmp = output_path + suffix
        if os.path.exists(tmp) and tmp != output_path:
            os.remove(tmp)

    return output_path


def mux_video(video_path: str, audio_path: str, output_path: str) -> str | None:
    """Replace the audio track of *video_path* with *audio_path*.

    Uses ffmpeg to copy the video stream and encode the new audio.
    Returns *output_path*, or ``None`` if ffmpeg is not installed.
    """
    if shutil.which("ffmpeg") is None:
        log.warning(
            "ffmpeg not found — cannot produce dubbed video. "
            "Install ffmpeg (e.g. 'apt install ffmpeg' or 'brew install ffmpeg') "
            "and re-run with --output-type video."
        )
        return None
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    log.info("Muxed video saved: %s", output_path)
    return output_path
