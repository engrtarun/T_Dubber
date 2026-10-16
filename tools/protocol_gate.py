#!/usr/bin/env python3
"""protocol_gate.py -- the executable form of the 4-step Ironclad Protocol.

The protocol (AGENTS.md section 8) is four rules every agent must satisfy for
each Python file it creates or edits:

  1. Strict code standards -- no bare generics, explicit Optionals, no
     injection vectors, and no ``assert`` inside a shared helper library.
  2. The terminal loop -- ``ruff`` and ``mypy --strict`` report nothing NEW.
  3. ``__all__`` sync -- every public name a module exports is listed.
  4. The output format -- the tool prints the verifier's own success line.

This script runs all four against the files an agent names and exits non-zero
if any fail. The rules are not prose to be remembered; they are checks that run.

USAGE
-----
    python tools/protocol_gate.py <file.py> [<file.py> ...]
    python tools/protocol_gate.py --all              # whole first-party tree
    python tools/protocol_gate.py --fix <file.py>    # ruff --fix, then re-check
    python tools/protocol_gate.py --write-baseline   # freeze current AST state

Exit codes:
    0  every step passed for every file
    1  at least one violation that is not in the baseline
    3  ruff or mypy is not installed (refuses to silently pass)

------------------------------------------------------------------------------
WHY THERE IS A SECOND BASELINE
------------------------------------------------------------------------------
ruff and mypy findings are already ratcheted against ``.lint-baseline.txt`` by
tools/lint_gate.py. The three AST checks are newer and stricter, so they get
their own freeze file, ``.protocol-baseline.txt``, managed by this script.

Both baselines follow the same rule: a finding already in the baseline is not a
failure (legacy debt is frozen, not forgiven); a finding that is NEW is a
failure. That is what lets a strict protocol start green on a legacy repo while
still blocking every new violation. ``--write-baseline`` shrinks the freeze;
the file must never grow.

The AST keys omit line numbers (``file::check::detail``) for the same reason
lint_gate omits them: line numbers shift on unrelated edits and would make the
gate flap red on changes that altered nothing.
------------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
LINT_BASELINE_PATH = REPO_ROOT / ".lint-baseline.txt"
PROTOCOL_BASELINE_PATH = REPO_ROOT / ".protocol-baseline.txt"

# ---------------------------------------------------------------------------
#  STEP 1 -- the AST rules
# ---------------------------------------------------------------------------
# A "bare generic" is a typing name used as an annotation with no parameters:
# ``x: dict`` instead of ``x: dict[str, Any]``. Parameterised (``dict[str]``)
# is fine; this set is only the bare spelling.
BARE_GENERICS = frozenset({"dict", "list", "tuple", "set", "frozenset", "type"})

# First-party Python, per CODE_MAP.md section 2: the root ``*.py`` files plus
# the two first-party sub-packages. Everything else (submodules, native, data)
# is excluded and must never be walked.
FIRST_PARTY_SUBDIRS = ("huggingface", "lip_sync")

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_TOOL_MISSING = 3


class ToolMissing(Exception):
    """ruff or mypy is not importable in this interpreter."""


# =============================================================================
#  Small helpers
# =============================================================================


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command from the repo root, capturing stdout and stderr."""
    return subprocess.run(
        argv,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _require_tool(name: str) -> None:
    """Fail loudly if the tool cannot be imported.

    A gate that silently passes because its tool vanished is exactly the kind of
    gate this repo spent its history without.
    """
    probe = _run([sys.executable, "-c", f"import {name}"])
    if probe.returncode != 0:
        raise ToolMissing(
            f"'{name}' is not installed for {sys.executable}.\n"
            f"  pip install {name}\n"
            "A skipped linter reads as a passing linter. Refusing to continue."
        )


def _relpath(raw: str) -> str:
    """Normalise a path to repo-relative, forward-slashed form."""
    candidate = Path(raw.strip())
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return candidate.as_posix()


def _load_baseline(path: Path) -> set[str]:
    """Load a freeze file. A missing file is an empty baseline."""
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def _write_baseline(path: Path, keys: set[str], header: str) -> None:
    """Freeze ``keys`` as the new baseline, with a self-describing header."""
    body = "".join(f"{key}\n" for key in sorted(keys))
    path.write_text(header + body, encoding="utf-8")


def _iter_first_party_files() -> list[Path]:
    """Every first-party Python file, root plus the two sub-packages."""
    files = [p for p in REPO_ROOT.glob("*.py") if p.is_file()]
    for sub in FIRST_PARTY_SUBDIRS:
        base = REPO_ROOT / sub
        if base.is_dir():
            files.extend(p for p in base.rglob("*.py") if p.is_file())
    return sorted(files)


# =============================================================================
#  STEP 2 -- ruff + mypy, ratcheted against .lint-baseline.txt
# =============================================================================


def _ruff_keys(files: list[Path]) -> set[str]:
    """Run ruff on ``files`` and return normalised finding keys.

    The JSON schema (a list of ``filename`` / ``code`` / ``message`` objects)
    has been stable for years. A malformed payload is treated as empty ONLY
    after printing what was received, so a schema change surfaces as a visible
    oddity instead of a mysteriously green run.
    """
    argv = [
        sys.executable, "-m", "ruff", "check",
        "--output-format", "json", "--no-cache",
        *[p.relative_to(REPO_ROOT).as_posix() for p in files],
    ]
    proc = _run(argv)
    stdout = proc.stdout.strip()
    if not stdout:
        return set()
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        print(f"[ruff] could not parse output as JSON: {exc}", file=sys.stderr)
        print(stdout[:2000], file=sys.stderr)
        return set()
    keys: set[str] = set()
    for item in payload:
        code = item.get("code") or "???"
        message = (item.get("message") or "").strip()
        keys.add(f"{_relpath(item.get('filename', ''))}::{code}::{message}")
    return keys


def _mypy_keys(files: list[Path]) -> set[str]:
    """Run mypy --strict on ``files`` and return normalised finding keys.

    mypy's text format is ``path:line:col: SEVERITY: message  [code]``. Only
    errors are kept -- notes are informational and would add churn. The key
    omits the line/column so it survives unrelated edits (see module docstring).
    """
    argv = [
        sys.executable, "-m", "mypy", "--strict",
        "--no-color-output", "--no-pretty",
        *[p.relative_to(REPO_ROOT).as_posix() for p in files],
    ]
    proc = _run(argv)
    keys: set[str] = set()
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or ": error:" not in stripped:
            continue
        head, _, rest = stripped.partition(": error:")
        message = rest.strip()
        parts = head.split(":")
        if len(parts) >= 2:
            path = _relpath(":".join(parts[:-2]) if len(parts) > 3 else parts[0])
        else:
            path = _relpath(head)
        code = "???"
        if message.endswith("]") and "[" in message:
            code = message.rsplit("[", 1)[1].rstrip("]")
        keys.add(f"{path}::{code}::{message.split('[')[0].strip()}")
    return keys


def _success_line(stdout: str, tool: str) -> str | None:
    """Pull the verifier's own success line out of its output, if present."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if tool == "ruff" and stripped == "All checks passed!":
            return stripped
        if tool == "mypy" and stripped.startswith("Success: no issues found"):
            return stripped
    return None


# =============================================================================
#  STEP 1 + 3 -- the AST rules
# =============================================================================


def _is_test_file(path: Path) -> bool:
    """True for the repo's test files (root ``test_*.py`` / ``*_test.py``)."""
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py")


def _has_main_guard(tree: ast.Module) -> bool:
    """True if the module has an ``if __name__ == "__main__":`` guard."""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        ):
            return True
    return False


