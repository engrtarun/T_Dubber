"""Carry the Python (Telethon) login over to the Go uploader — one login total.

Why this exists
---------------
``tgup`` speaks gotd, whose session storage Telethon cannot write, so the Go path
always started from an unauthorized ``tgup.session``. The only fix Telegram knows
is an interactive login: the code is dispatched the moment the auth flow starts,
and a machine with no terminal to type into can only fail after paying for it.

But the login already happened — in Python. ``telegram_uploader_session.session``
holds a *permanent* MTProto auth key for this account, and an auth key is just
256 bytes plus the DC it belongs to. gotd's storage is one small JSON document::

    {"Version": 1, "Data": {"Config": {...}, "DC": 5, "Addr": "91.108.56.185:443",
                            "AuthKey": "<b64 256 bytes>", "AuthKeyID": "<b64>",
                            "Salt": 0}}

so the key can be copied across instead of requested again::

    python telethon_session.py            # write tgup.session from the Python login
    python telethon_session.py --status   # say what would happen, write nothing

Only the key moves. The Python session keeps working — but do not run a Telethon
upload and a ``tgup`` upload at the same instant: two processes sharing one auth
key fight over its salt and can drop each other's messages.

The exact rules the bridge relies on:

* ``AuthKeyID`` is ``sha1(auth_key)[-8:]`` — the same bytes gotd checks in
  ``restoreConnection`` (a mismatch is reported as "corrupted key").
* ``Config`` of the destination is kept when it exists: those DC options are what
  the pool dials, and refetching them on every import would be pointless.
* A stale verdict (``tgup.session.state.json``) describes the *old* key, so it is
  removed rather than left to claim the new one is fine.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = ROOT / "telegram_uploader_session.session"
DEFAULT_DEST = ROOT / "tgup.session"

# gotd's session.Loader refuses anything else ("version mismatch").
GOTD_VERSION = 1

# An MTProto auth key is exactly 256 bytes; anything else is not a key.
AUTH_KEY_LEN = 256

# Values of $TGUP_TELETHON_SESSION that mean "do not carry the login over".
_DISABLED = ("0", "off", "none", "false", "no", "-")


class TelethonSessionError(RuntimeError):
    """The Python session is missing, unreadable, or has no usable auth key."""


def source_path() -> Path | None:
    """Where the Python login lives. ``None`` means "leave tgup.session alone".

    ``$TGUP_TELETHON_SESSION`` overrides the default beside this file, and
    setting it to ``off`` turns the carry-over off entirely — the escape hatch
    for the one case where tgup is deliberately logged into a different account.
    """
    override = os.environ.get("TGUP_TELETHON_SESSION", "").strip()
    if override:
        if override.lower() in _DISABLED:
            return None
        return Path(override)
    return DEFAULT_SOURCE


def auth_key_id(key: bytes) -> bytes:
    """The MTProto auth key id: last 8 bytes of SHA-1 over the key."""
    return hashlib.sha1(key).digest()[-8:]


def host_port(server_address: str, port: int) -> str:
    """``host:port`` the way Go's ``net.JoinHostPort`` would build it."""
    raw = (server_address or "").strip()
    if not raw:
        raise TelethonSessionError("the session has no server address")
    if raw.count(":") == 1:
        # Older Telethon stores "149.154.167.51:443" in server_address itself.
        return raw
    host = raw.strip("[]")
    if ":" in host:
        return f"[{host}]:{port or 443}"  # bare IPv6
    return f"{host}:{port or 443}"


def read_auth_key(path: Path | None = None) -> dict:
    """Read the permanent auth key out of a Telethon SQLite session.

    Opened read-only: Telethon may be holding the file open, and a session
    file must never be written by anyone but its own library.
    """
    source = source_path() if path is None else path
    if source is None:
        raise TelethonSessionError("Telegram session carry-over is disabled")
    source = Path(source)
    if not source.is_file():
        raise TelethonSessionError(f"no Telethon session at {source}")

    try:
        con = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise TelethonSessionError(f"cannot open {source}: {exc}") from exc
    try:
        try:
            cursor = con.execute("select * from sessions")
            names = [column[0] for column in cursor.description]
            rows = cursor.fetchall()
        except sqlite3.Error as exc:
            raise TelethonSessionError(
                f"{source} has no Telethon sessions table: {exc}"
            ) from exc
    finally:
        con.close()

    for row in rows:
        data = dict(zip(names, row))
        key = data.get("auth_key")
        if not isinstance(key, (bytes, bytearray, memoryview)):
            continue
        key = bytes(key)
        if len(key) != AUTH_KEY_LEN:
            continue
        dc = int(data.get("dc_id") or 0)
        if dc <= 0:
            continue
        return {
            "source": str(source),
            "dc": dc,
            "addr": host_port(str(data.get("server_address") or ""),
                               int(data.get("port") or 0)),
            "auth_key": key,
        }
    raise TelethonSessionError(
        f"{source} holds no {AUTH_KEY_LEN}-byte auth key — log in from Python "
        "once (the session is written only after a successful sign-in)"
    )


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def verdict_path(dest: Path) -> Path:
    """The cached authorized/rejected verdict that sits beside a session."""
    return dest.with_name(dest.name + ".state.json")


