"""Post-transcription gap detection and speech recovery.

After faster-whisper produces a raw SRT, this module scans for long silent
gaps between segments, checks whether those gaps contain speech-like audio
(compared against a reference segment), and re-transcribes any that do —
patching the missed segments back into the timeline.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Callable

log = logging.getLogger(__name__)

_SAMPLE_RATE = 16000


# ---------------------------------------------------------------------------
#  Audio feature extraction
# ---------------------------------------------------------------------------

def _extract_pcm(audio_path: str, start: float, end: float):
    """Extract a time range as float32 PCM samples via ffmpeg."""
    import numpy as np

    proc = subprocess.run(
        [
            "ffmpeg", "-loglevel", "error",
            "-ss", str(start), "-to", str(end),
            "-i", audio_path,
            "-f", "f32le", "-ac", "1", "-ar", str(_SAMPLE_RATE),
            "pipe:1",
        ],
        capture_output=True, check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _rms(samples) -> float:
    """Root-mean-square energy."""
    import numpy as np

    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples ** 2)))


def _zcr(samples) -> float:
    """Zero-crossing rate (fraction of sign changes)."""
    import numpy as np

    if len(samples) < 2:
        return 0.0
    return float(np.sum(np.abs(np.diff(np.sign(samples))) > 0) / len(samples))


# ---------------------------------------------------------------------------
#  Gap detection helpers
# ---------------------------------------------------------------------------

def _pick_reference(segments: list[dict]) -> dict | None:
    """Pick a representative speech segment for audio fingerprinting.

    Prefers a segment near the timeline midpoint with reasonable duration
    and non-trivial text.
    """
    if not segments:
        return None
    good = [
        s for s in segments
        if 0.5 <= (s["end"] - s["start"]) <= 10.0
        and len(s.get("text", "")) > 5
    ]
    pool = good or segments
    mid = (segments[0]["start"] + segments[-1]["end"]) / 2
    return min(pool, key=lambda s: abs((s["start"] + s["end"]) / 2 - mid))


def _find_gaps(
    segments: list[dict],
    audio_duration: float,
    threshold: float,
) -> list[tuple[float, float]]:
    """Return ``(start, end)`` for every gap exceeding *threshold* seconds.

    Checks head (before first segment), inter-segment gaps, and tail
    (after last segment).
    """
    gaps: list[tuple[float, float]] = []
    if not segments:
        if audio_duration > threshold:
            gaps.append((0.0, audio_duration))
        return gaps

    if segments[0]["start"] > threshold:
        gaps.append((0.0, segments[0]["start"]))

    for i in range(len(segments) - 1):
        gs = segments[i]["end"]
        ge = segments[i + 1]["start"]
        if (ge - gs) > threshold:
            gaps.append((gs, ge))

    if (audio_duration - segments[-1]["end"]) > threshold:
        gaps.append((segments[-1]["end"], audio_duration))

    return gaps


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

def validate_transcription(
    segments: list[dict],
    audio_path: str,
    audio_duration: float,
    gap_threshold: float = 5.0,
    energy_ratio: float = 0.3,
    transcribe_gap_fn: Callable | None = None,
) -> tuple[list[dict], bool]:
    """Check for skipped speech in SRT gaps and recover via re-transcription.

    Parameters
    ----------
    segments:
        Transcribed segments (each with ``start``, ``end``, ``text``).
    audio_path:
        Path to the source audio file.
    audio_duration:
        Total length of the audio in seconds.
    gap_threshold:
        Minimum gap duration (seconds) that triggers investigation.
    energy_ratio:
        A gap's RMS energy must exceed ``reference_rms * energy_ratio``
        to be considered speech.
    transcribe_gap_fn:
        ``fn(audio_path, start, end) -> list[dict]`` — callback that
        re-transcribes a specific time range and returns segments with
        timestamps already offset to the original timeline.

    Returns
    -------
    (patched_segments, was_modified)
    """
    gaps = _find_gaps(segments, audio_duration, gap_threshold)
    if not gaps:
        log.info("Validation: no gaps > %.1fs", gap_threshold)
        return segments, False

    log.info("Validation: %d gap(s) > %.1fs found", len(gaps), gap_threshold)

    ref = _pick_reference(segments)
    if not ref:
        log.warning("Validation: no reference segment available")
        return segments, False

    ref_pcm = _extract_pcm(audio_path, ref["start"], ref["end"])
    ref_rms = _rms(ref_pcm)
    ref_zcr_val = _zcr(ref_pcm)

    if ref_rms < 1e-6:
        log.warning("Validation: reference has near-zero energy")
        return segments, False

    log.info(
        "Validation: ref=%.1f-%.1fs rms=%.4f zcr=%.3f",
        ref["start"], ref["end"], ref_rms, ref_zcr_val,
    )

    recovered: list[dict] = []
    for gs, ge in gaps:
        gap_pcm = _extract_pcm(audio_path, gs, ge)
        gap_rms = _rms(gap_pcm)
        gap_zcr = _zcr(gap_pcm)

        has_energy = gap_rms > ref_rms * energy_ratio
        has_speech_zcr = 0.01 < gap_zcr < 0.40

        if not (has_energy and has_speech_zcr):
            log.info(
                "  Gap %.1f-%.1fs: no speech (rms=%.4f zcr=%.3f)",
                gs, ge, gap_rms, gap_zcr,
            )
            continue

        log.info(
            "  Gap %.1f-%.1fs: speech detected (rms=%.4f zcr=%.3f)",
            gs, ge, gap_rms, gap_zcr,
        )

        if transcribe_gap_fn is None:
            continue

        gap_segs = transcribe_gap_fn(audio_path, gs, ge)
        if gap_segs:
            log.info(
                "  Recovered %d segment(s) from %.1f-%.1fs",
                len(gap_segs), gs, ge,
            )
            recovered.extend(gap_segs)

    if not recovered:
        log.info("Validation: no segments recovered")
        return segments, False

    merged = sorted(segments + recovered, key=lambda s: s["start"])
    log.info(
        "Validation: recovered %d segment(s), %d → %d total",
        len(recovered), len(segments), len(merged),
    )
    return merged, True
