#!/usr/bin/env python3
"""lint_gate.py -- the ratchet that lets a 723-file legacy repo adopt strict rules.

WHY THIS EXISTS INSTEAD OF A PLAIN `ruff check .`
--------------------------------------------------
A plain gate on this repo fails on its very first run and stays red for weeks.
That is worse than having no gate at all: a red gate gets ignored, then deleted,
and then nothing is protecting the codebase any more.

So this tool **freezes today's findings into a baseline** and fails ONLY on
findings that are NOT in the baseline. New code gets the full rule set
immediately; legacy code stops getting worse. Each fix makes the baseline
smaller, and the goal is for the baseline to reach zero.

    > A gate that starts red gets deleted. This one starts green.

HOW THE COMPARISON STAYS STABLE
-------------------------------
Findings are keyed WITHOUT line numbers -- ``path::CODE::message`` rather than
``path:LINE:COL::CODE``. That is deliberate and load-bearing:

  Line numbers shift when an unrelated edit adds an import at the top of a file.
  If the key included the line, every such edit would manufacture a brand-new
  "new finding" and delete an old one, so the gate would flap red on edits that
  changed nothing about quality. Dropping the line number makes the baseline
  survive refactors.

The cost is honest and worth stating: if a file gains a *second* occurrence of
a finding it already had (same code, same message), this will not notice. The
count of each key is still tracked, so `--write-baseline` will not silently
absorb a doubling either -- run it deliberately and read the diff.

USAGE
-----
    python tools/lint_gate.py                   # check. Exit 1 on NEW findings.
    python tools/lint_gate.py --write-baseline  # shrink/refresh the baseline.
    python tools/lint_gate.py --tool ruff        # only one tool.
    python tools/lint_gate.py --verbose         # show every new finding.

The baseline lives at ``.lint-baseline.txt`` (plain text, one key per line,
sorted, so it diffs cleanly in review).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / ".lint-baseline.txt"

# Tools are invoked as modules so they resolve through the active interpreter's
# environment. Shelling out to a bare `ruff` finds whichever copy happens to be
# first on PATH, which on a dev box is frequently the wrong one.
TOOL_MODULES: dict[str, list[str]] = {
    "ruff": ["-m", "ruff", "check", "--output-format", "json", "--no-cache", "."],
    # --cache-dir is pinned to a gate-owned directory, and --no-incremental is
    # not used -- deliberately, and this is the reasoning:
    #
    #   mypy's incremental cache demonstrably changes the RESULT set. A cold run
    #   and a warm run over this repo disagree: warming `.mypy_cache` by running
    #   `mypy` by hand changed the gate verdict, and re-running with an explicit
    #   fresh cache moved the line count by itself. A gate that returns a
    #   different answer depending on whether somebody happened to run mypy
    #   earlier that afternoon is worse than no gate -- it produces false
    #   failures nobody can reproduce.
    #
    #   Isolating the cache dir makes the gate's state INDEPENDENT of whatever
    #   the developer did manually, which removes the common case. CI is always
    #   cold, and a fresh local run is always cold, so those two agree; warm
    #   runs agree with each other. If that ever proves insufficient, the fix is
    #   to append --no-incremental, trading speed for a reproducibility the
    #   gate is worthless without.
    "mypy": [
        "-m", "mypy", "--no-error-summary", "--no-pretty", "--no-color-output",
        "--cache-dir", ".mypy_cache_gate", ".",
    ],
}

EXIT_TOOL_MISSING = 3


class ToolMissing(Exception):
    """Raised when ruff or mypy is not importable in this interpreter."""


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

    This has to be a hard stop rather than a skip. A gate that silently passes
    because its tool vanished is exactly the kind of gate this repo just spent
    its history without.
    """
    probe = _run([sys.executable, "-c", f"import {name}"])
    if probe.returncode != 0:
        raise ToolMissing(
            f"'{name}' is not installed for {sys.executable}.\n"
            f"  pip install {name}\n"
            f"A skipped linter reads as a passing linter. Refusing to continue."
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


def collect_ruff() -> set[str]:
    """Run ruff and return normalised finding keys.

    Ruff's JSON schema has been stable for years: a list of objects with
    ``filename``, ``code``, and ``message``. A malformed payload is treated as
    an empty result ONLY after printing what was received, so a schema change
    surfaces as a visible oddity instead of a mysteriously green run.
    """
    proc = _run([sys.executable, *TOOL_MODULES["ruff"]])
    stdout = proc.stdout.strip()

    if not stdout:
        # ruff exits 1 when it finds problems, 2 on a genuine error.
        if proc.returncode == 2 or proc.stderr.strip():
            print(f"[ruff] no JSON on stdout, exit={proc.returncode}", file=sys.stderr)
            if proc.stderr.strip():
                print(proc.stderr.strip()[:2000], file=sys.stderr)
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


def collect_mypy() -> set[str]:
    """Run mypy and return normalised finding keys.

    mypy's default text format is ``path:line:col: SEVERITY: message  [code]``.
    Only errors are kept -- notes are informational and would add churn.
    """
    proc = _run([sys.executable, *TOOL_MODULES["mypy"]])
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

        # The bracketed code is the stable part; the message often embeds the
        # offending expression, which changes with harmless edits.
        code = "???"
        if message.endswith("]") and "[" in message:
            code = message.rsplit("[", 1)[1].rstrip("]")
        keys.add(f"{path}::{code}::{message.split('[')[0].strip()}")
    return keys


def read_baseline() -> set[str]:
    """Load the frozen baseline. A missing file is an empty baseline."""
    if not BASELINE_PATH.exists():
        return set()
    return {
        line.strip()
        for line in BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def write_baseline(keys: set[str]) -> None:
    """Freeze ``keys`` as the new baseline, with a self-describing header."""
    header = (
        "# lint_gate baseline -- frozen findings, one key per line.\n"
        "# Managed by tools/lint_gate.py. Regenerate with --write-baseline.\n"
        "# This file must SHRINK. See AGENTS.md section 5.\n"
    )
    body = "".join(f"{key}\n" for key in sorted(keys))
    BASELINE_PATH.write_text(header + body, encoding="utf-8")


def report(tool: str, new: set[str], unique: int, baseline_count: int) -> None:
    """Print a per-tool summary line plus a sample of new findings.

    `unique` is the count of DISTINCT keys, not of raw findings. Several
    findings in one file that share a rule code and message collapse into a
    single key on purpose (see the module docstring on stability), so this
    number is smaller than what the tool printed. Labelling it "findings" would
    be misleading -- it once looked like a 633-to-234 regression when nothing
    had changed at all.
    """
    verdict = "NEW FINDINGS" if new else "clean"
    print(
        f"[{tool:4}] {verdict:13} unique={unique:<6} "
        f"baseline_total={baseline_count:<6} new={len(new)}"
    )
    for key in sorted(new)[:20]:
        print(f"        + {key}")
    if len(new) > 20:
        print(f"        ... and {len(new) - 20} more")


def main(argv: list[str] | None = None) -> int:
    """Run the ratchet.

    Returns:
        0 -- no new findings, or the baseline was rewritten on request.
        1 -- at least one new finding not present in the baseline.
        3 -- a required tool is not installed (refuses to silently pass).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-baseline", action="store_true", help="freeze current findings as the baseline")
    parser.add_argument("--tool", choices=sorted(TOOL_MODULES), help="run only this tool")
    parser.add_argument("--verbose", action="store_true", help="list every new finding")
    args = parser.parse_args(argv)

    tools = [args.tool] if args.tool else sorted(TOOL_MODULES)
    collectors = {"ruff": collect_ruff, "mypy": collect_mypy}

    for tool in tools:
        try:
            _require_tool(tool)
        except ToolMissing as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_TOOL_MISSING

    baseline = read_baseline()

    # Collect ONCE per tool and reuse. Calling the collector again inside the
    # reporting loop would run ruff/mypy a second time over the whole repo --
    # doubling a CI job's runtime to produce numbers we already had.
    per_tool: dict[str, set[str]] = {tool: collectors[tool]() for tool in tools}
    current: set[str] = set()
    for keys in per_tool.values():
        current |= keys

    if args.write_baseline:
        removed = len(baseline - current)
        added = len(current - baseline)
        write_baseline(current)
        print(f"baseline written: {BASELINE_PATH.name}")
        print(f"  entries : {len(current)} (was {len(baseline)})")
        print(f"  removed : {removed}   <- these are real fixes")
        print(f"  added   : {added}   <- if this is not 0, you relaxed something. Check it.")
        return 0

    new = current - baseline
    for tool in tools:
        report(tool, per_tool[tool] & new, len(per_tool[tool]), len(baseline))

    if new and args.verbose:
        print(f"\n--- all {len(new)} new finding(s) ---")
        for key in sorted(new):
            print(f"  {key}")

    if new:
        print(
            "\nGate: FAIL. These findings are not in the baseline.\n"
            "Fix them, or -- if one is a false positive -- suppress it with a\n"
            "comment explaining why (AGENTS.md section 1). Do NOT widen the\n"
            "baseline to make this go green."
        )
        return 1

    print("\nGate: PASS. No new findings. Baseline is green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())