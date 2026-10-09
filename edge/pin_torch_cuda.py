"""pin_torch_cuda.py -- make the torch family impossible to split across CUDA builds.

THE BUG THIS FIXES (measured, not guessed)
------------------------------------------
Kaggle run `letest_test3.txt` (2026-10-09) died after 9 minutes with:

    RuntimeError: Detected that PyTorch and TorchAudio were compiled with
    different CUDA versions. PyTorch has CUDA version 13.0 whereas TorchAudio
    has CUDA version 12.8.

From that run's own log:

  torchaudio-2.11.0+cu128   <- download-r2.pytorch.org/whl/cu128/...
  torch-2.13.0             <- plain manylinux wheel, i.e. the PyPI build = CUDA 13.0
  torchvision-0.28.0       <- plain manylinux wheel
  nvidia-cusparselt-cu13, nvidia-cudnn-cu13, nccl4py ...  <- the cu13 stack

So it was never a missing pin. The two requests could not both be satisfied:

  * vLLM 0.29.0 pins `torch==2.13.0` (PyPI metadata, read directly).
  * The cu128 index has no torch 2.13.0 at all. Its newest cp312 wheels are
    torch 2.11.0, torchaudio 2.11.0, torchvision 0.26.0 (42 wheels each, checked
    against download.pytorch.org/whl/cu128/).

`--extra-index-url` does not prefer an index; pip takes the best version match
across both. torchaudio's local version `2.11.0+cu128` outranked plain
`2.11.0`, so it came from cu128, while `torch==2.13.0` only existed on PyPI and
came from there -- a cu130 build. One resolver pass, two CUDA stacks, a crash
20 minutes later.

THE FIX
-------
1. `vllm==0.26.0`, which is the newest release whose three pins
   (torch 2.11.0 / torchaudio 2.11.0 / torchvision 0.26.0) all exist on the
   cu128 index. Verified against PyPI metadata for 0.18.1 -> 0.29.0: everything
   from 0.27.0 onwards moved to torch 2.13.0.
2. The family is then installed as ONE atomic, explicitly versioned unit from
   `--index-url` (not `--extra-index-url`) with `+cu128` local tags, so no
   later pass can substitute a wheel from the other index.
3. `DEEP_PROBE` imports `torchaudio`. Importing it IS the alignment check --
   the very function that raised in the failed run is torchaudio's own
   `_check_cuda_version()` -- so a mismatch now costs one second instead of
   twenty minutes, and the message names the cause.

Run:  python edge/pin_torch_cuda.py          (dry: prints what it would change)
      python edge/pin_torch_cuda.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [
    ROOT / "kaggle_worker.ipynb",
    ROOT / "kaggle_worker_local" / "kaggle_worker.ipynb",
]

# One definition, so the notebook, the doc and this script cannot drift.
VLLM_VERSION = "0.26.0"
TORCH_TRIPLE = ["torch==2.11.0+cu128", "torchvision==0.26.0+cu128",
                "torchaudio==2.11.0+cu128"]
CUDA_INDEX = "https://download.pytorch.org/whl/cu128"

# --------------------------------------------------------------------------
# 1. the install block
# --------------------------------------------------------------------------

OLD_INSTALL = '''    cu128 = ["--extra-index-url", "https://download.pytorch.org/whl/cu128"]
    if run_pip(["vllm==0.29.0"] + cu128, "vllm+cu128") != 0:
        _tick("pip: vllm cu128 index failed; retrying from plain PyPI")
        if run_pip(["vllm==0.29.0"], "vllm plain") != 0:
            return False
'''

NEW_INSTALL = '''    # One index, one CUDA build, one atomic install of the torch family.
    #
    # `--extra-index-url` does not prefer an index -- pip takes the best version
    # match across both. That is how the failed run ended up with torch 2.13.0
    # (PyPI = CUDA 13.0) beside torchaudio 2.11.0+cu128 (cu128 index): two CUDA
    # stacks from a single resolver pass, and a crash 20 minutes later inside
    # `import torchaudio`. vLLM 0.26.0 is the newest release whose three pins
    # all exist on the cu128 index; 0.27.0+ moved to torch 2.13.0, which the
    # cu128 index does not carry at all.
    cu128 = ["--extra-index-url", "%(index)s"]
    triple = ["--index-url", "%(index)s", "--force-reinstall", "--no-deps"]
    if run_pip(["vllm==%(vllm)s"] + cu128, "vllm+cu128") != 0:
        _tick("pip: vllm cu128 index failed; retrying from plain PyPI")
        if run_pip(["vllm==%(vllm)s"], "vllm plain") != 0:
            return False
    # Force the exact family in afterwards. `--no-deps` stops pip from pulling a
    # fourth opinion about torch; `--index-url` (not --extra) means there is no
    # other place a cu13 wheel could come from.
    if run_pip(TORCH_TRIPLE + triple, "torch triple (cu128)") != 0:
        print("WARNING: could not force the cu128 torch family; the probe decides.", flush=True)
''' % {"index": CUDA_INDEX, "vllm": VLLM_VERSION}

# --------------------------------------------------------------------------
# 2. the second pass's torch pin must name the same build
# --------------------------------------------------------------------------

OLD_PIN = '    torch_pin = ["torch==2.13.0"]\n'
NEW_PIN = '    torch_pin = [TORCH_TRIPLE[0]]\n'

# The triple has to exist as a name in pass 2 as well, so it is defined once at
# module level rather than inside install_deps.
OLD_SPEECH = "SPEECH_DEPS = [\n"
NEW_SPEECH = (
    "# Installed last and as one unit, so every wheel of the family carries the\n"
    "# same CUDA tag. `pip --target` resolves against an empty directory, which\n"
    "# is why this has to be forced rather than merely requested.\n"
    "TORCH_TRIPLE = [\n"
    '    "%(t0)s", "%(t1)s", "%(t2)s",\n'
    "]\n"
    "\n"
    "SPEECH_DEPS = [\n"
) % {"t0": TORCH_TRIPLE[0], "t1": TORCH_TRIPLE[1], "t2": TORCH_TRIPLE[2]}

# --------------------------------------------------------------------------
# 3. the last-resort system install must not recreate the split
# --------------------------------------------------------------------------

OLD_LAST = ('    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", '
            '"vllm==0.29.0"], check=False)\n')
NEW_LAST = ('    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir",\n'
            '                   "--index-url", "%(index)s", "--no-deps"] + TORCH_TRIPLE,\n'
            '                   check=False)\n'
            '    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir",\n'
            '                   "vllm==%(vllm)s"], check=False)\n'
            ) % {"index": CUDA_INDEX, "vllm": VLLM_VERSION}

# --------------------------------------------------------------------------
# 4. the probe has to touch torchaudio, because torchaudio IS the check
# --------------------------------------------------------------------------

OLD_DEEP = '''DEEP_PROBE = """
import vllm, torch, faster_whisper, av, openai
import torchvision
import vllm.entrypoints.cli.main
torch.ops.torchvision.nms
print("deps ok: vllm %s / torch %s / torchvision %s" % (
    vllm.__version__, torch.__version__, torchvision.__version__))
