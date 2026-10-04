"""One-time import of historical state into t_dubber.db.

Brings over project manifests from projects/*/project.json and Telegram
journals from .tg_uploads/*.json, then links each run to the archive that
protects it. Safe to run repeatedly: existing rows are left alone.
"""
import glob
import json
import os
import sys

ROOT = r"C:\Users\pocot\Music\T_Dubber"
sys.path.insert(0, ROOT)

import db  # noqa: E402
import telegram_uploader as tg  # noqa: E402


def main():
    imported_projects = db.import_existing_projects(os.path.join(ROOT, "projects"))
    print(f"projects imported : {imported_projects}")

    archives = 0
    parts = 0
    for path in glob.glob(os.path.join(ROOT, ".tg_uploads", "*.json")):
        name = os.path.basename(path)
        if name.startswith("_"):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                journal = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(journal, dict) or not journal.get("parts"):
            continue
        if not journal.get("fingerprint"):
            file_path = journal.get("file_path")
            if file_path and os.path.isfile(file_path):
                journal["fingerprint"] = tg._fingerprint(file_path, journal["size"])
            else:
                channel = journal.get("channel") or db.UNKNOWN_CHANNEL
                journal["fingerprint"] = (
                    f"{journal.get('filename')}@{journal.get('size')}@{channel}"
                )
        db.upsert_archive(journal)
        archives += 1
        parts += len(journal["parts"])
    print(f"archives imported : {archives}  ({parts} parts)")

    conn = db.connect()
    linked = 0
    for row in db.list_projects():
        link = row.get("backup_link")
        if not link:
            continue
        found = conn.execute(
            "SELECT id FROM telegram_archives WHERE manifest_link=?", (link,)
        ).fetchone()
        if found:
            db.attach_archive_to_project(row["id"], found["id"])
            linked += 1
    print(f"projects linked   : {linked}")

    print("\nstats:")
    print(json.dumps(db.stats(), indent=2))

    print("\nper-channel usage:")
    for row in db.channel_usage():
        label = row["channel"]
        suffix = "  (died before the channel was recorded)" if label == db.UNKNOWN_CHANNEL else ""
        print(
            f"   {label:<18} {row['uploads']:>2} uploads  "
            f"{tg.human_bytes(row['total_bytes']):>10}  "
            f"split={row['split']} complete={row['complete']}{suffix}"
        )

    print("\nrecent projects:")
    for row in db.list_projects(limit=8):
        archive = ""
        if row.get("backup_archive_id"):
            entry = conn.execute(
                "SELECT chunk_count FROM telegram_archives WHERE id=?",
                (row["backup_archive_id"],),
            ).fetchone()
            archive = f"  <- archive ({entry['chunk_count']} parts)" if entry else ""
        print(
            f"   {row['status']:<12} {row['title'][:34]:<34} "
            f"{row.get('target_language') or '-':<7}{archive}"
        )


if __name__ == "__main__":
    main()
