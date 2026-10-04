"""
End-to-end proof: upload LuciferS01E13.mkv to Telegram, then verify the archive.

Prints a live log, ends with the manifest link, and independently re-reads the
manifest from Telegram to confirm every part really landed with the size we
sent. Optionally performs a full restore so the reassembled file is compared
byte for byte against the source.

Usage:
  python e2e_upload.py            upload + verify manifest
  python e2e_upload.py --restore  also download everything and compare SHA-256
"""

import hashlib
import json
import os
import sys
import time

ROOT = r"C:\Users\pocot\Music\T_Dubber"
sys.path.insert(0, ROOT)

import app  # noqa: E402
import telegram_uploader as tg  # noqa: E402

SOURCE = os.path.join(ROOT, "LuciferS01E13.mkv")
DO_RESTORE = "--restore" in sys.argv


def stamp():
    return time.strftime("%H:%M:%S")


def log(message):
    print(f"[{stamp()}] {message}", flush=True)


def main():
    if not os.path.isfile(SOURCE):
        raise SystemExit(f"source missing: {SOURCE}")

    # The session file is SQLite, so two live processes on it corrupt each
    # other's auth state. Refuse early with a clear message instead of dying
    # partway through a multi-gigabyte upload.
    if tg.hold_session_lock(wait_seconds=2.0) is not True:  # pragma: no cover
        raise SystemExit("another process holds the Telegram session lock")

    size = os.path.getsize(SOURCE)
    plan = tg.plan_chunks(size, tg.CHUNK_SIZE)
    log(f"file      : {SOURCE}")
    log(f"size      : {tg.human_bytes(size)}  ({size:,} bytes)")
    log(f"part plan : {len(plan)} parts of <= {tg.human_bytes(tg.CHUNK_SIZE)}")
    log(f"          : sizes = {[tg.human_bytes(length) for _n, _o, length in plan]}")

    source_digest = hashlib.sha256()
    with open(SOURCE, "rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            source_digest.update(block)
    source_sha = source_digest.hexdigest()
    log(f"source sha: {source_sha}")

    creds = app._tg_credentials()
    if not creds:
        raise SystemExit("Telegram is not configured. Run telegram_uploader.py first.")
    log(f"channel   : {creds['channel']}")

    # Report what a previous attempt already managed to store, so a resume is
    # obvious rather than looking like a fresh upload.
    key = tg._fingerprint(SOURCE, size)
    previous = tg._read_json(tg.state_path_for(key), {}) or {}
    done = previous.get("parts") or []
    if done:
        log(f"resume    : {len(done)}/{len(plan)} parts already stored "
            f"({tg.human_bytes(sum(int(p['size']) for p in done))})")
        for entry in done:
            log(f"            part {entry['part']} -> {entry['link']}")
        log("            only the missing parts will be sent")
    else:
        log("resume    : nothing stored yet, starting from part 1")
    log("-" * 70)

    seen_parts = {}
    started = time.time()

    def progress(info):
        phase = info.get("phase")
        message = info.get("message")
        if phase == "resume" and message:
            log(f"resume    : {message}")
        elif phase == "part" and message:
            seen_parts[int(info.get("chunk_index") or 0)] = message
            log(f"part {info.get('chunk_index')}/{info.get('chunk_count')} "
                f"{tg.human_bytes(info.get('current', 0))} of {tg.human_bytes(size)}")
        elif phase == "throttled":
            log(f"throttled : {message}")
        elif phase == "complete" and message:
            log(f"complete  : {message}")

    log("starting upload ...")
    try:
        result = tg.upload_file_detailed(
            SOURCE,
            creds["api_id"], creds["api_hash"], creds["phone"],
            creds["channel"],
            caption="T_Dubber end-to-end proof - Lucifer S01E13",
            progress_callback=progress,
            resume=True,
            reuse_completed=True,
        )
    except tg.TelegramSessionBusy as exc:
        log("SESSION BUSY: " + str(exc))
        raise SystemExit(2)
    elapsed = time.time() - started

    link = result.get("message_link")
    log("-" * 70)
    log(f"state     : {result.get('state')}")
    log(f"chunked   : {result.get('chunked')}")
    log(f"parts     : {result.get('chunk_count')}")
    log(f"took      : {elapsed / 60:.1f} min "
        f"({tg.human_bytes(size / max(elapsed, 1))}/s effective)")
    log(f"LINK      : {link}")

    print("\nPARTS")
    print("-" * 70)
    for entry in result.get("parts", []):
        print(f"  part {entry['part']}  {tg.human_bytes(entry['size']):>10}  "
              f"{entry['sha256'][:32]}...  msg {entry['message_id']}")
    print()

    log("verifying: re-reading the manifest back from Telegram ...")
    manifest = tg.fetch_manifest(creds["api_id"], creds["api_hash"], creds["phone"], link)
    manifest_parts = manifest.get("parts") or []

    checks = []
    checks.append(("manifest marker", manifest.get(tg.MANIFEST_MARKER) == tg.MANIFEST_VERSION))
    checks.append(("declared size matches source", int(manifest.get("size", -1)) == size))
    checks.append(("declared filename matches", manifest.get("filename") == os.path.basename(SOURCE)))
    checks.append(("part count matches plan", len(manifest_parts) == len(plan)))
    checks.append(("part sizes are contiguous and total the file",
                   sum(int(p["size"]) for p in manifest_parts) == size))
    checks.append(("every part carries a sha256",
                   all(len(p.get("sha256", "")) == 64 for p in manifest_parts)))
    checks.append(("every part carries a message id",
                   all(p.get("message_id") for p in manifest_parts)))
    checks.append(("every part carries a link",
                   all(p.get("link", "").startswith("http") for p in manifest_parts)))

    offsets_ok = True
    cursor = 0
    for entry in manifest_parts:
        if int(entry["offset"]) != cursor:
            offsets_ok = False
            break
        cursor += int(entry["size"])
    checks.append(("offsets are gapless", offsets_ok))

    log("-" * 70)
    for name, ok in checks:
        log(f"  {'PASS' if ok else 'FAIL'}  {name}")

    failed = [name for name, ok in checks if not ok]

    if DO_RESTORE and not failed:
        log("")
        log("full restore: downloading every part back ...")
        restore_dir = os.path.join(ROOT, "_e2e_restored")
        restored = tg.restore_from_link(
            link, creds["api_id"], creds["api_hash"], creds["phone"], restore_dir,
            progress_callback=lambda i: (
                log(f"  restored part {i.get('chunk_index')}/{i.get('chunk_count')} "
                    f"{tg.human_bytes(i.get('current', 0))}")
                if i.get("chunk_index") else None
            ),
            verify=True,
        )
        rebuilt = hashlib.sha256()
        with open(restored, "rb") as handle:
            for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                rebuilt.update(block)
        rebuilt_sha = rebuilt.hexdigest()
        same = rebuilt_sha == source_sha
        log(f"  {'PASS' if same else 'FAIL'}  restored file is byte identical")
        log(f"        source   sha256 = {source_sha}")
        log(f"        restored sha256 = {rebuilt_sha}")
        if not same:
            failed.append("restored file differs from source")

    print("\n" + "=" * 70)
    if failed:
        print("RESULT: FAILED -> " + ", ".join(failed))
        return 1
    print("RESULT: ALL CHECKS PASSED")
    print(f"MANIFEST LINK: {link}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
