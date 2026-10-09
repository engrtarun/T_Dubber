"""Log in to Telegram once — from Python — and hand the key to the Go uploader.

    python login_once.py --qr                  # scan the QR with your phone (no typing)
    python login_once.py --qr --qr-png qr.png  # ...and also write it as a PNG
    python login_once.py                      # type the code in this console
    python login_once.py --code-file otp.txt  # code is dropped into a file instead

This is the same Python login this repo always used (Telethon, session file
``telegram_uploader_session.session``), with two things added that did not exist
before:

1. It also runs ``telethon_session.import_into`` afterwards, so ``tgup.session``
   carries the *same* auth key. The Go uploader therefore never asks for a code
   again — the OTP is spent once, here, by hand.
2. ``--qr`` logs in the way Telegram Desktop does: a QR appears (in the
   terminal, and optionally as a PNG), the phone scans it, done. No code is
   typed and no code is spent. Note that Two-Step Verification is *not*
   covered by the scan: with 2FA on, Telegram asks for the cloud password
   before it will even issue a token, so ``--password-file`` (or the prompt)
   still applies on the QR path.
3. The code can come from a file. That exists because the tool driving this
   machine has no terminal to type into, and asking Telegram for a code that
   nobody can answer is exactly the waste this whole login path was built to
   stop. On a console the normal prompt is used.

If the session already works, nothing is requested at all: ``client.start``
returns immediately and only the carry-over runs.

Exit code 0 = authorized (and carried over). Exit code 1 = nothing to report.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import telethon_session  # noqa: E402

# Telegram codes are short-lived; beyond this the run is a lost cause.
CODE_TIMEOUT_SECONDS = 600
RETRY_ATTEMPTS = 3


def wait_for_code(path: Path, rejected: set[str]) -> str:
    """Block until ``path`` holds a code nobody has tried yet.

    One file, rewritten in place: the harness outside this process knows where
    to write and never needs to talk to a terminal.
    """
    print(f"code file: {path} (waiting for it to change)", flush=True)
    deadline = time.time() + CODE_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text and text not in rejected:
            rejected.add(text)
            return text
        time.sleep(1)
    raise TimeoutError(f"no code arrived in {path} within {CODE_TIMEOUT_SECONDS}s")


def run_qr_login(session: str, api_id: int, api_hash: str,
                 png_path: str | None, timeout: float,
                 password_callback=None):
    """The ``--qr`` path: connect, show the QR, wait for the phone.

    The client is built *inside* the running loop and torn down in the same
    one — a client created outside an event loop and driven from inside
    another one is a loop-mismatch trap, and this way there is only ever one
    loop. Returns the authorized ``User``.

    ``password_callback`` supplies the Two-Step-Verification cloud password.
    It is asked for only when Telegram actually demands one: with 2FA on,
    ``auth.exportLoginToken`` itself refuses before any QR is drawn.
    """
    import asyncio

    from telethon import TelegramClient

    import tg_qr_login

    async def flow():
        client = TelegramClient(session, api_id, api_hash)
        await client.connect()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                print(f"already authorized as {me.id} (@{me.username or 'no username'})")
                return me
            user = await tg_qr_login.qr_login(
                client, png_path=png_path or None, timeout=timeout,
                password_callback=password_callback)
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram returned a user but the session is not authorized")
            print(f"authorized as {user.id} (@{getattr(user, 'username', None) or 'no username'})")
            return user
        finally:
            await client.disconnect()

    return asyncio.run(flow())


def _force_utf8_console() -> None:
    """Make stdout/stderr UTF-8 so the QR art can be printed at all.

    A Windows console still defaults to cp1252, and the QR is drawn with
    block characters (U+2588, U+2580, U+2584). print() then raises
    UnicodeEncodeError and kills the login. errors="replace" keeps a
    legacy terminal usable: worst case the glyph is a '?', never a crash.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # A stream that cannot be reconfigured (already-detached, or a
            # real file we must not touch) just keeps its own encoding.
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    parser = argparse.ArgumentParser(
        description="One Python login, shared by the Telethon and Go uploaders."
    )
    parser.add_argument("--session", default=str(ROOT / "telegram_uploader_session"),
                        help="Telethon session to use (default: telegram_uploader_session)")
    parser.add_argument("--qr", action="store_true",
                        help="log in by scanning a QR code with the phone (no code typing)")
    parser.add_argument("--qr-png", default="",
                        help="with --qr: also write the QR to this PNG file")
    parser.add_argument("--qr-timeout", type=float, default=300.0,
                        help="with --qr: give up after this many seconds (default 300)")
    parser.add_argument("--code-file", default="",
                        help="read the login code from this file instead of stdin")
    parser.add_argument("--password-file", default="",
                        help="read the 2FA password from this file instead of stdin")
    args = parser.parse_args(argv)

    # Imported late: the config decrypts credentials, and a `--help` should not
    # need DPAPI or Flask to answer.
    import app
    from telethon import TelegramClient

    api_id, api_hash, phone, channel = app.load_tg_config()
    if not (api_id and api_hash and phone):
        print("config.json incomplete: api_id/api_hash/phone chahiye", file=sys.stderr)
        return 1

    code_file = Path(args.code_file) if args.code_file else None
    password_file = Path(args.password_file) if args.password_file else None
    tried_codes: set[str] = set()
    # Shared across retries, not a fresh set() per call: a rejected password
    # sitting in the file would otherwise be read back immediately and
    # re-tried forever, spinning instead of waiting for a corrected one.
    tried_passwords: set[str] = set()

    def read_code() -> str:
        if code_file:
            return wait_for_code(code_file, tried_codes)
        return input("Telegram login code: ").strip()

    def read_password() -> str:
        if password_file:
            return wait_for_code(password_file, tried_passwords)
        # getpass, not input: input() echoes the password to the screen and
        # into the shell's scrollback. A cloud password typed in the clear is
        # a leaked password.
        return getpass.getpass("Two-factor password: ").strip()

    print(f"phone   : {phone[:5]}****{phone[-2:]}")
    print(f"session : {args.session}.session")

    if args.qr:
        try:
            run_qr_login(args.session, int(api_id), api_hash,
                         args.qr_png or None, args.qr_timeout,
                         password_callback=read_password)
        except (TimeoutError, RuntimeError, OSError, ValueError) as exc:
            print(f"QR login failed: {exc}", file=sys.stderr)
            return 1
    else:
        client = TelegramClient(args.session, int(api_id), api_hash)
        try:
            last_error: Exception | None = None
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                try:
                    user = client.start(phone=phone, code_callback=read_code,
                                        password=read_password)
                    break
                except Exception as exc:  # noqa: BLE001 - reported, then retried
                    last_error = exc
                    print(f"attempt {attempt} failed: {exc}", flush=True)
                    if attempt == RETRY_ATTEMPTS:
                        print(f"login failed: {exc}", file=sys.stderr)
                        return 1
                    # A wrong code costs nothing to replace: start() asks Telegram
                    # for a fresh one on the next call.
                    if code_file:
                        try:
                            code_file.unlink()
                        except OSError:
                            pass
                        tried_codes.clear()
            else:
                print(f"login failed: {last_error}", file=sys.stderr)
                return 1

            if not user:
                print("Telegram did not accept that code", file=sys.stderr)
                return 1
            print(f"authorized as {user.id} (@{user.username or 'no username'})")
        finally:
            if client.is_connected():
                client.disconnect()

    # Now the part that stops every future OTP: tgup gets the same key.
    try:
        info = telethon_session.import_into(ROOT / "tgup.session")
    except (telethon_session.TelethonSessionError, OSError) as exc:
        print(f"carry-over to tgup.session failed: {exc}", file=sys.stderr)
        return 1
    # The key may be the same bytes as before (Telegram revokes a key, a fresh
    # sign-in revives it), so the carry-over can correctly report "nothing to
    # do" while a cached "rejected" verdict from those dead runs sits beside the
    # session claiming otherwise.
    try:
        telethon_session.verdict_path(ROOT / "tgup.session").unlink()
    except OSError:
        pass
    print(json.dumps(info, indent=2))
    print("\nDone — Go upload (tgup) will reuse this login. No more codes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
