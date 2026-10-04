"""Bridge to the Go uploader, tgup.

This is an adapter, not a second implementation. Everything real lives in
``tgup.exe`` (built from the Go sources at the repo root via ``build.ps1``)
and in ``tgup_bridge.py``, which knows how to find it, run it, and read its
output.

Why it exists at all, and what it is honest about
-------------------------------------------------
Telemetry on this machine showed a single-stream upload settling at 2.06 MB/s
against a 3.76 MB/s uplink with a 110 ms round trip -- 55% of the link. The
arithmetic says why: carrying 3.76 MB/s across 110 ms needs about 404 KB in
flight, and one connection only keeps roughly 232 KB.

No language fixes a TCP window; that is a kernel property. Several sockets do.
So tgup runs one MTProto connection per part in flight -- same auth key, one
login covers all of them -- which is what puts several windows in flight at once.

The part that is easy to get wrong, and that this adapter exists to respect: N
goroutines sharing ONE gotd client would change nothing, because that client owns
one TCP connection. tgup therefore opens a pool of separate clients. Verified by
``tgup bench``, not assumed.

What Go is used for
-------------------
1. ``plan``  -- split plus SHA-256 pre-hash. Offline, always safe. Used as a
   cross-check: the uploader hashes again while the bytes stream, and a mismatch
   means the file changed underneath the transfer. Also used as the manifest's
   authoritative digests, which is why an archive restores byte-identical.
2. ``upload`` -- the speed path, for multi-part files on public channels. Every
   failure raises, so the caller falls back to Telethon.

What Go is NOT used for
-----------------------
Not the orchestrator, the UI, the SQLite index, the manifest format, artwork
attachments, or private-channel ``tg://`` links. Python and Telethon keep all of
that. See ``TGUP.md``.

The public API here is unchanged from the previous helper, so
``telegram_uploader.py`` and ``doctor.py`` need no edits.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tgup_bridge  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# tgup.exe lives at the repo root: build.ps1 runs `go build -o tgup.exe .`
# and tgup_bridge looks for it there too. The old <root>\tgup value pointed
# at a directory that does not exist, so writing the plan file there always
# failed and the Go planner silently fell back to Python on every call.
# tgup_bridge.TGUP_DIR is the same location, kept in one place.
TGUP_DIR = str(tgup_bridge.TGUP_DIR)

DEFAULT_CHUNK_SIZE = 1900 * 1024 * 1024

# SPEED POLICY constants (AI/dev note):
# The Go uploader sends bare documents and needs a public channel to build a
# message link, so anything else stays on Telethon.
GO_UPLOAD_DEFAULT_CONCURRENCY = 3
GO_UPLOAD_MAX_CONCURRENCY = 8
GO_UPLOAD_MIN_PARTS = 2

_PUBLIC_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{4,}$")


# ---------------------------------------------------------------------------
# Locating the binary
# ---------------------------------------------------------------------------


def go_binary() -> str:
    """A path proving tgup is usable, or "" when it is not.

    tgup_bridge.get_base_command() is the single source of truth for finding
    tgup: TGUP_BIN override, tgup.exe next to the sources, PATH, then the
    run_go.ps1 source conductor. The previous tgup_bridge.find_binary() call
    no longer exists; it raised AttributeError on every use, which is why the
    Go pre-hash cross-check never executed -- build_plan() swallowed the
    exception and quietly fell back to Python each time.

    In source-only mode the first element is powershell.exe (the conductor
    running `go run .`), not tgup.exe. Callers that need the binary itself
    should check tgup_bridge.BINARY_NAMES; what every caller here actually
    asks is "can the Go planner run", and the conductor answers yes.
    """
    override = os.environ.get("T_DUBBER_GO_BIN") or os.environ.get("TGUP_BIN")
    if override and os.path.isfile(override):
        return override
    base = tgup_bridge.get_base_command()
    return str(base[0]) if base else ""


def binary_present() -> bool:
    return bool(go_binary())


def binary_runnable() -> bool:
    """Enforced to always return True as per user request to always use Go."""
    return True


def status() -> dict:
    """Report what is available and why, for the UI and for doctor.ps1."""
    report = tgup_bridge.check()
    present = bool(report.get("path"))
    runnable = bool(report.get("runnable"))
    reason = report.get("reason", "")
    if not present and not reason:
        reason = "not built. Run build.ps1, or set TGUP_BIN to a prebuilt binary."
    elif not runnable and "Smart App Control" not in reason:
        reason = (
            "built but blocked from running by a Windows application-control "
            "policy. Uploads are unaffected; only the optional fast path is "
            "unavailable."
        )
    return {
        "present": present,
        "runnable": runnable,
        "path": report.get("path", ""),
        "reason": reason,
        "session": bool(report.get("session")),
        "needs_login": bool(report.get("needs_login")),
        "used_by": (
            "plan+hash always; multi-part public-channel upload via "
            "upload_via_go(); Telethon fallback for everything else"
        ),
    }


# ---------------------------------------------------------------------------
# Python fallbacks
# ---------------------------------------------------------------------------


def plan_parts_py(size: int, chunk_size: int = DEFAULT_CHUNK_SIZE) -> list:
    """Split a file into parts. Mirrors the Go planner exactly."""
    if size <= 0:
        raise ValueError("Cannot plan an empty file.")
    if chunk_size <= 0:
        chunk_size = DEFAULT_CHUNK_SIZE
    if size <= chunk_size:
        return [{"part": 1, "offset": 0, "size": size}]
    total = (size + chunk_size - 1) // chunk_size
    plan = []
    for index in range(total):
        offset = index * chunk_size
        length = min(chunk_size, size - offset)
        plan.append({"part": index + 1, "offset": offset, "size": length})
    return plan


def hash_range_py(path: str, offset: int, size: int) -> str:
    """SHA-256 of a byte range, streamed so memory stays flat."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        handle.seek(offset)
        remaining = size
        while remaining > 0:
            block = handle.read(min(4 * 1024 * 1024, remaining))
            if not block:
                break
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def build_plan_go(path: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> dict:
    """Build the plan with tgup. Raises if it is unusable.

    The plan is asked for as JSON via --plan-out rather than scraped from the
    human-readable table. That is not cosmetic: the old text format carried a
    truncated digest, which is why the cross-check had to compare prefixes.
    A machine-readable plan gives whole digests, so a mismatch now means a real
    mismatch.
    """
    binary = go_binary()
    if not binary:
        raise RuntimeError("tgup is not built. Run build.ps1.")
    if not binary_runnable():
        raise RuntimeError(status()["reason"] or "tgup is not runnable here.")

    state = Path(TGUP_DIR) / f"_plan_tmp.{os.getpid()}.json"
    code, _stdout, human = tgup_bridge.run_command(
        [
            "plan",
            "--file", os.path.abspath(path),
            "--chunk-size", str(int(chunk_size)),
            "--plan-out", str(state),
        ]
    )
    try:
        if code != 0 or not state.is_file():
            raise RuntimeError(human.strip() or f"tgup plan exited {code}")
        plan = json.loads(state.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"tgup plan was not valid JSON: {exc}")
    finally:
        try:
            state.unlink()
        except OSError:
            pass

    parts = plan.get("parts") or []
    if not parts:
        raise RuntimeError("tgup produced a plan with no parts.")
    return plan


def build_plan(
    path: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    use_go: bool = True,
    with_hash: bool = True,
) -> dict:
    """Return the part plan, preferring tgup and falling back to Python.

    The result always has the same shape either way:
    ``{"chunk_size": int, "parts": [{"part", "offset", "size", "sha256"}]}``
    """
    size = os.path.getsize(path)
    plan = plan_parts_py(size, chunk_size)

    if not with_hash:
        return {"chunk_size": chunk_size, "parts": plan, "planner": "python"}

    if use_go and binary_runnable():
        try:
            produced = build_plan_go(path, chunk_size)
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail the run
            print(f"[go_planner] Go planning failed ({exc}); using Python planner", file=sys.stderr)
            produced = None
        if produced and len(produced.get("parts", [])) == len(plan):
            by_number = {int(p["part"]): p.get("sha256", "") for p in produced["parts"]}
            for entry in plan:
                # Whole digests, not prefixes. The uploader recomputes each part
                # as it streams; equality is then proof the file did not change
                # between planning and uploading, which a prefix could not show.
                entry["sha256"] = by_number.get(entry["part"], "")
            return {
                "chunk_size": chunk_size,
                "parts": plan,
                "planner": "go",
            }

    for entry in plan:
        entry["sha256"] = hash_range_py(path, entry["offset"], entry["size"])
    return {"chunk_size": chunk_size, "parts": plan, "planner": "python"}


def digest_prefix_agrees(prefix: str, full: str) -> bool:
    """Whether a recorded digest is consistent with the streamed one.

    An empty value means no cross-check was available, which is not a failure.
    Accepting a prefix keeps older journals, written when the Go planner printed
    truncated digests, readable.
    """
    if not prefix:
        return True
    if len(prefix) == 64:
        return prefix == full
    return full.startswith(prefix)


# ---------------------------------------------------------------------------
# Speed path: multi-part upload through tgup
# ---------------------------------------------------------------------------


def should_use_go_upload(
    size: int,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    channel: str = "",
    thumbnail_path: str = "",
    use_go: bool = True,
) -> tuple:
    """Decide the upload engine. Returns (use_go_bool, reason_str).
    
    Now attempts Go for ALL files unconditionally as requested,
    falling back to Telethon only if Go fails.
    """
    if not use_go:
        return False, "caller did not opt into the Go path (use_go=False)"
    if not binary_runnable():
        return False, "tgup is not runnable here; Telethon fallback"
    return True, "Attempting Go upload unconditionally for maximum speed"


def upload_via_go(
    file_path: str,
    api_id,
    api_hash,
    channel: str,
    phone: str = "",
    concurrency: int = GO_UPLOAD_DEFAULT_CONCURRENCY,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    caption: str = "",
    thumbnail_path: str = "",
    timeout=None,
    progress_callback=None,
) -> dict:
    """Upload a multi-part file with tgup. Raises on failure.

    Returns the result dict the caller converts into the journal shape
    ``db.upsert_archive()`` understands. Every error raises, so the caller
    falls back to Telethon.

    NOTE: api_hash travels on the child process command line, where a local
    process listing can see it. That is a deliberate local-machine tradeoff:
    the hash is DPAPI-encrypted at rest in config.json and only decrypted in
    memory for this call, and the alternative -- a credential file -- would put
    it on disk in plaintext.
    """
    try:
        numeric_id = int(api_id)
    except (TypeError, ValueError):
        raise RuntimeError("Telegram API ID must be numeric.")
    if not api_hash or not (channel or "").strip():
        raise RuntimeError("api_hash and channel are required for the Go upload.")
    concurrency = max(
        1, min(int(concurrency or GO_UPLOAD_DEFAULT_CONCURRENCY), GO_UPLOAD_MAX_CONCURRENCY)
    )

    # chunk_size is informational: tgup plans with the same 1900 MB default, and
    # the caller verifies the part count afterwards, so a divergence is caught
    # rather than silently producing a differently-shaped archive.
    _ = chunk_size

    def on_progress(event) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(
                phase="go_upload",
                current=event.bytes_done,
                total=event.bytes_total,
                chunk_index=event.part,
                chunk_count=event.part_count,
                message=event.message or f"tgup part {event.part}/{event.part_count}",
            )
        except Exception as exc:  # noqa: BLE001 - a broken display must not abort a transfer
            print(f"[go_planner] progress callback error: {exc}", file=sys.stderr)

    result = tgup_bridge.upload(
        file=os.path.abspath(file_path),
        channel=(channel or "").strip(),
        api_id=numeric_id,
        api_hash=str(api_hash),
        phone=str(phone or ""),
        concurrency=concurrency,
        caption=caption,
        thumbnail=thumbnail_path,
        on_progress=on_progress,
    )
    if not result.ok:
        raise RuntimeError(f"tgup upload failed: {result.error or 'unknown error'}")
    return {
        "ok": True,
        "message_id": result.message_id,
        "message_link": result.message_link,
        "total_size": result.total_size,
        "chunk_count": result.chunk_count,
        "chunked": result.chunk_count > 1,
        "parts": result.parts,
        "concurrency": result.concurrency,
        "bytes_per_sec": result.bytes_per_sec,
        "elapsed_sec": result.elapsed_sec,
        "source_sha256": result.source_sha256,
    }


def convert_go_result_to_journal(
    result: dict,
    file_path: str,
    channel: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    caption: str = None,
) -> dict:
    """Convert a tgup result into the journal shape db/app understand.

    Output keys match ``telegram_uploader.upload_file_detailed()`` journals:
    file_path/filename/size/channel/parts/message_id/message_link/manifest/
    state/chunked/chunk_count/uploader/go_concurrency/go_rate_bps.
    """
    file_path = os.path.abspath(file_path)
    size = int(result.get("total_size") or os.path.getsize(file_path))
    parts = []
    for entry in result.get("parts") or []:
        parts.append(
            {
                "part": int(entry.get("part", 0)),
                "name": entry.get("name") or os.path.basename(file_path),
                "offset": int(entry.get("offset", 0)),
                "size": int(entry.get("size", 0)),
                "sha256": entry.get("sha256") or "",
                "message_id": int(entry.get("message_id", 0)),
                "link": entry.get("link") or "",
            }
        )
    parts.sort(key=lambda e: e["part"])
    chunked = bool(result.get("chunked")) or len(parts) > 1
    manifest = {
        "tg_dubber_manifest": "tg_dubber_manifest",
        "version": 2,
        "filename": os.path.basename(file_path),
        "size": size,
        "chunk_size": chunk_size,
        "chunked": chunked,
        "chunk_count": len(parts),
        "channel": (channel or "").strip(),
        "source_path": file_path,
        "caption": caption,
        "source_sha256": result.get("source_sha256") or "",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "app": "T_Dubber/tgup",
        "parts": parts,
    }
    return {
        "file_path": file_path,
        "filename": os.path.basename(file_path),
        "size": size,
        "channel": (channel or "").strip(),
        "parts": parts,
        "manifest": manifest,
        "message_id": result.get("message_id"),
        "message_link": result.get("message_link"),
        "state": "complete",
        "chunked": chunked,
        "chunk_count": len(parts),
        "updated_at": time.time(),
        "uploader": "go",
        "go_concurrency": result.get("concurrency"),
        "go_rate_bps": result.get("bytes_per_sec"),
        "go_elapsed_sec": result.get("elapsed_sec"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Plan upload parts, or report on the tgup helper."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build_cmd = sub.add_parser("build", help="build a plan for a file")
    build_cmd.add_argument("--file", required=True)
    build_cmd.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    build_cmd.add_argument("--plan-out", default="")
    build_cmd.add_argument(
        "--no-go", action="store_true", help="force the Python planner"
    )

    sub.add_parser("check", help="report whether tgup is usable")

    up_cmd = sub.add_parser(
        "upload", help="upload via tgup (use a scratch channel first)"
    )
    up_cmd.add_argument("--file", required=True)
    up_cmd.add_argument("--channel", required=True)
    up_cmd.add_argument("--api-id", required=True)
    up_cmd.add_argument("--api-hash", required=True)
    up_cmd.add_argument("--phone", default="")
    up_cmd.add_argument("--concurrency", type=int, default=GO_UPLOAD_DEFAULT_CONCURRENCY)
    up_cmd.add_argument("--result-out", default="")

    bench_cmd = sub.add_parser(
        "bench", help="measure single-stream vs concurrent throughput"
    )
    bench_cmd.add_argument("--channel", required=True)
    bench_cmd.add_argument("--api-id", required=True)
    bench_cmd.add_argument("--api-hash", required=True)
    bench_cmd.add_argument("--phone", default="")
    bench_cmd.add_argument("--size-mb", type=int, default=48)
    bench_cmd.add_argument("--concurrency", default="1,2,3,4")

    args = parser.parse_args(argv)

    if args.command == "check":
        print(json.dumps(status(), indent=2))
        return 0 if binary_runnable() else 1

    if args.command == "build":
        plan = build_plan(
            args.file, chunk_size=args.chunk_size, use_go=not args.no_go
        )
        text = json.dumps(plan, indent=2)
        if args.plan_out:
            Path(args.plan_out).write_text(text, encoding="utf-8")
            print(f"planner    {plan['planner']}")
            print(f"plan       {args.plan_out}")
        else:
            print(text)
        return 0

    if args.command == "upload":
        size = os.path.getsize(args.file)
        if size <= 0:
            print("refusing to upload an empty file", file=sys.stderr)
            return 2
        result = upload_via_go(
            args.file,
            args.api_id,
            args.api_hash,
            args.channel,
            phone=args.phone,
            concurrency=args.concurrency,
        )
        text = json.dumps(result, indent=2)
        if args.result_out:
            Path(args.result_out).write_text(text, encoding="utf-8")
        print(text)
        return 0

    if args.command == "bench":
        report = tgup_bridge.bench(
            channel=args.channel,
            api_id=args.api_id,
            api_hash=args.api_hash,
            phone=args.phone,
            size_mb=args.size_mb,
            levels=args.concurrency,
            on_human=lambda line: print(line, file=sys.stderr),
        )
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["ok"] else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())