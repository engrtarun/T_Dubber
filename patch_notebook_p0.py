"""Patch kaggle_worker.ipynb cell 4 to honour the P0-3 fetch contract.

Run once. Idempotent: it refuses to touch a notebook that is already patched.
"""

import json
import sys
from pathlib import Path

NOTEBOOK = Path(__file__).resolve().parent / "kaggle_worker.ipynb"
MARKER = "fetch_from_telegram"

NEW_SOURCE = '''# Task 2: Locate the source video.
#
# P0-3: the host may deliberately ship NO video in this dataset. It leaves the
# original on Telegram instead, so the bytes cross the operator's uplink once
# rather than twice, and this cell restores them with `tgup fetch` (which
# verifies every part digest) before doing anything else.
#
# The mounted dataset video remains the fallback. A worker that cannot reach the
# channel must still be able to start, so nothing here is allowed to turn a
# missing file into a dead kernel on its own.
import os
import glob
import shutil
import json
import sys
from pathlib import Path  # own import: cell 4 must not depend on cell 2

input_dir = "/kaggle/input"
video_path = None
job_config = {"target_language": "Hindi"}
for config_path in Path(input_dir).rglob("dub_job.json"):
    try:
        job_config = json.loads(config_path.read_text(encoding="utf-8"))
        break
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Ignoring unreadable job config {config_path}: {exc}", flush=True)
target_language = str(job_config.get("target_language") or "Hindi")
print(f"Requested target language: {target_language}", flush=True)

if job_config.get("fetch_from_telegram"):
    print("Host left the source on Telegram; restoring it from the channel.", flush=True)
    try:
        mt_hits = sorted(Path(input_dir).rglob("multitasker.py"))
        if not mt_hits:
            raise FileNotFoundError("multitasker.py is not in the dataset")
        sys.path.insert(0, str(mt_hits[0].parent))
        import multitasker as mt
        job = mt.DubJob(
            job_id="job-000",
            video_path="",
            target_language=target_language,
            telegram_backup=str(job_config.get("telegram_backup") or ""),
            source_sha256=str(job_config.get("source_sha256") or ""),
            fetch_from_telegram=True,
            compress_480p=bool(job_config.get("compress_480p_on_worker")),
        )
        workdir = Path("/kaggle/working")
        workdir.mkdir(parents=True, exist_ok=True)
        restored = mt.restore_source_from_channel(job, workdir)
        if restored and Path(restored).is_file() and Path(restored).stat().st_size > 0:
            video_path = restored
            print(f"Restored source at: {video_path}", flush=True)
        else:
            print("Restore did not produce a usable file.", flush=True)
    except Exception as exc:  # noqa: BLE001 - never kill the kernel over this
        print(f"Channel restore failed ({exc}); trying the mounted dataset.", flush=True)

if not video_path:
    for root, dirs, files in os.walk(input_dir):
        for file in files:
            if file.endswith(('.mp4', '.mkv', '.avi', '.mov')):
                video_path = os.path.join(root, file)
                break
        if video_path:
            break

if not video_path:
    raise FileNotFoundError(
        "No video found: the dataset carried none, the host promised a "
        "telegram_backup link, and the channel restore did not work."
    )
print(f"Found video at: {video_path}")
'''


def main() -> int:
    raw = NOTEBOOK.read_text(encoding="utf-8")
    nb = json.loads(raw)

    cells = [c for c in nb["cells"] if c.get("cell_type") == "code"]
    if any(MARKER in "".join(c.get("source") or []) for c in cells):
        print("notebook already honours fetch_from_telegram; nothing to do")
        return 0

    target = None
    for index, cell in enumerate(nb["cells"]):
        source = "".join(cell.get("source") or [])
        if cell.get("cell_type") == "code" and "Video not found in Kaggle input" in source:
            target = index
            break
    if target is None:
        print("could not find the cell that locates the video", file=sys.stderr)
        return 1

    lines = NEW_SOURCE.splitlines(keepends=True)
    nb["cells"][target]["source"] = lines
    NOTEBOOK.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print(f"patched cell {target} of {NOTEBOOK.name}")

    # Re-read and re-locate, so a notebook that no longer parses is a failure
    # here rather than a surprise in a Kaggle kernel.
    verify = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    joined = "".join(verify["cells"][target]["source"])
    if "fetch_from_telegram" not in joined:
        print("verification failed", file=sys.stderr)
        return 1
    print("verified: notebook parses and the fetch branch is present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