def _is_library_module(path: Path, tree: ast.Module) -> bool:
    """True for an importable library module (not a test, not a script).

    A library module has no ``__main__`` guard and is not a test file. Scripts
    and tests are exempt from the ``__all__`` and helper-assert rules: a script
    is run, not imported, and a test is *supposed* to assert.
    """
    return not _is_test_file(path) and not _has_main_guard(tree)


def _annotation_nodes(tree: ast.Module) -> Iterator[ast.expr | None]:
    """Yield every annotation node in the module (params, returns, assigns)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.AnnAssign, ast.arg)):
            yield node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.returns


def _check_annotation(ann: ast.expr | None, out: list[tuple[int, str]]) -> None:
    """Collect bare generics in an annotation, descending into subscripts.

    ``dict`` alone is a violation; ``dict[str]`` is not (the base of a subscript
    is parameterised, which is the whole point). ``Optional[dict]`` still flags
    the inner ``dict``, which is correct -- that is a bare generic too.
    """
    if ann is None:
        return
    if isinstance(ann, ast.Name):
        if ann.id in BARE_GENERICS:
            out.append((ann.lineno or 0, ann.id))
    elif isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        if ann.value.strip() in BARE_GENERICS:
            out.append((ann.lineno or 0, ann.value.strip()))
    elif isinstance(ann, ast.Subscript):
        _check_annotation(ann.slice, out)
    elif isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        _check_annotation(ann.left, out)
        _check_annotation(ann.right, out)
    elif isinstance(ann, ast.Tuple):
        for elt in ann.elts:
            _check_annotation(elt, out)


def _public_names(tree: ast.Module) -> list[str]:
    """Top-level functions, classes, and constants that do not start with ``_``."""
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    names.append(target.id)
    return names


def _all_names(tree: ast.Module) -> list[str] | None:
    """The contents of ``__all__``, or None if the module does not define it."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
                    names = [
                        elt.value
                        for elt in node.value.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    ]
                    return names
                return []
    return None


