"""The smallest HCL2 reader that can hold a manifest.

Why this exists
---------------
``infra/manifest.auto.tfvars`` is the single source of truth for this project's
runtime wiring, and it is written in HCL so that ``terraform`` can consume the
same bytes unchanged the day the project gets a real provider (see
``infra/README.md``). But terraform is not installed on the workstation that
runs the pipeline, and a Kaggle kernel does not have it either -- so a tool
that could only read the manifest through ``terraform console`` would be
unusable at exactly the moment it matters: pre-flight, on the machine, before
a kernel starts.

So the manifest is parsed here instead. That is only defensible if the parser
is small enough to be obviously correct, which is why this module supports a
deliberately tiny subset of HCL2 and refuses everything else loudly rather
than guessing.

Supported
---------
* line comments (``#``, ``//``) and block comments (``/* ... */``)
* attributes: ``key = expression``
* blocks: ``type "label" { ... }``, nested arbitrarily deep
* values: quoted strings, integers, floats, ``true``/``false``/``null``,
  lists ``[...]`` and objects ``{ k = v }``

Refused
-------
Interpolation (``${...}``), heredocs, function calls, operators and
``for``-expressions. Each raises :class:`HclError` naming the line, because a
manifest that silently misparses is worse than one that refuses to load.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "HclBlock",
    "HclBody",
    "HclDocument",
    "HclError",
    "load_file",
    "loads",
]


class HclError(ValueError):
    """The text is not in the HCL subset this module supports."""

    def __init__(self, message: str, origin: str, line: int) -> None:
        self.origin = origin
        self.line = line
        super().__init__(f"{origin}:{line}: {message}")


@dataclass(frozen=True)
class HclBlock:
    """One ``type "label" { ... }`` and everything inside it."""

    type: str
    labels: tuple[str, ...]
    body: HclBody


@dataclass(frozen=True)
class HclBody:
    """Attributes and nested blocks of one brace level.

    Splitting the two matters because they mean different things in Terraform:
    an attribute is a value, a block is a container that may appear several
    times. Collapsing them into one mapping is what makes naive HCL readers
    lose the second ``variable`` block.
    """

    attributes: dict[str, object]
    blocks: tuple[HclBlock, ...]


@dataclass(frozen=True)
class HclDocument:
    """A whole file: its top-level body, and where it came from."""

    body: HclBody
    origin: str


# Characters that end an identifier. HCL identifiers may contain dashes, which
# is why '-' is a terminator here but may appear *inside* one.
_IDENT_STOP = frozenset(' \t\r\n={},[]()"#/*')

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
    "/": "/",
    "'": "'",
}


class _Parser:
    """Recursive descent over the source text.

    Kept as a private class with an explicit integer cursor rather than a
    regex soup: every branch that consumes input is visible in one place, which
    is the only property that makes a hand-written parser reviewable.
    """

    def __init__(self, text: str, origin: str) -> None:
        self._text = text
        self._origin = origin
        self._pos = 0

    # -- position helpers ---------------------------------------------------

    def _line(self, pos: int | None = None) -> int:
        """1-based line number of ``pos`` (default: the cursor)."""
        at = self._pos if pos is None else pos
        return self._text.count("\n", 0, at) + 1

    def _fail(self, message: str, pos: int | None = None) -> HclError:
        return HclError(message, self._origin, self._line(pos))

    def _skip(self) -> None:
        """Advance past whitespace and every kind of comment."""
        while self._pos < len(self._text):
            char = self._text[self._pos]
            if char in " \t\r\n":
                self._pos += 1
            elif char == "#" or (char == "/" and self._peek(1) == "/"):
                self._skip_line()
            elif char == "/" and self._peek(1) == "*":
                self._skip_block_comment()
            else:
                return

    def _skip_line(self) -> None:
        end = self._text.find("\n", self._pos)
        self._pos = len(self._text) if end < 0 else end + 1

    def _skip_block_comment(self) -> None:
        start = self._pos
        end = self._text.find("*/", start + 2)
        if end < 0:
            raise self._fail("unterminated block comment", start)
        self._pos = end + 2

    def _peek(self, ahead: int = 0) -> str:
        at = self._pos + ahead
        return self._text[at] if at < len(self._text) else ""

    def _expect(self, char: str) -> None:
        if self._peek() != char:
            found = self._peek() or "end of file"
            raise self._fail(f"expected {char!r}, found {found!r}")
        self._pos += 1

    # -- entry point --------------------------------------------------------

    def parse_document(self) -> HclDocument:
        self._skip()
        if self._pos >= len(self._text):
            return HclDocument(HclBody({}, ()), self._origin)
        body = self._parse_body(top_level=True)
        self._skip()
        if self._pos < len(self._text):
            raise self._fail(f"trailing text {self._peek()!r}")
        return HclDocument(body, self._origin)

    def _parse_body(self, *, top_level: bool) -> HclBody:
        attributes: dict[str, object] = {}
        blocks: list[HclBlock] = []

        while True:
            self._skip()
            if self._pos >= len(self._text):
                if not top_level:
                    raise self._fail("unterminated block: expected '}'")
                break
            if self._peek() == "}":
                if top_level:
                    raise self._fail("unexpected '}' with no block open")
                self._pos += 1
                break
            # HCL allows both newlines and commas between the entries of an
            # object literal, so `{ k = 1, j = 2 }` and the two-line form are
            # the same document. Commas are only legal inside a brace level --
            # at the top level they are a typo, and the next identifier parse
            # is what reports it.
            if not top_level and self._peek() == ",":
                self._pos += 1
                continue
            self._parse_item(attributes, blocks)

        return HclBody(attributes, tuple(blocks))

    def _parse_item(self, attributes: dict[str, object], blocks: list[HclBlock]) -> None:
        """Parse one ``key = value`` or one ``type labels { ... }``."""
        key = self._parse_identifier()

        # A bare identifier followed by '=' is an attribute. Anything else --
        # '{' or a quoted label -- opens a block.
        self._skip_inline()
        if self._peek() == "=" and self._peek(1) != "=":
            self._expect("=")
            attributes[key] = self._parse_value()
            return

        labels: list[str] = []
        while self._peek() == '"':
            labels.append(self._parse_quoted())
            self._skip_inline()

        if self._peek() != "{":
            found = self._peek() or "end of file"
            raise self._fail(f"expected '=' or '{{' after {key!r}, found {found!r}")
        self._expect("{")
        body = self._parse_body(top_level=False)
        blocks.append(HclBlock(key, tuple(labels), body))

    def _skip_inline(self) -> None:
        """Whitespace only, so that ``key /* c */ = v`` stays legal."""
        while self._pos < len(self._text) and self._text[self._pos] in " \t":
            self._pos += 1

    def _parse_identifier(self) -> str:
        start = self._pos
        while self._pos < len(self._text):
            char = self._text[self._pos]
            if char in _IDENT_STOP:
                break
            self._pos += 1
        if self._pos == start:
            found = _char_or_end(self._text, self._pos)
            raise self._fail(f"expected a name, found {found!r}")
        return self._text[start : self._pos]

    # -- values -------------------------------------------------------------

    def _parse_value(self) -> object:
        self._skip()
        char = self._peek()
        if char == '"':
            return self._parse_quoted()
        if char == "[":
            return self._parse_list()
        if char == "{":
            return self._parse_object()
        if char == "$":
            raise self._fail("interpolation is not supported; a manifest must be literal")
        if char == "<":
            raise self._fail("heredocs are not supported")
        if char == "-" or char.isdigit():
            return self._parse_number()
        if self._text.startswith("true", self._pos):
            self._pos += 4
            return True
        if self._text.startswith("false", self._pos):
            self._pos += 5
            return False
        if self._text.startswith("null", self._pos):
            self._pos += 4
            return None
        found = _char_or_end(self._text, self._pos)
        raise self._fail(
            f"unsupported value starting with {found!r}; functions and operators are not supported"
        )

    def _parse_quoted(self) -> str:
        self._expect('"')
        out: list[str] = []
        while True:
            if self._pos >= len(self._text):
                raise self._fail("unterminated string")
            char = self._text[self._pos]
            if char == '"':
                self._pos += 1
                return "".join(out)
            if char == "\\":
                self._pos += 1
                if self._pos >= len(self._text):
                    raise self._fail("unterminated escape")
                code = self._text[self._pos]
                if code not in _ESCAPES:
                    raise self._fail(f"unsupported escape \\{code}")
                out.append(_ESCAPES[code])
                self._pos += 1
                continue
            if char == "$" and self._peek(1) == "{":
                raise self._fail("interpolation is not supported inside a string")
            if char == "\n":
                raise self._fail("a string may not span lines")
            out.append(char)
            self._pos += 1

    def _parse_number(self) -> int | float:
        start = self._pos
        if self._peek() == "-":
            self._pos += 1
        is_float = False
        while self._pos < len(self._text):
            char = self._text[self._pos]
            if char.isdigit():
                self._pos += 1
            elif char == "." and not is_float:
                is_float = True
                self._pos += 1
            elif char in "eE" and self._peek(1) != "":
                is_float = True
                self._pos += 2
            else:
                break
        raw = self._text[start : self._pos]
        try:
            return float(raw) if is_float else int(raw)
        except ValueError:
            raise self._fail(f"{raw!r} is not a number", start) from None

    def _parse_list(self) -> list[object]:
        self._expect("[")
        items: list[object] = []
        while True:
            self._skip()
            if self._pos >= len(self._text):
                raise self._fail("unterminated list: expected ']'")
            if self._peek() == "]":
                self._pos += 1
                return items
            items.append(self._parse_value())
            self._skip()
            if self._peek() == ",":
                self._pos += 1
                continue
            self._skip()
            if self._peek() != "]":
                raise self._fail("expected ',' or ']' in list")

    def _parse_object(self) -> dict[str, object]:
        self._expect("{")
        # An object literal and a block body have the same syntax, so this reuses
        # _parse_body wholesale and takes only the attributes: a nested block
        # inside '{ }' is not valid HCL, and _parse_body will have refused it.
        body = self._parse_body(top_level=False)
        if body.blocks:
            raise self._fail("a block is not valid inside an object literal")
        return dict(body.attributes)


def _char_or_end(text: str, pos: int) -> str:
    """The character at ``pos``, or a readable stand-in at end of file."""
    return text[pos] if pos < len(text) else "end of file"


def loads(text: str, origin: str = "<string>") -> HclDocument:
    """Parse HCL source text. ``origin`` only decorates error messages."""
    return _Parser(text, origin).parse_document()


def load_file(path: Path) -> HclDocument:
    """Parse the HCL file at ``path``. UTF-8, because paths go in here."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HclError(str(exc), str(path), 0) from exc
    return loads(text, str(path))
