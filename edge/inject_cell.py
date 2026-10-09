"""Inject the edge cells (fetch + publish) into kaggle_worker.ipynb (root + local copy).

Both notebooks must stay byte-identical: pipeline.py:932 copies the root notebook
into the kernel directory, so a divergence here silently ships the wrong worker.

The cells are written from edge/*.py rather than kept as JSON, because a notebook
cell is code and reading it as escaped JSON lines is how mistakes get made.

Three cells, three jobs:
  * the FETCH cell runs directly after the pack cell and before the dependency
    cell -- ordering is its entire value;
  * the EARLY PUBLISH cell runs right after the dependency install, because that
    is the first moment the tree exists and it is the only moment a later crash
    cannot skip (measured: Kaggle run letest_test3.txt finished its install at
    ~274 s and then died at 545 s on a torch CUDA mismatch, so a publish cell
    that only ran LAST never ran and the repository stayed empty);
  * the LATE PUBLISH cell stays last for the case where later cells add more
    packages. Its digest check makes the second attempt a no-op when the early
    one already pushed the same bytes.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [
    ROOT / "kaggle_worker.ipynb",
    ROOT / "kaggle_worker_local" / "kaggle_worker.ipynb",
]

# The dependency-install cell: located by content, because the two notebooks
# have different cell counts and their indices do not line up.
INSTALL_CELL_MARKER = "def install_deps():"

# (source file, marker, insert position, variant)
#
# `insert_at` of None means "append at the end". The string "after:install" means
# "immediately after the cell that runs pip", resolved per notebook.
CELLS = [
    (ROOT / "edge" / "notebook_cell.py",
     "T_Dubber edge cache -- fetch warm artefacts",
     1, None),  # after the pack cell (which supplies the binary) and before the dependency cell (which it saves)
    (ROOT / "edge" / "publish_cell.py",
     "PUBLISH_VARIANT: early",
     "after:install", "early"),  # the tree is complete the moment the install returns
    (ROOT / "edge" / "publish_cell.py",
     "PUBLISH_VARIANT: late",
     None, "late"),  # last: catches any packages the later cells added
]

VARIANT_SENTINEL = "# PUBLISH_VARIANT: late"

# A publish cell written before the variant banner existed. It carries the title
# but no marker, so the idempotency check cannot see it -- and a notebook that kept
# one would end up with three publish cells, two of them invisible to every later
# check. Those are removed deliberately (and reported), never silently.
LEGACY_PUBLISH_TITLE = "T_Dubber edge publish -- push the built pylibs tree"


def to_source_lines(text):
    """nbformat stores source as a list of lines, each keeping its newline.

    The trailing newline on the final line is dropped, which is what nbformat
    itself writes; keeping it would add a spurious blank line in Jupyter.
    """
    lines = text.splitlines(keepends=True)
    return lines


def build_source(source_file, variant):
    text = source_file.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        raise SystemExit(f"{source_file.name}: cell source must end with a newline")
    if variant:
        if text.count(VARIANT_SENTINEL) != 1:
            raise SystemExit(
                f"{source_file.name}: expected exactly one {VARIANT_SENTINEL!r}, "
                f"found {text.count(VARIANT_SENTINEL)} -- a marker that is not "
                f"unique cannot identify a cell"
            )
        text = text.replace(
            VARIANT_SENTINEL,
            f"# PUBLISH_VARIANT: {variant}\n"
            f"# (injected by edge/inject_cell.py; the early copy exists because a\n"
            f"#  crash after the install must not be able to skip the publish)",
            1,
        )
    # A banner that also appears in the prose would make the marker match two
    # cells, or the same cell twice, and every idempotency check here would lie.
    for other in ("early", "late"):
        wanted = 1 if variant == other else 0
        if text.count(f"PUBLISH_VARIANT: {other}") != wanted:
            raise SystemExit(
                f"{source_file.name}: the built {variant or 'plain'} cell mentions "
                f"{other!r} the wrong number of times"
            )
    return to_source_lines(text)


def resolve_position(nb, insert_at):
    if insert_at is None:
        return len(nb["cells"])
    if insert_at == "after:install":
        hits = [i for i, c in enumerate(nb["cells"])
                if INSTALL_CELL_MARKER in "".join(c["source"])]
        if len(hits) != 1:
            raise SystemExit(
                f"expected exactly one install cell (looking for {INSTALL_CELL_MARKER!r}), "
                f"found {len(hits)}"
            )
        return hits[0] + 1
    return insert_at


def main():
    for target in TARGETS:
        if not target.is_file():
            raise SystemExit(f"missing notebook: {target}")

        nb = json.loads(target.read_text(encoding="utf-8"))

        # Drop publish cells that predate the variant banner. Reported, because a
        # notebook quietly losing a cell is exactly the failure this repo keeps
        # paying for.
        legacy = [i for i, c in enumerate(nb["cells"])
                  if LEGACY_PUBLISH_TITLE in "".join(c["source"])
                  and "PUBLISH_VARIANT" not in "".join(c["source"])]
        for i in reversed(legacy):
            print(f"{target.name}: removed legacy publish cell {i} (no variant banner)")
            del nb["cells"][i]

        for source_file, marker, insert_at, variant in CELLS:
            source = build_source(source_file, variant)

            # Idempotent: re-running must not stack copies of the cell. The marker
            # is per-variant, so the two publish copies never match each other.
            existing = [i for i, c in enumerate(nb["cells"]) if marker in "".join(c["source"])]
            if existing:
                if len(existing) > 1:
                    raise SystemExit(f"{target.name}: {len(existing)} copies of {source_file.name} ({variant})")
                nb["cells"][existing[0]]["source"] = source
                print(f"{target.name}: refreshed {source_file.name} ({variant}) cell {existing[0]}")
            else:
                cell = {
                    "cell_type": "code",
                    "execution_count": None,
                    "metadata": {},
                    "outputs": [],
                    "source": source,
                }
                at = resolve_position(nb, insert_at)
                nb["cells"].insert(at, cell)
                print(f"{target.name}: inserted {source_file.name} ({variant}) cell at {at}")

        target.write_text(
            json.dumps(nb, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    # Verify the part this script owns: each edge cell must be identical in both
    # copies.
    #
    # It deliberately does NOT assert the two notebooks are equal overall. They
    # are supposed to be kept in sync (pipeline.py:932 copies root into the
    # kernel directory), but they can legitimately diverge when someone else is
    # editing one of them concurrently -- and a check that fails on that turns a
    # routine re-run into a hard error that hides the real problem. A divergence
    # is reported, never overwritten: someone else's in-progress work is not
    # this script's to discard.
    a = json.loads(TARGETS[0].read_text(encoding="utf-8"))
    b = json.loads(TARGETS[1].read_text(encoding="utf-8"))

    positions = []
    for _, marker, _, _ in CELLS:
        ea = [c for c in a["cells"] if marker in "".join(c["source"])]
        eb = [c for c in b["cells"] if marker in "".join(c["source"])]
        if len(ea) != 1 or len(eb) != 1:
            raise SystemExit(
                f"expected exactly one cell matching {marker!r} per notebook, "
                f"got {len(ea)} and {len(eb)}"
            )
        if ea[0] != eb[0]:
            raise SystemExit(f"the two copies of the cell {marker!r} differ")
        positions.append(
            (marker,
             [i for i, c in enumerate(a["cells"]) if c is ea[0]][0],
             [i for i, c in enumerate(b["cells"]) if c is eb[0]][0])
        )

        # The cell must compile. A notebook cell is Python; a syntax error here
        # would only surface as a failed Kaggle run an hour into it.
        try:
            compile("".join(ea[0]["source"]), f"<{marker}>", "exec")
        except SyntaxError as exc:
            raise SystemExit(f"cell {marker!r} does not compile: {exc}")

    # The two publish copies must be the same code apart from their variant
    # banner. If they ever drift, the "already published" no-op stops holding and
    # the run uploads the same gigabytes twice.
    early = "".join(next(c for c in a["cells"] if "PUBLISH_VARIANT: early" in "".join(c["source"]))["source"])
    late = "".join(next(c for c in b["cells"] if "PUBLISH_VARIANT: late" in "".join(c["source"]))["source"])
    strip = lambda text: "\n".join(l for l in text.split("\n") if "PUBLISH_VARIANT" not in l and "injected by edge/inject_cell.py" not in l and "crash after the install must not" not in l)
    if strip(early) != strip(late):
        raise SystemExit("the early and late publish cells have drifted apart")

    print(
        "edge cells identical in both copies: "
        + ", ".join(f"{m!r} at {ri} (root) / {li} (local)" for m, ri, li in positions)
    )

    if a["cells"] != b["cells"]:
        print(
            f"NOTE: the two notebooks differ beyond the edge cells "
            f"({len(a['cells'])} vs {len(b['cells'])} cells). Left untouched -- "
            f"reconcile them deliberately, since pipeline.py:803 overwrites the "
            f"local copy from the root one."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