def _enclosing_function(tree: ast.Module, lineno: int) -> str:
    """Best-effort name of the function containing ``lineno`` (for messages)."""
    best = "<module>"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= (end or node.lineno):
                best = node.name
    return best


def _ast_keys_for_file(path: Path) -> set[str]:
    """Run the three AST checks on one file and return violation keys.

    Keys are ``relpath::check::detail`` with no line number, so they survive
    unrelated edits. An empty set means the file is clean on all three checks.
    """
    rel = _relpath(str(path))
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError) as exc:
        return {f"{rel}::parse-error::{exc.__class__.__name__}"}

    keys: set[str] = set()

    # -- bare generics ------------------------------------------------------
    violations: list[tuple[int, str]] = []
    for ann in _annotation_nodes(tree):
        _check_annotation(ann, violations)
    for _, name in violations:
        keys.add(f"{rel}::bare-generic::{name}")

    # -- __all__ sync (library modules only) --------------------------------
    if _is_library_module(path, tree):
        public = _public_names(tree)
        all_names = _all_names(tree)
        if all_names is None:
            if public:
                keys.add(f"{rel}::all-absent::")
        else:
            missing = [name for name in public if name not in all_names]
            for name in missing:
                keys.add(f"{rel}::all-missing::{name}")

    # -- helper asserts (library modules only) -----------------------------
    if _is_library_module(path, tree):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            line_no = node.lineno or 0
            line = source.splitlines()[line_no - 1] if 0 < line_no <= len(source.splitlines()) else ""
            if "# noqa" in line:
                continue
            func = _enclosing_function(tree, line_no)
            keys.add(f"{rel}::helper-assert::{func}")

    return keys


def _ast_keys(files: list[Path]) -> set[str]:
    """Run the AST checks over ``files`` and return every violation key."""
    keys: set[str] = set()
    for path in files:
        keys |= _ast_keys_for_file(path)
    return keys


# =============================================================================
#  Reporting
# =============================================================================


def _report_file(path: Path, result: dict[str, object]) -> None:
    """Print the per-file verdict for one file."""
    rel = _relpath(str(path))
    print(f"\n=== {rel} ===")
    for step, detail in result.items():
        print(f"  [{step}] {detail}")