"""
'''

NEW_DEEP = '''DEEP_PROBE = """
import vllm, torch, faster_whisper, av, openai
import torchvision
import torchaudio
import vllm.entrypoints.cli.main
torch.ops.torchvision.nms
# Importing torchaudio IS the alignment check: the function that raised in the
# failed run is torchaudio's own _check_cuda_version(). Probing it here costs a
# second and names the cause, instead of surfacing it 20 minutes later inside the
# server start, by which point the HF publish cell never runs.
print("deps ok: vllm %s / torch %s (cuda %s) / torchaudio %s / torchvision %s" % (
    vllm.__version__, torch.__version__, torch.version.cuda,
    torchaudio.__version__, torchvision.__version__))
"""
'''

OLD_SHALLOW = 'names = ["vllm", "torch", "faster_whisper", "av", "openai", "demucs", "omnivoice"]\n'
NEW_SHALLOW = ('names = ["vllm", "torch", "torchaudio", "torchvision", "faster_whisper", "av",\n'
               '          "openai", "demucs", "omnivoice"]\n')

# --------------------------------------------------------------------------
# 5. the surrounding prose still taught the old, wrong numbers
# --------------------------------------------------------------------------

OLD_DOC_VLLM = """      1. vLLM, which hard-pins torch==2.13.0 / torchvision==0.28.0 /
         torchaudio==2.11.0 -- ONE resolver pass, so the three agree.
"""
NEW_DOC_VLLM = """      1. vLLM, which hard-pins the torch family -- ONE resolver pass, so the
         three agree. vLLM 0.26.0 pins torch==2.11.0 / torchvision==0.26.0 /
         torchaudio==2.11.0, all three of which the cu128 index actually carries.
         vLLM 0.27.0+ pins torch==2.13.0, and the cu128 index has no 2.13.0 at
         all, so that pair of requests cannot both be satisfied.
