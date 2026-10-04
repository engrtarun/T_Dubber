"""Cross-check the timestamp algorithm used by subtitle_forge/src/main.rs.

There is no Rust toolchain on this machine, so the exact algorithm from
`main.rs` (`to_millis` / `to_centis` / `format_srt_time` / `format_ass_time`)
is ported here 1:1 and asserted against the expectations that are encoded in
the crate's `#[cfg(test)]` suite.  If this script passes, the *math* the Rust
code performs is correct; only the Rust syntax remains unverified (blocked on
installing cargo).

Run:  python tests/test_subtitle_forge_timecode.py
"""

from __future__ import annotations

import math

# ── 1:1 port of the Rust helpers ─────────────────────────────────────────────


def _round_half_away(x: float) -> int:
    """Mimic Rust's f64::round(): halfway cases go away from zero."""
    return math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)


def to_millis(seconds: float) -> int:
    if not math.isfinite(seconds) or seconds <= 0:
        return 0
    return _round_half_away(seconds * 1000.0)


def to_centis(seconds: float) -> int:
    if not math.isfinite(seconds) or seconds <= 0:
        return 0
    return _round_half_away(seconds * 100.0)


def format_srt_time(seconds: float) -> str:
    total = to_millis(seconds)
    hours, rest = divmod(total, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_ass_time(seconds: float) -> str:
    total = to_centis(seconds)
    hours, rest = divmod(total, 360_000)
    minutes, rest = divmod(rest, 6_000)
    secs, centis = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


# ── the expectations asserted by the Rust test-suite ─────────────────────────

SRT_CASES = [
    (0.0, "00:00:00,000"),
    (1.5, "00:00:01,500"),
    (61.25, "00:01:01,250"),
    (3661.001, "01:01:01,001"),
    (3600.0, "01:00:00,000"),
    # rounding the *total* must carry, never emit ",1000"
    (59.9996, "00:01:00,000"),
    (0.9999, "00:00:01,000"),
    (3599.9995, "01:00:00,000"),
    (119.9995, "00:02:00,000"),
    # degenerate inputs clamp to zero
    (-5.0, "00:00:00,000"),
    (float("nan"), "00:00:00,000"),
    (float("inf"), "00:00:00,000"),
    (float("-inf"), "00:00:00,000"),
    # long files widen the hour field instead of truncating
    (36_000.0, "10:00:00,000"),
    (36_000.001, "10:00:00,001"),
    (3.25, "00:00:03,250"),
    (2.0, "00:00:02,000"),
]

ASS_CASES = [
    (0.0, "0:00:00.00"),
    (1.5, "0:00:01.50"),
    (61.25, "0:01:01.25"),
    (3661.5, "1:01:01.50"),
    (3600.0, "1:00:00.00"),
    (59.996, "0:01:00.00"),
    (0.996, "0:00:01.00"),
    (35_999.996, "10:00:00.00"),
    (1.995, "0:00:02.00"),
    (-1.0, "0:00:00.00"),
    (float("nan"), "0:00:00.00"),
]


def naive_srt(seconds: float) -> str:
    """Reproduce mazinger's field-by-field rounding (transcribe.py/_fmt_srt_time).

    Used only to demonstrate *why* rounding the total first is required.
    """
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    sec = int(seconds % 60)
    ms = int(round((seconds % 1) * 1000))
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def main() -> int:
    failures: list[str] = []

    for seconds, want in SRT_CASES:
        got = format_srt_time(seconds)
        if got != want:
            failures.append(f"SRT {seconds!r}: got {got}, expected {want}")

    for seconds, want in ASS_CASES:
        got = format_ass_time(seconds)
        if got != want:
            failures.append(f"ASS {seconds!r}: got {got}, expected {want}")

    # ── demonstrate the divergence from the Python helper ────────────────────
    print("where rounding-the-total differs from mazinger's Python helper:")
    for seconds in (59.9996, 0.9999, 3599.9995, 119.9995):
        old, new = naive_srt(seconds), format_srt_time(seconds)
        print(f"  {seconds:>10}  python={old}  forge={new}  {'same' if old == new else 'DIFFERS'}")

    # ── sweep for format invariants ──────────────────────────────────────────
    violations = 0
    for i in range(400_000):
        seconds = i / 977.0
        _, m, rest = format_srt_time(seconds).split(":")
        secs, ms = rest.split(",")
        if int(m) > 59 or int(secs) > 59 or int(ms) > 999:
            violations += 1
        h, m, rest = format_ass_time(seconds).split(":")
        secs, cs = rest.split(".")
        if int(m) > 59 or int(secs) > 59 or int(cs) > 99:
            violations += 1
    if violations:
        failures.append(f"{violations} format invariant violations in sweep")

    print(f"sweep: 400 000 samples, {violations} invariant violations")

    if failures:
        print(f"\nFAIL ({len(failures)})")
        for line in failures:
            print("  -", line)
        return 1
    print(f"\nPASS ({len(SRT_CASES) + len(ASS_CASES)} assertions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
