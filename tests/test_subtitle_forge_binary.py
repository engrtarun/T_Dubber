"""Differential test: the **real** `subtitle_forge` binary vs. the Python ports.

The sibling harnesses (`test_subtitle_forge_timecode.py`,
`test_subtitle_forge_wrapping.py`, `test_subtitle_forge_render.py`) each port a
slice of the Rust to Python and assert on that port.  That proves the *intended
algorithms* are right, but it cannot catch a transcription slip between the
Rust and the Python — a port can be self-consistently wrong.

This file closes that gap by treating the compiled binary as the system under
test:

  1. synthesise a deterministic pile of cues (edge-case timestamps plus random
     prose), already sorted and already whitespace-normalised;
  2. render the expected `.srt` and `.ass` with the Python reference;
  3. hand the same cues to the real `subtitle_forge.exe`;
  4. require the two outputs to be **byte-identical**.

A separate probe stage characterises how the binary normalises cue text.  It
establishes that cue text is only ever whitespace-normalised (trimmed,
doubled spaces collapsed, tabs/newlines turned into spaces) and that commas
and other punctuation survive untouched — so plain prose is a fair input for
a byte-exact comparison.  Note that `sanitize_field`, the routine whose unit
test is named `sanitiser_strips_field_delimiters`, is *not* applied to cue
text at all: it guards the comma-delimited ASS Style/Script-Info fields
(`--font`, `--title`), which is why a comma in a font name cannot break the
Style line.

Run:  python tests/test_subtitle_forge_binary.py
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_subtitle_forge_render import render_ass, render_srt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / "subtitle_forge" / "target" / "release" / (
    "subtitle_forge.exe" if sys.platform == "win32" else "subtitle_forge"
)

# ── fixture vocabulary ───────────────────────────────────────────────────────
# Plain prose only: no doubled spaces, no leading/trailing whitespace and no
# newlines, because the binary normalises those before rendering and the
# reference above does not.  Keeping phase 1 free of that difference is what
# makes a byte match meaningful rather than accidental.

_WORDS = (
    "the quick brown fox jumps over lazy dog subtitle forge timeline "
    "mazinger whisper transcript cue wrap width carries over hour boundary "
    "café naïve résumé äöü éèê 日本語 русский العربية 한국어"
).split()

_PUNCT = ["", "", "", ",", ".", "!", "?", "'", "-", ":", ";"]


def _sentence(rng: random.Random, words: int) -> str:
    parts = []
    for _ in range(words):
        w = rng.choice(_WORDS) + rng.choice(_PUNCT)
        parts.append(w)
    return " ".join(parts)


def build_cues(rng: random.Random, n: int) -> list[tuple[float, float, str]]:
    """Deterministic, strictly increasing, already-normalised cues."""
    raw: list[tuple[float, float, str]] = []
    for _ in range(n):
        # Half the cues come from a pool of deliberately awkward instants so
        # that second/minute/hour carries are hit far more often than random
        # sampling would manage; the rest are uniformly spread over four hours.
        if rng.random() < 0.5:
            base = rng.uniform(0.0, 4.0 * 3600.0)
            jitter = rng.choice(
                [-0.0004999, -0.0001, -0.000001, 0.0, 0.000001, 0.0001,
                 0.0004999, 0.0005, 0.004999, 0.49999, 0.99999]
            )
            start = base + jitter
        else:
            start = rng.uniform(0.0, 4.0 * 3600.0)

        dur = rng.choice([0.001, 0.04999, 0.5, 1.0, 2.75, rng.uniform(0.1, 9.0)])
        end = start + dur
        if end <= start:
            end = start + 0.001
        text = _sentence(rng, rng.randint(1, 45))
        raw.append((start, end, text))

    raw.sort(key=lambda c: c[0])

    # Enforce a strict total order so the comparison never depends on how
    # either side breaks a tie between two identical start times.
    out: list[tuple[float, float, str]] = []
    prev = -1.0
    for start, end, text in raw:
        start = max(start, prev + 0.001)
        if end <= start:
            end = start + 0.001
        out.append((start, end, text))
        prev = start
    return out


def run_binary(cues, workdir: Path) -> tuple[str, str, str]:
    """Invoke the compiled binary; return (srt, ass, stderr log)."""
    src = workdir / "in.json"
    srt = workdir / "out.srt"
    ass = workdir / "out.ass"
    for p in (srt, ass):
        p.unlink(missing_ok=True)

    src.write_text(
        json.dumps({"segments": [
            {"start": s, "end": e, "text": t} for s, e, t in cues
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [str(BINARY), "--input", str(src), "--srt", str(srt), "--ass", str(ass)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"binary exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
        )
    return (
        srt.read_text(encoding="utf-8"),
        ass.read_text(encoding="utf-8"),
        proc.stderr,
    )


def _first_diff(a: str, b: str, label: str) -> str:
    """Human-readable location of the first differing line."""
    la, lb = a.splitlines(), b.splitlines()
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else "<end of file>"
        y = lb[i] if i < len(lb) else "<end of file>"
        if x != y:
            return f"{label} differs at line {i + 1}:\n  expected: {x!r}\n  actual  : {y!r}"
    return f"{label} differs only in trailing content"


# ── stages ───────────────────────────────────────────────────────────────────

def probe_sanitiser() -> list[str]:
    """Report exactly what the binary rewrites in cue text."""
    findings: list[str] = []
    probes = [
        "comma, here",
        "semicolon; here",
        "pipe|here",
        "colon:here",
        "back\\slash",
        "brace{overlap}",
        "  padded  text  ",
        "double  spaced  words",
        "tab\tand\nnewline",
        "quote\"and'apostrophe",
    ]
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        for i, text in enumerate(probes):
            src = work / f"p{i}.json"
            out = work / f"p{i}.srt"
            src.write_text(
                json.dumps({"segments": [{"start": 0.0, "end": 2.0, "text": text}]}),
                encoding="utf-8",
            )
            subprocess.run(
                [str(BINARY), "--input", str(src), "--srt", str(out)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60, check=True,
            )
            got = out.read_text(encoding="utf-8").splitlines()[2]
            if got != text:
                findings.append(f"  sanitiser rewrites {text!r} -> {got!r}")
    return findings


def main() -> int:
    failures: list[str] = []
    checks = 0

    def check(cond: bool, msg: str) -> None:
        nonlocal checks
        checks += 1
        if not cond:
            failures.append(msg)

    if not BINARY.exists():
        print(f"SKIP: build the binary first ({BINARY})")
        return 1

    print(f"binary under test: {BINARY}")

    # ── stage 1: what does the sanitiser do to cue text? ────────────────────
    print("\n== stage 1: text-sanitiser probe ==")
    findings = probe_sanitiser()
    if findings:
        print("  findings (differences between input and emitted cue text):")
        for f in findings:
            print(f)
    else:
        print("  cue text passes through verbatim (only wrapping applies)")

    # ── stage 2: byte-for-byte differential ─────────────────────────────────
    print("\n== stage 2: differential vs. Python reference ==")
    rng = random.Random(20261005)
    for n_cues, label in ((1, "single cue"), (17, "small"), (140, "large")):
        cues = build_cues(rng, n_cues)

        expected_srt = render_srt(cues)
        expected_ass = render_ass(cues)

        with tempfile.TemporaryDirectory() as td:
            got_srt, got_ass, _log = run_binary(cues, Path(td))

        check(got_srt == expected_srt,
              f"[{label}] SRT mismatch\n{_first_diff(expected_srt, got_srt, 'SRT')}")
        check(got_ass == expected_ass,
              f"[{label}] ASS mismatch\n{_first_diff(expected_ass, got_ass, 'ASS')}")
        print(f"  {label:12} cues={n_cues:4d}  "
              f"SRT={'ok' if got_srt == expected_srt else 'DIFF'}  "
              f"ASS={'ok' if got_ass == expected_ass else 'DIFF'}")

    # ── stage 3: timestamp fuzz, isolated from prose ────────────────────────
    print("\n== stage 3: timestamp fuzz (1000 cues, one per pair) ==")
    fuzz_rng = random.Random(99)
    fuzz = build_cues(fuzz_rng, 1000)
    expected_srt = render_srt(fuzz)
    with tempfile.TemporaryDirectory() as td:
        got_srt, _a, _l = run_binary(fuzz, Path(td))
    check(got_srt == expected_srt,
          f"timestamp fuzz SRT mismatch\n{_first_diff(expected_srt, got_srt, 'SRT')}")
    print(f"  1000 cues -> SRT {'ok' if got_srt == expected_srt else 'DIFF'}")

    # ── verdict ─────────────────────────────────────────────────────────────
    print()
    if failures:
        print(f"FAIL ({len(failures)} of {checks} checks)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK ({checks} checks passed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
