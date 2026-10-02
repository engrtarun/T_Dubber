"""SRT subtitle parsing and formatting utilities."""

from __future__ import annotations

import re


def time_to_seconds(ts: str) -> float:
    """Convert ``HH:MM:SS,mmm`` to fractional seconds."""
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def format_time(seconds: float) -> str:
    """Convert fractional seconds to ``HH:MM:SS,mmm``."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds % 1) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


_LLM_TAG_RE = re.compile(
    r"</?(?:index|translated[_ ]?text|start|end|subtitle|entry|segment|original[_ ]?text)>",
    re.IGNORECASE,
)
_TS_RE = re.compile(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})")


def sanitize(srt: str) -> str:
    """Strip common LLM artifacts from SRT output and rebuild valid structure."""
    srt = _LLM_TAG_RE.sub("", srt)
    srt = re.sub(r"```(?:srt)?", "", srt)

    lines = [ln.strip() for ln in srt.splitlines()]

    entries: list[tuple[str, str, list[str]]] = []
    pending_idx: str | None = None
    auto = 0

    for line in lines:
        if not line:
            continue
        ts_m = _TS_RE.match(line)
        if ts_m:
            auto += 1
            idx = pending_idx if pending_idx is not None else str(auto)
            entries.append((idx, line, []))
            pending_idx = None
        elif line.isdigit() and not entries or (line.isdigit() and entries and not _TS_RE.match(line)):
            pending_idx = line
            auto = int(line)
        elif entries:
            entries[-1][2].append(line)

    parts: list[str] = []
    for idx, ts, text_lines in entries:
        if text_lines:
            parts.append(f"{idx}\n{ts}\n{' '.join(text_lines)}\n")
    return "\n".join(parts) + "\n" if parts else "\n"


def parse_blocks(srt: str) -> list[tuple[str, float, float, str]]:
    """Parse SRT text into ``(index, start_sec, end_sec, text)`` tuples."""
    blocks: list[tuple[str, float, float, str]] = []
    for block in re.split(r"\n\n+", srt.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        idx = lines[0].strip()
        m = re.match(r"([\d:,]+)\s*-->\s*([\d:,]+)", lines[1])
        if not m:
            continue
        start = time_to_seconds(m.group(1))
        end = time_to_seconds(m.group(2))
        text = "\n".join(lines[2:])
        blocks.append((idx, start, end, text))
    return blocks


def blocks_to_text(blocks: list[tuple[str, float, float, str]]) -> str:
    """Reconstruct SRT string from ``(index, start, end, text)`` tuples."""
    parts: list[str] = []
    for idx, start, end, text in blocks:
        parts.append(f"{idx}\n{format_time(start)} --> {format_time(end)}\n{text}\n")
    return "\n".join(parts)


def parse_file(path: str) -> list[dict]:
    """Read an SRT file and return a list of dicts with ``idx``, ``start``,
    ``end``, and ``text`` keys."""
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    entries: list[dict] = []
    for block in re.split(r"\n\n+", content.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        idx = lines[0].strip()
        m = re.match(r"([\d:,]+)\s*-->\s*([\d:,]+)", lines[1])
        if not m:
            continue
        start = time_to_seconds(m.group(1))
        end = time_to_seconds(m.group(2))
        text = " ".join(lines[2:])
        entries.append({"idx": idx, "start": start, "end": end, "text": text})
    return entries


def build(entries: list[tuple[float, float, str]], wrap_at: int = 42) -> str:
    """Build an SRT string from ``(start, end, text)`` tuples.

    Long lines are optionally wrapped near the midpoint for display.
    """
    lines: list[str] = []
    for i, (start, end, text) in enumerate(entries, 1):
        lines.append(str(i))
        lines.append(f"{format_time(start)} --> {format_time(end)}")
        if wrap_at and len(text) > wrap_at:
            mid = len(text) // 2
            left = text.rfind(" ", 0, mid + 10)
            right = text.find(" ", mid - 10)
            if left == -1:
                left = mid
            if right == -1:
                right = mid
            split_at = left if abs(left - mid) <= abs(right - mid) else right
            lines.append(text[:split_at].strip())
            lines.append(text[split_at:].strip())
        else:
            lines.append(text)
        lines.append("")
    return "\n".join(lines)
