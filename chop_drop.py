#!/usr/bin/env python3
"""
T_Dubber · "Chop & Drop" worker  (Moon Mission)
================================================
Dub a 100 GB video on Kaggle (20 GB disk, 12 h hard timeout) without ever
holding the whole file:

    CHOP  -> ffmpeg streams ONE 10-minute chunk off the wire (no full download)
    DROP  -> Mazinger dubs it, tgup uploads it, then the chunk is deleted
    LOOP  -> next chunk, until the video ends
    SAFETY-> at 11 h 30 m, stop cleanly and checkpoint progress to disk

Resume-safe: state is atomically saved after every *finished* chunk, so a
Kaggle kill costs at most one chunk of work.

Usage:
    python chop_drop.py "https://example.com/huge_movie.mp4"
    python chop_drop.py "https://..." --chunk-minutes 10
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"

CHUNK_SECONDS = 600                                # 10-minute working chunk
HARD_TIME_LIMIT = timedelta(hours=11, minutes=30)  # stop 30 min before Kaggle's 12 h kill
TEMP_CHUNK = "temp_chunk.mp4"                      # single scratch file, recycled every chunk
STATE_FILE = "chop_drop_state.json"                # resume checkpoint
MAX_RETRIES = 3                                    # transient network blips are normal at 100 GB
NET_RW_TIMEOUT_US = 30_000_000                     # 30 s ffmpeg network stall timeout (microseconds)
END_OF_STREAM_EPS = 1.0                            # chunks shorter than 1 s = "nothing left"


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    """Kaggle buffers stdout; flush every line so logs survive a kill."""
    print(msg, flush=True)


def fmt_duration(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


def remove_quietly(path: str) -> None:
    """Best-effort delete. Frees disk even if the file is locked/missing."""
    try:
        if os.path.exists(path):
            os.remove(path)
            log(f"[disk] removed {path}")
    except OSError as exc:
        log(f"[disk] WARNING: could not remove {path}: {exc}")


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #
def probe_duration(target: str) -> float | None:
    """
    ffprobe the total duration (seconds) of a REMOTE URL or a local file.
    Returns None if the duration is unavailable (e.g. live stream).
    ffprobe only reads container metadata (moov atom) via HTTP range requests —
    it does NOT download the video.
    """
    cmd = [
        FFPROBE_BIN,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",  # raw number, no JSON noise
        target,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log(f"[ffprobe] failed: {exc}")
        return None
    if proc.returncode != 0:
        log(f"[ffprobe] could not read duration: {proc.stderr.strip()[:300]}")
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None  # e.g. "N/A" for live/segmented streams


def server_supports_range(url: str) -> bool:
    """
    CRITICAL CHECK: does the host honour HTTP Range requests?
    Without it, ffmpeg must download from byte 0 for EVERY chunk -> O(n^2)
    disaster on a 100 GB file. We probe with HEAD, then a 1-byte ranged GET.
    """
    from urllib.request import Request, urlopen

    probes = [
        Request(url, method="HEAD", headers={"User-Agent": "T-Dubber/1.0"}),
        Request(url, headers={"User-Agent": "T-Dubber/1.0", "Range": "bytes=0-0"}),
    ]
    for req in probes:
        try:
            with urlopen(req, timeout=30) as resp:
                if resp.headers.get("Accept-Ranges", "").lower() == "bytes":
                    return True
                if resp.status == 206:  # server actually served our 1-byte range
                    return True
        except Exception as exc:  # noqa: BLE001 — any failure just means "unknown"
            log(f"[warn] range probe failed: {exc}")
    return False


# --------------------------------------------------------------------------- #
# The CHOP: stream exactly one window off the wire
# --------------------------------------------------------------------------- #
def download_chunk(url: str, start_sec: float, chunk_sec: float, out_path: str) -> str:
    """
    Stream ONE chunk [start_sec, start_sec + chunk_sec] from the remote URL
    into out_path. Returns:
        "ok"    -> chunk written
        "empty" -> clean end-of-stream (nothing left to fetch)
        "error" -> real failure (retryable)
    """
    cmd = [
        FFMPEG_BIN,
        "-nostdin",                        # ffmpeg must never eat our stdin (we're in a loop)
        "-hide_banner",
        "-loglevel", "error",              # only real errors -> clean logs
        "-y",                              # overwrite any leftover temp file
        "-rw_timeout", str(NET_RW_TIMEOUT_US),  # abort on a 30 s network stall instead of hanging
        "-ss", f"{start_sec:.3f}",         # INPUT-side seek: fast (keyframe / HTTP-range seek)
        "-i", url,                         # read straight from the URL — the file is never fully downloaded
        "-t", f"{chunk_sec:.3f}",          # stop after exactly this many seconds
        "-c", "copy",                      # bitstream remux — zero re-encoding, Kaggle CPU stays free
        "-reset_timestamps", "1",          # each chunk becomes a standalone clip starting at t=0
        "-movflags", "+faststart",         # moov atom up front -> playable while uploading
        "-f", "mp4",
        out_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(
            "ffmpeg binary not found — on Kaggle run: !apt update && apt install -y ffmpeg"
        )

    # Success = a non-empty output file exists.
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return "ok"

    # No output. Either the stream truly ended, or something broke.
    err = (proc.stderr or "").lower()
    if proc.returncode == 0 or "empty" in err:  # "Output file is empty, nothing was encoded"
        return "empty"
    log(f"[ffmpeg] rc={proc.returncode}: {proc.stderr.strip()[:500]}")
    return "error"


# --------------------------------------------------------------------------- #
# Placeholders for the mission's other stages
# --------------------------------------------------------------------------- #
def process_chunk(filepath: str) -> str:
    """PLACEHOLDER — Mazinger AI dubbing stage. Return the path to the dubbed file."""
    log(f"[mazinger] dubbing {filepath} ...")
    # dubbed = mazinger.dub(filepath)
    # return dubbed
    return filepath


def upload_chunk(filepath: str) -> None:
    """PLACEHOLDER — tgup Go engine: upload the chunk to Telegram."""
    log(f"[tgup] uploading {filepath} ...")
    # tgup.upload(filepath)


# --------------------------------------------------------------------------- #
# Resume state (atomic: temp file + os.replace, never a half-written checkpoint)
# --------------------------------------------------------------------------- #
def load_state(url: str, state_file: str) -> dict | None:
    """Return the checkpoint if it belongs to this exact URL, else None."""
    if not os.path.exists(state_file):
        return None
    try:
        with open(state_file, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"[state] ignoring unreadable state file ({exc})")
        return None
    if state.get("url") != url:
        log("[state] saved state belongs to a different URL — starting fresh")
        return None
    return state


def save_state(url: str, chunk_index: int, next_start: float, total: float | None,
               state_file: str) -> None:
    """Persist 'which chunk we are on' atomically."""
    state = {
        "url": url,
        "chunk_index": chunk_index,
        "next_start_seconds": round(next_start, 3),
        "total_duration_seconds": round(total, 3) if total else None,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    tmp = state_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, state_file)  # atomic on POSIX and NTFS


# --------------------------------------------------------------------------- #
# The Chop & Drop loop
# --------------------------------------------------------------------------- #
def chop_and_drop(url: str, chunk_seconds: float = CHUNK_SECONDS,
                  state_file: str = STATE_FILE) -> int:
    """Stream-chunk-process-upload-delete a remote video, chunk by chunk."""
    started = time.monotonic()
    deadline = started + HARD_TIME_LIMIT.total_seconds()  # 11 h 30 m wall clock

    log(f"[start] T_Dubber 'Chop & Drop' — hard limit {HARD_TIME_LIMIT}, chunk {chunk_seconds:.0f}s")

    if not server_supports_range(url):
        log("[WARN] server did not confirm HTTP Range support.")
        log("[WARN] without it, ffmpeg re-downloads from byte 0 for EVERY chunk (O(n^2)).")
        log("[WARN] continuing — watch the transfer rate and abort if it crawls.")

    # 1) Proper ffprobe of the ONLINE video first.
    total = probe_duration(url)
    if total:
        log(f"[probe] source duration: {fmt_duration(total)} ({total / 3600:.2f} h) "
            f"-> ~{math.ceil(total / chunk_seconds)} chunks")
    else:
        log("[probe] duration unknown — will loop until ffmpeg reports end-of-stream.")

    # 2) Resume from checkpoint if one exists for this URL.
    state = load_state(url, state_file)
    if state:
        chunk_index = int(state["chunk_index"])
        start = float(state["next_start_seconds"])
        log(f"[resume] continuing at chunk #{chunk_index} (t={start:.1f}s)")
    else:
        chunk_index, start = 0, 0.0

    cycle_seconds: float | None = None  # rolling measurement of one full CHOP->DROP cycle
    exit_code = 0

    while True:
        now = time.monotonic()

        # --- FAILSAFE 1: hard wall clock. Never run past 11 h 30 m. ---------
        if now >= deadline:
            log(f"[failsafe] {HARD_TIME_LIMIT} wall-clock reached — stopping cleanly.")
            break
        # --- FAILSAFE 2: never START a chunk we cannot finish in time. ------
        if cycle_seconds is not None and (deadline - now) < cycle_seconds * 1.5:
            log("[failsafe] too little time left for one more full chunk — stopping cleanly.")
            break
        # --- Natural end of the video. --------------------------------------
        if total and start + 0.5 >= total:
            log("[done] every chunk processed — video complete.")
            break

        chunk_len = chunk_seconds if not total else min(chunk_seconds, total - start)
        where = f"{fmt_duration(start)} -> {fmt_duration(start + chunk_len)}"
        log(f"[chunk #{chunk_index}] {where}" + (f"  ({start / total * 100:.1f}%)" if total else ""))

        # ---- CHOP: stream exactly this window off the wire ------------------
        status = "error"
        for attempt in range(1, MAX_RETRIES + 1):
            remove_quietly(TEMP_CHUNK)
            status = download_chunk(url, start, chunk_len, TEMP_CHUNK)
            if status != "error":
                break
            log(f"[retry {attempt}/{MAX_RETRIES}] download failed, backing off...")
            time.sleep(min(2 ** attempt, 30))

        if status == "empty":
            log("[done] ffmpeg fetched nothing — end of stream reached.")
            remove_quietly(TEMP_CHUNK)
            break
        if status != "ok":
            log("[fatal] chunk download failed repeatedly; state is saved — rerun to resume.")
            remove_quietly(TEMP_CHUNK)
            exit_code = 1
            break

        actual = probe_duration(TEMP_CHUNK)
        if not actual or actual < END_OF_STREAM_EPS:
            log("[done] chunk contains no playable media — assuming end of stream.")
            remove_quietly(TEMP_CHUNK)
            break

        # ---- PROCESS + UPLOAD + DROP ----------------------------------------
        cycle_start = time.monotonic()
        dubbed = None
        try:
            dubbed = process_chunk(TEMP_CHUNK)   # Mazinger AI dubbing (placeholder)
            upload_chunk(dubbed)                 # tgup Go engine -> Telegram (placeholder)
        finally:
            remove_quietly(TEMP_CHUNK)
            if dubbed and os.path.abspath(dubbed) != os.path.abspath(TEMP_CHUNK):
                remove_quietly(dubbed)

        cycle_seconds = time.monotonic() - cycle_start

        # ---- CHECKPOINT: only now is this chunk fully handled --------------
        start += chunk_len
        chunk_index += 1
        save_state(url, chunk_index, start, total, state_file)

    save_state(url, chunk_index, start, total, state_file)
    log(f"[exit] stopped after chunk #{chunk_index - 1}; next run resumes at t={start:.1f}s "
        f"({(time.monotonic() - started) / 3600:.2f} h used)")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="T_Dubber 'Chop & Drop' worker for Kaggle")
    parser.add_argument("url", help="Direct video URL (HTTP/HTTPS mp4 stream)")
    parser.add_argument("--chunk-minutes", type=float, default=CHUNK_SECONDS / 60.0,
                        help="chunk length in minutes (default: 10)")
    args = parser.parse_args()
    try:
        return chop_and_drop(args.url, chunk_seconds=args.chunk_minutes * 60.0)
    except KeyboardInterrupt:
        log("[interrupt] Ctrl-C caught — state preserved, safe to rerun.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
