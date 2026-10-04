"""
Telegram Cloud Storage engine for T_Dubber.

SPEED POLICY — who uploads what, and when (AI/dev note, keep in sync
with go_planner.py + TGUP.md + boost.ps1):
* DEFAULT engine = Telethon (this file, sequential `send_file` loop).
  Correct for EVERY file: thumbnails, video-preview attributes, private
  tg:// links, FloodWait/OTP handling, resume journal.
* SPEED engine = tgup (`tgup/tgup.exe upload`, gotd/td,
  `--concurrency 3..8` overlapping parts). Used ONLY when
  `upload_file_detailed(..., use_go=True)` AND the file splits into >=2
  parts AND the channel is public (@name) AND no thumbnail is requested
  AND the binary is runnable. See `_try_go_upload()` — any Go failure
  falls back to Telethon, never fails the run.
  Why only multi-part? One part == one TCP stream either way; Go gives no
  parallel gain there and lacks Telethon's artwork/preview features.
  Measured baseline: single-stream ~2.06 MB/s vs 3.76 MB/s link at 110ms
  RTT (TCP bandwidth-delay-product limit) — overlapping streams are the
  only thing that can close that gap; language speed cannot.
* MACHINE tuning = ps1, never correctness: `boost.ps1` / `start-boosted.ps1`
  (CPU High priority + High-Performance power plan + TCP/QoS) and
  `doctor.ps1` (pre-flight incl. `go_planner.py check`). Run them from
  Windows; Python never requires them.

Design goals
------------
* Practically unlimited single-file size. Telegram's MTProto user API caps a
  single upload at 2 GB for normal accounts (4 GB for Premium), so anything
  larger is streamed as fixed-size parts. Nothing is ever copied to a temp file
  first, so a 90 GB upload does not need 90 GB of free RAM or disk.
* Resumable. Per-file progress is journalled to disk after every confirmed
  part, so a crash, a closed tab, or a dropped connection never restarts a
  multi-hour upload from part 1.
* Verified. Every part is SHA-256 hashed while it streams past, and the size
  Telegram reports back for the stored document is compared against the local
  byte count. Corrupt or truncated parts are rejected instead of silently
  archived.
* Restorable. A JSON manifest describing every part is uploaded as the last
  message of the album. Pasting that link (or any part link) back into the UI
  reassembles the original file and verifies each hash on the way.

Public API
----------
upload_to_telegram(...)            legacy wrapper, returns a share link
upload_file_detailed(...)          full result: parts, hashes, links, manifest
restore_from_link(...)             reassemble a file from a manifest/part link
fetch_manifest(...)                read a manifest without downloading parts
list_uploads() / forget_upload()   local journal, used by the Drive tab
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import io
import json
import os
import re
import shutil
import sys
import threading
import time
from urllib.parse import urlparse

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - exercised on Linux/macOS only
    import fcntl

APP_DIR = os.path.dirname(os.path.abspath(__file__))

SESSION_PATH = os.path.join(APP_DIR, "telegram_uploader_session")
STATE_DIR = os.path.join(APP_DIR, ".tg_uploads")

# 1900 MB stays under Telegram's 2 GB non-Premium ceiling with room for the
# transport overhead, so one code path works for every account type.
CHUNK_SIZE = 1900 * 1024 * 1024

MANIFEST_MARKER = "tg_dubber_manifest"
MANIFEST_VERSION = 2

# Telegram throttles bursts of large uploads. A short pause between parts keeps
# long jobs from tripping FloodWait instead of being rejected outright.
CHUNK_GAP_SECONDS = 1.5
FLOOD_RETRIES = 3
PART_UPLOAD_RETRIES = 3
OPEN_RETRIES = 10
OPEN_RETRY_DELAY = 2.0

# Telegram renders a document's artwork inline only for small files. Below this
# the cover image shows next to the row in the channel; above it, Telegram
# treats any attachment as a streaming-video preview instead.
THUMBNAIL_INLINE_MAX_BYTES = 10 * 1024 * 1024

TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegram.dog", "www.t.me"}

_MEDIA_MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".ts": "video/mp2t",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".opus": "audio/opus",
}


class TelegramCloudError(RuntimeError):
    """Raised when an upload or restore cannot be completed."""


class TelegramSessionBusy(TelegramCloudError):
    """Another process on this machine is already using the session."""


# ---------------------------------------------------------------------------
# Cross-process session guard
# ---------------------------------------------------------------------------

# A Telethon session is a SQLite file holding the account's auth key. Two live
# processes sharing it corrupt each other's DC auth state; the visible symptom
# is a "database is locked" warning followed by an opaque CancelledError deep
# inside Telethon. An advisory lock turns that into one clear message.
SESSION_LOCK_PATH = SESSION_PATH + ".lock"


def _acquire_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_lock(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


_session_lock_handle = None
_session_lock_guard = threading.Lock()


def hold_session_lock(wait_seconds: float = 8.0) -> bool:
    """Take this machine's Telegram session lock, or raise TelegramSessionBusy.

    The lock is held for the process lifetime and released by the operating
    system when the process exits, so a crash cannot leave it stranded.
    """
    global _session_lock_handle
    with _session_lock_guard:
        if _session_lock_handle is not None:
            return True

        os.makedirs(os.path.dirname(SESSION_LOCK_PATH) or ".", exist_ok=True)
        handle = open(SESSION_LOCK_PATH, "a+b")
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)

        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                _acquire_lock(handle)
                _session_lock_handle = handle
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TelegramSessionBusy(
                        "Another T_Dubber window on this machine is already using "
                        "the Telegram session. Two processes cannot share one "
                        "Telegram session safely. Close the other window (or the "
                        "other python process) and retry. Parts already uploaded "
                        "are journalled and will not be sent again."
                    )
                time.sleep(0.5)


def release_session_lock() -> None:
    global _session_lock_handle
    with _session_lock_guard:
        if _session_lock_handle is None:
            return
        try:
            _release_lock(_session_lock_handle)
            _session_lock_handle.close()
        finally:
            _session_lock_handle = None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def human_bytes(count) -> str:
    """Render a byte count as a short human readable string."""
    try:
        count = float(count)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if count < 1024 or unit == "TB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.2f} {unit}"
        count /= 1024
    return f"{count:.2f} TB"


def plan_chunks(size: int, chunk_size: int = CHUNK_SIZE):
    """Split ``size`` bytes into ``(part_number, offset, length)`` tuples.

    Part numbers are 1-based. A file that already fits inside ``chunk_size``
    yields a single part, so "one part" is the normal case rather than a
    special case.
    """
    size = int(size)
    if size <= 0:
        raise ValueError("Cannot upload an empty file.")
    if size <= chunk_size:
        return [(1, 0, size)]
    total = (size + chunk_size - 1) // chunk_size
    plan = []
    for index in range(total):
        offset = index * chunk_size
        plan.append((index + 1, offset, min(chunk_size, size - offset)))
    return plan


def _emit(callback, **payload):
    """Call a progress callback, tolerating 1-, 2- and 3-argument signatures.

    Older callers in this repo pass ``(current, total)``; the Gradio UI wants
    the richer dict. Supporting both keeps every existing call site working.
    """
    if callback is None:
        return
    try:
        parameters = inspect.signature(callback).parameters.values()
        positional = [
            p
            for p in parameters
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        takes_varargs = any(p.kind == p.VAR_POSITIONAL for p in parameters)
        arity = len(positional)
    except (TypeError, ValueError):
        arity, takes_varargs = 3, False

    try:
        if takes_varargs or arity >= 3:
            callback(payload.get("current", 0), payload.get("total", 0), payload)
        elif arity == 2:
            callback(payload.get("current", 0), payload.get("total", 0))
        elif arity == 1:
            callback(payload)
    except Exception:  # a broken progress widget must never abort an upload
        pass


def _atomic_write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def _read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def state_path_for(key: str) -> str:
    return os.path.join(STATE_DIR, f"{key}.json")


def _fingerprint(path: str, size: int) -> str:
    """Identify a file by path, size and mtime.

    Editing or replacing the file changes the key, which correctly invalidates
    a resume journal instead of stitching new bytes onto old parts.
    """
    stat = os.stat(path)
    seed = f"{os.path.abspath(path)}|{size}|{stat.st_mtime_ns}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def _digest_prefix_agrees(prefix: str, full: str) -> bool:
    """Whether the pre-hashed digest is consistent with the streamed one.

    Defined here rather than imported so this module keeps no dependency on the
    optional Go helper; an absent helper must not be able to break an import.

    The name still says "prefix" because journals written by an older build
    stored truncated digests, and those must keep restoring. A full 64-character
    value is compared for equality, which is strictly stronger than comparing a
    prefix; anything shorter is compared as a prefix for that backward
    compatibility.
    """
    if not prefix:
        return True
    if len(prefix) == 64:
        return prefix == full
    return full.startswith(prefix)


def _plan_digests_from_go(path: str, chunk_size: int, expected_parts: int) -> dict:
    """Pre-compute part digests with the optional tgup helper.

    Returns ``{part_number: digest}``. On any problem -- helper not built,
    blocked by an application-control policy, a different part count -- this
    returns an empty dict and the caller falls back to hashing as it streams.
    Being an optimisation, it must never be able to fail an upload.

    The value is a whole SHA-256 when tgup supplied one. That makes the check at
    upload time a real equality test, so a file that changed between planning and
    uploading is caught rather than merely suspected.
    """
    try:
        import go_planner

        if not go_planner.binary_runnable():
            return {}
        plan = go_planner.build_plan(path, chunk_size=chunk_size, use_go=True)
        parts = plan.get("parts") or []
        if plan.get("planner") != "go" or len(parts) != expected_parts:
            return {}
        return {
            int(entry["part"]): entry["sha256"]
            for entry in parts
            if entry.get("sha256")
        }
    except Exception:  # noqa: BLE001 - an optimisation must never break a run
        return {}


def build_message_link(channel: str, message_id: int, peer_id=None) -> str:
    """Return a clickable link for an uploaded message.

    Public channels get a normal ``t.me`` URL. Numeric or invite-style targets
    cannot be linked publicly, so a deep link into the Telegram client is
    produced instead of a URL that would show "post not found".
    """
    name = (channel or "").strip().lstrip("@")
    message_id = int(message_id)
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", name):
        return f"https://t.me/{name}/{message_id}"
    if peer_id is not None:
        raw = str(peer_id)
        if raw.startswith("-100"):
            raw = raw[4:]
        return f"tg://openmessage?user_id={raw}&message_id={message_id}"
    return f"tg://privatepost?channel={name}&post={message_id}"


def parse_tg_link(url: str) -> dict:
    """Parse a ``t.me`` message link into a chat/message reference.

    Handles the public form ``t.me/<username>/<id>`` and the private join-link
    form ``t.me/c/<channel-id>/<id>``.
    """
    raw = (url or "").strip()
    if not raw:
        raise TelegramCloudError("Empty Telegram link.")

    if raw.startswith("tg://"):
        query = dict(
            pair.split("=", 1) for pair in raw[6:].split("&") if "=" in pair
        )
        if "user_id" in query:
            return {
                "kind": "private",
                "chat": int(query["user_id"]),
                "message_id": int(query["message_id"]),
            }
        if "channel" in query:
            return {
                "kind": "private",
                "chat": int(query["channel"]),
                "message_id": int(query["post"]),
            }
        raise TelegramCloudError(f"Unsupported Telegram deep link: {url}")

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.netloc.lower() not in TELEGRAM_HOSTS:
        raise TelegramCloudError(f"Not a Telegram link: {url}")

    parts = [segment for segment in parsed.path.split("/") if segment]
    if not parts:
        raise TelegramCloudError(
            "That is a Telegram channel/username link, not a file link. "
            "Use the exact message link you got after an upload."
        )
    if parts[0] == "c":
        if len(parts) < 3:
            raise TelegramCloudError("Malformed private Telegram message link.")
        return {"kind": "private", "chat": int(parts[1]), "message_id": int(parts[2])}
    if len(parts) < 2:
        raise TelegramCloudError(
            "That is a channel link, not a message link. "
            "Open the video and copy the message link instead."
        )
    return {"kind": "public", "chat": parts[0], "message_id": int(parts[1])}


def is_tg_link(url: str) -> bool:
    try:
        parse_tg_link(url)
        return True
    except (TelegramCloudError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Streaming part reader
# ---------------------------------------------------------------------------


class HashingFileSlice(io.IOBase):
    """A read-only window over a file that hashes the bytes as they stream by.

    Telethon only needs ``read``/``seek``/``tell``/``__len__``, so instead of
    materialising a slice we hand it this view. The part therefore never
    occupies more than Telethon's own read buffer, no matter how large it is.
    """

    def __init__(self, filepath: str, offset: int, length: int, name: str):
        self.filepath = filepath
        self.offset = int(offset)
        self.length = int(length)
        self.name = name
        self.size = int(length)
        self._digest = hashlib.sha256()
        self.read_bytes = 0

        self._handle = None
        last_error = None
        for attempt in range(OPEN_RETRIES):
            try:
                self._handle = open(filepath, "rb")
                break
            except PermissionError as exc:
                # Windows Defender and OneDrive commonly hold a brief lock.
                last_error = exc
                if attempt == OPEN_RETRIES - 1:
                    raise
                time.sleep(OPEN_RETRY_DELAY)
        if self._handle is None:
            raise TelegramCloudError(f"Could not open {filepath}: {last_error}")

        self._handle.seek(self.offset)

    def read(self, size: int = -1) -> bytes:
        if self.read_bytes >= self.length:
            return b""
        if size is None or size < 0 or size > (self.length - self.read_bytes):
            size = int(self.length - self.read_bytes)
        data = self._handle.read(size)
        if not data:
            return b""
        self.read_bytes += len(data)
        self._digest.update(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            target = offset
        elif whence == 1:
            target = self.read_bytes + offset
        else:
            target = self.length + offset
        target = max(0, min(int(target), self.length))
        self.read_bytes = target
        self._handle.seek(self.offset + target)
        # Re-reading a position invalidates the running digest, so start over.
        self._digest = hashlib.sha256()
        return self.read_bytes

    def tell(self) -> int:
        return self.read_bytes

    def hexdigest(self) -> str:
        return self._digest.hexdigest()

    def __len__(self) -> int:
        return self.length

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        super().close()


# ---------------------------------------------------------------------------
# Client lifecycle
# ---------------------------------------------------------------------------

_client_cache = threading.local()


def _get_client(api_id, api_hash, phone=None, session_path: str = SESSION_PATH):
    """Return a connected, authorised client, reusing the thread's instance.

    Opening a fresh client per upload wasted a handshake and, worse, called
    ``start()`` unconditionally -- which drops into an interactive OTP prompt
    whenever the session had expired. We now connect first and only prompt when
    authorisation is genuinely missing.
    """
    try:
        numeric_id = int(api_id)
    except (TypeError, ValueError):
        raise TelegramCloudError("Telegram API ID must be numeric.")

    cached = getattr(_client_cache, "client", None)
    if cached is not None:
        try:
            if cached.is_connected() and cached.is_user_authorized():
                return cached
            cached.disconnect()
        except Exception:
            pass

    # telethon.sync is not optional here. ``from telethon import
    # TelegramClient`` gives the async client in Telethon 1.4x, so every call
    # would quietly return an un-awaited coroutine: connect() would do nothing,
    # is_user_authorized() would be a truthy coroutine (so the OTP branch would
    # never run), and send_file() would upload nothing at all. The sync wrapper
    # is the only import that behaves like the rest of this module's code.
    from telethon.sync import TelegramClient

    hold_session_lock()

    client = TelegramClient(session_path, numeric_id, api_hash)
    client.connect()
    if not client.is_user_authorized():
        if not phone:
            client.disconnect()
            raise TelegramCloudError(
                "Telegram session missing or expired. Run "
                "`python telegram_uploader.py` in a terminal once and complete "
                "the OTP login."
            )
        client.start(phone=phone)

    _client_cache.client = client
    return client


def disconnect_client() -> None:
    """Close the thread's cached client, if any."""
    cached = getattr(_client_cache, "client", None)
    if cached is not None:
        try:
            cached.disconnect()
        except Exception:
            pass
        _client_cache.client = None


