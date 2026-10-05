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

The two engine stages are wired to the real engines:
* process_chunk() shells out to `python -m mazinger dub`, one project
  slug per chunk so outputs never collide, then stages the dubbed file
  outside Mazinger's project tree and removes that tree so a 20 GB
  Kaggle disk only ever holds roughly one chunk of intermediates.
* upload_chunk() asks the Havaldar (db.py) for the next Telegram
  channel with daily quota (round robin, 50 GB/channel/day), runs the
  tgup Go binary against it, and refunds the reserved quota if the
  transfer fails for any reason.

Resume-safe: state is atomically saved after every *finished* chunk, so a
Kaggle kill costs at most one chunk of work.

Usage:
    python chop_drop.py "https://example.com/huge_movie.mp4"
    python chop_drop.py "https://..." --chunk-minutes 10

Environment:
    TGUP_API_ID / TGUP_API_HASH / TGUP_PHONE   Telegram credentials
    TGUP_BIN                                   optional tgup binary path
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

try:
    import db  # the Havaldar: round-robin channels + daily quota ledger
except ImportError as exc:
    raise SystemExit(
        f"[fatal] db.py (the Havaldar) must sit beside chop_drop.py: {exc}"
    ) from exc

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

# --- Engines --------------------------------------------------------------- #
MAZINGER_DIR = os.path.join(APP_DIR, "mazinger")       # `python -m mazinger` runs from here
MAZINGER_OUTPUT_DIR = os.path.join(APP_DIR, "mazinger_output")
TARGET_LANGUAGE = "Hindi"                              # the mission's dubbing language
OUTPUT_TYPE = "video"                                  # Telegram wants video, not audio-only
KEEP_PROJECTS = False                                  # keep Mazinger trees for QA (costs disk)
TGUP_CONCURRENCY = 3                                   # parallel MTProto connections
TGUP_TIMEOUT_SEC = 3600.0                              # kill a stuck upload after 1 h (0 = off)


class QuotaExhaustedError(RuntimeError):
    """Every Telegram channel has spent its daily 50 GB quota."""


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
# Shared subprocess runner: logs every line live so the UI can follow along
# --------------------------------------------------------------------------- #
def _run_streaming(cmd: list[str], cwd: str = None, env: dict = None,
                   stdin_text: str = None, timeout: float = None,
                   formatter=None):
    """Run a command, echoing each output line through log() as it arrives.

    Returns (returncode, full_output). A timeout kills the child and
    returns -1 so callers can treat it as a failure with a known cause.
    ``formatter`` optionally rewrites a line (e.g. tgup's JSON progress
    events) before it is logged.
    """
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,  # merge so interleaving is preserved
        "stdin": subprocess.PIPE if stdin_text else subprocess.DEVNULL,
        "text": True,
        "bufsize": 1,  # line-buffered: progress must not sit in a pipe buffer
    }
    if cwd:
        kwargs["cwd"] = cwd
    if env:
        kwargs["env"] = env
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except FileNotFoundError:
        raise RuntimeError(f"command not found: {cmd[0]}")

    if stdin_text:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except (OSError, BrokenPipeError):
            pass  # the child may have exited before reading credentials

    lines = []
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        if line:
            log(f"    {formatter(line) if formatter else line}")
        lines.append(line)
    try:
        returncode = proc.wait(timeout=timeout) if timeout else proc.wait()
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        log(f"[worker] killed after {timeout:.0f}s timeout")
        returncode = -1
    return returncode, "\n".join(lines)


# --------------------------------------------------------------------------- #
# Mazinger: the AI dubbing engine
# --------------------------------------------------------------------------- #
def _find_dubbed_output(project_root: str) -> str | None:
    """Locate Mazinger's final output inside a finished project tree.

    Video first (that is what Telegram receives), then the audio-only
    fallbacks. Newest hit wins: a resumed run may leave several behind.
    """
    if not os.path.isdir(project_root):
        return None
    for name in ("dubbed.mp4", "dubbed.mkv", "dubbed.wav",
                 "dubbed.m4a", "dubbed.mp3"):
        hits = [
            p for p in glob.glob(os.path.join(project_root, "**", name),
                                 recursive=True)
            if os.path.isfile(p) and os.path.getsize(p) > 0
        ]
        if hits:
            hits.sort(key=lambda p: os.path.getmtime(p))
            return hits[-1]
    return None


