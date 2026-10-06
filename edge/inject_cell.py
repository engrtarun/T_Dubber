"""Inject the edge-cache cell into kaggle_worker.ipynb (root + local copy).

Both notebooks must stay byte-identical: pipeline.py:803 copies the root notebook
into the kernel directory, so a divergence here silently ships the wrong worker.

The cell is written from edge/notebook_cell.py rather than kept as JSON, because
a notebook cell is code and reading it as escaped JSON lines is how mistakes get
made.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "edge" / "notebook_cell.py"
TARGETS = [
    ROOT / "kaggle_worker.ipynb",
    ROOT / "kaggle_worker_local" / "kaggle_worker.ipynb",
]

# Insert after the pack cell (index 0) and before the dependency cell. The pack
# cell supplies the binary this cell runs; the dependency cell is what it saves.
INSERT_AT = 1

MARKER = "T_Dubber edge cache -- fetch warm artefacts"


def to_source_lines(text):
    """nbformat stores source as a list of lines, each keeping its newline.

    The trailing newline on the final line is dropped, which is what nbformat
    itself writes; keeping it would add a spurious blank line in Jupyter.
    """
    lines = text.splitlines(keepends=True)
    return lines


def main():
    text = SOURCE.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        raise SystemExit("cell source must end with a newline")
    source = to_source_lines(text)

    for target in TARGETS:
        if not target.is_file():
            raise SystemExit(f"missing notebook: {target}")

        nb = json.loads(target.read_text(encoding="utf-8"))

        # Idempotent: re-running must not stack copies of the cell.
        existing = [i for i, c in enumerate(nb["cells"]) if MARKER in "".join(c["source"])]
        if existing:
            if len(existing) > 1:
                raise SystemExit(f"{target.name}: {len(existing)} copies of the edge cell")
            nb["cells"][existing[0]]["source"] = source
            print(f"{target.name}: refreshed cell {existing[0]}")
        else:
            cell = {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": source,
            }
            nb["cells"].insert(INSERT_AT, cell)
            print(f"{target.name}: inserted cell at {INSERT_AT}")

        target.write_text(
            json.dumps(nb, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    # Verify the part this script owns: the edge cell must be identical in both
    # copies.
    #
    # It deliberately does NOT assert the two notebooks are equal overall. They
    # are supposed to be kept in sync (pipeline.py:803 copies root into the
    # kernel directory), but they can legitimately diverge when someone is
    # editing one of them concurrently -- and a check that fails on that turns a
    # routine re-run into a hard error that hides the real problem. A divergence
    # is reported, never overwritten: someone else's in-progress work is not
    # this script's to discard.
    a = json.loads(TARGETS[0].read_text(encoding="utf-8"))
    b = json.loads(TARGETS[1].read_text(encoding="utf-8"))

    ea = [c for c in a["cells"] if MARKER in "".join(c["source"])]
    eb = [c for c in b["cells"] if MARKER in "".join(c["source"])]
    if len(ea) != 1 or len(eb) != 1:
        raise SystemExit(
            f"expected exactly one edge cell per notebook, got {len(ea)} and {len(eb)}"
        )
    if ea[0] != eb[0]:
        raise SystemExit("the two copies of the edge cell differ")

    # The cell must compile. A notebook cell is Python; a syntax error here would
    # only surface as a failed Kaggle run an hour into it.
    try:
        compile("".join(ea[0]["source"]), "<edge cell>", "exec")
    except SyntaxError as exc:
        raise SystemExit(f"edge cell does not compile: {exc}")

    print(f"edge cell identical in both copies, at index "
          f"{[i for i, c in enumerate(a['cells']) if c is ea[0]][0]} "
          f"(root) and "
          f"{[i for i, c in enumerate(b['cells']) if c is eb[0]][0]} (local)")

    if a["cells"] != b["cells"]:
        print(
            f"NOTE: the two notebooks differ beyond the edge cell "
            f"({len(a['cells'])} vs {len(b['cells'])} cells). Left untouched -- "
            f"reconcile them deliberately, since pipeline.py:803 overwrites the "
            f"local copy from the root one."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())