def _fetch_message(client, reference: dict):
    from telethon.tl import types

    if reference["kind"] == "private":
        peer = types.PeerChannel(int(reference["chat"]))
    else:
        peer = reference["chat"]
    message = client.get_messages(peer, ids=int(reference["message_id"]))
    if message is None or getattr(message, "media", None) is None:
        raise TelegramCloudError(
            "That message has no downloadable media. It may have been deleted, "
            "or the account may not be a member of the channel."
        )
    return message


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm", ".mov", ".mkv", ".avi", ".3gp"}


def _probe_video(path):
    """Return (duration_seconds, width, height) for a media file, or None."""
    try:
        import json as _json
        import subprocess

        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=width,height,codec_type",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = _json.loads(proc.stdout or "{}")
        duration = 0
        try:
            duration = int(float(data.get("format", {}).get("duration", 0) or 0))
        except (TypeError, ValueError):
            duration = 0
        width = height = 0
        for stream in data.get("streams", []) or []:
            if stream.get("codec_type") == "video":
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
                break
        if width and height:
            return (duration, width, height)
    except Exception:  # noqa: BLE001
        return None
    return None


def _extract_video_thumb(path, out_path):
    """Capture a 1-frame JPEG from the video and return its path, or None."""
    try:
        import subprocess

        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-ss",
                "00:00:01",
                "-i",
                path,
                "-frames:v",
                "1",
                "-q:v",
                "4",
                out_path,
            ],
            capture_output=True,
            timeout=30,
        )
        if (
            proc.returncode == 0
            and os.path.isfile(out_path)
            and os.path.getsize(out_path) > 0
        ):
            return out_path
    except Exception:  # noqa: BLE001
        return None
    return None


