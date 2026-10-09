"""QR login for Telegram — scan with your phone, type nothing.

This is the same ``auth.exportLoginToken`` protocol Telegram Desktop uses for
"scan the QR code" login:

1. The client asks Telegram for a *login token* (it is unauthorized at this
   point, which is exactly what the call is for).
2. The token is shown as a QR code (``tg://login?token=...``).
3. The phone scans it and the user taps confirm. That tap is the second
   factor — no OTP is ever typed, and no login code is spent.
4. The server sends ``updateLoginToken`` to the client; the next
   ``exportLoginToken`` comes back as ``auth.loginTokenSuccess``, and the
   auth key this client is connected with becomes authorized.

The token rotates when it expires, so the loop re-exports and redraws.

Two-Step Verification is *not* covered by the scan. This module used to claim
otherwise, and that was wrong: with 2FA on, Telegram answers
``auth.exportLoginToken`` itself with ``SESSION_PASSWORD_NEEDED`` — before any
QR is ever drawn. The cloud password is still needed, and it is asked for
through ``password_callback``. Only the OTP is saved, not the password.

Rendering happens in two channels, on purpose:

* a half-block drawing on **stderr**, so a human at a console can scan it
  straight off the terminal, and
* an optional **PNG** (``png_path``), for when there is no console in front
  of the person who has to scan — the file can be shown on screen instead.

Everything here is import-safe: Telethon is imported lazily inside the
functions that need it, so tests can exercise the pure parts (URL format,
matrix rendering, PNG bytes) without a network or an event loop.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone

# A login QR is small (the token is ~44 bytes); this is the whole frame a
# human needs. Quiet zone comes from the border argument, not from guessing.
TERMINAL_BORDER_MODULES = 2
PNG_SCALE = 8
PNG_BORDER_MODULES = 4  # the QR standard's quiet zone


def login_url(token: bytes) -> str:
    """The exact string Telegram Desktop puts in the QR: URL-safe base64, no padding."""
    return "tg://login?token=" + base64.urlsafe_b64encode(token).rstrip(b"=").decode()


def _matrix_rows(url: str) -> list[list[bool]]:
    import segno

    qr = segno.make(url, error="m")
    return [[bool(bit) for bit in row] for row in qr.matrix]


def render_text(url: str, border: int = TERMINAL_BORDER_MODULES) -> str:
    """Draw the QR with half-block characters — two module rows per text line.

    Uses ``▀▄█`` so a phone camera can read a standard terminal at normal
    font size; plain ``#`` art is usually too fine-grained to scan.
    """
    rows = _matrix_rows(url)
    height, width = len(rows), len(rows[0])
    pad = " " * (width + 2 * border)
    lines = [pad] * border
    for y in range(0, height, 2):
        top = rows[y]
        bottom = rows[y + 1] if y + 1 < height else [False] * width
        chars = [" "]
        for t, b in zip(top, bottom):
            if t and b:
                chars.append("█")
            elif t:
                chars.append("▀")
            elif b:
                chars.append("▄")
            else:
                chars.append(" ")
        chars.append(" ")
        lines.append("".join(chars))
    lines.extend([pad] * border)
    return "\n".join(lines)


def save_png(url: str, path) -> None:
    """Write the same QR as a PNG — for showing it on screen instead of a console."""
    import segno

    segno.make(url, error="m").save(str(path), scale=PNG_SCALE, border=PNG_BORDER_MODULES)


async def qr_login(client, *, png_path=None, timeout: float = 300.0,
                   password_callback=None, log=None):
    """Run the QR flow on an (unauthorized) connected Telethon client.

    Returns the authorized ``User`` from ``auth.Authorization``. Raises
    ``TimeoutError`` when nobody scanned in ``timeout`` seconds — which costs
    nothing: no code was requested, so a retry is always clean.

    Accounts with Two-Step Verification still need the cloud password: the
    server answers ``SESSION_PASSWORD_NEEDED`` to ``exportLoginToken`` itself,
    not after the scan. Pass ``password_callback`` for that, and it is asked
    for only when the server actually demands it. (Scanning alone is *not*
    always enough — that was the assumption this docstring used to make.)
    """
    if log is None:
        def log(message: str) -> None:
            print(message, flush=True)

    from telethon.errors import (
        PasswordHashInvalidError,
        SessionPasswordNeededError,
    )
    from telethon import utils
    from telethon.tl.functions.auth import ExportLoginTokenRequest
    from telethon.tl.types import UpdateLoginToken
    from telethon.tl.types.auth import (
        LoginToken,
        LoginTokenMigrateTo,
        LoginTokenSuccess,
    )

    async def _export():
        """One ``auth.exportLoginToken``, answering a 2FA demand if it comes.

        The 2FA prompt belongs here rather than at the call site: Telegram
        raises ``SessionPasswordNeededError`` on *every* export once 2FA is
        on, including the very first one.
        """
        while True:
            try:
                return await client(ExportLoginTokenRequest(
                    client.api_id, client.api_hash, []))
            except SessionPasswordNeededError:
                if password_callback is None:
                    raise RuntimeError(
                        "this account has Two-Step Verification and the "
                        "server wants the cloud password; no password source "
                        "was given (use --password-file)"
                    ) from None
                log("Two-Step Verification is on — the cloud password is needed.")
                while True:
                    password = await utils.maybe_async(password_callback())
                    try:
                        return await client.sign_in(password=password)
                    except PasswordHashInvalidError:
                        log("wrong password, try again")

    confirmed = asyncio.Event()

    async def _on_update(update):
        if isinstance(update, UpdateLoginToken):
            confirmed.set()

    # Telethon calls these add_event_handler now (the old add_update_handler
    # name is gone in 1.45). events.Raw() is what `event=None` defaults to,
    # and it is what we want: the callback does its own isinstance check.
    client.add_event_handler(_on_update)
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"no phone scanned the QR within {timeout:.0f}s "
                    "(nothing was spent — run again to get a fresh code)"
                )

            result = await _export()
            # When 2FA is on, _export answers the password itself and
            # sign_in() hands back a User instead of a LoginToken. That is
            # already an authorized session — no QR involved from here on.
            if not isinstance(result, (LoginToken, LoginTokenMigrateTo,
                                       LoginTokenSuccess)):
                return result
            if isinstance(result, LoginTokenSuccess):
                return result.authorization.user
            if isinstance(result, LoginTokenMigrateTo):
                # The account lives on another DC: reconnect there (fresh
                # auth key, same QR flow) and export again.
                log(f"migrating to DC {result.dc_id} for the login token")
                await client._switch_dc(result.dc_id)
                continue

            assert isinstance(result, LoginToken), f"unexpected login token type: {result!r}"
            url = login_url(result.token)
            log("\nScan this QR with your phone (Telegram → Settings → Link Desktop Device):\n")
            log(render_text(url))
            if png_path:
                save_png(url, png_path)
                log(f"\nQR also written to: {png_path}")

            # Keep this QR on screen for its whole lifetime: a re-export
            # makes the previous token expire, and an expired QR on screen
            # is a scan that goes nowhere. updateLoginToken wakes us early
            # the moment the phone confirms.
            expires = getattr(result, "expires", None)
            ttl = remaining
            if isinstance(expires, datetime):
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                ttl = min(ttl, max(1.0, (expires - datetime.now(timezone.utc)).total_seconds()))
            confirmed.clear()
            try:
                await asyncio.wait_for(confirmed.wait(), timeout=ttl)
            except asyncio.TimeoutError:
                log("QR expired — drawing a fresh one")
    finally:
        client.remove_event_handler(_on_update)