def build_payload(info: dict, previous: dict | None) -> dict:
    """The gotd ``Loader`` document, keeping the destination's DC config."""
    config = {}
    if isinstance(previous, dict):
        data = previous.get("Data")
        if isinstance(data, dict) and isinstance(data.get("Config"), dict):
            config = data["Config"]
    key = info["auth_key"]
    return {
        "Version": GOTD_VERSION,
        "Data": {
            "Config": config,
            "DC": info["dc"],
            "Addr": info["addr"],
            "AuthKey": base64.b64encode(key).decode("ascii"),
            "AuthKeyID": base64.b64encode(auth_key_id(key)).decode("ascii"),
            "Salt": 0,
        },
    }


def import_into(dest: Path, source: Path | None = None) -> dict:
    """Copy the Python login into ``dest`` for gotd. Returns what happened.

    Writing is atomic (temp file + rename) because a half-written session is
    indistinguishable from a corrupted one to the binary that reads it.
    """
    dest = Path(dest)
    info = read_auth_key(source)
    payload = build_payload(info, _read_json(dest))
    wanted = payload["Data"]["AuthKeyID"]

    previous = _read_json(dest)
    have = ((previous.get("Data") or {}) if isinstance(previous, dict) else {})
    have = have.get("AuthKeyID") if isinstance(have, dict) else None
    if have == wanted:
        return {
            "imported": False,
            "reason": "tgup already carries this key",
            "source": info["source"],
            "dest": str(dest),
            "dc": info["dc"],
            "addr": info["addr"],
            "key_id": wanted,
        }

    backup = None
    if dest.is_file():
        backup = dest.with_name(dest.name + ".bak")
        shutil.copy2(dest, backup)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, dest)

    # The verdict belongs to the key we just replaced.
    try:
        verdict_path(dest).unlink()
    except OSError:
        pass

    return {
        "imported": True,
        "source": info["source"],
        "dest": str(dest),
        "backup": str(backup) if backup else "",
        "dc": info["dc"],
        "addr": info["addr"],
        "key_id": wanted,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Carry the Telethon login into tgup's session (no OTP)."
    )
    parser.add_argument("--dest", default=str(DEFAULT_DEST),
                        help="gotd session to write (default: tgup.session)")
    parser.add_argument("--source", default="",
                        help="Telethon .session to read "
                             "(default: telegram_uploader_session.session)")
    parser.add_argument("--status", action="store_true",
                        help="report what would happen; write nothing")
    args = parser.parse_args(argv)

    source = Path(args.source) if args.source else source_path()
    if source is None:
        print("carry-over is disabled (TGUP_TELETHON_SESSION=off)", file=sys.stderr)
        return 1

    try:
        info = read_auth_key(source)
    except TelethonSessionError as exc:
        print(f"no Python login to carry over: {exc}", file=sys.stderr)
        return 1

    dest = Path(args.dest)
    key_id = base64.b64encode(auth_key_id(info["auth_key"])).decode("ascii")
    existing = _read_json(dest)
    have = existing.get("Data", {}) if isinstance(existing, dict) else {}
    have = have.get("AuthKeyID") if isinstance(have, dict) else None

    if args.status:
        print(json.dumps({
            "source": info["source"],
            "dest": str(dest),
            "dc": info["dc"],
            "addr": info["addr"],
            "key_id": key_id,
            "already_there": have == key_id,
            "dest_exists": dest.is_file(),
        }, indent=2))
        return 0

    try:
        result = import_into(dest, source)
    except (TelethonSessionError, OSError) as exc:
        print(f"carry-over failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
