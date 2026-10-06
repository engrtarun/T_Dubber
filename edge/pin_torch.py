"""Pin the speech pass to vLLM's torch, in both notebook copies.

Verified with a pip dry-run against live PyPI:

  speech pass, unpinned, no CUDA index  ->  torch 2.14.1   (the wrong one)
  speech pass, torch==2.13.0 + cu128     ->  torch 2.13.0   (what vLLM pinned)

The notebook installs vLLM first and the speech deps second, but `pip --target`
resolves against an EMPTY directory: it does not see what the first pass wrote.
So the second pass picks the newest torch, downloads it (~554 MB), and then
fails to overwrite the files already present, producing the "Target directory
... already exists" churn and leaving torch 2.13.0 in place. Correct result,
wasted download.

Passing the same extra-index and pinning torch makes the second resolve agree
with the first.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [
    ROOT / "kaggle_worker.ipynb",
    ROOT / "kaggle_worker_local" / "kaggle_worker.ipynb",
]

# The replacement is anchored on the exact current text so that a notebook which
# someone else has edited is reported rather than silently mangled.
OLD = '''    if run_pip(SPEECH_DEPS + ["kaggle"], "speech_deps") != 0:
        return False

    return True'''

NEW = '''    # Pass 2 gets the SAME extra-index AND an explicit torch pin.
    #
    # `pip --target` resolves against an EMPTY directory: it cannot see the tree
    # pass 1 just wrote. So the unpinned form of this line resolved torch to
    # 2.14.1 (verified with a dry run against live PyPI), downloaded that wheel
    # -- ~554 MB -- and then refused to overwrite the 2.13.0 files vLLM had
    # pinned, emitting the "Target directory ... already exists" churn and
    # leaving the right version in place anyway. Correct result, wasted download,
    # every single cold run.
    #
    # Pinning to the version vLLM pinned makes the second resolve agree with the
    # first, so the wheel is fetched once instead of twice. The extra-index has
    # to match too: without it pip looks at PyPI, where the torch CUDA wheels
    # are named `nvidia-*-cu13`, while the cu128 index serves `nvidia-*-cu12`.
    # That naming difference is what makes the unpinned pass drag in a second,
    # different CUDA stack.
    torch_pin = ["torch==2.13.0"]
    if run_pip(SPEECH_DEPS + ["kaggle"] + torch_pin + cu128, "speech_deps") != 0:
        print("WARNING: speech deps failed with the torch pin; retrying unpinned.", flush=True)
        if run_pip(SPEECH_DEPS + ["kaggle"], "speech_deps-unpinned") != 0:
            return False

    return True'''


def main():
    changed = 0
    for target in TARGETS:
        nb = json.loads(target.read_text(encoding="utf-8"))
        found = 0
        for cell in nb["cells"]:
            src = "".join(cell["source"])
            if OLD in src:
                src = src.replace(OLD, NEW)
                cell["source"] = src.splitlines(keepends=True)
                found += 1
        if found == 0:
            print(f"{target.name}: anchor NOT found -- left untouched")
            continue
        if found > 1:
            raise SystemExit(f"{target.name}: anchor appears {found} times, refusing")
        target.write_text(
            json.dumps(nb, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"{target.name}: patched ({found} cell)")
        changed += 1

    if changed != len(TARGETS):
        raise SystemExit(f"only patched {changed} of {len(TARGETS)} notebooks")

    # The edited cell must still parse as Python.
    for target in TARGETS:
        nb = json.loads(target.read_text(encoding="utf-8"))
        for cell in nb["cells"]:
            src = "".join(cell["source"])
            if "torch_pin" in src:
                compile(src, "<cell>", "exec")
        print(f"{target.name}: parses")
    return 0


if __name__ == "__main__":
    sys.exit(main())