def process_chunk(filepath: str, chunk_index: int = 0) -> str:
    """Dub ONE chunk with Mazinger (``python -m mazinger dub``).

    Each chunk gets its own project slug, so a resumed run never
    collides with a previous chunk and the output location is
    predictable. The dubbed file is moved out of Mazinger's project
    tree and the tree is then removed, which keeps Kaggle's 20 GB
    disk bounded to roughly one chunk of intermediates at a time
    (transcription, TTS segments, voice profile).

    Returns the path of the dubbed output file.
    """
    if not os.path.isdir(MAZINGER_DIR):
        log("[mazinger] WARNING: no mazinger folder beside chop_drop.py — "
            "assuming `python -m mazinger` is installed in this environment")

    slug = f"chopdrop-chunk-{chunk_index:06d}"
    project_root = os.path.join(MAZINGER_OUTPUT_DIR, "projects", slug)
    cmd = [
        sys.executable, "-m", "mazinger", "dub", os.path.abspath(filepath),
        "--slug", slug,                 # unique per chunk: no output collisions
        "--base-dir", MAZINGER_OUTPUT_DIR,
        "--output-type", OUTPUT_TYPE,
        "--target-language", TARGET_LANGUAGE,
    ]
    log(f"[mazinger] dubbing chunk #{chunk_index} as project '{slug}' "
        f"(target: {TARGET_LANGUAGE})")

    env = dict(os.environ)
    if os.path.isdir(MAZINGER_DIR):
        # Make `import mazinger` resolve to the workspace folder even
        # when the worker was started from a different directory.
        env["PYTHONPATH"] = MAZINGER_DIR + os.pathsep + env.get("PYTHONPATH", "")
    returncode, _output = _run_streaming(
        cmd,
        cwd=MAZINGER_DIR if os.path.isdir(MAZINGER_DIR) else None,
        env=env,
    )
    if returncode != 0:
        raise RuntimeError(
            f"mazinger dub exited {returncode} for chunk #{chunk_index}"
        )

    dubbed = _find_dubbed_output(project_root)
    if not dubbed:
        raise RuntimeError(
            f"mazinger finished but left no dubbed output under {project_root}"
        )
    log(f"[mazinger] chunk #{chunk_index} dubbed -> {dubbed} "
        f"({os.path.getsize(dubbed) / 1e6:.1f} MB)")

    if not KEEP_PROJECTS:
        # Move the final file out, then reclaim the whole project tree.
        staged = os.path.join(
            APP_DIR, f"dubbed_chunk_{chunk_index:06d}"
            + os.path.splitext(dubbed)[1],
        )
        shutil.move(dubbed, staged)
        shutil.rmtree(project_root, ignore_errors=True)
        log(f"[disk] mazinger project tree removed; output staged at {staged}")
        return staged
    return dubbed


