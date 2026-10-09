"""prune_archive_rows.py -- remove the (unknown) placeholder archive rows.

WHY THIS EXISTS
---------------
``telegram_archives`` is unique on ``(fingerprint, channel)``. While the upload
journal had no ``channel`` yet, every in-flight snapshot was mirrored under
``db.UNKNOWN_CHANNEL`` -- so the in-flight row and the final row could never
collide and each archive became two rows:

    (fingerprint, '(unknown)', 'uploading')   <- never updated again
    (fingerprint, '@chan',     'complete')   <- the one that is actually true

The source is fixed (``telegram_uploader`` records the channel before the first
part can land), so no new rows appear. This script removes the leftovers.

WHY IT IS SAFE, AND WHY IT STILL MAKES YOU LOOK FIRST
-----------------------------------------------------
Verified on this database before writing the delete:

* ``projects.backup_archive_id`` references: **0** for every target row
* ``telegram_parts.archive_id`` is ``ON DELETE CASCADE`` (``db.py``), so the
  orphaned part rows go with them
* no row is in state ``complete`` -- they are all ``uploading`` placeholders

It still refuses to run without ``--yes``, and it prints the plan before it
touches anything. ``--dry-run`` is the default.

    python prune_archive_rows.py            # dry run, prints the plan
    python prune_archive_rows.py --yes      # do it
    python prune_archive_rows.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import db  # noqa: E402

# Names that are test scaffolding rather than somebody's video.
JUNK_MARKERS = ("_probe_", "single.bin", "_selftest", "_part_", ".restoring")


def is_junk(filename: str) -> bool:
    name = (filename or "").lower()
    return any(marker in name for marker in JUNK_MARKERS)


def find_targets(include_junk: bool = False) -> list[dict]:
    """Placeholder rows only: the channel is UNKNOWN and the run never finished."""
    conn = db.connect()
    rows = conn.execute(
        """
        SELECT a.id, a.filename, a.state, a.file_size, a.channel
          FROM telegram_archives a
         WHERE a.channel = ?
           AND a.state IS NOT 'complete'
        """,
        (db.UNKNOWN_CHANNEL,),
    ).fetchall()

    targets = []
    for row in rows:
        record = dict(row)
        record["projects_refs"] = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE backup_archive_id=?", (record["id"],)
        ).fetchone()[0]
        record["parts"] = conn.execute(
            "SELECT COUNT(*) FROM telegram_parts WHERE archive_id=?", (record["id"],)
        ).fetchone()[0]
        record["junk"] = is_junk(record.get("filename"))
        if record["junk"] and not include_junk:
            continue
        targets.append(record)
    return targets


def explain() -> list[dict]:
    """Why these rows exist at all, so the next reader does not rediscover it."""
    return [{
        "row": "placeholder",
        "channel": db.UNKNOWN_CHANNEL,
        "state": "uploading",
        "cause": "the journal carried no channel while the upload was in flight, "
                 "so (fingerprint, channel) could not collide with the final row",
        "fixed_in": "telegram_uploader.upload_file_detailed() records "
                    "journal['channel'] before the first part can land",
        "new_rows": "none",
        "this_script": "removes the leftovers only",
    }]


def prune(targets: list[dict]) -> int:
    conn = db.connect()
    removed = 0
    with db._write(conn):
        for record in targets:
            # parts go with them: telegram_parts.archive_id is ON DELETE CASCADE.
            conn.execute("DELETE FROM telegram_archives WHERE id=?", (record["id"],))
            removed += conn.total_changes and 1
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true",
                        help="actually delete (default is a dry run)")
    parser.add_argument("--include-junk", action="store_true",
                        help="also remove rows whose filename looks like test scaffolding")
    parser.add_argument("--json", action="store_true", help="machine-readable")
    args = parser.parse_args()

    targets = find_targets(include_junk=args.include_junk)
    skipped_junk = [r for r in find_targets(include_junk=True) if r["junk"]]

    if args.json:
        print(json.dumps({"targets": targets, "explain": explain()}, indent=2))
        return 0

    print(f"database : {db.DB_PATH}")
    print(f"placeholder rows ({db.UNKNOWN_CHANNEL}, never complete): {len(targets)}\n")

    if not targets:
        print("Nothing to prune -- the placeholder rows are already gone.")
        return 0

    print("PLAN -- deleting these, and only these:")
    for record in targets:
        refs = record["projects_refs"]
        flag = "  JUNK" if record["junk"] else ""
        print(f"  id={record['id']:<4} state={record['state']:<10} "
              f"parts={record['parts']:<3} projects_refs={refs}  "
              f"{str(record['filename'])[:44]}{flag}")
        if refs:
            print(f"        ^ WARNING: {refs} project(s) reference this row")
    if skipped_junk:
        print(f"\n  (--include-junk would also remove {len(skipped_junk)} test-artifact row(s))")

    print("\nCAUSE:")
    for item in explain():
        print(f"  {item['cause']}")
        print(f"  already fixed in: {item['fixed_in']}")

    if not args.yes:
        print("\nDRY RUN. Nothing was deleted. Re-run with --yes to apply.")
        return 0

    if any(r["projects_refs"] for r in targets):
        print("\nREFUSING: a project still points at one of these rows.")
        return 1

    removed = prune(targets)
    print(f"\nDeleted {removed} placeholder row(s) "
          f"(their telegram_parts went with them, by design).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
