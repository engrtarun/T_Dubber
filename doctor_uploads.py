"""doctor_uploads.py -- what is actually stuck, and is it recoverable?

An interrupted upload leaves a journal in state "uploading". That is correct
while the run can continue, but it is indistinguishable from a run that can
never continue. This tells the two apart, because the answer decides what
somebody should do about it:

* the source file is still there  -> resumable, just run the upload again
* the source file is gone        -> unrecoverable. The stored parts on Telegram
                                    have no manifest, so nothing can join them
                                    back into the original.

Run:  python doctor_uploads.py
      python doctor_uploads.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import telegram_uploader as tu  # noqa: E402


def human(count) -> str:
    try:
        return tu.human_bytes(int(count))
    except Exception:  # noqa: BLE001
        return str(count)


def _age(ts) -> str:
    try:
        delta = time.time() - float(ts)
    except (TypeError, ValueError):
        return "unknown age"
    if delta < 3600:
        return f"{int(delta / 60)} min"
    if delta < 86400:
        return f"{int(delta / 3600)} h"
    return f"{int(delta / 86400)} d"


def expected_parts(size: int, chunk_size: int) -> int:
    try:
        return len(tu.plan_chunks(int(size), chunk_size))
    except Exception:  # noqa: BLE001
        return 0


def scan(state_dir: str = None):
    """Return one record per journal, classified."""
    state_dir = state_dir or tu.STATE_DIR
    records = []
    if not os.path.isdir(state_dir):
        return records
    for name in sorted(os.listdir(state_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(state_dir, name)
        journal = tu._read_json(path, {}) or {}
        if not journal:
            continue
        size = int(journal.get("size") or 0)
        parts = journal.get("parts") or []
        want = expected_parts(size, tu.CHUNK_SIZE) if size else 0
        file_path = journal.get("file_path") or ""
        exists = bool(file_path) and os.path.isfile(file_path)
        state = (journal.get("state") or "unknown").strip()

        resumable = state == "uploading" and exists and want and len(parts) < want
        unrecoverable = state == "uploading" and not exists

        records.append({
            "key": name[:-5],
            "filename": journal.get("filename"),
            "state": state,
            "channel": journal.get("channel") or None,
            "size": size,
            "size_human": human(size),
            "parts_done": len(parts),
            "parts_expected": want,
            "file_exists": exists,
            "has_manifest": bool(journal.get("manifest_link") or journal.get("message_id")),
            "error": journal.get("error") or None,
            "age": _age(journal.get("updated_at")),
            "verdict": (
                "COMPLETE" if state == "complete"
                else "RESUMABLE (run the upload again)" if resumable
                else "UNRECOVERABLE (source file gone)" if unrecoverable
                else "INCOMPLETE"
            ),
        })
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable")
    args = parser.parse_args()

    records = scan()

    if args.json:
        print(json.dumps(records, indent=2))
        return 0

    buckets = {"COMPLETE": [], "RESUMABLE (run the upload again)": [],
               "UNRECOVERABLE (source file gone)": [], "INCOMPLETE": []}
    for record in records:
        buckets.setdefault(record["verdict"], []).append(record)

    print(f"state dir: {tu.STATE_DIR}")
    print(f"journals  : {len(records)}\n")

    for verdict in ("RESUMABLE (run the upload again)",
                    "UNRECOVERABLE (source file gone)",
                    "INCOMPLETE", "COMPLETE"):
        items = buckets.get(verdict) or []
        if not items:
            continue
        print(f"--- {verdict}  ({len(items)}) ---")
        for r in items:
            channel = r["channel"] or "(no channel recorded)"
            print(f"  {r['filename']}")
            print(f"      {r['size_human']}  parts {r['parts_done']}/{r['parts_expected']}"
                  f"  channel={channel}  age={r['age']}")
            print(f"      manifest={'yes' if r['has_manifest'] else 'NO'}"
                  f"  file_on_disk={'yes' if r['file_exists'] else 'NO'}")
            if r["error"]:
                print(f"      error: {str(r['error'])[:150]}")
        print()

    stuck = buckets.get("UNRECOVERABLE (source file gone)") or []
    if stuck:
        stranded = sum(r["parts_done"] for r in stuck)
        print(f"NOTE: {len(stuck)} unrecoverable upload(s) have {stranded} part(s) "
              f"stored on Telegram")
        print("      with no manifest. Those bytes are orphaned: nothing in the")
        print("      codebase can rejoin them, because the source no longer exists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