# --------------------------------------------------------------------------- #
# tgup: the Go upload engine, fronted by the Havaldar's quota ledger
# --------------------------------------------------------------------------- #
def _load_tg_credentials() -> tuple[str, str, str]:
    """Return (api_id, api_hash, phone) for tgup.

    Environment first (the worker/Kaggle way), then config.json's
    plaintext fields. The DPAPI-encrypted api_hash in config.json
    only decrypts on the Windows machine that encrypted it, so it is
    deliberately never attempted here.
    """
    api_id = (os.environ.get("TGUP_API_ID")
              or os.environ.get("TG_API_ID") or "").strip()
    api_hash = (os.environ.get("TGUP_API_HASH")
                or os.environ.get("TG_API_HASH") or "").strip()
    phone = (os.environ.get("TGUP_PHONE")
             or os.environ.get("TG_API_PHONE") or "").strip()

    if not api_id or not api_hash:
        try:
            with open(os.path.join(APP_DIR, "config.json"),
                      "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            api_id = api_id or str(cfg.get("api_id") or "").strip()
            api_hash = api_hash or str(cfg.get("api_hash") or "").strip()
            phone = phone or str(cfg.get("phone") or "").strip()
        except (OSError, json.JSONDecodeError):
            pass

    if not api_id or not api_hash:
        raise RuntimeError(
            "Telegram credentials not found. Set TGUP_API_ID and "
            "TGUP_API_HASH in the worker environment (config.json's "
            "api_hash is DPAPI-encrypted and only decrypts on the "
            "machine that encrypted it)."
        )
    return api_id, api_hash, phone


def _tgup_base_command() -> list[str]:
    """Locate the tgup Go binary, reusing tgup_bridge's discovery."""
    try:
        import tgup_bridge
        base = tgup_bridge.get_base_command()
        if base:
            return base
    except ImportError:
        pass
    # Manual fallback: TGUP_BIN override, beside this script, then PATH.
    override = os.environ.get("TGUP_BIN", "").strip()
    if override and os.path.isfile(override):
        return [override]
    for name in ("tgup.exe", "tgup"):
        candidate = os.path.join(APP_DIR, name)
        if os.path.isfile(candidate):
            return [candidate]
    on_path = shutil.which("tgup")
    if on_path:
        return [on_path]
    raise RuntimeError(
        "tgup Go binary not found — set TGUP_BIN or build it "
        "(go build -o tgup .) beside chop_drop.py"
    )


def _pretty_tgup_line(line: str) -> str:
    """Turn a tgup JSON progress event into a short human line."""
    if not line.startswith("{"):
        return line
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return line
    if "event" not in data:
        return line
    part = int(data.get("part", 0))
    part_count = int(data.get("part_count", 0))
    done_mb = int(data.get("bytes") or 0) / 1e6
    total_mb = int(data.get("total") or 0) / 1e6
    rate = float(data.get("bytes_per_sec") or 0)
    if rate:
        return f"part {part}/{part_count} · {done_mb:.1f}/{total_mb:.1f} MB · {rate / 1e6:.1f} MB/s"
    if data.get("event") == "progress":
        return f"part {part}/{part_count} · {done_mb:.1f} MB"
    message = data.get("message") or ""
    return f"{data.get('event')}: {message}".strip(": ")


def _read_tgup_result(path: str) -> dict:
    """Read tgup's --result-out JSON; {} when absent or malformed."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def upload_chunk(filepath: str) -> dict:
    """Upload the dubbed chunk to Telegram with the Go engine (tgup).

    The Havaldar (db.py) picks the channel first: round robin across
    channels.json, skipping any channel that has already spent its
    50 GB daily quota. The chosen channel's quota is reserved inside
    the same transaction, so two workers can never be handed the same
    budget. If the upload fails for any reason, the reservation is
    refunded so a failed transfer never counts against the channel
    until midnight.

    Returns tgup's result dict (message_link, parts, throughput...).
    Raises QuotaExhaustedError when every channel is out of quota,
    RuntimeError on any other failure.
    """
    size_bytes = os.path.getsize(filepath)
    size_mb = size_bytes / (1024 * 1024)

    # tgup_bridge owns the session path and the "may we log in at all" rule.
    # It is imported here rather than at module level so chop_drop still runs
    # when the bridge is absent.
    try:
        import tgup_bridge
    except ImportError:
        tgup_bridge = None  # type: ignore[assignment]

    # tgup can only send with a session it already holds. Starting one here
    # would ask Telegram for a login code nobody at this end can answer -- an
    # OTP spent, then an EOF failure -- so refuse BEFORE any quota is
    # reserved. See tgup_bridge.session_refusal().
    refusal = tgup_bridge.session_refusal("upload") if tgup_bridge else ""
    if refusal:
        raise RuntimeError(refusal)

    # 1) Round-robin channel with enough daily quota left.
    channel = db.get_next_telegram_channel(size_mb)
    if not channel:
        raise QuotaExhaustedError(
            f"every channel in channels.json has spent its "
            f"{db.CHANNEL_DAILY_QUOTA_MB:.0f} MB daily quota"
        )
    log(f"[tgup] channel {channel} selected by round-robin "
        f"({size_mb:.1f} MB against its {db.CHANNEL_DAILY_QUOTA_MB:.0f} MB "
        f"daily quota)")

    # 2) Hand the file and the channel to the Go binary.
    api_id, api_hash, phone = _load_tg_credentials()
    base = _tgup_base_command()
    result_file = filepath + ".tgup-result.json"
    cmd = [
        *base, "upload",
        "--file", os.path.abspath(filepath),
        "--channel", channel,
        "--credentials-stdin",   # secrets travel via stdin, not the cmdline
        "--concurrency", str(TGUP_CONCURRENCY),
        "--result-out", result_file,
    ]
    if tgup_bridge is not None:
        # Named explicitly so a run from any working directory still finds the
        # session it is expected to have, instead of looking beside itself.
        cmd += ["--session", str(tgup_bridge.session_path())]
    if phone:
        cmd += ["--phone", phone]
    credentials = json.dumps({"api_id": api_id, "api_hash": api_hash}) + "\n"

    log(f"[tgup] uploading {os.path.basename(filepath)} ({size_mb:.1f} MB) "
        f"to {channel} ...")
    try:
        returncode, _output = _run_streaming(
            cmd,
            stdin_text=credentials,
            timeout=TGUP_TIMEOUT_SEC or None,
            formatter=_pretty_tgup_line,
        )
        if returncode != 0:
            raise RuntimeError(
                f"tgup exited {returncode} while uploading to {channel}"
            )
        result = _read_tgup_result(result_file)
        if not result.get("ok", returncode == 0):
            raise RuntimeError(
                f"tgup upload to {channel} failed: "
                f"{result.get('error') or 'unknown error'}"
            )
    except BaseException:
        # Any failure — network, timeout, crash, even Ctrl-C — gives
        # the reserved quota back before the error travels upward.
        db.release_channel_quota(channel, size_mb)
        log(f"[tgup] {size_mb:.1f} MB quota refunded to {channel}")
        raise
    finally:
        remove_quietly(result_file)

    log(f"[tgup] upload complete -> {result.get('message_link') or channel} "
        f"({int(result.get('chunk_count') or 1)} part(s), "
        f"{float(result.get('bytes_per_sec') or 0) / 1e6:.1f} MB/s)")
    result["channel"] = channel
    return result


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
            try:
                dubbed = process_chunk(TEMP_CHUNK, chunk_index=chunk_index)
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        upload_chunk(dubbed)
                        break
                    except QuotaExhaustedError:
                        # Retrying cannot free quota — stop now.
                        raise
                    except Exception as exc:  # noqa: BLE001
                        log(f"[tgup] upload attempt {attempt}/{MAX_RETRIES} "
                            f"failed: {exc}")
                        if attempt >= MAX_RETRIES:
                            raise
                        time.sleep(min(2 ** attempt, 30))
            except Exception as exc:  # noqa: BLE001
                # A failed chunk is NOT checkpointed, so a rerun
                # redoes it: at-least-once delivery to Telegram.
                log(f"[fatal] chunk #{chunk_index} failed: {exc}")
                log("[fatal] state is saved — rerun to resume this chunk.")
                exit_code = 1
                break
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
    global TARGET_LANGUAGE, OUTPUT_TYPE, KEEP_PROJECTS
    global TGUP_CONCURRENCY, TGUP_TIMEOUT_SEC

    parser = argparse.ArgumentParser(
        description="T_Dubber 'Chop & Drop' worker for Kaggle")
    parser.add_argument("url", help="Direct video URL (HTTP/HTTPS mp4 stream)")
    parser.add_argument("--chunk-minutes", type=float,
                        default=CHUNK_SECONDS / 60.0,
                        help="chunk length in minutes (default: 10)")
    parser.add_argument("--target-language", default=TARGET_LANGUAGE,
                        help="Mazinger dubbing language "
                             "(default: %(default)s)")
    parser.add_argument("--output-type", choices=("audio", "video"),
                        default=OUTPUT_TYPE,
                        help="what Mazinger produces for Telegram "
                             "(default: %(default)s)")
    parser.add_argument("--keep-projects", action="store_true",
                        help="keep Mazinger project trees (SRTs, TTS "
                             "segments) for QA; uses more disk")
    parser.add_argument("--tgup-concurrency", type=int,
                        default=TGUP_CONCURRENCY,
                        help="parallel tgup connections "
                             "(default: %(default)s)")
    parser.add_argument("--tgup-timeout", type=float,
                        default=TGUP_TIMEOUT_SEC,
                        help="seconds before a stuck upload is killed "
                             "(0 = unlimited, default: %(default)s)")
    args = parser.parse_args()

    TARGET_LANGUAGE = args.target_language
    OUTPUT_TYPE = args.output_type
    KEEP_PROJECTS = args.keep_projects
    TGUP_CONCURRENCY = max(1, args.tgup_concurrency)
    TGUP_TIMEOUT_SEC = args.tgup_timeout

    try:
        return chop_and_drop(args.url, chunk_seconds=args.chunk_minutes * 60.0)
    except KeyboardInterrupt:
        log("[interrupt] Ctrl-C caught — state preserved, safe to rerun.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
