# Subtitle Styling

Mazinger burns subtitles into video using the ffmpeg `subtitles` filter. All styling options are available as CLI flags (prefixed with `--subtitle-`) and as fields on the `SubtitleStyle` dataclass.

## Styling Options

| CLI Flag | Python Field | Default | Description |
|----------|-------------|---------|-------------|
| `--subtitle-font` | `font` | `Arial` | Font family name (must be installed on the system) |
| `--subtitle-font-file` | `font_file` | — | Path to a local TTF or OTF file |
| `--subtitle-google-font` | — | — | Google Font name (downloaded automatically) |
| `--subtitle-font-size` | `font_size` | `14` | Font size in pixels |
| `--subtitle-font-color` | `font_color` | `white` | Text color — name or `#RRGGBB` |
| `--subtitle-bg-color` | `bg_color` | `black` | Background box color |
| `--subtitle-bg-alpha` | `bg_alpha` | `0.6` | Background opacity (0.0 = transparent, 1.0 = opaque) |
| `--subtitle-outline-color` | `outline_color` | `black` | Text outline color |
| `--subtitle-outline-width` | `outline_width` | `1` | Outline thickness in pixels |
| `--subtitle-position` | `position` | `bottom` | `top`, `center`, or `bottom` |
| `--subtitle-margin` | `margin_v` | `20` | Vertical margin from edge in pixels |
| `--subtitle-bold` | `bold` | `False` | Bold text |
| `--subtitle-line-spacing` | `line_spacing` | `8` | Extra vertical space between wrapped lines (px) |

### Supported Color Names

`white`, `black`, `yellow`, `red`, `green`, `blue`, `cyan`, `magenta`, `gray`

Any `#RRGGBB` hex value also works.

## Font Selection

Mazinger supports three ways to specify a font, in order of priority:

### 1. Google Font (recommended for non-Latin scripts)

```bash
mazinger subtitle video.mp4 --srt translated.srt -o output.mp4 \
    --subtitle-google-font "Noto Sans Arabic" \
    --subtitle-font-size 24
```

The font is downloaded from Google Fonts and cached in `/tmp/mazinger-fonts/`. This is the easiest way to get proper rendering for Arabic, CJK, Hindi, Thai, and other non-Latin scripts.

### 2. Local font file

```bash
mazinger subtitle video.mp4 --srt translated.srt -o output.mp4 \
    --subtitle-font-file /path/to/MyFont-Regular.ttf
```

The font family name is auto-detected from the file metadata.

### 3. System font

```bash
mazinger subtitle video.mp4 --srt translated.srt -o output.mp4 \
    --subtitle-font "DejaVu Sans"
```

The font must be installed on the system and visible to ffmpeg/fontconfig.

## Python API

```python
from mazinger.subtitle import SubtitleStyle, burn_subtitles, download_google_font

style = SubtitleStyle(
    font="DejaVu Sans",
    font_size=28,
    font_color="yellow",
    bg_color="black",
    bg_alpha=0.8,
    position="bottom",
    bold=True,
    line_spacing=10,
)

burn_subtitles("video.mp4", "output.mp4", "translated.srt", style)
```

### Using a Google Font in Python

```python
font_path = download_google_font("Noto Sans Arabic")
style = SubtitleStyle(font_file=font_path, font_size=24)
burn_subtitles("video.mp4", "output.mp4", "translated.srt", style)
```

### Replacing audio in the same pass

```python
burn_subtitles(
    "video.mp4", "output.mp4", "translated.srt", style,
    audio_path="dubbed.wav",
)
```

## RTL Support

Mazinger detects Arabic, Farsi (Persian), and Hebrew characters in subtitle text and automatically inserts Unicode directional markers (RLE/PDF) so that right-to-left text renders correctly in the ffmpeg subtitle filter.

No manual configuration is needed. Pair RTL text with an appropriate font:

```bash
mazinger subtitle video.mp4 --srt arabic.srt -o output.mp4 \
    --subtitle-google-font "Noto Sans Arabic"
```

## Display Splitting

Long subtitle entries are automatically split across multiple display lines before burning. The split threshold is 120 characters. Lines are broken at word boundaries so no word is cut in half.

## Examples

### Clean white text on semi-transparent black

```bash
mazinger subtitle video.mp4 --srt subs.srt -o output.mp4 \
    --subtitle-font-size 20 \
    --subtitle-font-color white \
    --subtitle-bg-color black \
    --subtitle-bg-alpha 0.6
```

### Bold yellow text, fully opaque background

```bash
mazinger subtitle video.mp4 --srt subs.srt -o output.mp4 \
    --subtitle-font-size 24 \
    --subtitle-font-color yellow \
    --subtitle-bg-color black \
    --subtitle-bg-alpha 0.9 \
    --subtitle-bold
```

### Top-positioned subtitles

```bash
mazinger subtitle video.mp4 --srt subs.srt -o output.mp4 \
    --subtitle-position top \
    --subtitle-margin 30
```

### Arabic with Noto Sans

```bash
mazinger subtitle video.mp4 --srt arabic.srt -o output.mp4 \
    --subtitle-google-font "Noto Sans Arabic" \
    --subtitle-font-size 24 \
    --subtitle-font-color yellow \
    --subtitle-bg-color black \
    --subtitle-bg-alpha 0.9 \
    --subtitle-bold
```