"""

OLD_DOC_PIN = """    # Pinning to the version vLLM pinned makes the second resolve agree with the
    # first, so the wheel is fetched once instead of twice. The extra-index has
"""
NEW_DOC_PIN = """    # Pinning to the version installed above makes the second resolve agree with
    # the first, so the wheel is fetched once instead of twice. The extra-index has
"""

PATCHES = [
    ("install block", OLD_INSTALL, NEW_INSTALL),
    ("torch triple definition", OLD_SPEECH, NEW_SPEECH),
    ("second pass torch pin", OLD_PIN, NEW_PIN),
    ("last-resort install", OLD_LAST, NEW_LAST),
    ("deep probe", OLD_DEEP, NEW_DEEP),
    ("shallow probe", OLD_SHALLOW, NEW_SHALLOW),
    ("install_deps docstring", OLD_DOC_VLLM, NEW_DOC_VLLM),
    ("pass-2 comment", OLD_DOC_PIN, NEW_DOC_PIN),
]


def patch_cell(index: int, src: str, apply: bool):
    """Apply every anchor to one cell's source. Returns (new_src, notes).

    Three states per anchor, because this script has to be safe to re-run:
    ``patched`` (the old text was there), ``already applied`` (the new text is
    there), and ``ANCHOR NOT FOUND`` (neither -- someone edited the cell by
    hand, which is worth shouting about rather than silently skipping).
    """
    notes = []
    dirty = False
    for name, old, new in PATCHES:
        # "Already applied" is checked FIRST. Two of these anchors are substrings
        # of their own replacement (SPEECH_DEPS appears in both), so counting the
        # old text first would match forever and duplicate the block on every
        # run. The first meaningful line of the replacement is unique, so it is
        # the honest marker.
        marker = next((l for l in new.split("\n") if l.strip() and not l.strip().startswith('"""')), "")
        if marker and src.count(marker):
            notes.append(f"    {name}: already applied")
            continue
        hits = src.count(old)
        if hits == 0:
            notes.append(f"    {name}: ANCHOR NOT FOUND")
            continue
        if hits > 1:
            notes.append(f"    {name}: ANCHOR AMBIGUOUS ({hits} matches) -- refusing")
            raise SystemExit(f"ambiguous anchor {name!r}: {hits} matches")
        src = src.replace(old, new)
        dirty = True
        notes.append(f"    {name}: patched")
    if apply and dirty:
        compile(src, f"<cell {index}>", "exec")
    return src, notes, dirty


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the notebooks")
    args = ap.parse_args()

    print(f"vllm={VLLM_VERSION}  index={CUDA_INDEX}  triple={TORCH_TRIPLE}\n")
    changed_any = False
    for path in TARGETS:
        label = path.relative_to(ROOT).as_posix()
        if not path.is_file():
            print(f"{label}: MISSING -- skipped")
            continue
        nb = json.loads(path.read_text(encoding="utf-8"))
        # The install cell is the one that runs pip; find it by content rather
        # than by index, because the two notebooks have different cell counts.
        target = None
        for i, cell in enumerate(nb["cells"]):
            src = "".join(cell.get("source", []))
            if "def install_deps():" in src and "run_pip(" in src:
                target = i
                break
        if target is None:
            print(f"{label}: no install cell found -- skipped")
            continue

        src, notes, dirty = patch_cell(
            target, "".join(nb["cells"][target]["source"]), args.apply)
        print(f"{label}: cell {target}")
        for note in notes:
            print(note)
        if dirty:
            changed_any = True
            nb["cells"][target]["source"] = src.splitlines(keepends=True)
            if args.apply:
                path.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + "\n",
                                encoding="utf-8", newline="\n")
                # verify by re-reading, because a notebook that cannot be opened
                # is worse than one that was never patched
                again = json.loads(path.read_text(encoding="utf-8"))
                compile("".join(again["cells"][target]["source"]),
                        f"<{label} cell {target}>", "exec")
                print(f"    wrote + re-read OK ({len(again['cells'])} cells)")

    print("\nDRY RUN. Re-run with --apply." if not args.apply else "\nAPPLIED.")
    return 0 if changed_any else 1


if __name__ == "__main__":
    raise SystemExit(main())