"""Burn subtitles into a video using ffmpeg."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_POSITIONS = {"bottom": 2, "top": 8, "center": 5}

_nvenc_available: bool | None = None


def _has_nvenc() -> bool:
    """Check whether ffmpeg can actually encode with h264_nvenc."""
    global _nvenc_available
    if _nvenc_available is None:
        try:
            r = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.1",
                    "-c:v", "h264_nvenc", "-f", "null", "-",
                ],
                capture_output=True, timeout=10,
            )
            _nvenc_available = r.returncode == 0
        except Exception:
            _nvenc_available = False
    return _nvenc_available


def _video_encode_args() -> list[str]:
    """Return ffmpeg video-codec arguments, preferring NVENC when available."""
    if _has_nvenc():
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "23"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"]

# Unicode ranges for Arabic, Arabic Supplement, Arabic Extended,
# Arabic Presentation Forms A/B, and Farsi-specific characters.
_RTL_RE = re.compile(
    "[\u0600-\u06FF"     # Arabic
    "\u0750-\u077F"      # Arabic Supplement
    "\u08A0-\u08FF"      # Arabic Extended-A
    "\uFB50-\uFDFF"      # Arabic Presentation Forms-A
    "\uFE70-\uFEFF"      # Arabic Presentation Forms-B
    "\U00010D00-\U00010D3F"  # Arabic Extended-C (Hanifi Rohingya)
    "]"
)

# Unicode directional markers.
_RLE = "\u202B"   # Right-to-Left Embedding
_PDF = "\u202C"   # Pop Directional Formatting

_NAMED_COLORS = {
    "white": "FFFFFF", "black": "000000", "yellow": "FFFF00",
    "red": "FF0000", "green": "00FF00", "blue": "0000FF",
    "cyan": "00FFFF", "magenta": "FF00FF", "gray": "808080",
}


def _parse_color(value: str) -> str:
    """Normalise a color name or ``#RRGGBB`` hex string to bare ``RRGGBB``."""
    low = value.strip().lower()
    if low in _NAMED_COLORS:
        return _NAMED_COLORS[low]
    stripped = value.strip().lstrip("#")
    if len(stripped) == 6 and all(c in "0123456789abcdefABCDEF" for c in stripped):
        return stripped.upper()
    raise ValueError(f"Unsupported color: {value!r} — use a name or #RRGGBB hex.")


def _to_ass_color(rgb: str, opacity: float = 1.0) -> str:
    """Convert ``RRGGBB`` + opacity (1=opaque, 0=transparent) to ASS ``&HAABBGGRR``."""
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    a = f"{int((1.0 - opacity) * 255):02X}"
    return f"&H{a}{b}{g}{r}"


def _escape_filter_path(path: str) -> str:
    """Escape a file path for an ffmpeg filter expression (within single quotes)."""
    return path.replace("\\", "\\\\").replace("'", "'\\''")


def _detect_font_family(font_path: str) -> str | None:
    """Return the font family name embedded in a TTF/OTF file.

    Uses *fontTools* when available, otherwise falls back to the filename stem
    with common weight suffixes stripped.
    """
    try:
        from fontTools.ttLib import TTFont

        font = TTFont(font_path)
        for record in font["name"].names:
            if record.nameID == 1:  # Font Family
                try:
                    return record.toUnicode()
                except Exception:
                    continue
    except Exception:
        pass
    # Fallback: filename stem, strip weight suffixes.
    stem = os.path.splitext(os.path.basename(font_path))[0]
    for suffix in (
        "-Regular", "-Bold", "-Italic", "-Light", "-Medium",
        "-Thin", "-SemiBold", "-ExtraBold", "-Black", "-ExtraLight",
    ):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem.replace("-", " ") or None


def _find_font_file(directory: str) -> str | None:
    """Find the best TTF/OTF file in *directory* (recursive).

    Prefers variable fonts, then Regular weights, then the first match.
    """
    if not os.path.isdir(directory):
        return None
    candidates: list[str] = []
    for root, _, files in os.walk(directory):
        for f in files:
            if f.lower().endswith((".ttf", ".otf")):
                candidates.append(os.path.join(root, f))
    if not candidates:
        return None
    for c in candidates:
        if "variable" in os.path.basename(c).lower():
            return c
    for c in candidates:
        if "regular" in os.path.basename(c).lower():
            return c
    return candidates[0]