def _try_go_upload(
    file_path: str,
    api_id,
    api_hash,
    phone,
    channel: str,
    size: int,
    plan: list,
    chunk_size: int,
    caption,
    thumbnail_path,
    progress_callback,
    on_journal,
    go_concurrency: int = 3,
) -> dict | None:
    """SPEED PATH: try the tgup multi-connection uploader. Journal or None.

    AI/dev note - when this runs: ONLY called from ``upload_file_detailed()``
    when ``use_go=True`` and ``go_planner.should_use_go_upload()`` says the file
    is multi-part on a public channel with no thumbnail. Returns a completed
    journal on success, or None when Telethon should take over (tgup missing,
    not logged in yet, blocked by policy, errored, or the result failed
    validation). NEVER raises for speed reasons - correctness, which means the
    Telethon fallback, always wins.

    Validation (correctness first): the part count must match ``plan``, every
    part needs message_id and link, and a multi-part result needs a manifest
    link. Anything short of that is discarded rather than journalled, because a
    half-written journal would make a later run believe the archive is complete.

    tgup builds its manifest from every part it has confirmed stored, resume
    included, so a resumed upload still produces a complete descriptor.
    """
    try:
        import go_planner
    except ImportError:
        return None
    ok, reason = go_planner.should_use_go_upload(
        size, chunk_size, channel, thumbnail_path=thumbnail_path, use_go=True,
    )
    if not ok:
        return None
    _emit(
        progress_callback, phase="start", current=0, total=size,
        chunk_count=len(plan),
        message=f"Go parallel upload x{go_concurrency} ({reason}) …",
    )
    try:
        result = go_planner.upload_via_go(
            file_path, api_id, api_hash, channel, phone=phone,
            concurrency=go_concurrency, chunk_size=chunk_size,
            caption=caption, thumbnail_path=thumbnail_path
        )
    except Exception as exc:  # noqa: BLE001 - speed must never fail a run
        _emit(
            progress_callback, phase="note", current=0, total=size,
            chunk_count=len(plan),
            message=f"Go upload unavailable ({exc}); Telethon fallback …",
        )
        return None
    # Validate before trusting: a partial/wrong result must not be journaled.
    got_parts = result.get("parts") or []
    if len(got_parts) != len(plan):
        _emit(
            progress_callback, phase="note", current=0, total=size,
            chunk_count=len(plan),
            message=(f"Go returned {len(got_parts)}/{len(plan)} parts; "
                     "Telethon fallback …"),
        )
        return None
    if any(not p.get("message_id") or not p.get("link") for p in got_parts):
        return None
    if len(plan) > 1 and not result.get("message_link"):
        _emit(
            progress_callback, phase="note", current=0, total=size,
            chunk_count=len(plan),
            message="Go manifest missing; Telethon fallback …",
        )
        return None
    journal = go_planner.convert_go_result_to_journal(
        result, file_path, channel, chunk_size=chunk_size, caption=caption,
    )
    journal["file_path"] = os.path.abspath(file_path)
    journal["reused"] = False
    # Persist exactly like the Telethon path so resume/reuse/db see one shape.
    try:
        _atomic_write_json(state_path_for(_fingerprint(file_path, size)), journal)
    except OSError:
        pass
    if on_journal:
        try:
            on_journal(dict(journal))
        except Exception:
            pass
    rate = float(result.get("bytes_per_sec") or 0)
    _emit(
        progress_callback, phase="complete", current=size, total=size,
        chunk_count=len(parts_to_count(journal)),
        journal=dict(journal),
        message=(f"Go upload complete x{result.get('concurrency')} · "
                 f"{human_bytes(rate)}/s · {journal.get('message_link')}"),
    )
    return journal


