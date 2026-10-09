"""
direct_archive.py -- link to Telegram, with 0 bytes written to this PC.

WHY THIS EXISTS
---------------
The owner asked for "0% PC use -- jahan storage ki baat ho, Telegram use karo".
Until now that was half a feature: ``tgup --url`` could stream a direct media URL
part by part with nothing on local disk, and Python could call it
(``go_planner.upload_url_via_go``), but a *pasted link* was a YouTube watch
page, not a media URL -- so the only route left was
``link_resolver.resolve_to_local_file()``, which downloads the whole video first.
A 9 GB source therefore meant 9 GB of laptop disk before a single byte reached
Telegram.

This module is the missing front half. It resolves a link to something tgup can
range-stream (``link_resolver.resolve_to_stream``), streams it into a channel,
and mirrors the result into SQLite so the next run recognises the same bytes and
skips the upload (Tier 0 dedup, keyed on the content digest tgup already
computed).

WHAT IT REFUSES TO DO
---------------------
It never falls back to downloading. That is the entire point: a "helpful"
silent download would turn a 9 GB stream into a 9 GB disk write behind the
caller's back, and the caller would have no idea the guarantee was gone. When
streaming is impossible, the returned dict says so, names the tier that failed,
and points at the existing download path for the caller to decide about.

The three tiers, each falling through to the next:

1. ``tgup_url``       -- the real send.
2. ``tgup_dry_run``   -- tgup planned and hashed without sending, so the reason
                         is now concrete ("origin will not serve byte ranges")
                         instead of a guess. No session, no OTP, no upload.
3. ``unsupported``    -- nothing here can stream it. The caller is told to use
                         ``link_resolver.resolve_to_local_file``.

WHAT IS STILL PREDICTED
-----------------------
No real Telegram send has been made from this path: that needs an authorised
session and somebody to type an OTP. What is proved is the plan, the digest, the
row that lands in SQLite and the refusal behaviour. See section 9d of
DIRECT_LINK_TG_UPLOAD.md.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import link_resolver  # noqa: E402

# Imported lazily inside functions that need them. ``db`` must not be touched at
# import time: importing this module has to stay free of side effects so a caller
# (or a test) can inspect the ladder without a database in reach.


def _env_flag(name: str, default: bool = True) -> bool:
    """Read a boolean kill-switch from the environment.

    Same contract as ``pipeline._env_flag`` and ``link_resolver._env_flag``:
    only an explicit falsey value turns a feature off, so a typo can never
    silently disable it.
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in ("0", "off", "false", "no")


def _archive_fingerprint(source_sha256: str) -> str:
    """The canonical archive key: the whole-content digest, truncated.

    Mirrors ``app._archive_fingerprint`` deliberately -- the writer and the Tier-0
    reader must agree on what identifies a file, or dedup never fires and the
    same bytes get uploaded twice under two different keys.
    """
    return (source_sha256 or "")[:32]


def _channel_from_env(channel: str = "") -> str:
    """Let the environment force the destination channel.

    Useful for a scheduled machine with no UI: one env var instead of editing
    every call site.
    """
    forced = (os.environ.get("TDUBBER_DIRECT_ARCHIVE_CHANNEL") or "").strip()
    return forced or (channel or "").strip()


def _archive_journal(target: link_resolver.StreamTarget, result: dict, channel: str) -> dict:
    """Shape a tgup result into the journal ``db.upsert_archive`` expects.

    ``file_path`` holds the *URL*, not a path. There is no local file, and the
    column is a TEXT column that exists to say where the protected bytes came
    from -- a URL is the honest answer, and pretending otherwise would let some
    later reader try to reopen this "file".
    """
    size = int(result.get("total_size") or target.size or 0)
    parts = result.get("parts") or []
    return {
        "fingerprint": _archive_fingerprint(result.get("source_sha256") or ""),
        "filename": target.filename or "source",
        "file_path": target.url,
        "size": size,
        "channel": channel,
        "state": "complete",
        "message_id": int(result.get("message_id") or 0) or None,
        "message_link": result.get("message_link") or "",
        "chunked": bool(result.get("chunked")),
        "chunk_count": int(result.get("chunk_count") or len(parts) or 0),
        "manifest": {"chunk_size": 0},
        "source_sha256": result.get("source_sha256") or "",
    }


def _mirror_to_db(journal: dict) -> int | None:
    """Record the archive in SQLite so the next run can dedup on it.

    Failures are swallowed and reported through the returned ``None``: the bytes
    are already safe in Telegram, and losing the index is recoverable while
    losing the upload is not.
    """
    try:
        import db

        return db.upsert_archive(journal)
    except Exception:  # noqa: BLE001
        return None


