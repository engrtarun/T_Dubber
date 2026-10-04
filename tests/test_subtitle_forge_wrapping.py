"""Cross-check the line-wrapping algorithm used by subtitle_forge/src/main.rs.

No Rust toolchain is available, so `greedy_wrap` / `wrap_text` are ported here
1:1 and checked against brute force:

* greedy line-breaking is asserted to be *optimal* for line count (this is the
  fact that invalidates a naive "re-balance to hit max_lines" strategy);
* the binary-search width relaxation in `wrap_text` is compared against an
  exhaustive search for the minimum width, over randomised inputs;
* no input may ever lose or gain words.

Run:  python tests/test_subtitle_forge_wrapping.py
"""

from __future__ import annotations

import random

# ── 1:1 port of the Rust helpers ─────────────────────────────────────────────


def greedy_wrap(words: list[str], max_chars: int) -> list[str]:
    lines: list[str] = []
    line = ""
    line_len = 0
    for word in words:
        word_len = len(word)
        if not line:
            line, line_len = word, word_len
        elif line_len + 1 + word_len <= max_chars:
            line += " " + word
            line_len += 1 + word_len
        else:
            lines.append(line)
            line, line_len = word, word_len
    if line:
        lines.append(line)
    return lines


def wrap_text(text: str, max_chars: int, max_lines: int) -> list[str]:
    max_lines = max(max_lines, 1)
    words = text.split()
    if not words:
        return []
    if max_chars == 0:
        return [text]

    greedy = greedy_wrap(words, max_chars)
    if len(greedy) <= max_lines:
        return greedy

    total = sum(len(w) for w in words) + len(words) - 1
    lo, hi = max_chars, total
    while lo < hi:
        mid = lo + (hi - lo) // 2
        if len(greedy_wrap(words, mid)) <= max_lines:
            hi = mid
        else:
            lo = mid + 1
    return greedy_wrap(words, lo)


def min_width_bruteforce(words: list[str], max_lines: int) -> int:
    """Exhaustive reference for the minimum width that fits in max_lines."""
    total = sum(len(w) for w in words) + len(words) - 1
    for width in range(1, total + 1):
        if len(greedy_wrap(words, width)) <= max_lines:
            return width
    raise AssertionError("unreachable: width = total always yields 1 line")


def min_lines_dp(words: list[str], max_chars: int) -> int:
    """Globally optimal (dynamic-programming) line count at a fixed width.

    `dp[i]` = fewest lines needed to place `words[:i]`, taking `dp[0] = 0` and
    `dp[i] = 1 + min(dp[j])` over every `j` whose chunk `words[j:i]` fits.
    This is the reference that `greedy_wrap` is asserted against: greedy
    minimises the line count *at a given width*, it just does not optimise
    for even-looking lines.
    """
    n = len(words)
    dp = [0] + [10**9] * n
    for i in range(1, n + 1):
        width = 0
        for j in range(i - 1, -1, -1):
            width += len(words[j]) + (1 if j != i - 1 else 0)
            # A single word wider than the budget still gets its own line —
            # that is `greedy_wrap`'s documented behaviour, so the reference
            # model has to allow it too.
            if width > max_chars and j != i - 1:
                break
            dp[i] = min(dp[i], dp[j] + 1)
    return dp[n]


def line_count_is_monotone(words: list[str], limit: int) -> bool:
    prev = 10**9
    for width in range(1, limit + 1):
        n = len(greedy_wrap(words, width))
        if n > prev:
            return False
        prev = n
    return True


def main() -> int:
    failures: list[str] = []
    checks = 0

    def check(cond: bool, msg: str) -> None:
        nonlocal checks
        checks += 1
        if not cond:
            failures.append(msg)

    # ── the exact cases asserted by the Rust test-suite ─────────────────────
    check(wrap_text("short cue", 42, 2) == ["short cue"], "simple fit")

    boundary = "one two three four five six seven eight nine ten eleven twelve"
    lines = wrap_text(boundary, 20, 10)
    check(len(lines) > 1, f"expected multiple lines: {lines}")
    check(all(len(l) <= 20 for l in lines), f"line too long: {lines}")
    check(" ".join(lines) == boundary, "words lost/duplicated")

    words = "word ".split() * 40
    long_text = " ".join(words)
    lines = wrap_text(long_text, 42, 2)
    check(len(lines) == 2, f"expected widening to 2 lines, got {len(lines)}: {lines}")
    check(" ".join(lines) == long_text, "words lost by widening")

    check(
        wrap_text("a very long cue that would otherwise be split", 0, 2)
        == ["a very long cue that would otherwise be split"],
        "max_chars=0 disables wrapping",
    )
    check(
        wrap_text("Supercalifragilisticexpialidocious", 10, 2)
        == ["Supercalifragilisticexpialidocious"],
        "oversized single word stays whole",
    )
    check(wrap_text("", 42, 2) == [], "empty input")

    multi = "first half second half third half fourth"
    lines = wrap_text(multi, 20, 2)
    check(len(lines) == 2, f"multi-line ASS cue should fit in 2 lines: {lines}")
    check(all(len(l) <= 22 for l in lines), f"over-widened: {lines}")
    check(" ".join(lines) == multi, "words lost in ASS cue")

    # ── greedy must be optimal for line count at a fixed width ──────────────
    # This is why a "re-balance at average width to hit max_lines" strategy
    # can never reduce the line count, and why `wrap_text` relaxes *width*
    # instead of trying to squeeze the cue into fewer lines.
    rng = random.Random(20261005)
    for _ in range(400):
        ws = ["".join(rng.choice("abcd") for _ in range(rng.randint(1, 30)))
              for _ in range(rng.randint(1, 18))]
        width = rng.randint(5, 60)
        greedy_n = len(greedy_wrap(ws, width))
        optimal_n = min_lines_dp(ws, width)
        check(greedy_n == optimal_n,
              f"greedy not optimal: {greedy_n} vs {optimal_n} for {ws} @ {width}")

    # ── binary search must equal exhaustive search, and be monotone ─────────
    for trial in range(500):
        ws = ["".join(rng.choice("abcdefgh") for _ in range(rng.randint(1, 40)))
              for _ in range(rng.randint(1, 30))]
        max_lines = rng.randint(1, 4)
        max_chars = rng.randint(1, 60)
        check(line_count_is_monotone(ws, 400),
              f"line count not monotone in width for {ws}")
        got = wrap_text(" ".join(ws), max_chars, max_lines)
        check(len(got) <= max_lines,
              f"exceeded max_lines={max_lines}: {got}")
        if len(greedy_wrap(ws, max_chars)) > max_lines:
            want = min_width_bruteforce(ws, max_lines)
            check(len(greedy_wrap(ws, want)) == len(got),
                  f"binary search != brute force (wanted width {want}) for {ws}")
        check(" ".join(got) == " ".join(ws),
              f"words changed: {got} vs {ws}")
        check(all(len(l) == len(l.strip()) for l in got),
              f"stray whitespace in {got}")

    if failures:
        print(f"FAIL ({len(failures)} of {checks} checks)")
        for line in failures[:20]:
            print("  -", line)
        return 1
    print(f"PASS ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