def parts_to_count(journal: dict) -> list:
    """Small helper so the Go-complete emit above stays readable."""
    return journal.get("parts") or []


def upload_file_detailed(
    file_path: str,
    api_id,
    api_hash,
    phone,
    channel: str,
    caption: str = None,
    progress_callback=None,
    resume: bool = True,
    reuse_completed: bool = True,
    chunk_size: int = CHUNK_SIZE,
    on_journal=None,
    thumbnail_path: str = None,
    # SPEED POLICY params (AI/dev note):
    # As per user request, Go path is now the default everywhere for max speed.
    # go_concurrency 3..8 maps straight to `tgup upload --concurrency`.
    # For machine-level speed see boost.ps1 / start-boosted.ps1 (Windows).
    use_go: bool = True,
    go_concurrency: int = 0, # 0 means auto-tune
) -> dict:
    """Upload any file to Telegram and return a full archival description.

    Returns a dict with ``link`` (the shareable URL), ``parts`` and
    ``manifest_link``. When the file needed splitting, ``link`` points at the
    manifest message and ``parts`` holds every individual part link.

    SPEED ROUTING (which engine runs):
    * use_go=True + multi-part + public channel + no thumbnail + Go runnable
      -> `_try_go_upload()` (gotd/td, parallel). Any miss -> Telethon below.
    * everything else -> Telethon sequential loop (thumbnails, previews,
      private tg:// links, FloodWait/OTP, resume journal all live here).

    ``thumbnail_path`` attaches cover art to the stored document, which is what
    makes a channel row recognisable without opening it. Telegram only shows the
    artwork for files small enough to preview inline, so on a multi-gigabyte
    video it is metadata rather than a visible image; it still travels with the
    file and is preserved on download.
    """
    if go_concurrency <= 0:
        import auto_tuner
        go_concurrency = auto_tuner.get_optimal_concurrency(channel, api_id, api_hash)
    from telethon.errors import FloodWaitError
    from telethon.tl.types import DocumentAttributeFilename

    if not os.path.isfile(file_path):
        raise TelegramCloudError(f"File not found: {file_path}")
    channel = (channel or "").strip()
    if not channel:
        raise TelegramCloudError("No Telegram channel configured.")

    file_path = os.path.abspath(file_path)
    size = os.path.getsize(file_path)
    if size <= 0:
        raise TelegramCloudError("Refusing to upload a 0-byte file.")
    base_name = os.path.basename(file_path)

    key = _fingerprint(file_path, size)
    journal_path = state_path_for(key)
    # Keep the previously persisted record separate: the reuse check below has
    # to inspect the state as it was *before* this run marks it as uploading.
    stored = _read_json(journal_path, {}) or {}
    journal = dict(stored)
    journal.update({"file_path": file_path, "filename": base_name, "size": size})

    # A journal left mid-flight means the previous attempt died (crash, closed
    # terminal, cancelled session). Its parts are still valid on Telegram, so
    # resume from them, but record that this run is the recovery.
    recovering = stored.get("state") == "uploading" and stored.get("parts")
    if recovering:
        journal["recovered_from"] = stored.get("updated_at")

    if (
        reuse_completed
        and resume
        and stored.get("state") == "complete"
        and stored.get("channel") == channel
        and stored.get("message_id")
    ):
        _emit(
            progress_callback,
            phase="complete",
            current=size,
            total=size,
            message=f"Already archived on {build_message_link(channel, stored['message_id'])}",
        )
        journal["reused"] = True
        journal.pop("error", None)
        return journal

    journal["state"] = "uploading"

    plan = plan_chunks(size, chunk_size)
    chunked = len(plan) > 1

    # SPEED PATH 1/2 — Go parallel upload (multi-part, public channel only).
    # Python orchestrates, Go pumps bytes with --concurrency overlapping parts.
    # Returns a completed journal on success, None on any miss (then Telethon
    # below takes over). Single-part/private/thumb files skip this by design.
    if use_go:
        go_journal = _try_go_upload(
            file_path, api_id, api_hash, phone, channel, size, plan,
            chunk_size, caption, thumbnail_path, progress_callback, on_journal,
            go_concurrency=go_concurrency,
        )
        if go_journal is not None:
            return go_journal
        # Fall through: Go missed (not runnable, single-part, private
        # channel, or error) — Telethon sequential path below is authoritative.

    # SPEED PATH 2/2 — Go pre-hash cross-check (always safe, no network).
    # Go hashed the same byte ranges beforehand; the streaming hash below is
    # compared against it to catch a file changing mid-flight (OneDrive/sync).
    digests = _plan_digests_from_go(file_path, chunk_size, len(plan))

    thumb = None
    if thumbnail_path and os.path.isfile(thumbnail_path):
        # Only inline artwork can be displayed by Telegram; sending a 2 GB
        # "thumbnail" would just make the client try to preview the movie.
        if size <= THUMBNAIL_INLINE_MAX_BYTES:
            thumb = thumbnail_path
        else:
            _emit(
                progress_callback,
                phase="note",
                current=0,
                total=size,
                message=(
                    f"Cover art kept in the archive but not attached to the "
                    f"document: Telegram only renders artwork inline below "
                    f"{human_bytes(THUMBNAIL_INLINE_MAX_BYTES)}."
                ),
            )

    done_parts = {
        int(entry["part"]): entry
        for entry in (journal.get("parts") or [])
        if entry.get("message_id")
    }

    _emit(
        progress_callback,
        phase="start",
        current=0,
        total=size,
        chunk_count=len(plan),
        message=(
            f"Uploading {base_name} ({human_bytes(size)}) as {len(plan)} "
            f"part{'s' if chunked else ''} to {channel}"
        ),
    )

    client = _get_client(api_id, api_hash, phone)
    total_done = sum(entry["size"] for entry in done_parts.values())
    parts = []

    if recovering:
        _emit(
            progress_callback,
            phase="resume",
            current=total_done,
            total=size,
            chunk_count=len(plan),
            message=(
                f"Resuming: {len(done_parts)}/{len(plan)} parts already stored "
                f"({human_bytes(total_done)} of {human_bytes(size)})"
            ),
        )

    try:
        for part_number, offset, length in plan:
            existing = done_parts.get(part_number)
            if existing and existing.get("size") == length and existing.get("sha256"):
                parts.append(existing)
                _emit(
                    progress_callback,
                    phase="part",
                    current=total_done,
                    total=size,
                    chunk_index=part_number,
                    chunk_count=len(plan),
                    message=f"Part {part_number}/{len(plan)} already stored, skipping",
                )
                continue

            part_name = (
                f"{base_name}.part{part_number:04d}of{len(plan):04d}"
                if chunked
                else base_name
            )
            if chunked:
                part_name += os.path.splitext(base_name)[1] or ".bin"

            # When Go already hashed this part, hash while streaming anyway but
            # compare against its digest -- two independent computations agreeing
            # is strictly stronger than either alone, and costs nothing extra.
            expected = digests.get(part_number)
            part_slice = HashingFileSlice(file_path, offset, length, part_name)
            base_current = total_done
            _emit(
                progress_callback,
                phase="part",
                current=base_current,
                total=size,
                chunk_index=part_number,
                chunk_count=len(plan),
                message=f"Part {part_number}/{len(plan)} uploading ({human_bytes(length)})",
            )

            def on_part_progress(current, _total, _slice=part_slice, _base=base_current):
                _emit(
                    progress_callback,
                    phase="part",
                    current=_base + current,
                    total=size,
                    chunk_index=part_number,
                    chunk_count=len(plan),
                )

            # Treat a single-part video as an inline video message (thumbnail +
            # play button) instead of a bare document, so small videos show up
            # as a video in the channel rather than a generic file.
            preview = None
            if (not chunked) and os.path.splitext(base_name)[1].lower() in _VIDEO_EXTENSIONS:
                meta = _probe_video(file_path)
                if meta:
                    duration, width, height = meta
                    preview = {
                        "duration": duration,
                        "w": width,
                        "h": height,
                    }
            local_thumb = None
            if preview is not None:
                local_thumb = _extract_video_thumb(
                    file_path, os.path.splitext(file_path)[0] + ".t_dub_thumb.jpg"
                )
            send_thumb = None
            if part_number == 1:
                # An inline video preview only renders with JPEG/PNG artwork, so
                # prefer the freshly extracted frame; fall back to the provided
                # artwork when that is all we have.
                send_thumb = local_thumb or thumb

            if preview is not None:
                try:
                    from telethon.tl.types import DocumentAttributeVideo

                    video_attr = DocumentAttributeVideo(
                        duration=preview["duration"],
                        w=preview["w"],
                        h=preview["h"],
                        round_message=False,
                    )
                    send_attributes = [
                        DocumentAttributeFilename(part_name),
                        video_attr,
                    ]
                except Exception:  # noqa: BLE001
                    send_attributes = [DocumentAttributeFilename(part_name)]
                    preview = None
            else:
                send_attributes = [DocumentAttributeFilename(part_name)]

            message = None
            try:
                for attempt in range(1, PART_UPLOAD_RETRIES + 1):
                    try:
                        message = client.send_file(
                            channel,
                            file=part_slice,
                            file_size=length,
                            force_document=preview is None,
                            attributes=send_attributes,
                            supports_streaming=preview is not None,
                            # Only the first part gets the artwork, otherwise
                            # every row in the album would render an image.
                            thumb=send_thumb,
                            caption=(
                                f"📦 Part {part_number}/{len(plan)} · {base_name}\n"
                                f"{human_bytes(length)}"
                                if chunked
                                else (caption or f"🎥 Uploaded by T_Dubber · {base_name}")
                            ),
                            progress_callback=on_part_progress,
                        )
                        break
                    except FloodWaitError as exc:
                        wait = int(getattr(exc, "seconds", 30)) + 5
                        _emit(
                            progress_callback,
                            phase="throttled",
                            current=base_current,
                            total=size,
                            chunk_index=part_number,
                            chunk_count=len(plan),
                            message=(
                                f"Telegram asked us to wait {wait}s before part "
                                f"{part_number}. Waiting..."
                            ),
                        )
                        if attempt == PART_UPLOAD_RETRIES:
                            raise
                        time.sleep(wait)
                        part_slice.seek(0)
                    except asyncio.CancelledError as exc:
                        # Telethon cancels the transfer when another process is
                        # hammering the same SQLite session file. Surface that as
                        # the actionable message it is instead of an opaque
                        # traceback from deep inside the library.
                        if attempt == PART_UPLOAD_RETRIES:
                            raise TelegramSessionBusy(
                                f"Telegram cancelled part {part_number} because "
                                "the session file was locked by another process. "
                                "Close other T_Dubber windows and retry; parts "
                                f"1-{part_number - 1} are already stored and will "
                                "be skipped."
                            ) from exc
                        _emit(
                            progress_callback,
                            phase="throttled",
                            current=base_current,
                            total=size,
                            chunk_index=part_number,
                            chunk_count=len(plan),
                            message=(
                                "Session file is busy (another T_Dubber window?). "
                                f"Retrying part {part_number} in 10s..."
                            ),
                        )
                        time.sleep(10)
                        part_slice.seek(0)
                    except Exception:
                        # Rewind so the retry re-sends the whole part rather
                        # than resuming from a half-written offset.
                        part_slice.seek(0)
                        if attempt == PART_UPLOAD_RETRIES:
                            raise
                        time.sleep(3 * attempt)
            finally:
                part_slice.close()

            if message is None:
                raise TelegramCloudError(f"Part {part_number} produced no message.")
            if part_slice.read_bytes != length:
                raise TelegramCloudError(
                    f"Part {part_number} transferred only "
                    f"{human_bytes(part_slice.read_bytes)} of {human_bytes(length)}. "
                    "The network dropped mid-part."
                )

            streamed_digest = part_slice.hexdigest()
            if expected and not _digest_prefix_agrees(expected, streamed_digest):
                # Go hashed the same byte range before the transfer and this
                # hash was computed while the bytes streamed past. Disagreement
                # means the file changed underneath us, so the archive would not
                # be restorable to the file the user picked.
                raise TelegramCloudError(
                    f"Part {part_number} changed while it was being uploaded. "
                    "The Go pre-hash and the streamed hash disagree, so this "
                    "part was not stored. Re-run now that the file is settled."
                )

            remote = getattr(getattr(message, "media", None), "document", None)
            if remote is not None and int(remote.size) != length:
                raise TelegramCloudError(
                    f"Telegram stored {human_bytes(remote.size)} for part "
                    f"{part_number} but {human_bytes(length)} was sent. "
                    "Archive is incomplete and was not journaled."
                )

            entry = {
                "part": part_number,
                "name": part_name,
                "offset": offset,
                "size": length,
                "sha256": streamed_digest,
                "message_id": int(message.id),
                "link": build_message_link(channel, message.id),
            }
            parts.append(entry)
            done_parts[part_number] = entry
            total_done += length

            journal["parts"] = [done_parts[key_] for key_ in sorted(done_parts)]
            journal["updated_at"] = time.time()
            _atomic_write_json(journal_path, journal)
            # Hand the partial journal to any listener so an external index can
            # stay in step with Telegram. The upload itself must not depend on
            # the listener, so a failure here is ignored.
            if on_journal:
                try:
                    on_journal(dict(journal))
                except Exception:
                    pass

            _emit(
                progress_callback,
                phase="part",
                current=total_done,
                total=size,
                journal=dict(journal),
                chunk_index=part_number,
                chunk_count=len(plan),
                message=f"Part {part_number}/{len(plan)} stored ✓ ({human_bytes(total_done)} of {human_bytes(size)})",
            )

            if chunked and part_number < len(plan):
                time.sleep(CHUNK_GAP_SECONDS)

            manifest = {
            MANIFEST_MARKER: MANIFEST_VERSION,
            "filename": base_name,
            "size": size,
            "chunk_size": chunk_size,
            "chunked": chunked,
            "chunk_count": len(parts),
            "channel": channel,
            "source_path": file_path,
            "thumbnail_path": thumb,
            "caption": caption,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "app": "T_Dubber",
            "parts": parts,
        }

        if chunked:
            manifest_document = json.dumps(manifest, ensure_ascii=False, indent=2)
            manifest_name = f"{base_name}.manifest.json"
            manifest_path = os.path.join(STATE_DIR, manifest_name)
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(manifest_path, "w", encoding="utf-8") as handle:
                handle.write(manifest_document)
            try:
                manifest_message = client.send_file(
                    channel,
                    manifest_path,
                    caption=(
                        f"📄 MANIFEST · {base_name}\n"
                        f"{human_bytes(size)} · {len(parts)} part"
                        f"{'s' if len(parts) > 1 else ''}\n\n"
                        f"Paste this link into T_Dubber → Telegram Drive → "
                        f"Restore to rebuild the original file."
                    ),
                )
            finally:
                try:
                    os.remove(manifest_path)
                except OSError:
                    pass
            journal["message_id"] = int(manifest_message.id)
            journal["message_link"] = build_message_link(channel, manifest_message.id)
        else:
            only = parts[0]
            journal["message_id"] = only["message_id"]
            journal["message_link"] = only["link"]

        journal["channel"] = channel
        journal["parts"] = parts
        journal["manifest"] = manifest
        journal["state"] = "complete"
        journal["chunked"] = chunked
        journal["chunk_count"] = len(parts)
        journal["updated_at"] = time.time()
        _atomic_write_json(journal_path, journal)

        _emit(
            progress_callback,
            phase="complete",
            current=size,
            total=size,
            chunk_count=len(parts),
            journal=dict(journal),
            message=f"Archive complete · {journal['message_link']}",
        )
        if on_journal:
            try:
                on_journal(dict(journal))
            except Exception:
                pass
        return journal
    except BaseException as exc:
        # BaseException, not Exception: asyncio.CancelledError no longer derives
        # from Exception, so a cancelled transfer used to leave the journal
        # stuck at "uploading" with no recorded reason.
        journal["state"] = "failed"
        journal["error"] = f"{type(exc).__name__}: {exc}"
        journal["updated_at"] = time.time()
        _atomic_write_json(journal_path, journal)
        raise


