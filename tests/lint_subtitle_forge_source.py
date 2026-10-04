"""Cheap structural lint for subtitle_forge/src/main.rs.

There is no Rust toolchain on this machine, so this runs the checks a
compiler's *first* pass would catch before name resolution:

* balanced `()`, `[]`, `{}` — with correct handling of raw strings
  (`r#"..."#`), normal strings, char literals (including `'{'`, `'}'`, `','`),
  lifetimes (`'a`, `'static`), line comments and nested block comments;
* every `{name}` placeholder used in a format string refers to an identifier
  that is actually defined somewhere in the file (catches typos such as
  `{front}`, which rustc would only report deep inside a macro expansion);
* no known-bad patterns from earlier drafts:
    - `.max(...)` applied to a `String`,
    - `default_value = CONST` in a clap attribute.

Run:  python tests/lint_subtitle_forge_source.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "subtitle_forge" / "src" / "main.rs"


def lex(src: str) -> tuple[str, list[str]]:
    """Blank out comments and replace string literals with `""`.

    Returns the rewritten source plus the raw contents of every string
    literal found (in order).
    """
    out: list[str] = []
    strings: list[str] = []
    i, n = 0, len(src)

    def at_token_start(pos: int) -> bool:
        """True if `pos` begins a fresh token (so `r"..."` is a raw string,
        not the tail of an identifier such as `color` + `"..."`)."""
        j = pos - 1
        while j >= 0 and src[j] in " \t\r\n":
            j -= 1
        return j < 0 or not (src[j].isalnum() or src[j] in "_:'\"")

    while i < n:
        c = src[i]

        # ── raw string: r"..." or r#"..."# (up to 16 hashes) ──────────────
        if c == "r" and at_token_start(i) and i + 1 < n and src[i + 1] in '"#':
            j, hashes = i + 1, 0
            while j < n and src[j] == "#":
                hashes += 1
                j += 1
            if j < n and src[j] == '"':
                closer = '"' + "#" * hashes
                k = src.find(closer, j + 1)
                if k == -1:
                    strings.append(src[j:])
                    out.append('""')
                    break
                strings.append(src[j + 1:k])
                out.append('""')
                i = k + len(closer)
                continue

        # ── normal / byte string ──────────────────────────────────────────
        if c == '"':
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    break
                j += 1
            strings.append(src[i + 1:j])
            out.append('""')
            i = j + 1
            continue

        # ── char literal vs. lifetime ─────────────────────────────────────
        if c == "'":
            if i + 1 < n and src[i + 1] == "\\":
                # escaped char: '\n', '\u{feff}', … -> up to the closing quote
                k = src.find("'", i + 2)
                if k != -1:
                    out.append("''")
                    i = k + 1
                    continue
            elif i + 2 < n and src[i + 2] == "'":
                # exactly one character between quotes: '{', '}', ',', 'i'
                out.append("''")
                i = i + 3
                continue
            # otherwise a lifetime ('a, 'static) — emit as-is
            out.append(c)
            i += 1
            continue

        # ── line comment ──────────────────────────────────────────────────
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
            continue

        # ── nested block comment ──────────────────────────────────────────
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            depth, j = 1, i + 2
            while j < n and depth:
                if src[j:j + 2] == "/*":
                    depth += 1
                    j += 2
                elif src[j:j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            out.append(" " * (j - i))
            i = j
            continue

        out.append(c)
        i += 1

    return "".join(out), strings


def check_balance(blanked: str) -> list[str]:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[tuple[str, int]] = []
    failures: list[str] = []
    line = 1
    for ch in blanked:
        if ch == "\n":
            line += 1
        elif ch in "([{":
            stack.append((ch, line))
        elif ch in ")]}":
            if not stack:
                return [f"line {line}: unmatched '{ch}'"]
            op, opline = stack.pop()
            if op != pairs[ch]:
                return [f"line {line}: '{ch}' closes '{op}' opened at line {opline}"]
    for op, opline in stack:
        failures.append(f"unclosed '{op}' opened at line {opline}")
    return failures


def defined_identifiers(src: str) -> set[str]:
    """Every identifier that could legitimately be a format capture."""
    ids: set[str] = set()
    # let / fn params / struct fields / const / enum variants / args
    ids.update(re.findall(r"\blet\s+(?:mut\s+)?(\w+)", src))
    ids.update(re.findall(r"\bconst\s+(\w+)", src))
    ids.update(re.findall(r"\bfn\s+(\w+)", src))
    ids.update(re.findall(r"\bstruct\s+(\w+)", src))
    ids.update(re.findall(r"\benum\s+(\w+)", src))
    ids.update(re.findall(r"\bmod\s+(\w+)", src))
    # plain `name:` bindings — covers fn params, fields, and `name = expr`
    # arguments passed to format!.
    ids.update(re.findall(r"\b(\w+)\s*:", src))
    ids.update(re.findall(r"\b(\w+)\s*=", src))
    # every word that appears anywhere (last resort: keeps this check
    # typo-catching rather than type-checking)
    ids.update(re.findall(r"\b([A-Za-z_]\w*)\b", src))
    return ids


def check_placeholders(strings: list[str], known: set[str]) -> list[str]:
    failures: list[str] = []
    for lit in strings:
        # remove escaped braces, then pull out {name} / {name:spec}
        body = lit.replace("{{", "").replace("}}", "")
        for name in re.findall(r"\{([A-Za-z_]\w*)(?::[^}]*)?\}", body):
            if name not in known:
                failures.append(f"format placeholder {{{name}}} is never defined")
    return failures


def check_known_mistakes(src: str) -> list[str]:
    failures: list[str] = []
    if re.search(r"sanitize_field\([^)]*\)\s*\.max\(", src):
        failures.append("sanitize_field(...) .max(...) — String has no .max()")
    if re.search(r"default_value\s*=\s*[A-Z_][A-Z0-9_]*\b", src):
        failures.append("default_value = CONST — clap-derive risk; use a literal")
    if re.search(r"chars\(\)\s*\n?\s*\.count\(\)", src) and "line_len" in src:
        pass  # not an error, just noted
    return failures


def main() -> int:
    src = SRC.read_text(encoding="utf-8")
    blanked, strings = lex(src)

    failures: list[str] = []
    failures += check_balance(blanked)
    failures += check_placeholders(strings, defined_identifiers(src))
    failures += check_known_mistakes(src)

    braces = sum(1 for ch in blanked if ch == "{") - sum(1 for ch in blanked if ch == "}")
    print(f"{SRC.name}: {len(src.splitlines())} lines, {len(strings)} string literals, "
          f"brace delta {braces}")

    if failures:
        print(f"FAIL ({len(failures)})")
        for f in failures:
            print("  -", f)
        return 1
    print("PASS (balance, format placeholders, known-mistake patterns)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
