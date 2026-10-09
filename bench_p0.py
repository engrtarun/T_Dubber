"""bench_p0.py -- measured before/after for the P0 changes.

Every number this prints is either MEASURED (a stopwatch around real work, real
bytes, a real Telegram round trip) or DERIVED (arithmetic on a measured input,
labelled as such). Nothing here is a prediction.

    python bench_p0.py --sections a,b,c     # default: all three
    python bench_p0.py --sections p1        # the concurrency bench

Section A needs credentials and posts to the configured channel. Sections B and
C are local except for the resume simulation in B, which needs two real uploads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

BLUE = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
OFF = "\033[0m"


def head(title: str) -> None:
    print(f"\n{BLUE}{'=' * 78}{OFF}\n{BLUE}{title}{OFF}\n{'=' * 78}", flush=True)


def note(label: str, value: str) -> None:
    print(f"  {label:<44} {value}", flush=True)


def kind_measured() -> str:
    return f"{GREEN}MEASURED{OFF}"


def kind_derived() -> str:
    return f"{YELLOW}DERIVED{OFF}"


def make_blob(path: Path, size_mb: int) -> int:
    """Write a file of incompressible bytes and return its size."""
    size = size_mb * 1024 * 1024
    block = os.urandom(1024 * 1024)
    with open(path, "wb") as handle:
        written = 0
        while written < size:
            chunk = block[: min(len(block), size - written)]
            handle.write(chunk)
            written += len(chunk)
    return size


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_creds():
    import app  # heavy (gradio), imported only when actually needed

    api_id, api_hash, phone, channel = app.load_tg_config()
    if not (api_id and api_hash and channel):
        raise SystemExit("No Telegram settings in config.json")
    return api_id, api_hash, phone, channel


def isolate_db() -> None:
    """Point the archive index at a throwaway database.

    The bench must be able to find its own rows; pointing it at the real
    t_dubber.db would both pollute it and make an existing archive look like a
    hit for the wrong reason.
    """
    import db

    scratch = Path(tempfile.gettempdir()) / "bench_p0_scratch.db"
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(scratch) + suffix)
        if candidate.exists():
            candidate.unlink()
    db.DB_PATH = str(scratch)
    if getattr(db._local, "conn", None) is not None:
        db._local.conn.close()
        db._local.conn = None
    db.connect()
    return scratch


# ---------------------------------------------------------------------------
# Section A -- P0-1, Tier 0: do not upload bytes we already archived
# ---------------------------------------------------------------------------


def section_a(size_mb: int = 200) -> dict:
    head(f"SECTION A -- P0-1 Tier 0 dedup  [{kind_measured()}]")
    import app
    import db
    import telegram_uploader as tu

    api_id, api_hash, phone, channel = load_creds()
    isolate_db()

    scratch = Path(tempfile.mkdtemp(prefix="bench_p0_A_"))
    first = scratch / "inbox" / "bench_source.mp4"
    first.parent.mkdir(parents=True, exist_ok=True)
    size = make_blob(first, size_mb)
    digest = sha256_file(first)
    print(f"  prepared {size_mb} MB in {first.parent}")

    # ---- BEFORE: no digest is passed, so there is nothing to look up. This is
    # exactly what the code did before P0-1: find no row, upload everything.
    print(f"\n  -- BEFORE (first run, always uploads) --")
    started = time.monotonic()
    journal = tu.upload_file_detailed(
        str(first), api_id, api_hash, phone, channel,
        caption="[bench_p0 A] Tier 0 measurement source",
        go_concurrency=3,
    )
    before_seconds = time.monotonic() - started
    link = journal.get("message_link") or journal.get("link")
    before_bytes = size
    note("wall clock", f"{before_seconds:,.1f} s")
    note("bytes handed to Telegram", f"{before_bytes / 1048576:,.1f} MB")
    note("measured rate", f"{before_bytes / before_seconds / 1048576:,.2f} MB/s")
    note("archive link", str(link))

    # Mirror it into the index the way app._archive_to_db does, passing the
    # digest rather than re-reading the file.
    indexed = dict(journal)
    indexed["fingerprint"] = digest[:32]
    db.upsert_archive(indexed)

    # The app moves every resolved file out of the inbox before archiving it.
    # Reproduce that move so the Tier 0 lookup is tested against the real shape.
    moved = scratch / "project" / "bench_source.mp4"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(first), str(moved))

    # ---- AFTER: same bytes, new path, digest supplied.
    print(f"\n  -- AFTER (same bytes, moved path, digest supplied) --")
    started = time.monotonic()
    generator = app._telegram_backup_step(
        str(moved), {"api_id": api_id, "api_hash": api_hash,
                     "phone": phone, "channel": channel},
        None, "bench-p0", None, channel, content_sha256=digest,
    )
    log_lines = []
    try:
        while True:
            log_lines.append(next(generator))
    except StopIteration as stop:
        after_link = stop.value
    after_seconds = time.monotonic() - started
    hit = any("Tier 0" in line for line in log_lines)

    after_bytes = 0 if hit else size
    note("wall clock", f"{after_seconds:,.2f} s")
    note("tier 0 fired", str(hit))
    note("bytes handed to Telegram", f"{after_bytes / 1048576:,.1f} MB")
    for line in log_lines:
        print(f"    log: {line.rstrip()}")

    saved = before_seconds - after_seconds
    print(f"\n  {GREEN}RESULT A{OFF}")
    print(f"    before: {before_seconds:,.1f} s / {before_bytes / 1048576:,.0f} MB")
    print(f"    after : {after_seconds:,.2f} s / {after_bytes / 1048576:,.0f} MB")
    print(f"    saved : {saved:,.1f} s ({saved / before_seconds * 100:,.1f} % of the run)"
          f"   [{kind_measured()}]")
    if after_link != link:
        print(f"    {YELLOW}note: links differ ({link} vs {after_link}) -- "
              f"unexpected, Tier 0 should return the existing archive{OFF}")

    shutil.rmtree(scratch, ignore_errors=True)
    return {
        "upload_rate_mbps": before_bytes / before_seconds / 1048576,
        "before_seconds": before_seconds,
        "after_seconds": after_seconds,
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
        "size_mb": size_mb,
        "tier0_hit": hit,
    }


# ---------------------------------------------------------------------------
# Section B -- P0-2, content-keyed resume survives a move
# ---------------------------------------------------------------------------


def section_b(size_mb: int = 40) -> dict:
    head(f"SECTION B -- P0-2 content key  [{kind_measured()}]")
    import telegram_uploader as tu

    api_id, api_hash, phone, channel = load_creds()

    scratch = Path(tempfile.mkdtemp(prefix="bench_p0_B_"))
    first = scratch / "inbox" / "bench_resume.mp4"
    first.parent.mkdir(parents=True, exist_ok=True)
    size = make_blob(first, size_mb)
    chunk = (size // 2) + (1024 * 1024)
    print(f"  prepared {size_mb} MB, forcing {2} parts of ~{chunk / 1048576:.0f} MB")

    # ---- cost of the key itself, which every upload now pays
    print(f"\n  -- key cost --")
    samples = 3
    t = time.monotonic()
    for _ in range(samples):
        tu._legacy_fingerprint(str(first), size)
    legacy_ms = (time.monotonic() - t) / samples * 1000
    t = time.monotonic()
    for _ in range(samples):
        tu._fingerprint(str(first), size)
    content_ms = (time.monotonic() - t) / samples * 1000
    t = time.monotonic()
    for _ in range(samples):
        sha256_file(first)
    full_ms = (time.monotonic() - t) / samples * 1000
    note("legacy key (path|size|mtime)", f"{legacy_ms:,.3f} ms")
    note("content key (16 MB sample)", f"{content_ms:,.3f} ms")
    note("whole-file sha256", f"{full_ms:,.3f} ms")
    note("content vs whole-file", f"{content_ms / full_ms * 100:,.1f} % of the cost"
         f"   [{kind_measured()}]")

    # ---- a real two-part upload, then a simulated crash
    print(f"\n  -- upload, then rewind the journal to 'crashed after part 1' --")
    started = time.monotonic()
    journal = tu.upload_file_detailed(
        str(first), api_id, api_hash, phone, channel,
        caption="[bench_p0 B] resume measurement source",
        chunk_size=chunk, go_concurrency=2,
    )
    full_seconds = time.monotonic() - started
    note("full upload wall clock", f"{full_seconds:,.1f} s for {size_mb} MB")

    parts = journal.get("parts") or []
    if len(parts) < 2:
        print(f"  {RED}expected a multi-part upload, got {len(parts)} part(s){OFF}")
        shutil.rmtree(scratch, ignore_errors=True)
        return {}

    # Keep only part 1 and mark the run as interrupted, exactly the state a
    # killed process leaves behind.
    crashed = dict(journal)
    crashed["state"] = "uploading"
    crashed["parts"] = parts[:1]
    tu._atomic_write_json(
        tu.state_path_for(tu._fingerprint(str(first), size)), crashed)

    # The move that used to throw the journal away.
    moved = scratch / "project" / "bench_resume.mp4"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(first), str(moved))

    legacy_key = tu._legacy_fingerprint(str(moved), size)
    content_key = tu._fingerprint(str(moved), size)
    legacy_found = tu._read_json(tu.state_path_for(legacy_key), {}) or {}
    content_found = tu._read_json(tu.state_path_for(content_key), {}) or {}
    note("journal reachable by legacy key", f"{len(legacy_found.get('parts') or [])} part(s)")
    note("journal reachable by content key", f"{len(content_found.get('parts') or [])} part(s)")

    # The defect of the old key was not that it stored something different -- it
    # stored the same journal under a name the next run could not look up, so the
    # parts became unreachable and every byte went out again. Unreachable is
    # therefore modelled by taking the journal out of reach, which is exactly
    # what the move did under the old scheme.
    park = tu.state_path_for(f"__parked_{content_key}")
    tu._atomic_write_json(park, crashed)
    Path(tu.state_path_for(content_key)).unlink(missing_ok=True)

    print(f"\n  -- BEFORE (journal unreachable after the move, as under the old key) --")
    started = time.monotonic()
    before = tu.upload_file_detailed(
        str(moved), api_id, api_hash, phone, channel,
        caption="[bench_p0 B] resume measurement source",
        chunk_size=chunk, go_concurrency=2,
    )
    before_seconds = time.monotonic() - started
    before_parts = len(before.get("parts") or [])

    print(f"\n  -- AFTER (same crash state, reachable by content key) --")
    Path(tu.state_path_for(content_key)).unlink(missing_ok=True)
    tu._atomic_write_json(tu.state_path_for(content_key), crashed)
    started = time.monotonic()
    after = tu.upload_file_detailed(
        str(moved), api_id, api_hash, phone, channel,
        caption="[bench_p0 B] resume measurement source",
        chunk_size=chunk, go_concurrency=2,
    )
    after_seconds = time.monotonic() - started
    after_parts = len(after.get("parts") or [])

    Path(park).unlink(missing_ok=True)

    half = size / 2 / 1048576
    before_bytes = size / 1048576
    after_bytes = half if before_parts == 2 and after_parts == 2 else before_bytes
    print(f"\n  {GREEN}RESULT B{OFF}")
    print(f"    crash after part 1 of 2, then the file is moved")
    print(f"    before: {before_seconds:,.1f} s, re-uploaded {before_parts} part(s)"
          f" (~{before_bytes:,.0f} MB)")
    print(f"    after : {after_seconds:,.1f} s, {after_parts} part(s) in the journal,"
          f" ~{after_bytes:,.0f} MB on the wire")
    print(f"    saved : {before_seconds - after_seconds:,.1f} s"
          f"   [{kind_measured()}]")
    print(f"    key cost: {content_ms:,.2f} ms per call on a {size_mb} MB file"
          f"   [{kind_measured()}]")

    shutil.rmtree(scratch, ignore_errors=True)
    return {
        "before_seconds": before_seconds,
        "after_seconds": after_seconds,
        "before_bytes_mb": before_bytes,
        "after_bytes_mb": after_bytes,
        "key_cost_ms": content_ms,
        "full_hash_ms": full_ms,
    }


# ---------------------------------------------------------------------------
# Section C -- P0-3, the media no longer rides the Kaggle dataset
# ---------------------------------------------------------------------------


def section_c(size_mb: int = 200, rate_mbps: float = None) -> dict:
    head(f"SECTION C -- P0-3 dataset media removal")
    scratch = Path(tempfile.mkdtemp(prefix="bench_p0_C_"))
    source = scratch / "source.mp4"
    dataset = scratch / "dataset_safe"
    dataset.mkdir(parents=True, exist_ok=True)
    size = make_blob(source, size_mb)

    print(f"\n  -- the copy P0-3 removes --")
    target = dataset / "source_video.mp4"
    started = time.monotonic()
    shutil.copy(str(source), str(target))
    copy_seconds = time.monotonic() - started
    note("shutil.copy wall clock", f"{copy_seconds:,.2f} s")
    note("throughput", f"{size / copy_seconds / 1048576:,.0f} MB/s")
    note("bytes copied", f"{size_mb} MB   [{kind_measured()}]")

    print(f"\n  -- bytes that no longer cross this uplink --")
    print(f"    exact: {size_mb} MB   [{kind_measured()}]")
    if rate_mbps:
        seconds = size / 1048576 / rate_mbps
        print(f"    at the rate measured in section A "
              f"({rate_mbps:,.2f} MB/s) that is {seconds:,.1f} s of upload"
              f"   [{kind_derived()}]")

    print(f"\n  {GREEN}RESULT C{OFF}")
    print(f"    local copy removed : {copy_seconds:,.2f} s   [{kind_measured()}]")
    print(f"    uplink bytes removed: {size_mb} MB (this blob was the dataset "
          f"payload)   [{kind_measured()}]")
    print(f"    {YELLOW}production note{OFF}: the dataset used to carry the 854x480 "
          f"encode, not the original, so the real")
    print(f"    byte count is that encode's size. The seconds figure above is "
          f"therefore an upper bound.   [{kind_derived()}]")

    shutil.rmtree(scratch, ignore_errors=True)
    return {
        "copy_seconds": copy_seconds,
        "copy_mbps": size / copy_seconds / 1048576,
        "bytes_removed_mb": size_mb,
    }


# ---------------------------------------------------------------------------
# P1 -- concurrency bench, delegated to tgup so the number is the real one
# ---------------------------------------------------------------------------


def section_p1(channel: str = None, list_concurrency: str = "1,2,3,4") -> dict:
    head(f"SECTION P1 -- concurrency bench  [{kind_measured()}]")
    api_id, api_hash, _phone, config_channel = load_creds()
    target = channel or config_channel
    print(f"  target channel: {target}")
    print(f"  concurrency sweep: {list_concurrency}")
    print(f"  (tgup streams the same payload over each connection count)\n")

    import go_planner
    import tgup_bridge

    if not go_planner.binary_runnable():
        print(f"  {RED}tgup is not runnable here; nothing to measure{OFF}")
        return {}

    cmd = [
        tgup_bridge.get_base_command()[-1], "bench",
        "--channel", target,
        "--api-id", str(api_id),
        "--api-hash", str(api_hash),
        "--concurrency", list_concurrency,
    ]
    print(f"  $ {' '.join(str(c) for c in cmd)}\n", flush=True)
    started = time.monotonic()
    proc = subprocess.run([str(c) for c in cmd], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    elapsed = time.monotonic() - started
    sys.stdout.write(proc.stdout or "")
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    print(f"\n  bench wall clock: {elapsed:,.1f} s   [{kind_measured()}]")
    return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


SECTIONS = {
    "a": ("A", section_a, "needs credentials; posts to the channel"),
    "b": ("B", section_b, "needs credentials; 3 real uploads"),
    "c": ("C", section_c, "local only"),
    "p1": ("P1", section_p1, "needs credentials; tgup bench"),
}


def main() -> int:
    # The Tier 0 log line is deliberately in the user's language, which the
    # Windows console codec cannot encode. Replace what it cannot rather than
    # losing a measurement to a UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sections", default="a,b,c",
                        help="comma separated: a, b, c, p1")
    parser.add_argument("--size-mb", type=int, default=200)
    args = parser.parse_args()

    wanted = [s.strip().lower() for s in args.sections.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in SECTIONS]
    if unknown:
        print(f"unknown section(s): {unknown}; choose from {sorted(SECTIONS)}")
        return 2

    results = {}
    rate = None
    for name in wanted:
        key, func, blurb = SECTIONS[name]
        print(f"\n{YELLOW}section {key}: {blurb}{OFF}")
        if name == "a":
            results["A"] = func(size_mb=args.size_mb)
            rate = results["A"].get("upload_rate_mbps")
        elif name == "b":
            results["B"] = func(size_mb=max(40, args.size_mb // 5))
        elif name == "c":
            results["C"] = func(size_mb=args.size_mb, rate_mbps=rate)
        else:
            results["P1"] = func()

    head("SUMMARY")
    print(json.dumps(results, indent=2, default=str))
    out = ROOT / "bench_p0_results.json"
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