def upload_to_telegram(
    file_path: str,
    api_id,
    api_hash,
    phone,
    channel_username: str,
    progress_callback=None,
    caption: str = None,
) -> str:
    """Backward-compatible wrapper returning just the shareable link."""
    result = upload_file_detailed(
        file_path,
        api_id,
        api_hash,
        phone,
        channel_username,
        caption=caption,
        progress_callback=progress_callback,
    )
    return result.get("message_link") or result.get("link")


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def _download_message_to(client, message, destination: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(destination)) or ".", exist_ok=True)
    if os.path.exists(destination):
        os.remove(destination)
    saved = client.download_media(message, file=destination)
    if saved is None or not os.path.isfile(saved) or os.path.getsize(saved) == 0:
        raise TelegramCloudError(
            "Telegram returned an empty file for that message. "
            "The message may have expired or been deleted."
        )
    return saved


def message_filename(message):
    """Best-effort filename for a message attachment, or None for media."""
    from telethon.tl.types import DocumentAttributeFilename

    document = getattr(getattr(message, "media", None), "document", None)
    for attribute in getattr(document, "attributes", []) or []:
        if isinstance(attribute, DocumentAttributeFilename) and attribute.file_name:
            return attribute.file_name
    return None


def _is_manifest_message(message) -> bool:
    """A manifest is recognised by its .json filename.

    Deciding from metadata instead of downloading the payload first avoids
    pulling a multi-gigabyte file twice just to find out whether it happened to
    be an archive descriptor.
    """
    return (message_filename(message) or "").lower().endswith(".json")