def download_google_font(font_name: str, cache_dir: str | None = None) -> str:
    """Download a Google Font by name and return the path to a TTF/OTF file.

    Uses the Google Fonts CSS API to discover direct TTF download URLs.
    Results are cached under *cache_dir* (default ``/tmp/mazinger-fonts/``).
    """
    import urllib.parse
    import urllib.request

    if cache_dir is None:
        cache_dir = os.path.join(tempfile.gettempdir(), "mazinger-fonts")
    safe_name = font_name.replace(" ", "_")
    font_dir = os.path.join(cache_dir, safe_name)

    cached = _find_font_file(font_dir)
    if cached:
        log.info("Using cached Google Font: %s", cached)
        return cached

    os.makedirs(font_dir, exist_ok=True)

    # The CSS API serves TTF URLs when the User-Agent is an older browser.
    css_url = (
        "https://fonts.googleapis.com/css2?family="
        + urllib.parse.quote(font_name)
    )
    log.info("Downloading Google Font '%s' …", font_name)
    req = urllib.request.Request(
        css_url, headers={"User-Agent": "Mozilla/4.0"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            css = resp.read().decode()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to fetch Google Font '{font_name}'. "
            "Check the name matches one on https://fonts.google.com."
        ) from exc

    # Extract .ttf / .otf URLs from the CSS @font-face rules.
    ttf_urls = re.findall(r"url\((https://[^)]+\.(?:ttf|otf))\)", css)
    if not ttf_urls:
        raise RuntimeError(
            f"No TTF/OTF URLs found in Google Fonts CSS for '{font_name}'. "
            "Check the font name matches one on https://fonts.google.com."
        )

    # Download the first TTF (regular weight).
    font_url = ttf_urls[0]
    ext = ".ttf" if font_url.endswith(".ttf") else ".otf"
    dest = os.path.join(font_dir, safe_name + ext)
    req = urllib.request.Request(font_url)
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as out:
        out.write(resp.read())

    log.info("Google Font ready: %s", dest)
    return dest


def _starts_rtl(text: str) -> bool:
    """Return ``True`` if *text* begins with an Arabic / Farsi character.

    Ignores leading whitespace, digits, and punctuation.
    """
    for ch in text:
        if _RTL_RE.match(ch):
            return True
        # Skip whitespace, digits, common punctuation — keep scanning.
        if ch.isspace() or ch.isdigit() or not ch.isalpha():
            continue
        return False  # First alphabetic char is not RTL.
    return False


def _prepare_rtl_srt(srt_path: str, position: str) -> str | None:
    """Return the path to a temp SRT with per-entry RTL markers, or ``None``.

    Scans every subtitle entry.  If *any* entry begins with an RTL character
    its text lines are wrapped with Unicode RLE/PDF directional markers so
    the text renders right-to-left while keeping the same centered alignment
    as LTR entries.

    Returns ``None`` when no RTL entries are found (no temp file created).
    """
    with open(srt_path, encoding="utf-8") as fh:
        content = fh.read()

    blocks = re.split(r"\n\n+", content.strip())
    changed = False
    new_blocks: list[str] = []

    for block in blocks:
        lines = block.split("\n")
        # SRT block: index, timestamp, text line(s)…
        if len(lines) >= 3:
            text_lines = lines[2:]
            first_text = "".join(text_lines).strip()
            if _starts_rtl(first_text):
                changed = True
                # Wrap text lines with Unicode RLE/PDF for RTL rendering.
                # No ASS alignment override — keep the global centered position.
                lines[2] = _RLE + lines[2]
                lines[-1] = lines[-1] + _PDF
        new_blocks.append("\n".join(lines))

    if not changed:
        return None

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".srt", encoding="utf-8", delete=False,
    )
    tmp.write("\n\n".join(new_blocks) + "\n")
    tmp.close()
    log.info("Prepared RTL-aware SRT: %s", tmp.name)
    return tmp.name


def _prepare_line_spacing(srt_path: str, spacing: int) -> str | None:
    """Return a temp SRT with extra vertical padding between wrapped lines.

    libass has no native line-spacing style property.  This inserts a tiny
    transparent line (``{\\fs<N>\\alpha&HFF&} ``) between text lines in each
    subtitle block, which acts as a vertical spacer.

    Returns ``None`` when *spacing* is 0 (no temp file created).
    """
    if spacing <= 0:
        return None

    with open(srt_path, encoding="utf-8") as fh:
        content = fh.read()

    # Invisible spacer line: tiny font, fully transparent, single space.
    spacer = f"{{\\fs{spacing}\\alpha&HFF&}} "

    blocks = re.split(r"\n\n+", content.strip())
    new_blocks: list[str] = []

    for block in blocks:
        lines = block.split("\n")
        if len(lines) >= 3:
            # Insert spacer between every pair of text lines.
            text_lines = lines[2:]
            if len(text_lines) > 1:
                spaced: list[str] = [text_lines[0]]
                for tl in text_lines[1:]:
                    spaced.append(spacer)
                    spaced.append(tl)
                lines = lines[:2] + spaced
        new_blocks.append("\n".join(lines))

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".srt", encoding="utf-8", delete=False,
    )
    tmp.write("\n\n".join(new_blocks) + "\n")
    tmp.close()
    log.info("Prepared line-spaced SRT (spacing=%d): %s", spacing, tmp.name)
    return tmp.name


