"""Pre-pull the whole model roster into one folder, ready to become a
Kaggle **cache dataset**.

WHY
---
A cold Kaggle run spends ~15 minutes downloading Homura-2B before
the first dub starts. If the weights already sit in a Kaggle dataset
attached to the worker as *input*, the download is zero: the notebook
mounts them read-only and `vllm serve <path>` loads from disk.

WHAT THIS DOES
--------------
1. Reads ``models.json``.
2. ``snapshot_download``s every public entry into ``<out>/models--<org>--<name>``
   (the exact layout ``hf_store._snapshot_dirs`` scans, so a mounted
   copy is indistinguishable from a hub cache).
3. Writes ``MANIFEST.json`` (roster + sizes + timestamp).

Then, once per roster change:

    kaggle datasets create -p <out> --dir-mode tar   # or: datasets version
    # attach the dataset to kaggle_worker.ipynb alongside the job input

From then on every kernel start reports ``source: mounted`` for each
model and starts dubbing in seconds, not minutes.

Gated models (CohereX) are skipped unless ``HF_TOKEN`` is set.

Usage::

    python huggingface/seed_cache.py --out ./hf_seed
    python huggingface/seed_cache.py --out /kaggle/working/hf_seed   # on Kaggle
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hf_store  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="./hf_seed",
                        help="folder to fill (becomes the Kaggle dataset root)")
    parser.add_argument("--roster", default=None,
                        help="alternate models.json")
    args = parser.parse_args(argv)

    hf_store.setup_env()
    roster = hf_store.load_roster(args.roster)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    token = None
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("pip install huggingface-hub first.", file=sys.stderr)
        return 2

    manifest = {
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "hf_home": os.environ.get("HF_HOME", ""),
        "models": [],
    }

    failures = 0
    for entry in roster.get("models", []):
        repo_id = entry.get("repo_id", "")
        if not repo_id or entry.get("runtime") == "dataset":
            # Datasets are fetched by mazinger itself (fetch_profile);
            # seeding them as model snapshots would be the wrong layout.
            continue
        if entry.get("gated") and not (entry.get("token") or os.environ.get("HF_TOKEN")):
            print(f"skip (gated, no HF_TOKEN): {repo_id}")
            continue

        dest = out / ("models--" + repo_id.replace("/", "--"))
        print(f"[seed] {repo_id} -> {dest}")
        try:
            snapshot_download(
                repo_id=repo_id,
                cache_dir=str(out),
                token=os.environ.get("HF_TOKEN") or None,
            )
            size_gb = sum(p.stat().st_size for p in dest.rglob("*")
                          if p.is_file()) / (1024 ** 3) if dest.is_dir() else 0.0
        except Exception as exc:
            failures += 1
            print(f"  FAILED: {exc}", file=sys.stderr)
            continue
        manifest["models"].append({"repo_id": repo_id, "role": entry.get("role"),
                                   "approx_gb": round(size_gb, 2)})
        print(f"  ok ({size_gb:.2f} GB)")

    (out / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nseeded {len(manifest['models'])} model(s), {failures} failure(s)")
    print(f"next:  kaggle datasets create -p {out} --dir-mode tar")
    print("then attach that dataset to kaggle_worker.ipynb as an extra input.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