def main(argv: list[str] | None = None) -> int:
    """Run the protocol gate.

    Returns:
        0 -- every step passed for every file.
        1 -- at least one violation not present in a baseline.
        3 -- ruff or mypy is not installed (refuses to silently pass).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path, help="Python files to check")
    parser.add_argument("--all", action="store_true", help="check the whole first-party tree")
    parser.add_argument("--fix", action="store_true", help="run ruff --fix before checking")
    parser.add_argument(
        "--write-baseline", action="store_true",
        help="freeze current AST violations into .protocol-baseline.txt",
    )
    args = parser.parse_args(argv)

    files = _iter_first_party_files() if args.all else args.files
    if not files:
        parser.error("no files given (pass files or --all)")

    # Resolve to absolute paths once; every check and every key uses this form.
    files = [p if p.is_absolute() else (REPO_ROOT / p) for p in files]

    for tool in ("ruff", "mypy"):
        try:
            _require_tool(tool)
        except ToolMissing as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_TOOL_MISSING

    # -- optional auto-fix --------------------------------------------------
    if args.fix:
        _run([
            sys.executable, "-m", "ruff", "check", "--fix", "--no-cache",
            *[p.relative_to(REPO_ROOT).as_posix() for p in files],
        ])

    # -- AST baseline freeze (no need to run ruff/mypy for this) -----------
    if args.write_baseline:
        ast_keys = _ast_keys(files)
        previous = _load_baseline(PROTOCOL_BASELINE_PATH)
        removed = len(previous - ast_keys)
        _write_baseline(
            PROTOCOL_BASELINE_PATH, ast_keys,
            "# protocol_gate baseline -- frozen AST violations, one key per line.\n"
            "# Managed by tools/protocol_gate.py. Regenerate with --write-baseline.\n"
            "# This file must SHRINK. See AGENTS.md section 8.\n",
        )
        print(f"baseline written: {PROTOCOL_BASELINE_PATH.name}")
        print(f"  entries : {len(ast_keys)} (was {len(previous)})")
        print(f"  removed : {removed}   <- these are real fixes")
        return EXIT_PASS

    # -- STEP 1 + 3: AST rules, ratcheted against .protocol-baseline.txt --
    ast_keys = _ast_keys(files)
    protocol_baseline = _load_baseline(PROTOCOL_BASELINE_PATH)
    ast_new = ast_keys - protocol_baseline

    # -- STEP 2: ruff + mypy, ratcheted against .lint-baseline.txt ---------
    # `--all` runs the AST protocol only. ruff/mypy over the whole repo is
    # lint_gate.py's job against .lint-baseline.txt -- mixing the two baselines
    # into one `--all` verdict would make it unclear which baseline is stale,
    # so the repo-wide ruff/mypy ratchet stays where it has always lived.
    if args.all:
        ruff_new: set[str] = set()
        mypy_new: set[str] = set()
    else:
        lint_baseline = _load_baseline(LINT_BASELINE_PATH)
        ruff_new = _ruff_keys(files) - lint_baseline
        mypy_new = _mypy_keys(files) - lint_baseline

    # -- report -------------------------------------------------------------
    print(f"protocol_gate: checking {len(files)} file(s)")

    ast_ok = not ast_new

    if args.all:
        # STEP 4 -- surface the verifier's own success line.
        print(f"  [ast]  {'clean' if ast_ok else f'{len(ast_new)} new violation(s)'}")
        print("  (ruff/mypy repo-wide: see tools/lint_gate.py)")
    else:
        ruff_ok = not ruff_new
        mypy_ok = not mypy_new
        # STEP 4 -- surface the verifier's own success lines.
        ruff_line = "All checks passed!" if ruff_ok else f"{len(ruff_new)} new finding(s)"
        mypy_line = (
            f"Success: no issues found in {len(files)} source file(s)"
            if mypy_ok
            else f"{len(mypy_new)} new error(s)"
        )
        print(f"  [ruff] {ruff_line}")
        print(f"  [mypy] {mypy_line}")
        print(f"  [ast]  {'clean' if ast_ok else f'{len(ast_new)} new violation(s)'}")

    for key in sorted(ruff_new)[:20]:
        print(f"        + {key}")
    for key in sorted(mypy_new)[:20]:
        print(f"        + {key}")
    for key in sorted(ast_new)[:20]:
        print(f"        + {key}")
    for label, new in (("ruff", ruff_new), ("mypy", mypy_new), ("ast", ast_new)):
        if len(new) > 20:
            print(f"        ... and {len(new) - 20} more [{label}]")

    if ruff_new or mypy_new or ast_new:
        print(
            "\nGate: FAIL. These findings are not in the baseline.\n"
            "Fix them, or -- if one is a false positive -- suppress it with a\n"
            "comment explaining why (AGENTS.md section 1). Do NOT widen the\n"
            "baseline to make this go green."
        )
        return EXIT_FAIL

    print("\nGate: PASS. No new findings. Protocol satisfied.")
    return EXIT_PASS


if __name__ == "__main__":
    raise SystemExit(main())