# ── Display-split: break long entries for comfortable on-screen reading ──────

# Subtitle best practice: ≤ 2 lines of ~42 chars each.  We use a higher
# character limit to account for proportional fonts, mixed scripts, and
# Arabic text where characters are narrower.
_DISPLAY_MAX_CHARS = 120

# SRT timestamp helpers
_TS_FMT = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    r"\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _ts_to_secs(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _secs_to_ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = round((t - int(t)) * 1000) % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _split_text_for_display(text: str, max_chars: int) -> list[str]:
    """Split *text* into display-friendly segments at sentence/clause boundaries."""
    if len(text) <= max_chars:
        return [text]

    # Prefer splitting at sentence-ending punctuation.
    parts = re.split(r'(?<=[.!?…؟。！？])\s+', text)
    segments = _join_parts(parts, max_chars)

    # If any segment is still too long, re-split at clause boundaries.
    final: list[str] = []
    for seg in segments:
        if len(seg) <= max_chars:
            final.append(seg)
        else:
            clause_parts = re.split(
                r'(?<=[,;،])\s+|(?<=\u2014)\s*|\s+(?=\u2014)', seg,
            )
            sub = _join_parts(clause_parts, max_chars)
            # Last resort: word wrap any still-oversized chunks.
            for chunk in sub:
                if len(chunk) <= max_chars:
                    final.append(chunk)
                else:
                    final.extend(_word_wrap(chunk, max_chars))
    return final


def _join_parts(parts: list[str], max_chars: int) -> list[str]:
    """Join *parts* greedily, keeping each result ≤ *max_chars*."""
    segments: list[str] = []
    buf = ""
    for p in parts:
        candidate = (buf + " " + p).strip() if buf else p
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            if buf:
                segments.append(buf)
            buf = p
    if buf:
        segments.append(buf)
    return segments


def _word_wrap(text: str, max_chars: int) -> list[str]:
    """Word-wrap *text* as a last resort."""
    words = text.split()
    segments: list[str] = []
    buf = ""
    for w in words:
        candidate = (buf + " " + w) if buf else w
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            if buf:
                segments.append(buf)
            buf = w
    if buf:
        segments.append(buf)
    return segments


def _prepare_display_split(
    srt_path: str, max_chars: int = _DISPLAY_MAX_CHARS,
) -> str | None:
    """Split long subtitle entries into shorter ones for comfortable on-screen display.

    Returns a temp SRT path, or ``None`` if no entries needed splitting.
    """
    with open(srt_path, encoding="utf-8") as fh:
        content = fh.read()

    blocks = re.split(r"\n\n+", content.strip())
    changed = False
    new_blocks: list[str] = []
    idx = 1

    for block in blocks:
        lines = block.split("\n")
        if len(lines) < 3:
            new_blocks.append(f"{idx}\n" + "\n".join(lines[1:]) if len(lines) > 1 else str(idx))
            idx += 1
            continue

        ts_match = _TS_FMT.search(lines[1])
        text = "\n".join(lines[2:])
        flat = " ".join(text.split())

        if not ts_match or len(flat) <= max_chars:
            new_blocks.append(f"{idx}\n" + "\n".join(lines[1:]))
            idx += 1
            continue

        # Entry needs splitting.
        changed = True
        start = _ts_to_secs(*ts_match.group(1, 2, 3, 4))
        end = _ts_to_secs(*ts_match.group(5, 6, 7, 8))
        segments = _split_text_for_display(flat, max_chars)

        total_chars = sum(len(s) for s in segments)
        cursor = start
        for seg in segments:
            proportion = len(seg) / total_chars if total_chars else 1.0 / len(segments)
            seg_dur = max(0.5, (end - start) * proportion)
            seg_end = min(cursor + seg_dur, end)
            ts_line = f"{_secs_to_ts(cursor)} --> {_secs_to_ts(seg_end)}"
            new_blocks.append(f"{idx}\n{ts_line}\n{seg}")
            idx += 1
            cursor = seg_end
        continue

    if not changed:
        return None

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".srt", encoding="utf-8", delete=False,
    )
    tmp.write("\n\n".join(new_blocks) + "\n")
    tmp.close()
    log.info("Prepared display-split SRT (max_chars=%d): %s", max_chars, tmp.name)
    return tmp.name