def archive_link(
    url: str,
    channel: str,
    api_id,
    api_hash: str = "",
    *,
    phone: str = "",
    caption: str = "",
    concurrency: int = 4,
    dry_run: bool = False,
    url_timeout: float = 0.0,
    timeout: float = 15.0,
    progress_callback=None,
    resolve_timeout: float = 15.0,
) -> dict:
    """Archive a pasted link into a Telegram channel without touching the disk.

    ``dry_run=True`` resolves the link, plans every part and returns the digests
    without sending anything -- so it needs neither a session nor an OTP, and it
    still writes nothing locally.

    The returned dict always carries ``ok``, ``tier`` and ``reason``:

    * ``tier="tgup_url"``    the send happened (or, in dry-run, the plan hashed).
    * ``tier="tgup_dry_run"`` the send failed; a dry run explains why.
    * ``tier="unsupported"`` the link cannot be streamed at all. Use
      ``link_resolver.resolve_to_local_file`` if a local copy is acceptable.

    On a successful send with a digest, ``fingerprint`` is that digest truncated
    the same way every other archive key is -- pass it and the channel to
    ``db.find_archive`` and a repeat run is a Tier-0 hit instead of a re-upload.
    ``already_archived`` says whether such a row already existed before this run.
    """
    if not _env_flag("TDUBBER_DIRECT_ARCHIVE", True):
        raise link_resolver.StreamNotStreamable(
            "Direct (zero-disk) archiving is switched off "
            "(TDUBBER_DIRECT_ARCHIVE). Use link_resolver.resolve_to_local_file "
            "for the disk path."
        )

    destination = _channel_from_env(channel)
    if not dry_run and not destination:
        raise link_resolver.StreamNotStreamable(
            "A Telegram channel is required. Pass channel= or set "
            "TDUBBER_DIRECT_ARCHIVE_CHANNEL."
        )

    target = link_resolver.resolve_to_stream(url, timeout=resolve_timeout)
    if not target.direct:
        # Tier 3. Not an exception: the caller asked a question and the honest
        # answer is "no, and here is why", not a traceback.
        return {
            "ok": False,
            "tier": "unsupported",
            "reason": target.reason,
            "kind": target.kind,
            "page_url": target.page_url,
            "direct": False,
            "use_instead": "link_resolver.resolve_to_local_file",
        }

    import go_planner

    base = {
        "ok": False,
        "kind": target.kind,
        "page_url": target.page_url or target.url,
        "direct_url": target.url,
        "filename": target.filename,
        "size": int(target.size or 0),
        "channel": destination,
        "dry_run": bool(dry_run),
    }

    payload = dict(base)

    # Tier 1: the real send.
    try:
        result = go_planner.upload_url_via_go(
            target.url,
            api_id=api_id,
            api_hash=api_hash,
            channel=destination,
            phone=phone,
            concurrency=concurrency,
            caption=caption,
            url_timeout=url_timeout,
            dry_run=dry_run,
            timeout=timeout,
            progress_callback=progress_callback,
        )
    except RuntimeError as exc:
        failure = str(exc)
    else:
        payload.update({
            "ok": True,
            "tier": "tgup_url",
            "reason": "",
            "message_id": int(result.get("message_id") or 0),
            "message_link": result.get("message_link") or "",
            "chunked": bool(result.get("chunked")),
            "chunk_count": int(result.get("chunk_count") or 0),
            "source_sha256": result.get("source_sha256") or "",
            "fingerprint": _archive_fingerprint(result.get("source_sha256") or ""),
            "elapsed_sec": float(result.get("elapsed_sec") or 0.0),
            "bytes_per_sec": float(result.get("bytes_per_sec") or 0.0),
        })

        digest = result.get("source_sha256") or ""
        # Asked before the row is written, not after: otherwise the very run that
        # created the archive would report itself as a Tier-0 hit and the flag
        # would mean nothing.
        payload["already_archived"] = already_archived(digest, destination)
        if digest and not dry_run:
            # A dry run has no bytes on Telegram, so there is nothing to index.
            # Skipping the write here is what keeps "0 bytes on this PC" true and
            # keeps a plan out of the archive list where it would look like a
            # completed backup.
            payload["archive_id"] = _mirror_to_db(
                _archive_journal(target, result, destination)
            )
        else:
            payload["archive_id"] = None
        return payload

    # Tier 2: ask tgup what it thinks, without sending. This needs no session
    # and no OTP, so a failure on the first tier can always be explained.
    explanation = ""
    if not dry_run:
        try:
            go_planner.upload_url_via_go(
                target.url,
                api_id=api_id,
                api_hash="",
                channel="",
                phone="",
                concurrency=concurrency,
                dry_run=True,
                timeout=timeout,
            )
        except RuntimeError as exc:
            explanation = str(exc)
        except Exception as exc:  # noqa: BLE001
            explanation = str(exc)
        else:
            explanation = (
                "tgup planned and hashed the link successfully, so the failure "
                "above is Telegram-side (session, channel or network), not the "
                "origin."
            )

    payload.update({
        "tier": "tgup_dry_run",
        "reason": failure,
        "explanation": explanation,
        "use_instead": "",
    })
    return payload


def already_archived(source_sha256: str, channel: str) -> bool:
    """Has these exact bytes already been archived on this channel?

    Answers "was a previous run of this same link already protected in Telegram?"
    -- which is only knowable by looking at the digest tgup just computed and
    asking whether a completed archive row for it existed *before* this run.
    Useful to a caller deciding whether the transfer was redundant, and it is
    what makes the content-derived fingerprint worth having.
    """
    if not source_sha256 or not channel:
        return False
    try:
        import db

        row = db.find_archive(_archive_fingerprint(source_sha256), channel)
    except Exception:  # noqa: BLE001
        return False
    return bool(row) and str(row.get("state") or "") == "complete"