def _read_manifest_message(client, message) -> dict:
    document = getattr(getattr(message, "media", None), "document", None)
    size = int(getattr(document, "size", 0)) if document is not None else 0
    if size > 8 * 1024 * 1024:
        raise TelegramCloudError(
            f"That JSON file is {human_bytes(size)}, far too large to be a "
            "manifest. Paste the link to the small manifest message."
        )

    scratch = os.path.join(STATE_DIR, "_manifest_probe.json")
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        _download_message_to(client, message, scratch)
        with open(scratch, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TelegramCloudError(f"Could not read that manifest: {exc}")
    finally:
        try:
            os.remove(scratch)
        except OSError:
            pass

    if not isinstance(data, dict) or data.get(MANIFEST_MARKER) != MANIFEST_VERSION:
        raise TelegramCloudError(
            "That JSON file is not a T_Dubber manifest. Paste the manifest link "
            "that was posted right after the uploaded parts."
        )
    return data


def fetch_manifest(api_id, api_hash, phone, link: str) -> dict:
    """Download and parse the archive manifest behind a manifest link."""
    client = _get_client(api_id, api_hash, phone)
    message = _fetch_message(client, parse_tg_link(link))
    if not _is_manifest_message(message):
        raise TelegramCloudError(
            "That link points at a stored file, not a manifest. T_Dubber accepts "
            "both, so paste it and the file will be restored as-is."
        )
    return _read_manifest_message(client, message)


def _restore_from_manifest(client, manifest, dest_dir, progress_callback, verify):
    parts = sorted(
        manifest.get("parts") or [], key=lambda entry: int(entry.get("part", 0))
    )
    if not parts:
        raise TelegramCloudError("The manifest lists no parts.")

    base_name = (
        os.path.basename(manifest.get("filename") or "restored.bin") or "restored.bin"
    )
    total = int(manifest.get("size") or sum(int(p.get("size", 0)) for p in parts))
    os.makedirs(dest_dir, exist_ok=True)
    staging = os.path.join(dest_dir, base_name + ".restoring")
    final_path = os.path.join(dest_dir, base_name)

    _emit(
        progress_callback,
        phase="restore",
        current=0,
        total=total,
        chunk_count=len(parts),
        message=(
            f"Restoring {base_name} from {len(parts)} archived parts "
            f"({human_bytes(total)})"
        ),
    )

    if os.path.exists(staging):
        os.remove(staging)

    done = 0
    try:
        with open(staging, "wb") as sink:
            for entry in parts:
                part_reference = (
                    parse_tg_link(entry["link"])
                    if entry.get("link") and is_tg_link(entry.get("link"))
                    else None
                )
                if part_reference is None:
                    raise TelegramCloudError(
                        f"Part {entry.get('part')} has no usable link in the manifest."
                    )
                part_message = _fetch_message(client, part_reference)
                part_file = os.path.join(
                    STATE_DIR, f"_part_{int(entry['part']):04d}.bin"
                )
                try:
                    _download_message_to(client, part_message, part_file)
                    if verify and entry.get("sha256"):
                        digest = hashlib.sha256()
                        with open(part_file, "rb") as source:
                            for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
                                digest.update(block)
                        if digest.hexdigest() != entry["sha256"]:
                            raise TelegramCloudError(
                                f"Part {entry['part']} failed its SHA-256 check. "
                                "The archived copy is damaged and was not rebuilt."
                            )
                    with open(part_file, "rb") as source:
                        shutil.copyfileobj(source, sink, 4 * 1024 * 1024)
                finally:
                    try:
                        os.remove(part_file)
                    except OSError:
                        pass
                done += int(entry.get("size", 0))
                _emit(
                    progress_callback,
                    phase="restore",
                    current=done,
                    total=total,
                    chunk_index=int(entry.get("part", 0)),
                    chunk_count=len(parts),
                    message=(
                        f"Verified part {entry['part']}/{len(parts)} "
                        f"({human_bytes(done)} of {human_bytes(total)})"
                    ),
                )
    except BaseException:
        # Never leave a half-rebuilt file behind. A retry would otherwise find
        # the staging file and could mistake it for a finished restore.
        try:
            os.remove(staging)
        except OSError:
            pass
        raise

    rebuilt = os.path.getsize(staging)
    if total and rebuilt != total:
        os.remove(staging)
        raise TelegramCloudError(
            f"Rebuilt {human_bytes(rebuilt)} but the manifest declares "
            f"{human_bytes(total)}. Restore aborted and nothing was kept."
        )

    os.replace(staging, final_path)
    _emit(
        progress_callback,
        phase="restore",
        current=total,
        total=total,
        message=f"Restore complete, checksums OK -> {final_path}",
    )
    return final_path


def describe_link(link: str, api_id, api_hash, phone) -> dict:
    """Describe what a Telegram link holds, without downloading the payload.

    Returns ``{"filename", "size", "chunked", "chunk_count", "is_manifest"}``.
    A manifest is expanded so the UI can show the real restored filename and
    total size rather than the size of the small descriptor file.
    """
    client = _get_client(api_id, api_hash, phone)
    message = _fetch_message(client, parse_tg_link(link))
    document = getattr(getattr(message, "media", None), "document", None)
    size = int(getattr(document, "size", 0)) if document is not None else 0
    name = message_filename(message) or "archive.bin"

    if _is_manifest_message(message):
        manifest = _read_manifest_message(client, message)
        parts = manifest.get("parts") or []
        return {
            "filename": manifest.get("filename") or name,
            "size": int(manifest.get("size") or sum(int(p.get("size", 0)) for p in parts)),
            "chunked": bool(manifest.get("chunked")) or len(parts) > 1,
            "chunk_count": len(parts),
            "is_manifest": True,
        }

    return {
        "filename": name,
        "size": size,
        "chunked": False,
        "chunk_count": 1,
        "is_manifest": False,
    }


def download_or_restore(
    link: str,
    api_id,
    api_hash,
    phone,
    dest_dir: str,
    progress_callback=None,
    verify: bool = True,
) -> str:
    """Fetch a file back from any link this project has ever produced.

    A manifest link rebuilds the whole archive with per-part checksum
    verification. Any other message link downloads that single stored file.
    """
    client = _get_client(api_id, api_hash, phone)
    message = _fetch_message(client, parse_tg_link(link))

    if _is_manifest_message(message):
        manifest = _read_manifest_message(client, message)
        return _restore_from_manifest(
            client, manifest, dest_dir, progress_callback, verify
        )

    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(message_filename(message) or "") or "downloaded.bin"
    target = os.path.join(dest_dir, name)
    document = getattr(getattr(message, "media", None), "document", None)
    total = int(getattr(document, "size", 0)) if document is not None else 0

    _emit(
        progress_callback,
        phase="restore",
        current=0,
        total=total,
        message=f"Downloading {name} ({human_bytes(total)}) from Telegram",
    )

    def on_progress(current, _total):
        _emit(progress_callback, phase="restore", current=current, total=total)

    _download_message_to(client, message, target)
    _emit(
        progress_callback,
        phase="restore",
        current=total,
        total=total,
        message=f"Download complete -> {target}",
    )
    return target


def restore_from_link(
    link: str,
    api_id,
    api_hash,
    phone,
    dest_dir: str,
    progress_callback=None,
    verify: bool = True,
) -> str:
    """Backwards-compatible alias for download_or_restore."""
    return download_or_restore(
        link, api_id, api_hash, phone, dest_dir, progress_callback, verify
    )


# ---------------------------------------------------------------------------
# Local journal (drives the Telegram Drive tab)
# ---------------------------------------------------------------------------


def list_uploads(limit: int = 60) -> list:
    """Return journalled uploads, newest first, for the Drive tab."""
    if not os.path.isdir(STATE_DIR):
        return []
    records = []
    for name in os.listdir(STATE_DIR):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        data = _read_json(os.path.join(STATE_DIR, name))
        if isinstance(data, dict) and data.get("parts"):
            records.append(data)
    records.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
    return records[:limit]


def forget_upload(key: str) -> bool:
    """Delete a local journal entry; the Telegram messages are left untouched."""
    path = state_path_for(key)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


# ---------------------------------------------------------------------------
# CLI: first-time login + offline self-test
# ---------------------------------------------------------------------------


def _bootstrap() -> None:
    print("=== Telegram first-time setup ===")
    api_id = input("Enter your API ID: ").strip()
    if not api_id.isdigit():
        print("API ID must be an integer.")
        sys.exit(1)
    api_hash = input("Enter your API HASH: ").strip()
    phone = input("Enter your Phone Number (with country code, e.g. +91...): ").strip()

    from telethon.sync import TelegramClient

    client = TelegramClient(SESSION_PATH, int(api_id), api_hash)
    client.start(phone=phone)
    print("Session created successfully. You can now use the UI.")
    client.disconnect()


def _selftest() -> None:
    """Verify chunk planning and hashing without touching the network."""
    # Telethon 1.4x exposes an async client from the package root. Importing
    # that one makes every call a silent no-op, so guard the import itself.
    import inspect

    from telethon import TelegramClient as AsyncClient
    from telethon.sync import TelegramClient as SyncClient

    assert not inspect.iscoroutinefunction(
        AsyncClient.connect
    ), "expected telethon.TelegramClient to be the async client"
    assert not inspect.iscoroutinefunction(
        SyncClient.connect
    ), "telethon.sync.TelegramClient must be synchronous"

    scratch = os.path.join(STATE_DIR, "_selftest.bin")
    os.makedirs(STATE_DIR, exist_ok=True)
    payload = os.urandom(5 * 1024 * 1024 + 12345)
    with open(scratch, "wb") as handle:
        handle.write(payload)
    try:
        small = plan_chunks(len(payload), chunk_size=8 * 1024 * 1024)
        assert small == [(1, 0, len(payload))], small

        ninety_gb = 90 * 1024 ** 3
        big = plan_chunks(ninety_gb, CHUNK_SIZE)
        assert len(big) == 49, len(big)
        assert big[-1][2] == ninety_gb - 48 * CHUNK_SIZE
        assert sum(length for _n, _o, length in big) == ninety_gb
        assert all(
            big[i][1] == sum(length for _n, _o, length in big[:i]) for i in range(len(big))
        ), "part offsets must be contiguous and gapless"

        rebuilt = hashlib.sha256()
        for part, offset, length in small:
            view = HashingFileSlice(scratch, offset, length, f"part{part}")
            try:
                while True:
                    block = view.read(64 * 1024)
                    if not block:
                        break
                    rebuilt.update(block)
            finally:
                view.close()
        assert rebuilt.hexdigest() == hashlib.sha256(payload).hexdigest()

        assert parse_tg_link("https://t.me/tgwebcloud1/123")["message_id"] == 123
        assert parse_tg_link("https://t.me/c/1234567890/55") == {
            "kind": "private",
            "chat": 1234567890,
            "message_id": 55,
        }
        assert build_message_link("@tgwebcloud1", 9) == "https://t.me/tgwebcloud1/9"

        print(f"OK  chunk planning ({len(big)} parts for 90 GB)  {human_bytes(ninety_gb)}")
        print("OK  streaming slice round-trip matches source SHA-256")
        print("OK  Telegram link parsing (public + private) and link building")
        print("OK  Telethon sync client is the one imported (async would no-op)")
    finally:
        try:
            os.remove(scratch)
        except OSError:
            pass


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        _bootstrap()