@dataclass
class SubtitleStyle:
    """Visual styling for burned-in subtitles."""

    font: str = "Arial"
    font_file: str | None = None
    font_size: int = 14
    font_color: str = "white"
    bg_color: str = "black"
    bg_alpha: float = 0.6
    outline_color: str = "black"
    outline_width: int = 1
    position: str = "bottom"
    margin_v: int = 20
    bold: bool = False
    line_spacing: int = 8

    def __post_init__(self) -> None:
        if self.font_file:
            if not os.path.isfile(self.font_file):
                raise FileNotFoundError(f"Font file not found: {self.font_file}")
            if self.font == "Arial":
                detected = _detect_font_family(self.font_file)
                if detected:
                    self.font = detected

    def to_force_style(self) -> str:
        """Build an ASS ``force_style`` string for the ffmpeg ``subtitles`` filter."""
        fc = _to_ass_color(_parse_color(self.font_color))
        # BorderStyle=3 (opaque box): the box is drawn with OutlineColour,
        # while BackColour only affects the shadow.  Apply the background
        # color + alpha to OutlineColour so the box opacity works correctly.
        box_color = _to_ass_color(_parse_color(self.bg_color), self.bg_alpha)
        shadow_color = _to_ass_color(_parse_color(self.bg_color), 0.0)
        alignment = _POSITIONS.get(self.position, 2)
        return ",".join([
            f"FontName={self.font}",
            f"FontSize={self.font_size}",
            f"PrimaryColour={fc}",
            f"BackColour={shadow_color}",
            f"OutlineColour={box_color}",
            f"Outline={self.outline_width}",
            "BorderStyle=3",
            "Shadow=0",
            f"Alignment={alignment}",
            f"MarginV={self.margin_v}",
            f"Bold={-1 if self.bold else 0}",
        ])


def burn_subtitles(
    video_path: str,
    output_path: str,
    srt_path: str,
    style: SubtitleStyle | None = None,
    audio_path: str | None = None,
) -> str | None:
    """Burn subtitles into *video_path*, optionally replacing the audio track.

    Uses the ffmpeg ``subtitles`` filter with ASS ``force_style`` overrides.
    When *audio_path* is given the original audio is replaced in the same
    encoding pass to avoid a double re-encode.

    Returns *output_path* on success, or ``None`` if ffmpeg is unavailable.
    """
    if shutil.which("ffmpeg") is None:
        log.warning(
            "ffmpeg not found — cannot burn subtitles.  "
            "Install ffmpeg and re-run."
        )
        return None

    if style is None:
        style = SubtitleStyle()

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # Auto-detect RTL content and download an Arabic-capable font when the
    # user hasn't supplied a custom font file and is still on the default
    # "Arial" (which is often missing on headless Linux / Colab).
    if style.font_file is None and style.font == "Arial":
        try:
            with open(srt_path, encoding="utf-8") as _fh:
                _srt_sample = _fh.read(8192)
            if _RTL_RE.search(_srt_sample):
                log.info(
                    "RTL text detected and no custom font set — "
                    "downloading Noto Sans Arabic …"
                )
                style.font_file = download_google_font("Noto Sans Arabic")
                style.font = _detect_font_family(style.font_file) or "Noto Sans Arabic"
        except Exception as exc:
            log.warning("Could not auto-download Arabic font: %s", exc)

    # Preprocess SRT: display split → RTL → line spacing.
    # Each step creates a temp file; we track them all for cleanup.
    cleanup_paths: list[str] = []
    effective_srt = srt_path

    # 1. Split long entries for comfortable on-screen reading.
    display_srt = _prepare_display_split(effective_srt)
    if display_srt:
        cleanup_paths.append(display_srt)
        effective_srt = display_srt

    # 2. RTL directional markers.
    rtl_srt = _prepare_rtl_srt(effective_srt, style.position)
    if rtl_srt:
        cleanup_paths.append(rtl_srt)
        effective_srt = rtl_srt

    # 3. Line spacing.
    spaced_srt = _prepare_line_spacing(effective_srt, style.line_spacing)
    if spaced_srt:
        cleanup_paths.append(spaced_srt)
        effective_srt = spaced_srt

    escaped = _escape_filter_path(effective_srt)
    force = style.to_force_style()
    if style.font_file:
        escaped_dir = _escape_filter_path(
            os.path.dirname(os.path.abspath(style.font_file))
        )
        vf = f"subtitles='{escaped}':fontsdir='{escaped_dir}':force_style='{force}'"
    else:
        vf = f"subtitles='{escaped}':force_style='{force}'"

    cmd = ["ffmpeg", "-y", "-i", video_path]
    if audio_path:
        cmd += ["-i", audio_path]

    cmd += ["-vf", vf] + _video_encode_args()

    if audio_path:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]

    cmd += ["-shortest", output_path]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    finally:
        for p in cleanup_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    log.info("Subtitled video saved: %s", output_path)
    return output_path
