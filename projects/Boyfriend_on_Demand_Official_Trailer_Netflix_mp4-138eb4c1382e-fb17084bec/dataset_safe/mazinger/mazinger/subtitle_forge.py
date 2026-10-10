"""Wrapper for the Rust subtitle_forge binary.

This module provides a Python interface to the compiled subtitle_forge.exe,
which converts faster-whisper JSON transcripts into styled .srt and .ass files.

The binary must be built with: cargo build --release (in subtitle_forge/)
Expected location: <repo_root>/subtitle_forge/target/release/subtitle_forge.exe
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class SubtitleForgeResult:
    """Result of a subtitle_forge run."""
    success: bool
    srt_path: Optional[str] = None
    ass_path: Optional[str] = None
    error: str = ""


def _repo_root() -> Path:
    """Return the workspace root (3 dirname hops from this file)."""
    # File is at: <repo_root>/mazinger/mazinger/subtitle_forge.py
    # parent -> mazinger/mazinger
    # parent -> mazinger
    # parent -> repo_root
    return Path(__file__).resolve().parent.parent.parent


def _subtitle_forge_binary() -> Optional[Path]:
    """Locate the subtitle_forge binary, preferring release build."""
    root = _repo_root()
    candidates = [
        root / "subtitle_forge" / "target" / "release" / "subtitle_forge.exe",
        root / "subtitle_forge" / "target" / "release" / "subtitle_forge",
        root / "subtitle_forge" / "target" / "debug" / "subtitle_forge.exe",
        root / "subtitle_forge" / "target" / "debug" / "subtitle_forge",
    ]
    for c in candidates:
        if c.is_file():
            return c
    # Fallback: check PATH
    on_path = shutil.which("subtitle_forge")
    if on_path:
        return Path(on_path)
    return None


def _subtitle_forge_wanted() -> bool:
    """Check if the fast path is explicitly disabled via env var."""
    return os.environ.get("MAZINGER_SUBTITLE_FORGE", "on").lower() not in ("0", "off", "false", "no")


def run_subtitle_forge(
    transcript_json: str,
    srt_output: Optional[str] = None,
    ass_output: Optional[str] = None,
    *,
    font: str = "Roboto",
    font_size_ratio: float = 0.05,
    margin_v_ratio: float = 0.10,
    margin_x_ratio: float = 0.0625,
    shadow_alpha: int = 128,
    max_chars: int = 42,
    max_lines: int = 2,
    play_res_x: int = 1920,
    play_res_y: int = 1080,
    title: str = "T_Dubber",
) -> SubtitleForgeResult:
    """Run subtitle_forge on a transcript JSON file.

    Args:
        transcript_json: Path to faster-whisper JSON transcript (segments with words).
        srt_output: Optional path to write .srt file.
        ass_output: Optional path to write .ass file.
        font: Font family for ASS style.
        font_size_ratio: Font size as ratio of PlayResY.
        margin_v_ratio: Bottom margin as ratio of PlayResY.
        margin_x_ratio: Left/right margin as ratio of PlayResX.
        shadow_alpha: Drop-shadow alpha (0=opaque, 255=invisible).
        max_chars: Max characters per line (0 = no wrapping).
        max_lines: Max lines per cue.
        play_res_x: ASS PlayResX.
        play_res_y: ASS PlayResY.
        title: Title for ASS [Script Info].

    Returns:
        SubtitleForgeResult with success status and output paths.
    """
    binary = _subtitle_forge_binary()
    if not binary:
        return SubtitleForgeResult(
            success=False,
            error="subtitle_forge binary not found. Build with: cargo build --release (in subtitle_forge/)"
        )

    if not _subtitle_forge_wanted():
        return SubtitleForgeResult(success=False, error="MAZINGER_SUBTITLE_FORGE=off")

    # Ensure at least one output is requested
    if not srt_output and not ass_output:
        return SubtitleForgeResult(success=False, error="at least one of srt_output or ass_output must be provided")

    cmd = [
        str(binary),
        "--input", transcript_json,
    ]
    if srt_output:
        cmd += ["--srt", srt_output]
    if ass_output:
        cmd += ["--ass", ass_output]
    cmd += [
        "--font", font,
        "--font-size", str(int(play_res_y * font_size_ratio)),
        "--margin-v", str(int(play_res_y * margin_v_ratio)),
        "--margin-l", str(int(play_res_x * margin_x_ratio)),
        "--margin-r", str(int(play_res_x * margin_x_ratio)),
        "--shadow-alpha", str(shadow_alpha),
        "--max-chars", str(max_chars),
        "--max-lines", str(max_lines),
        "--play-res-x", str(play_res_x),
        "--play-res-y", str(play_res_y),
        "--title", title,
    ]

    log.debug("Running subtitle_forge: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return SubtitleForgeResult(success=False, error="subtitle_forge timed out after 60s")
    except OSError as e:
        return SubtitleForgeResult(success=False, error=f"failed to spawn subtitle_forge: {e}")

    if result.returncode != 0:
        return SubtitleForgeResult(
            success=False,
            error=f"subtitle_forge exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
        )

    # Verify outputs exist and are non-empty
    if srt_output and (not os.path.exists(srt_output) or os.path.getsize(srt_output) == 0):
        return SubtitleForgeResult(success=False, error="subtitle_forge produced empty or missing SRT")
    if ass_output and (not os.path.exists(ass_output) or os.path.getsize(ass_output) == 0):
        return SubtitleForgeResult(success=False, error="subtitle_forge produced empty or missing ASS")

    return SubtitleForgeResult(
        success=True,
        srt_path=srt_output,
        ass_path=ass_output,
    )


def transcribe_segments_to_json(
    segments: list[dict],
    output_path: str,
) -> bool:
    """Save transcription segments to JSON format expected by subtitle_forge.

    subtitle_forge accepts either:
    - {"segments": [...]}  (object with segments key)
    - [...]                (bare array of segments)

    Each segment should have: start, end, text, and optionally words.

    Args:
        segments: List of segment dicts (from faster-whisper etc.)
        output_path: Path to write the JSON file.

    Returns:
        True on success, False on failure.
    """
    try:
        # Write bare array format (simpler, subtitle_forge accepts both)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(segments, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log.error("Failed to write transcript JSON: %s", e)
        return False


def generate_subtitles_from_segments(
    segments: list[dict],
    base_path: str,
    *,
    font: str = "Roboto",
    font_size_ratio: float = 0.05,
    margin_v_ratio: float = 0.10,
    margin_x_ratio: float = 0.0625,
    shadow_alpha: int = 128,
    max_chars: int = 42,
    max_lines: int = 2,
    play_res_x: int = 1920,
    play_res_y: int = 1080,
    title: str = "T_Dubber",
) -> SubtitleForgeResult:
    """Convenience: save segments as JSON and run subtitle_forge to generate SRT/ASS.

    This is a one-stop function that:
    1. Writes segments to <base_path>.transcript.json
    2. Runs subtitle_forge to produce <base_path>.srt and <base_path>.ass

    Args:
        segments: Transcription segments (with start, end, text, words).
        base_path: Base path (without extension) for output files.
        ...: Style options passed to run_subtitle_forge.

    Returns:
        SubtitleForgeResult with generated file paths.
    """
    json_path = f"{base_path}.transcript.json"
    srt_path = f"{base_path}.srt"
    ass_path = f"{base_path}.ass"

    if not transcribe_segments_to_json(segments, json_path):
        return SubtitleForgeResult(success=False, error="failed to write transcript JSON")

    return run_subtitle_forge(
        transcript_json=json_path,
        srt_output=srt_path,
        ass_output=ass_path,
        font=font,
        font_size_ratio=font_size_ratio,
        margin_v_ratio=margin_v_ratio,
        margin_x_ratio=margin_x_ratio,
        shadow_alpha=shadow_alpha,
        max_chars=max_chars,
        max_lines=max_lines,
        play_res_x=play_res_x,
        play_res_y=play_res_y,
        title=title,
    )


def check_subtitle_forge() -> dict:
    """Report whether subtitle_forge is available, for doctor.py."""
    binary = _subtitle_forge_binary()
    if not binary:
        return {
            "present": False,
            "runnable": False,
            "reason": "not built. Run: cargo build --release (in subtitle_forge/)",
        }
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode in (0, 1, 2):
            version = (result.stdout or result.stderr).strip().splitlines()[0]
            return {
                "present": True,
                "runnable": True,
                "path": str(binary),
                "version": version,
            }
    except Exception:
        pass
    return {
        "present": True,
        "runnable": False,
        "path": str(binary),
        "reason": "binary exists but will not run (Smart App Control?)",
    }