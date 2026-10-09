"""prune_test_project_rows.py -- remove project rows this test suite created.

WHY
---
``test_flow_order.py`` (and friends) redirected ``app.PROJECTS_DIR`` but never
``db.DB_PATH``, so every run appended real rows to the production
``t_dubber.db``. Measured on 2026-10-08:

    projects rows           : 232
    test-fixture rows       : 200
    distinct fixtures       :   4   (clip, clip2, Some_YouTube_Video, restored)
    re-runs of each         :  47

The source is now fixed (``test_isolation.use_scratch_db()``), so this removes
the leftovers. It deletes **only** rows whose id matches a known fixture shape,
never anything that looks like a real run.

    python prune_test_project_rows.py          # dry run
    python prune_test_project_rows.py --yes    # delete
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import db  # noqa: E402

# Anchored, so a real title can never match by accident.
FIXTURES = [
    # test_flow_order.py
    re.compile(r"^clip2?-[0-9a-f]{12}-[0-9a-f]{10}$"),
    re.compile(r"^restored-[0-9a-f]{12}-[0-9a-f]{10}$"),
    re.compile(r"^Some_YouTube_Video-[0-9a-f]{12}-[0-9a-f]{10}$"),
    re.compile(r"^480p_she_will_do_anything_to_straight_her_hair-"
               r"[0-9a-f]{12}-[0-9a-f]{10}$"),
    # test_db.py -- short, obviously synthetic ids
    re.compile(r"^p\d+$"),
    re.compile(r"^seed$"),
    re.compile(r"^w\d+-\d+$"),
    re.compile(r"^demo-\d+-aaa\d+$"),
    re.compile(r"^lucifer-abc123-def456$"),
    re.compile(r"^lucifer-aaa-bbb$"),
    re.compile(r"^weeknd-ccc-ddd$"),
]


def is_test_row(project_id: str) -> bool:
    return any(pattern.match(project_id or "") for pattern in FIXTURES)


def find(include_kernels: bool = False) -> list[dict]:
    rows = db.connect().execute(
        "SELECT id, title, status, backup_archive_id, kernel_id FROM projects"
    ).fetchall()
    found = []
    for row in rows:
        record = dict(row)
        if not is_test_row(record["id"]):
            continue
        if not include_kernels and record.get("kernel_id"):
            continue
        found.append(record)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="actually delete")
    parser.add_argument("--include-kernels", action="store_true",
                        help="also match rows that still carry a kernel_id")
    args = parser.parse_args()

    targets = find(include_kernels=args.include_kernels)
    total = db.connect().execute("SELECT COUNT(*) FROM projects").fetchone()[0]

    print(f"database        : {db.DB_PATH}")
    print(f"projects rows   : {total}")
    print(f"test-fixture rows: {len(targets)}\n")

    if not targets:
        print("Nothing to prune -- the fixtures are already gone.")
        return 0

    print("PLAN -- delete these, and only these:")
    for record in targets[:12]:
        print(f"  {record['id'][:64]:<66} {record['status']}")
    if len(targets) > 12:
        print(f"  ... and {len(targets) - 12} more")

    backup_refs = [r for r in targets if r.get("backup_archive_id")]
    if backup_refs:
        print(f"\n  {len(backup_refs)} of these point at a backup_archive_id "
              f"(that archive row is left untouched).")

    print("\nCAUSE: the suite wrote to production because db.DB_PATH was never")
    print("       redirected. Now fixed via test_isolation.use_scratch_db().")

    if not args.yes:
        print("\nDRY RUN. Nothing deleted. Re-run with --yes.")
        return 0

    conn = db.connect()
    ids = [r["id"] for r in targets]
    with db._write(conn):
        conn.executemany("DELETE FROM projects WHERE id=?",
                         [(i,) for i in ids])

    remaining = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    print(f"\nDeleted {len(ids)} test-fixture row(s). "
          f"projects rows: {total} -> {remaining}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
