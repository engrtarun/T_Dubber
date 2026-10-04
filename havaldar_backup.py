#!/usr/bin/env python3
"""
havaldar_backup.py — T_Dubber disaster-recovery archivist (Havaldar = the sentry).

Ships the three files that hold T_Dubber's memory off the PC and onto Telegram:

    t_dubber.db                    the whole project state (runs, uploads, parts)
    t_dubber.db-wal                the WAL that pairs with it
    space_sweeper_audit.jsonl      the cleanup audit trail (if present)

Flow
----
1.  Best-effort ``PRAGMA wal_checkpoint(TRUNCATE)`` on the live database so the
    WAL is folded into the main file and the archived pair is coherent.
2.  Consistent snapshot with SQLite's online backup API + ``integrity_check``.
    If that cannot run (file unreadable, sqlite refuses), fall back to a raw
    byte copy of ``t_dubber.db`` + ``t_dubber.db-wal`` so *something* always
    leaves the machine. The mode used is recorded in the caption.
3.  Deflate-level-9 ZIP built in a temp staging directory, then reopened and
    CRC-verified with ``zipfile.testzip()`` before it is offered to Telegram.
4.  Upload as a document through the existing Telethon session
    (``telegram_uploader_session``), taking the same cross-process session lock
    that ``telegram_uploader.py`` uses so two programs never share the session.
5.  Verify Telegram stored the exact byte count, print a message link, then
    delete the local .zip. A failed upload leaves the .zip on disk for retry.

Destination
-----------
Default target is the private chat titled "The Station". Override with
``--chat`` accepting a username (``@thedubber``), a numeric id
(``-1001234567890``), ``me`` (Saved Messages), or an exact chat title.

Usage
-----
    python havaldar_backup.py                      # push to "The Station"
    python havaldar_backup.py --chat @mybackup      # explicit username/id
    python havaldar_backup.py --probe               # connect + resolve only
    python havaldar_backup.py --dry-run             # build + verify zip only
    python havaldar_backup.py --keep --raw -v       # keep zip, raw-copy mode
    python havaldar_backup.py --session embedded    # auth without touching the file

Session lock and authentication
-------------------------------
``telegram_uploader.py`` takes a machine-wide lock the first time it opens the
session and keeps it until that process exits. What it really protects is the
shared SQLite session file: two writers on that file produce ``database is
locked`` errors and can corrupt the auth state.

* ``--session file``     lock the file and let Telethon use it. Refuses while
                         the T_Dubber window holds the lock.
* ``--session embedded`` read the auth key into memory once and authenticate
                         with an in-memory session. This script then performs
                         zero writes to the shared file, so a DR backup can run
                         while T_Dubber is busy uploading chunks.
* ``--session auto``     (default) try ``file``, switch to ``embedded`` when
                         the lock is busy -- a logged warning says so.
* ``--ignore-session-lock``  write the shared file even while another process
                         holds it. Last resort: this is the mode that
                         reproduces ``database is locked``.

Restore
-------
1.  Download the newest ``havaldar_backup_*.zip`` from "The Station".
2.  Stop T_Dubber.
3.  Unzip into this folder, replacing ``t_dubber.db``.
4.  Delete any stale ``t_dubber.db-wal`` / ``t_dubber.db-shm`` that are there
    (SQLite recreates them); the archived WAL is empty by design when the
    snapshot mode was used.
5.  Start T_Dubber.

Security notes
--------------
* Credentials come from ``TG_API_ID`` / ``TG_API_HASH`` if set, otherwise from
  ``config.json`` including its Windows-DPAPI ``api_hash_protected`` blob. The
  hash is never logged or written anywhere.
* The Telethon session file is deliberately NOT archived: it holds the
  account's auth key, and copying a session another process is using is how
  sessions get corrupted.
* Telegram transport is MTProto-encrypted end to end from this client.

Exit codes: 0 success · 1 failure · 2 bad usage.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from datetime import datetime

APP_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.path.join(APP_DIR, "t_dubber.db")
WAL_PATH = DB_PATH + "-wal"
SHM_PATH = DB_PATH + "-shm"
AUDIT_PATH = os.path.join(APP_DIR, "space_sweeper_audit.jsonl")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
SESSION_PATH = os.path.join(APP_DIR, "telegram_uploader_session")

DEFAULT_DESTINATION = "The Station"

# Telethon's user-API ceiling is 2 GB (4 GB Premium). Staying under the same
# 1900 MB guard telegram_uploader.py uses keeps one code path for all accounts.
MAX_ZIP_BYTES = 1900 * 1024 * 1024
TELEGRAM_CAPTION_LIMIT = 1024

CONNECT_RETRIES = 3
UPLOAD_RETRIES = 3
FLOOD_WAIT_CAP_SECONDS = 900
SQLITE_TIMEOUT_SECONDS = 20
COPY_RETRIES = 3

log = logging.getLogger("havaldar")


class HavaldarError(RuntimeError):
    """Any failure the operator should see as one clear sentence."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def human_bytes(count) -> str:
    """Render a byte count the way the rest of this repo does."""
    try:
        size = float(count)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def force_utf8_console() -> None:
    """Make stdout/stderr tolerate emoji on a cp1252 Windows console.

    Without this a single ``print("❌ ...")`` inside an *error handler* raises
    UnicodeEncodeError and hides the failure the operator needed to read.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):  # exotic streams, py<3.7
            pass


def _copy_with_retry(src: str, dst: str) -> None:
    """Copy a file that Windows may be holding (OneDrive, Defender)."""
    last_error = None
    for attempt in range(1, COPY_RETRIES + 1):
        try:
            shutil.copy2(src, dst)
            return
        except OSError as exc:  # PermissionError is a subclass
            last_error = exc
            log.debug("copy attempt %d/%d for %s failed: %s", attempt, COPY_RETRIES, src, exc)
            time.sleep(0.5 * attempt)
    raise HavaldarError(f"Could not copy {os.path.basename(src)}: {last_error}")


# ---------------------------------------------------------------------------
# Credentials (env first, then config.json with its DPAPI-protected hash)
# ---------------------------------------------------------------------------


def _dpapi_unprotect(raw: bytes) -> bytes:
    """Undo Windows DPAPI on the stored api_hash (same blob app.py writes)."""
    if os.name != "nt":
        raise HavaldarError(
            "config.json holds a DPAPI-protected api_hash, which only Windows "
            "can decrypt. Export TG_API_ID / TG_API_HASH instead."
        )
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = _DataBlob()
    description = wintypes.LPWSTR()

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    unprotect.restype = wintypes.BOOL

    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    if not unprotect(
        ctypes.byref(source),
        ctypes.byref(description),
        None,
        None,
        None,
        0,
        ctypes.byref(destination),
    ):
        raise HavaldarError(
            f"Windows refused to decrypt the API hash (DPAPI error {ctypes.get_last_error()}). "
            "It must be read by the same Windows account that stored it."
        )
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        local_free(ctypes.cast(destination.pbData, ctypes.c_void_p))
        if description:
            local_free(ctypes.cast(description, ctypes.c_void_p))


def _unprotect_api_hash(value: str) -> str:
    if not value.startswith("dpapi:v1:"):
        raise HavaldarError("config.json api_hash_protected is not in the expected format.")
    encrypted = base64.b64decode(value[len("dpapi:v1:"):], validate=True)
    return _dpapi_unprotect(encrypted).decode("utf-8")


def load_credentials() -> tuple[int, str]:
    """Return ``(api_id, api_hash)`` without ever logging the hash."""
    env_id = (os.environ.get("TG_API_ID") or os.environ.get("T_DUBBER_API_ID") or "").strip()
    env_hash = (os.environ.get("TG_API_HASH") or os.environ.get("T_DUBBER_API_HASH") or "").strip()
    if env_id and env_hash:
        try:
            return int(env_id), env_hash
        except ValueError:
            raise HavaldarError("TG_API_ID must be numeric.") from None

    if not os.path.isfile(CONFIG_PATH):
        raise HavaldarError(
            "No credentials: config.json is missing and TG_API_ID/TG_API_HASH "
            "are not set. Save your Telegram API settings in the T_Dubber UI "
            "first (they are written to config.json)."
        )

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise HavaldarError(f"Could not read {os.path.basename(CONFIG_PATH)}: {exc}") from exc

    api_id = data.get("api_id", "")
    protected = data.get("api_hash_protected", "") or ""
    if protected:
        api_hash = _unprotect_api_hash(protected)
    else:
        api_hash = data.get("api_hash", "") or ""

    try:
        numeric_id = int(api_id)
    except (TypeError, ValueError):
        raise HavaldarError("config.json has no numeric api_id; re-save your API settings.") from None
    if not api_hash:
        raise HavaldarError("config.json has no api_hash; re-save your API settings.")
    return numeric_id, api_hash


# ---------------------------------------------------------------------------
# Staging: a consistent copy of the database plus the audit trail
# ---------------------------------------------------------------------------


def _checkpoint_wal(db_path: str) -> str:
    """Fold the WAL into the main DB (best effort; never fatal).

    This is what makes an archived ``t_dubber.db`` + ``t_dubber.db-wal`` pair
    coherent: after a TRUNCATE checkpoint the WAL holds no frames, so shipping
    both files is safe. Readers can legitimately block the checkpoint; that is
    logged and the snapshot below still produces a consistent file.
    """
    if not os.path.isfile(db_path):
        raise HavaldarError(f"Database not found: {db_path}")

    try:
        conn = sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT_SECONDS)
    except sqlite3.Error as exc:
        return f"checkpoint skipped (cannot open database: {exc})"

    try:
        journal = conn.execute("PRAGMA journal_mode").fetchone()
        mode = str(journal[0]).lower() if journal else ""
        if mode != "wal":
            return f"journal_mode={mode or 'unknown'} (nothing to checkpoint)"
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        busy = int(row[0]) if row else 1
        if busy:
            log.warning("WAL checkpoint returned busy=%s; continuing with the snapshot API", busy)
            return "checkpoint busy (readers active); snapshot taken anyway"
        return "checkpointed, WAL truncated"
    except sqlite3.Error as exc:
        log.warning("wal_checkpoint failed: %s", exc)
        return f"checkpoint failed ({exc})"
    finally:
        conn.close()


def _integrity_ok(path: str) -> tuple[bool, str]:
    """Run integrity_check on a database file; never raises."""
    conn = None
    try:
        conn = sqlite3.connect(path, timeout=SQLITE_TIMEOUT_SECONDS)
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        verdict = "; ".join(str(row[0]) for row in rows[:3])
        return (verdict.lower() == "ok", verdict or "no verdict")
    except sqlite3.Error as exc:
        return False, f"sqlite error: {exc}"
    finally:
        if conn is not None:
            conn.close()


def _snapshot_database(source: str, destination: str) -> str:
    """SQLite online-backup snapshot: consistent even with writers active."""
    src = None
    dst = None
    try:
        src = sqlite3.connect(source, timeout=SQLITE_TIMEOUT_SECONDS)
        dst = sqlite3.connect(destination)
        src.backup(dst)  # copies pages while holding only a momentary read lock
        dst.commit()
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()

    ok, verdict = _integrity_ok(destination)
    if not ok:
        raise HavaldarError(f"snapshot failed integrity_check: {verdict}")
    return verdict


def _raw_copy_database(source: str, wal_source: str, staging_dir: str) -> str:
    """Last-resort byte copy of the live files when sqlite cannot snapshot."""
    _copy_with_retry(source, os.path.join(staging_dir, os.path.basename(source)))
    if os.path.isfile(wal_source):
        _copy_with_retry(wal_source, os.path.join(staging_dir, os.path.basename(wal_source)))
    ok, verdict = _integrity_ok(os.path.join(staging_dir, os.path.basename(source)))
    return ("raw copy, integrity_check ok" if ok else f"raw copy, integrity_check FAILED ({verdict})")


def stage_files(
    staging_dir: str, raw: bool = False, checkpoint: bool = True
) -> tuple[list[dict], str, str]:
    """Stage every file that belongs in the archive.

    Returns ``(files, mode_note, integrity_note)`` where ``files`` holds
    name/size/note entries in archive order. All three requested names are
    always represented: ``t_dubber.db`` and ``t_dubber.db-wal`` come from the
    snapshot (or a raw copy when snapshotting is impossible), and the audit
    trail is included whenever it exists.
    """
    if not os.path.isfile(DB_PATH):
        raise HavaldarError(
            f"No database to back up: {os.path.basename(DB_PATH)} does not exist in {APP_DIR}."
        )

    checkpoint_note = _checkpoint_wal(DB_PATH) if checkpoint else "checkpoint disabled"
    log.info("WAL checkpoint: %s", checkpoint_note)

    staged_db = os.path.join(staging_dir, os.path.basename(DB_PATH))
    staged_wal = os.path.join(staging_dir, os.path.basename(WAL_PATH))

    files: list[dict] = []

    if raw:
        mode = "raw file copy (--raw)"
        integrity = _raw_copy_database(DB_PATH, WAL_PATH, staging_dir)
        if not os.path.isfile(staged_wal):
            # Keep the requested name present so the restore step is uniform.
            open(staged_wal, "wb").close()
            wal_note = "absent at backup time"
        else:
            wal_note = "live WAL bytes (pair may be torn if writers were active)"
    else:
        try:
            integrity = _snapshot_database(DB_PATH, staged_db)
            mode = "sqlite online-backup snapshot"
            # The snapshot already folded every WAL frame into staged_db, so
            # shipping the live WAL beside it would invite a double replay.
            open(staged_wal, "wb").close()
            wal_note = "0 B (checkpointed into the snapshot)"
        except (sqlite3.Error, HavaldarError, OSError) as exc:
            log.warning("Snapshot path failed (%s); falling back to a raw copy", exc)
            mode = "raw file copy (snapshot unavailable)"
            integrity = _raw_copy_database(DB_PATH, WAL_PATH, staging_dir)
            if not os.path.isfile(staged_wal):
                open(staged_wal, "wb").close()
                wal_note = "absent at backup time"
            else:
                wal_note = "live WAL bytes (raw fallback)"

    db_size = os.path.getsize(staged_db) if os.path.isfile(staged_db) else 0
    if db_size <= 0:
        raise HavaldarError("Staged database is empty; aborting before anything is uploaded.")
    files.append(
        {
            "name": os.path.basename(DB_PATH),
            "size": db_size,
            "note": f"integrity: {integrity}",
        }
    )

    wal_size = os.path.getsize(staged_wal) if os.path.isfile(staged_wal) else 0
    files.append({"name": os.path.basename(WAL_PATH), "size": wal_size, "note": wal_note})

    if os.path.isfile(AUDIT_PATH):
        staged_audit = os.path.join(staging_dir, os.path.basename(AUDIT_PATH))
        try:
            _copy_with_retry(AUDIT_PATH, staged_audit)
        except HavaldarError as exc:
            # The audit trail is secondary: a locked JSONL must not stop the
            # database from reaching safety.
            log.warning("Audit trail not archived (%s)", exc)
            files.append(
                {"name": os.path.basename(AUDIT_PATH), "size": -1, "note": "unreadable at backup time"}
            )
        else:
            files.append(
                {
                    "name": os.path.basename(AUDIT_PATH),
                    "size": os.path.getsize(staged_audit),
                    "note": "audit trail",
                }
            )
    else:
        files.append({"name": os.path.basename(AUDIT_PATH), "size": -1, "note": "absent at backup time"})

    # t_dubber.db-shm is deliberately excluded: it is a shared-memory index
    # SQLite rebuilds on open, and shipping a stale one can mis-drive recovery.
    log.info("Staged %d entries in %s", len(files), staging_dir)
    return files, mode, integrity


# ---------------------------------------------------------------------------
# ZIP
# ---------------------------------------------------------------------------


def build_zip(files: list[dict], staging_dir: str, zip_path: str) -> tuple[int, int]:
    """Deflate everything into ``zip_path``, then CRC-verify the archive.

    Returns ``(raw_bytes, zip_bytes)``. The archive is verified before upload
    because a backup that cannot be opened is worse than no backup at all.
    """
    raw_total = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for entry in files:
            staged = os.path.join(staging_dir, entry["name"])
            if entry["size"] < 0 or not os.path.isfile(staged):
                continue  # absent source (e.g. audit trail) is skipped, not faked
            archive.write(staged, arcname=entry["name"])
            raw_total += os.path.getsize(staged)

    zip_size = os.path.getsize(zip_path)
    if zip_size <= 0:
        raise HavaldarError("The ZIP came out empty.")

    with zipfile.ZipFile(zip_path, "r") as archive:
        broken = archive.testzip()
        if broken is not None:
            raise HavaldarError(f"ZIP verification failed on {broken}; the archive was not uploaded.")
        log.info(
            "Archive verified: %s entries, %s compressed from %s",
            len(archive.namelist()),
            human_bytes(zip_size),
            human_bytes(raw_total),
        )
    return raw_total, zip_size


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def _import_telethon():
    try:
        from telethon.errors import FloodWaitError
        # telethon.sync, not telethon: with the async client every call would
        # silently return an un-awaited coroutine. See telegram_uploader.py.
        from telethon.sync import TelegramClient
    except ImportError as exc:
        raise HavaldarError(
            "Telethon is not installed. Run: pip install -r requirements.txt"
        ) from exc
    return TelegramClient, FloodWaitError


SESSION_LOCK_HELD = "held"
SESSION_LOCK_BUSY = "busy"
SESSION_LOCK_UNAVAILABLE = "unavailable"
SESSION_LOCK_IGNORED = "ignored"


def _acquire_session_lock(
    wait_seconds: float, *, strict: bool, ignore: bool
) -> tuple:
    """Take the machine-wide lock used by telegram_uploader.py.

    Returns ``(release_callable_or_None, status)`` where status is one of
    ``held`` / ``busy`` / ``unavailable`` / ``ignored``.

    telegram_uploader takes this lock the first time it opens the session and
    keeps it until its process exits, so while the T_Dubber window is open a
    strict (``file``) run is refused. ``strict=False`` (the default ``auto``
    strategy) reports ``busy`` instead and lets the caller switch to the
    in-memory session, which never writes the shared file.
    """
    try:
        from telegram_uploader import TelegramSessionBusy, hold_session_lock, release_session_lock
    except Exception as exc:  # noqa: BLE001 - standalone use must still work
        log.debug("telegram_uploader lock unavailable (%s); nothing to coordinate with", exc)
        return None, SESSION_LOCK_UNAVAILABLE

    if ignore:
        log.warning(
            "--ignore-session-lock: using the session file while another process "
            "may also be writing it, which the repo's lock exists to prevent."
        )
        return None, SESSION_LOCK_IGNORED

    try:
        hold_session_lock(wait_seconds=wait_seconds)
    except TelegramSessionBusy as exc:
        if strict:
            raise HavaldarError(
                "Another T_Dubber window is holding the Telegram session, so this "
                "backup cannot authenticate safely yet. Close that window and "
                "retry, or re-run with --session embedded to authenticate without "
                f"touching the session file. Detail: {exc}"
            ) from exc
        return None, SESSION_LOCK_BUSY
    return release_session_lock, SESSION_LOCK_HELD


def _read_session_row(session_file: str) -> tuple:
    """Copy the live session DB aside and read one row out of it.

    The copy avoids reading a file another process may be writing, and the
    retries absorb the brief write locks SQLite takes while Telethon saves.
    """
    last_error = None
    for attempt in range(COPY_RETRIES):
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix="tg_session_", suffix=".session")
            os.close(fd)
            shutil.copyfile(session_file, temp_path)
            conn = sqlite3.connect(temp_path, timeout=SQLITE_TIMEOUT_SECONDS)
            try:
                row = conn.execute(
                    "SELECT dc_id, server_address, port, auth_key FROM sessions"
                ).fetchone()
            finally:
                conn.close()
            if row and row[3]:
                return row
            last_error = "the sessions table has no auth key yet"
        except (OSError, sqlite3.Error) as exc:
            last_error = exc
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        time.sleep(0.4 * attempt)
    raise HavaldarError(
        f"Could not read the Telegram auth key from {os.path.basename(session_file)}: "
        f"{last_error}. Run `python telegram_uploader.py` once to log in."
    )


def _embedded_session():
    """Build an in-memory session from the existing file -- never writing to it.

    Telethon's SQLite session file is shared with ``telegram_uploader.py``,
    which holds it open for the whole life of its process; two writers on that
    file is what the repo's lock prevents (and what ``database is locked``
    errors look like when it is bypassed). Reading the auth key once and
    handing Telethon an in-memory session means this script performs zero
    writes to the shared file, so a disaster-recovery backup can run while
    T_Dubber is busy uploading chunks.
    """
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    session_file = SESSION_PATH + ".session"
    if not os.path.isfile(session_file):
        raise HavaldarError(
            f"No Telethon session at {session_file}. Run `python telegram_uploader.py` "
            "once and complete the OTP login."
        )

    dc_id, server_address, port, auth_key = _read_session_row(session_file)
    session = StringSession()
    session.set_dc(int(dc_id or 0), server_address, int(port or 443))
    session.auth_key = AuthKey(auth_key)
    log.info(
        "Session strategy: embedded (in-memory auth key for DC %s; %s is never written)",
        session.dc_id,
        os.path.basename(session_file),
    )
    return session


def connect_client(api_id: int, api_hash: str, strategy: str = "file"):
    """Connect the existing session; never prompts for an OTP.

    ``strategy`` is ``file`` (Telethon's own SQLite session, lock held) or
    ``embedded`` (auth key read into memory, shared file left untouched).
    """
    TelegramClient, _ = _import_telethon()
    session = SESSION_PATH if strategy == "file" else _embedded_session()
    client = TelegramClient(session, api_id, api_hash)
    last_error = None
    for attempt in range(1, CONNECT_RETRIES + 1):
        try:
            client.connect()
            break
        except Exception as exc:  # noqa: BLE001 - transient network/handshake
            last_error = exc
            log.warning("connect attempt %d/%d failed: %s", attempt, CONNECT_RETRIES, exc)
            if attempt == CONNECT_RETRIES:
                raise HavaldarError(f"Could not reach Telegram: {exc}") from exc
            time.sleep(2 * attempt)

    if not client.is_user_authorized():
        client.disconnect()
        raise HavaldarError(
            "The Telethon session is missing or expired. Run "
            "`python telegram_uploader.py` once in a terminal and complete the "
            f"OTP login so {os.path.basename(SESSION_PATH)}.session is authorised."
        )
    return client


def _entity_title(entity) -> str:
    """Channel title, or a person's first/last name."""
    title = (getattr(entity, "title", None) or "").strip()
    if title:
        return title
    first = (getattr(entity, "first_name", None) or "").strip()
    last = (getattr(entity, "last_name", None) or "").strip()
    return f"{first} {last}".strip()


def _iter_dialog_entities(client, page_size: int = 200, max_pages: int = 60):
    """Yield every chat entity the account can see, newest first.

    Deliberately uses the raw ``GetDialogsRequest`` rather than
    ``client.iter_dialogs()``: Telethon's ``Dialog`` wrapper raises
    ``AttributeError: 'DialogCommunity' object has no attribute 'peer'`` as
    soon as the account has a Communities/Folder entry, which would abort the
    whole destination lookup for an unrelated reason.
    """
    from telethon.tl.functions.messages import GetDialogsRequest
    from telethon.tl.types import InputPeerEmpty

    offset_date = None
    offset_id = 0
    offset_peer = InputPeerEmpty()

    for _page in range(max_pages):
        try:
            result = client(
                GetDialogsRequest(
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_peer=offset_peer,
                    limit=page_size,
                    hash=0,  # 0 = always return the full listing
                )
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as one clear line
            raise HavaldarError(f"Could not list the account's chats: {exc}") from exc

        dialogs = getattr(result, "dialogs", None) or []
        if not dialogs:
            return

        by_key: dict = {}
        for user in getattr(result, "users", None) or []:
            by_key[("user", user.id)] = user
        for chat in getattr(result, "chats", None) or []:
            by_key[("chat", chat.id)] = chat
            by_key[("channel", chat.id)] = chat

        last_entity = None
        for dialog in dialogs:
            peer = getattr(dialog, "peer", None)
            entity = None
            if peer is not None:
                if hasattr(peer, "user_id"):
                    entity = by_key.get(("user", peer.user_id))
                elif hasattr(peer, "chat_id"):
                    entity = by_key.get(("chat", peer.chat_id))
                elif hasattr(peer, "channel_id"):
                    entity = by_key.get(("channel", peer.channel_id))
            if entity is not None:
                last_entity = entity
                yield entity

        if len(dialogs) < page_size or last_entity is None:
            return
        next_offset_id = int(getattr(dialogs[-1], "top_message", 0) or 0)
        if next_offset_id <= 0:
            return
        try:
            next_peer = client.get_input_entity(last_entity)
        except Exception:  # noqa: BLE001 - unresolvable page cursor: stop cleanly
            return
        if next_offset_id == offset_id:
            return  # no progress; refuse to loop forever
        offset_id = next_offset_id
        offset_peer = next_peer
        offset_date = None


def resolve_destination(client, target: str):
    """Accept a username, numeric id, ``me``, or a chat title like The Station."""
    target = (target or "").strip()
    if not target:
        raise HavaldarError("Empty destination. Pass --chat @username, an id, or a title.")

    lowered = target.lower()
    if lowered in {"me", "self", "saved"}:
        return "me"

    if re.fullmatch(r"-?\d+", target):
        return int(target)

    if target.startswith("@") or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", target):
        handle = target if target.startswith("@") else f"@{target}"
        try:
            return client.get_entity(handle)
        except Exception as exc:  # noqa: BLE001 - fall through to a title search
            log.warning("%s did not resolve (%s); searching chat titles instead", handle, exc)

    exact, prefix, contains = [], [], []
    for entity in _iter_dialog_entities(client):
        name = _entity_title(entity)
        if not name:
            continue
        lowered_name = name.lower()
        if lowered_name == lowered:
            exact.append(entity)
        elif lowered_name.startswith(lowered):
            prefix.append(entity)
        elif lowered in lowered_name:
            contains.append(entity)

    for bucket in (exact, prefix, contains):
        if len(bucket) == 1:
            log.info("Destination '%s' matched chat title '%s'", target, _entity_title(bucket[0]))
            return bucket[0]
        if len(bucket) > 1:
            names = ", ".join(
                f"{_entity_title(e)} ({getattr(e, 'id', '?')})" for e in bucket[:6]
            )
            raise HavaldarError(
                f"'{target}' matches {len(bucket)} chats: {names}. "
                "Pass an @username or a numeric chat id with --chat."
            )

    raise HavaldarError(
        f"No chat named '{target}' is visible to this account. Check the title, "
        "or pass --chat @username / --chat -100xxxxxxxxxx."
    )


class _UploadProgress:
    """Throttled percent logger for Telethon's (sent, total) callback."""

    def __init__(self, label: str):
        self.label = label
        self.started = time.monotonic()
        self.next_percent = 10
        self.last_line = 0.0

    def __call__(self, sent: int, total: int) -> None:
        now = time.monotonic()
        percent = (sent / total * 100) if total else 100
        if percent >= self.next_percent or now - self.last_line >= 10 or sent >= total:
            self.last_line = now
            self.next_percent = int(percent // 10 + 1) * 10
            rate = sent / max(now - self.started, 0.001)
            log.info(
                "%s: %s%% · %s of %s · %s/s",
                self.label,
                int(percent),
                human_bytes(sent),
                human_bytes(total),
                human_bytes(rate),
            )


def send_backup(client, zip_path: str, caption: str, entity) -> object:
    """Upload the ZIP as a document, honouring FloodWait, then verify sizes."""
    _, FloodWaitError = _import_telethon()
    zip_size = os.path.getsize(zip_path)
    last_error = None

    for attempt in range(1, UPLOAD_RETRIES + 1):
        try:
            message = client.send_file(
                entity,
                file=zip_path,
                caption=caption,
                force_document=True,
                progress_callback=_UploadProgress(os.path.basename(zip_path)),
            )
            return _verify_upload(message, zip_size)
        except FloodWaitError as exc:
            wait = min(int(getattr(exc, "seconds", 30)) + 5, FLOOD_WAIT_CAP_SECONDS)
            log.warning(
                "Telegram rate limit: waiting %ss (attempt %d/%d)", wait, attempt, UPLOAD_RETRIES
            )
            if attempt == UPLOAD_RETRIES:
                raise HavaldarError(f"Telegram kept rate-limiting the upload for {wait}s.") from exc
            time.sleep(wait)
        except HavaldarError:
            raise
        except Exception as exc:  # noqa: BLE001 - network, disconnects, retries
            last_error = exc
            log.warning("upload attempt %d/%d failed: %s", attempt, UPLOAD_RETRIES, exc)
            if attempt == UPLOAD_RETRIES:
                break
            time.sleep(3 * attempt)

    raise HavaldarError(
        f"Upload failed after {UPLOAD_RETRIES} attempts: {last_error}. "
        "The local ZIP was kept for a retry."
    )


def _verify_upload(message, expected_size: int):
    """Confirm Telegram really stored the document before the ZIP is deleted."""
    document = getattr(getattr(message, "media", None), "document", None)
    if document is None:
        raise HavaldarError(
            "Telegram accepted the message but stored no document, so the ZIP "
            "was kept locally."
        )
    stored = int(getattr(document, "size", 0) or 0)
    if stored != expected_size:
        raise HavaldarError(
            f"Telegram stored {human_bytes(stored)} but we sent "
            f"{human_bytes(expected_size)}; the ZIP was kept locally."
        )
    log.info("Verified: Telegram stored %s in message %s", human_bytes(stored), message.id)
    return message


# ---------------------------------------------------------------------------
# Caption
# ---------------------------------------------------------------------------


def build_caption(zip_name: str, zip_size: int, raw_total: int, files: list[dict],
                  stamp: str, mode: str, integrity: str) -> str:
    """Timestamp + size + date summary, inside Telegram's 1024-char caption limit."""
    present = [f for f in files if f["size"] >= 0]
    absent = [f["name"] for f in files if f["size"] < 0]
    contents = " · ".join(f"{f['name']} {human_bytes(f['size'])}" for f in present)
    if absent:
        contents += " · absent: " + ", ".join(absent)

    lines = [
        "🗄️ T_Dubber DR backup · Havaldar",
        f"📅 Date: {stamp}",
        f"📦 {zip_name} · {human_bytes(zip_size)} (from {human_bytes(raw_total)})",
        f"🔎 {mode} · integrity: {integrity[:80]}",
        f"📥 {contents}",
        f"🖥️ Host: {os.environ.get('COMPUTERNAME') or platform.node() or 'unknown'}",
        "↩️ Restore: stop T_Dubber → unzip here → replace t_dubber.db → "
        "delete t_dubber.db-wal/-shm → start.",
    ]
    caption = "\n".join(lines)
    if len(caption) > TELEGRAM_CAPTION_LIMIT:
        caption = caption[: TELEGRAM_CAPTION_LIMIT - 1] + "…"
    return caption


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="havaldar_backup.py",
        description="Compress t_dubber.db (+ WAL + audit trail) and ship it to Telegram.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python havaldar_backup.py\n"
            "  python havaldar_backup.py --chat @mybackup\n"
            "  python havaldar_backup.py --chat -1001234567890\n"
            "  python havaldar_backup.py --dry-run -v\n"
        ),
    )
    parser.add_argument(
        "--chat",
        default=DEFAULT_DESTINATION,
        help=f"Destination @username, numeric id, 'me', or chat title (default: {DEFAULT_DESTINATION!r}).",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Build and verify the ZIP, print the caption, upload nothing.")
    parser.add_argument("--probe", action="store_true",
                        help="Connect and resolve the destination, then exit without sending.")
    parser.add_argument("--keep", action="store_true",
                        help="Keep the local ZIP after a verified upload.")
    parser.add_argument("--raw", action="store_true",
                        help="Byte-copy the live files instead of using the SQLite snapshot API.")
    parser.add_argument("--no-checkpoint", action="store_true",
                        help="Do not run wal_checkpoint on the live database first.")
    parser.add_argument("--session-wait", type=float, default=10.0, metavar="SECONDS",
                        help="How long to wait for another T_Dubber window to free the session (default: 10).")
    parser.add_argument(
        "--session",
        choices=("auto", "file", "embedded"),
        default="auto",
        help="How to authenticate: 'file' locks and uses telegram_uploader_session.session "
             "(refuses if the T_Dubber window holds it); 'embedded' reads the auth key into "
             "memory and never writes the shared file; 'auto' (default) tries 'file' and "
             "silently switches to 'embedded' when the lock is busy.",
    )
    parser.add_argument("--ignore-session-lock", action="store_true",
                        help="Use the session file even though another process holds the lock "
                             "(two writers on one session file can corrupt it -- last resort).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return parser.parse_args(argv)


def _cleanup_staging(staging_dir: str) -> None:
    if staging_dir and os.path.isdir(staging_dir):
        shutil.rmtree(staging_dir, ignore_errors=True)


def main(argv=None) -> int:
    force_utf8_console()  # before any print/log: error paths print emoji too
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    now = datetime.now().astimezone()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    zip_name = f"havaldar_backup_{now.strftime('%Y%m%d_%H%M%S')}.zip"
    zip_path = os.path.join(APP_DIR, zip_name)

    staging_dir = tempfile.mkdtemp(prefix="t_dubber_havaldar_")
    client = None
    release_lock = None

    try:
        # --- 1. stage + zip (no credentials or network needed) -------------
        files, mode, integrity = stage_files(
            staging_dir, raw=args.raw, checkpoint=not args.no_checkpoint
        )
        raw_total, zip_size = build_zip(files, staging_dir, zip_path)

        if zip_size > MAX_ZIP_BYTES:
            raise HavaldarError(
                f"Refusing to upload {human_bytes(zip_size)}: over the "
                f"{human_bytes(MAX_ZIP_BYTES)} Telegram limit for one document."
            )

        caption = build_caption(zip_name, zip_size, raw_total, files, stamp, mode, integrity)
        log.info("Archive ready: %s (%s)", zip_path, human_bytes(zip_size))
        for line in caption.splitlines():
            log.info("caption| %s", line)

        if args.dry_run:
            print("\n--dry-run: nothing was uploaded. Local ZIP kept at:")
            print(f"  {zip_path}")
            return 0

        # --- 2. connect through the existing session -----------------------
        api_id, api_hash = load_credentials()

        if args.session == "embedded":
            strategy = "embedded"
            release_lock = None
        else:
            release_lock, lock_status = _acquire_session_lock(
                args.session_wait,
                strict=(args.session == "file"),
                ignore=args.ignore_session_lock,
            )
            if lock_status == SESSION_LOCK_BUSY:
                # auto: another process owns the file, so authenticate without it
                log.warning(
                    "Session lock busy (another T_Dubber window is uploading); using an "
                    "in-memory session so %s.session is never written from two processes.",
                    os.path.basename(SESSION_PATH),
                )
                strategy = "embedded"
                release_lock = None
            else:
                strategy = "file"

        client = connect_client(api_id, api_hash, strategy)
        who = client.get_me()
        log.info(
            "Session authorised as %s (id %s)",
            getattr(who, "username", None) or getattr(who, "first_name", "?"),
            getattr(who, "id", "?"),
        )

        entity = resolve_destination(client, args.chat)
        if args.probe:
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or args.chat
            print(f"\n--probe: destination '{args.chat}' resolved to '{title}'. Nothing was uploaded.")
            print(f"ZIP built and verified but retained: {zip_path}")
            return 0

        # --- 3. upload, verify, then drop the local copy -------------------
        message = send_backup(client, zip_path, caption, entity)
        link = getattr(message, "link", None) or f"message id {message.id}"

        if args.keep:
            log.info("--keep: retained %s", zip_path)
            print(f"ZIP kept at: {zip_path}")
        else:
            try:
                os.remove(zip_path)
                log.info("Removed local ZIP after verified upload: %s", zip_path)
            except OSError as exc:
                log.warning("Upload succeeded but the local ZIP could not be removed: %s", exc)

        print("\n✅ Havaldar backup complete")
        print(f"   Archive : {zip_name} · {human_bytes(zip_size)} (from {human_bytes(raw_total)})")
        print(f"   Target  : {args.chat}")
        print(f"   Stored  : {link}")
        print(f"   Taken   : {stamp}")
        return 0

    except KeyboardInterrupt:
        log.error("Interrupted by operator.")
        print("\n⚠️ Interrupted. The local ZIP (if built) was kept.")
        return 1
    except HavaldarError as exc:
        # The full detail already went to the log; the console gets one line.
        headline = str(exc).split(" Detail: ")[0].strip()
        log.error("%s", exc)
        print(f"\n❌ Backup failed: {headline}")
        if os.path.isfile(zip_path):
            print(f"   Local ZIP kept for retry: {zip_path}")
        return 1
    except Exception as exc:  # noqa: BLE001 - never die without a readable line
        log.exception("Unexpected failure")
        print(f"\n❌ Backup failed: {type(exc).__name__}: {exc}")
        if os.path.isfile(zip_path):
            print(f"   Local ZIP kept for retry: {zip_path}")
        return 1
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if release_lock is not None:
            try:
                release_lock()
            except Exception:  # noqa: BLE001
                pass
        _cleanup_staging(staging_dir)


if __name__ == "__main__":
    sys.exit(main())
