//! # subtitle_forge
//!
//! Turn a `faster-whisper` JSON transcript into subtitles:
//!
//! * a plain **SubRip** (`.srt`) file, and
//! * a **styled Advanced SubStation Alpha** (`.ass`) file with a modern,
//!   Netflix-flavoured look (Roboto, resolution-proportioned margins,
//!   semi-transparent black drop shadow).
//!
//! ## CLI
//!
//! ```text
//! subtitle_forge --input transcript.json --srt output.srt --ass output.ass
//! ```
//!
//! ## Input contract
//!
//! The Kaggle worker writes the transcript that `faster-whisper` produces
//! while `word_timestamps=True`.  Two shapes are accepted, because both
//! occur in the wild:
//!
//! ```jsonc
//! { "language": "en", "segments": [ { "start": 0.0, "end": 1.5, "text": "Hi",
//!                                     "words": [ ... ] } ] }
//! ```
//!
//! ```jsonc
//! [ { "start": 0.0, "end": 1.5, "text": "Hi" } ]
//! ```
//!
//! Unknown keys are ignored, `start`/`end` may be omitted when word-level
//! timings are present (they are then recovered from the first/last word),
//! and malformed cues are *dropped with a warning* rather than aborting the
//! run — a single corrupt timestamp must never cost a multi-hour job.
//!
//! ## Layout
//!
//! This is a `[[bin]]`, not a library, so everything lives in one translation
//! unit organised into banner-separated sections — the same convention used
//! by `cpp_accelerator/normalizer.cpp` elsewhere in this repository:
//!
//! 1. constants
//! 2. logging
//! 3. error
//! 4. cli
//! 5. input model
//! 6. timecode
//! 7. text shaping
//! 8. renderers
//! 9. orchestration
//! 10. tests
//!
//! ## Timecode accuracy
//!
//! Every timestamp is rounded **once, on the total**, and only then
//! decomposed with integer division.  Rounding each field independently is
//! how formatters end up emitting the illegal `00:00:59,1000` (which the
//! Python helper in `mazinger.transcribe._fmt_srt_time` genuinely does at
//! e.g. `59.9996 s`); rounding the total first makes that state unreachable
//! by construction.  See `tests/test_subtitle_forge_timecode.py`, which
//! ports this algorithm and sweeps it for format invariants.

use std::error::Error as _;
use std::fmt;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Instant;

use clap::Parser;
use serde::Deserialize;

// ─────────────────────────────────────────────────────────────────────────────
// constants — the defaults that define the "Netflix-style" look
// ─────────────────────────────────────────────────────────────────────────────

/// Font size as a fraction of `PlayResY`.
///
/// 5 % of a 1080-line frame = 54 px: large enough to read on a phone, small
/// enough that two lines still fit inside the safe area.  Expressing it as a
/// ratio (rather than a fixed pixel size) means the style stays proportionate
/// if `--play-res-y` is changed to 720 or 2160.
const FONT_SIZE_RATIO: f64 = 0.05;

/// Bottom margin as a fraction of `PlayResY`.
///
/// 10 % (= 108 px at 1080p) keeps text clear of the bottom of the frame,
/// where player seek bars, broadcast graphics and phone notches live.
const MARGIN_V_RATIO: f64 = 0.10;

/// Left/right margin as a fraction of `PlayResX`.
///
/// 6.25 % (= 120 px at 1920 wide) keeps lines away from the screen edges on
/// overscanned displays while still leaving room for long cues.
const MARGIN_X_RATIO: f64 = 0.0625;

/// Outline thickness, in `PlayResY` units — fully opaque black, so the text
/// keeps a hard edge over any background.
const OUTLINE_PX: f64 = 2.0;

/// Drop-shadow offset, in `PlayResY` units.  Rendered in `BackColour`, i.e.
/// the semi-transparent black that gives cues their lift off busy video.
const SHADOW_PX: f64 = 2.0;

/// ASS subtitle alignment `2` = bottom-centre.
const ALIGNMENT: u8 = 2;

/// Default wrap width, matching the Netflix timed-text guideline of 42
/// characters per line for Latin scripts.
const DEFAULT_MAX_CHARS: usize = 42;

/// Default maximum lines per cue, also from that guideline (2 lines).
const DEFAULT_MAX_LINES: usize = 2;

/// Fallback family if `--font` is passed something empty after sanitising.
const FALLBACK_FONT: &str = "Roboto";

/// Cap on how many per-cue warnings are echoed, so a badly broken transcript
/// produces a readable log instead of tens of thousands of lines.  Totals are
/// still reported by [`LoadReport`] regardless of this cap.
const MAX_WARNINGS: usize = 8;

// ─────────────────────────────────────────────────────────────────────────────
// logging — mirrors the `[stitcher]` convention used by the sibling crate
// ─────────────────────────────────────────────────────────────────────────────

/// Print a timestamped status line on stderr.
///
/// ```ignore
/// slog!(t0, "read {} cues from {}", cues.len(), path.display());
/// // [subtitle_forge] [  0.01s] read 128 cues from transcript.json
/// ```
macro_rules! slog {
    ($t0:expr, $($arg:tt)*) => {
        eprintln!(
            "[subtitle_forge] [{:>7.2}s] {}",
            $t0.elapsed().as_secs_f32(),
            format!($($arg)*)
        )
    };
}

// ─────────────────────────────────────────────────────────────────────────────
// error
// ─────────────────────────────────────────────────────────────────────────────

/// Result alias so fallible helpers read cleanly.
type Result<T> = std::result::Result<T, ForgeError>;

/// Every way this program can fail.
///
/// Variants deliberately carry the path (or the offending message) so an
/// error is actionable without re-running under extra verbosity.
#[derive(Debug)]
enum ForgeError {
    /// A filesystem operation failed.
    Io {
        /// Human-readable verb phrase, e.g. `"read transcript"`.
        context: &'static str,
        /// File the operation was performed on.
        path: PathBuf,
        /// Underlying I/O error, surfaced through [`std::error::Error::source`].
        source: io::Error,
    },
    /// The input file is not syntactically valid JSON.
    Json {
        path: PathBuf,
        source: serde_json::Error,
    },
    /// The input is valid JSON but not a transcript we understand.
    Format(String),
    /// The command line itself is inconsistent (e.g. neither output given).
    Usage(String),
}

impl fmt::Display for ForgeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Deliberately *not* inlining the underlying cause: `main` walks the
        // `source()` chain and prints it on its own "caused by" line.
        match self {
            ForgeError::Io { context, path, .. } => {
                write!(f, "cannot {context} {}", path.display())
            }
            ForgeError::Json { path, .. } => {
                write!(f, "{} is not valid JSON", path.display())
            }
            ForgeError::Format(msg) | ForgeError::Usage(msg) => write!(f, "{msg}"),
        }
    }
}

impl std::error::Error for ForgeError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            ForgeError::Io { source, .. } => Some(source),
            ForgeError::Json { source, .. } => Some(source),
            ForgeError::Format(_) | ForgeError::Usage(_) => None,
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// cli
// ─────────────────────────────────────────────────────────────────────────────

/// Command-line interface.
///
/// clap derives `--help`, `--version`, typed parsing and error messages from
/// this struct.  The three flags named in the brief (`--input`, `--srt`,
/// `--ass`) are all present; "at least one output" is validated by [`run`],
/// which yields clap-compatible exit code 2.
#[derive(Parser, Debug)]
#[command(
    name = "subtitle_forge",
    version,
    about = "Convert a faster-whisper JSON transcript into styled .srt and .ass subtitles",
    long_about = None,
    after_help = "Examples:\n  subtitle_forge --input transcript.json --srt out.srt --ass out.ass\n  subtitle_forge -i t.json --ass out.ass --font \"Open Sans\" --font-size 60"
)]
struct Cli {
    /// faster-whisper JSON transcript to read
    #[arg(long, short = 'i', value_name = "FILE")]
    input: PathBuf,

    /// Write a SubRip (.srt) file to this path
    #[arg(long, short = 's', value_name = "FILE")]
    srt: Option<PathBuf>,

    /// Write a styled Advanced SubStation Alpha (.ass) file to this path
    #[arg(long, short = 'a', value_name = "FILE")]
    ass: Option<PathBuf>,

    /// Font family used by the ASS style [default: Roboto]
    ///
    /// Held as `Option` rather than a clap default so that the fallback
    /// lives in exactly one place — [`resolve_style`] — where it can also
    /// cope with a value that sanitises down to nothing.
    #[arg(long, value_name = "NAME")]
    font: Option<String>,

    /// ASS font size in PlayResY units (default: 5% of PlayResY)
    #[arg(long, value_name = "PX")]
    font_size: Option<u32>,

    /// Bottom margin in PlayResY units (default: 10% of PlayResY)
    #[arg(long, value_name = "PX")]
    margin_v: Option<u32>,

    /// Left margin in PlayResX units (default: 6.25% of PlayResX)
    #[arg(long, value_name = "PX")]
    margin_l: Option<u32>,

    /// Right margin in PlayResX units (default: 6.25% of PlayResX)
    #[arg(long, value_name = "PX")]
    margin_r: Option<u32>,

    /// ASS script resolution width
    #[arg(long, default_value_t = 1920, value_name = "PX")]
    play_res_x: u32,

    /// ASS script resolution height
    #[arg(long, default_value_t = 1080, value_name = "PX")]
    play_res_y: u32,

    /// Drop-shadow alpha: 0 = opaque, 255 = invisible (128 = 50% transparent)
    #[arg(long, default_value_t = 128, value_name = "0-255")]
    shadow_alpha: u8,

    /// Wrap cues to at most this many characters per line (0 = no wrapping)
    #[arg(long, default_value_t = DEFAULT_MAX_CHARS, value_name = "N")]
    max_chars: usize,

    /// Keep every cue within this many lines (width relaxes if it must)
    #[arg(long, default_value_t = DEFAULT_MAX_LINES, value_name = "N")]
    max_lines: usize,

    /// Title recorded in the ASS [Script Info] header
    #[arg(long, default_value = "T_Dubber", value_name = "TEXT")]
    title: String,
}

// ─────────────────────────────────────────────────────────────────────────────
// input model
// ─────────────────────────────────────────────────────────────────────────────

/// One word and its timing, present when the worker ran with
/// `word_timestamps=True`.
///
/// Everything is `Option` because third-party wrappers routinely strip
/// fields; the word timings are also what let us *recover* a segment whose
/// own `start`/`end` were dropped.
#[derive(Debug, Deserialize)]
struct RawWord {
    /// The token itself.  Carried so the model mirrors the real document;
    /// only the timings are consumed below.
    #[allow(dead_code)]
    word: Option<String>,
    start: Option<f64>,
    end: Option<f64>,
}

/// One cue exactly as deserialised, before validation.
#[derive(Debug, Deserialize)]
struct RawSegment {
    start: Option<f64>,
    end: Option<f64>,
    text: Option<String>,
    /// Absent in most real-world transcripts, hence `#[serde(default)]`.
    #[serde(default)]
    words: Vec<RawWord>,
}

/// The two transcript shapes we accept.
///
/// `#[serde(untagged)]` tries the variants in order: a JSON object matches
/// [`Transcript::Wrapped`], a JSON array falls through to
/// [`Transcript::Bare`].  Unknown top-level keys (`language`, `text`, …) are
/// ignored by serde's default behaviour.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum Transcript {
    Wrapped { segments: Vec<RawSegment> },
    Bare(Vec<RawSegment>),
}

impl Transcript {
    /// Consume the enum and return the raw cue list.
    fn into_segments(self) -> Vec<RawSegment> {
        match self {
            Transcript::Wrapped { segments } => segments,
            Transcript::Bare(segments) => segments,
        }
    }
}

/// A validated cue, ready to be rendered.
#[derive(Debug, Clone, PartialEq)]
struct Cue {
    /// Start time in seconds, `>= 0.0`.
    start: f64,
    /// End time in seconds, `> start`.
    end: f64,
    /// Whitespace-normalised single-line text, non-empty.
    text: String,
}

/// Everything loading had to do to the input, so a short subtitle file can
/// never be produced silently.
#[derive(Debug, Default)]
struct LoadReport {
    /// Cues dropped because their text was empty (normal for whisper).
    empty: usize,
    /// Cues dropped because `start`/`end` were missing or not finite.
    malformed: usize,
    /// Cues dropped because `end <= start`.
    inverted: usize,
    /// Cues whose negative timestamps were clamped to zero.
    clamped: usize,
    /// Cues that had to move when [`load`] put them into time order.
    reordered: usize,
}

impl LoadReport {
    /// Total number of cues that were rejected outright (empty cues are an
    /// expected whisper artefact and are *not* counted here).
    fn dropped(&self) -> usize {
        self.malformed + self.inverted
    }
}

/// Parse the transcript JSON into raw cues.
///
/// Parsing happens in two deliberate steps:
///
/// 1. syntax (`serde_json::from_str::<Value>`) — reported as
///    [`ForgeError::Json`] with the file path, so a truncated download says
///    *where* it broke;
/// 2. shape (`serde_json::from_value::<Transcript>`) — reported as
///    [`ForgeError::Format`] naming both accepted shapes, which is far more
///    useful than serde's "did not match any variant" text.
fn parse_transcript(path: &Path, raw: &str) -> Result<Vec<RawSegment>> {
    // A UTF-8 BOM is legal in a text file but `serde_json` rejects it.
    let text = raw.strip_prefix('\u{feff}').unwrap_or(raw);

    let value: serde_json::Value =
        serde_json::from_str(text).map_err(|source| ForgeError::Json {
            path: path.to_path_buf(),
            source,
        })?;

    let transcript: Transcript = serde_json::from_value(value).map_err(|err| {
        ForgeError::Format(format!(
            "{}: not a transcript — expected {{\"segments\": [ ... ]}} \
             or a bare [ ... ] array of cues: {err}",
            path.display()
        ))
    })?;

    Ok(transcript.into_segments())
}

/// Validate, normalise and time-order raw cues.
///
/// Rejections are counted in [`LoadReport`].  The genuinely unexpected ones
/// (missing/NaN/inverted timings) are also echoed to stderr, but at most
/// [`MAX_WARNINGS`] times so one broken file cannot flood the log.
fn load(raw: Vec<RawSegment>) -> (Vec<Cue>, LoadReport) {
    let mut report = LoadReport::default();
    let mut warned = 0usize;
    let mut cues: Vec<Cue> = Vec::with_capacity(raw.len());

    for (index, seg) in raw.into_iter().enumerate() {
        // 1. Text first: empty cues are *expected* from whisper, so they are
        //    counted but not shouted about.
        let text = normalize_text(seg.text.as_deref().unwrap_or_default());
        if text.is_empty() {
            report.empty += 1;
            continue;
        }

        // 2. Recover missing timings from word-level data before giving up.
        let start = seg.start.or_else(|| seg.words.first().and_then(|w| w.start));
        let end = seg.end.or_else(|| seg.words.last().and_then(|w| w.end));

        let (mut start, mut end) = match (start, end) {
            (Some(s), Some(e)) => (s, e),
            _ => {
                report.malformed += 1;
                warn_segment(index, "missing or incomplete start/end", &mut warned);
                continue;
            }
        };

        if !start.is_finite() || !end.is_finite() {
            report.malformed += 1;
            warn_segment(index, "non-finite start/end", &mut warned);
            continue;
        }

        // 3. Clamp negatives rather than dropping the cue: the text is fine,
        //    only the boundary was off (chunked transcription restarts at 0).
        if start < 0.0 {
            start = 0.0;
            report.clamped += 1;
        }
        if end < 0.0 {
            end = 0.0;
            report.clamped += 1;
        }

        if end <= start {
            report.inverted += 1;
            warn_segment(index, "end <= start", &mut warned);
            continue;
        }

        cues.push(Cue { start, end, text });
    }

    // Time-ordered output is required by both formats.  Input order is
    // usually already correct, but concatenated/chunked transcripts may not
    // be.  `windows(2)` short-circuits: the common case costs nothing.
    if !cues.windows(2).all(|w| w[0].start <= w[1].start) {
        let before = cues.clone();
        cues.sort_by(|a, b| a.start.total_cmp(&b.start));
        let mut moved = 0usize;
        for (new, old) in cues.iter().zip(before.iter()) {
            if new != old {
                moved += 1;
            }
        }
        report.reordered = moved;
    }

    (cues, report)
}

/// Echo a single per-cue warning, subject to the [`MAX_WARNINGS`] cap.
///
/// `index` is reported 1-based to match how editors number cues.
fn warn_segment(index: usize, reason: &str, warned: &mut usize) {
    if *warned >= MAX_WARNINGS {
        return;
    }
    *warned += 1;
    let note = if *warned == MAX_WARNINGS {
        " (further per-cue warnings suppressed)"
    } else {
        ""
    };
    eprintln!(
        "[subtitle_forge] warning: cue #{} skipped: {reason}{note}",
        index + 1
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// timecode
// ─────────────────────────────────────────────────────────────────────────────

/// Whole milliseconds in `0..` for a duration expressed in seconds.
///
/// * non-finite or `<= 0` → `0` (never a negative or `NaN` timestamp);
/// * rounds **once on the total**, so `59.9996 s` becomes `60 000 ms`
///   rather than a millisecond field overflowing to `1000`.
fn to_millis(seconds: f64) -> i64 {
    if !seconds.is_finite() || seconds <= 0.0 {
        return 0;
    }
    (seconds * 1000.0).round() as i64
}

/// Whole centiseconds in `0..` for a duration expressed in seconds.
///
/// Same contract as [`to_millis`] but at ASS's `.cc` resolution.
fn to_centis(seconds: f64) -> i64 {
    if !seconds.is_finite() || seconds <= 0.0 {
        return 0;
    }
    (seconds * 100.0).round() as i64
}

/// Format seconds as a SubRip timestamp: `HH:MM:SS,mmm`.
///
/// The millisecond field is guaranteed to be `000..999` and the second field
/// `00..59`, because both are remainders of integer division on the
/// already-rounded total.
fn format_srt_time(seconds: f64) -> String {
    let total = to_millis(seconds);
    let hours = total / 3_600_000;
    let rest = total % 3_600_000;
    let minutes = rest / 60_000;
    let rest = rest % 60_000;
    let secs = rest / 1_000;
    let millis = rest % 1_000;
    // `{:02}` pads to *at least* two digits, so a >99-hour file still works.
    format!("{hours:02}:{minutes:02}:{secs:02},{millis:03}")
}

/// Format seconds as an Advanced SubStation Alpha timestamp: `H:MM:SS.cc`.
///
/// ASS uses centiseconds and an unpadded hour field.  The same
/// round-then-decompose discipline guarantees `.cc` stays in `00..99`.
fn format_ass_time(seconds: f64) -> String {
    let total = to_centis(seconds);
    let hours = total / 360_000;
    let rest = total % 360_000;
    let minutes = rest / 6_000;
    let rest = rest % 6_000;
    let secs = rest / 100;
    let centis = rest % 100;
    format!("{hours}:{minutes:02}:{secs:02}.{centis:02}")
}

// ─────────────────────────────────────────────────────────────────────────────
// text shaping — normalisation, escaping and line wrapping
// ─────────────────────────────────────────────────────────────────────────────

/// Collapse every whitespace run (spaces, tabs, newlines) to one space and
/// trim the ends.
///
/// faster-whisper text is logically a single line; a stray `\n` inside it
/// would otherwise be rendered as an unintended second subtitle line in SRT,
/// where newlines *are* meaningful.
fn normalize_text(text: &str) -> String {
    text.split_whitespace().collect::<Vec<&str>>().join(" ")
}

/// Greedy word wrap: fill each line up to `max_chars`, breaking on spaces.
///
/// A single word wider than `max_chars` is left intact on its own line —
/// breaking inside a word would corrupt the text, and hyphenation is far
/// outside this tool's remit.
///
/// Greedy filling is also *optimal for line count* at a fixed width, which
/// is what makes the width relaxation in [`wrap_text`] the right lever: no
/// smarter distribution can tell the same cue in fewer lines.  That property
/// is asserted against a dynamic-programming reference in
/// `tests/test_subtitle_forge_wrapping.py`.
fn greedy_wrap(words: &[&str], max_chars: usize) -> Vec<String> {
    let mut lines: Vec<String> = Vec::new();
    let mut line = String::new();
    let mut line_len = 0usize;

    for word in words {
        let word_len = word.chars().count();
        if line.is_empty() {
            line.push_str(word);
            line_len = word_len;
        } else if line_len + 1 + word_len <= max_chars {
            line.push(' ');
            line.push_str(word);
            line_len += 1 + word_len;
        } else {
            lines.push(std::mem::take(&mut line));
            line.push_str(word);
            line_len = word_len;
        }
    }
    if !line.is_empty() {
        lines.push(line);
    }
    lines
}

/// Wrap a cue into at most `max_lines` lines, targeting `max_chars` per line.
///
/// Policy, in order:
///
/// 1. `max_chars == 0` → no wrapping, the cue stays on one line;
/// 2. wrap greedily at `max_chars`;
/// 3. if that already fits inside `max_lines`, keep it — this is the normal
///    path and it honours the 42-character guideline exactly;
/// 4. otherwise the cue *cannot* be told in `max_lines` lines at that width.
///    Words are never dropped, so the **width** is relaxed instead: binary
///    search the narrowest width that fits the cue in `max_lines` lines.
///    Line count only ever falls as width grows, so the search is safe.
///
/// Relaxing width rather than emitting an extra line is deliberate — a
/// slightly wider cue still reads as "two lines of subtitles", whereas a
/// third line starts covering the picture.  And if the relaxed line turns out
/// wider than the frame, the renderer's own margin wrapping (`WrapStyle: 0`)
/// falls back to the same visual result we would have produced anyway, so
/// widening is never worse than the fallback.
///
/// The search is verified against exhaustive search in
/// `tests/test_subtitle_forge_wrapping.py`.
fn wrap_text(text: &str, max_chars: usize, max_lines: usize) -> Vec<String> {
    let max_lines = max_lines.max(1);
    let words: Vec<&str> = text.split_whitespace().collect();
    if words.is_empty() {
        return Vec::new();
    }
    if max_chars == 0 {
        return vec![text.to_string()];
    }

    let greedy = greedy_wrap(&words, max_chars);
    if greedy.len() <= max_lines {
        return greedy;
    }

    // `total` is the cue on a single line, so it is an upper bound that is
    // always feasible (one line ≤ max_lines), and `max_chars` is a lower
    // bound that is infeasible by the check above.
    let total: usize =
        words.iter().map(|w| w.chars().count()).sum::<usize>() + words.len() - 1;
    let mut lo = max_chars;
    let mut hi = total;
    while lo < hi {
        let mid = lo + (hi - lo) / 2;
        if greedy_wrap(&words, mid).len() <= max_lines {
            hi = mid;
        } else {
            lo = mid + 1;
        }
    }
    greedy_wrap(&words, lo)
}

/// Make a string safe to place in a comma-delimited ASS header field.
///
/// Commas would shift every following column of the `Style:` line, and
/// newlines would end the record outright.
fn sanitize_field(value: &str) -> String {
    value
        .chars()
        .filter(|c| *c != ',' && *c != '\n' && *c != '\r')
        .collect::<String>()
        .trim()
        .to_string()
}

/// Escape cue text for the ASS `Text` field.
///
/// * `{` / `}` would open an override block, so they are backslash-escaped.
/// * remaining control characters are dropped (already impossible after
///   [`normalize_text`], but defence in depth).
///
/// Backslashes otherwise pass through: ASS defines only `\N`, `\n` and `\h`
/// as escapes and has no general "literal backslash" form, so mangling them
/// here would be guesswork.  Literal backslashes are vanishingly rare in
/// speech transcripts.
fn escape_ass(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '{' => out.push_str("\\{"),
            '}' => out.push_str("\\}"),
            _ if c.is_control() => {}
            _ => out.push(c),
        }
    }
    out
}

/// Render an ASS colour as `&HAABBGGRR`.
///
/// ASS stores colours little-endian (blue first) with an *inverted* alpha:
/// `00` is fully opaque and `FF` fully transparent.
fn ass_colour(r: u8, g: u8, b: u8, alpha: u8) -> String {
    format!("&H{alpha:02X}{b:02X}{g:02X}{r:02X}")
}

// ─────────────────────────────────────────────────────────────────────────────
// renderers
// ─────────────────────────────────────────────────────────────────────────────

/// Resolved ASS styling — everything the `[V4+ Styles]` line needs.
#[derive(Debug, Clone)]
struct AssStyle {
    title: String,
    play_res_x: u32,
    play_res_y: u32,
    font: String,
    font_size: u32,
    margin_l: u32,
    margin_r: u32,
    margin_v: u32,
    /// Drop-shadow alpha: `00` opaque … `FF` invisible.
    shadow_alpha: u8,
}

/// Scale `base` by `ratio`, never returning 0 (a 0 px font draws nothing).
fn scale(base: u32, ratio: f64) -> u32 {
    ((base as f64) * ratio).round().max(1.0) as u32
}

/// Resolve CLI options into a complete [`AssStyle`].
///
/// Resolution-sensitive defaults are computed from `--play-res-x/y` so the
/// proportions survive a change of target resolution; explicitly supplied
/// values win.
fn resolve_style(cli: &Cli) -> Result<AssStyle> {
    if cli.play_res_x == 0 || cli.play_res_y == 0 {
        return Err(ForgeError::Usage(
            "--play-res-x and --play-res-y must both be greater than 0".to_string(),
        ));
    }

    // Sanitising can strip a whole string (e.g. `--font ","`), and clap has
    // no default here — so both the "not supplied" and "supplied as nothing"
    // cases collapse onto FALLBACK_FONT rather than emitting a Style line
    // that no renderer can parse.
    let font = sanitize_field(cli.font.as_deref().unwrap_or(FALLBACK_FONT));
    let font = if font.is_empty() {
        FALLBACK_FONT.to_string()
    } else {
        font
    };

    Ok(AssStyle {
        title: sanitize_field(&cli.title),
        play_res_x: cli.play_res_x,
        play_res_y: cli.play_res_y,
        font,
        font_size: cli
            .font_size
            .unwrap_or_else(|| scale(cli.play_res_y, FONT_SIZE_RATIO))
            .max(1),
        margin_l: cli.margin_l.unwrap_or_else(|| scale(cli.play_res_x, MARGIN_X_RATIO)),
        margin_r: cli.margin_r.unwrap_or_else(|| scale(cli.play_res_x, MARGIN_X_RATIO)),
        margin_v: cli.margin_v.unwrap_or_else(|| scale(cli.play_res_y, MARGIN_V_RATIO)),
        shadow_alpha: cli.shadow_alpha,
    })
}

/// Render a complete SubRip document.
///
/// Layout matches `mazinger.transcribe._segments_to_srt` byte for byte: each
/// cue is `index\ntimestamps\ntext\n`, cues are separated by a single blank
/// line, and the file ends with exactly one trailing newline.
fn render_srt(cues: &[Cue], max_chars: usize, max_lines: usize) -> String {
    let mut out = String::with_capacity(cues.len() * 96);
    for (i, cue) in cues.iter().enumerate() {
        if i > 0 {
            out.push('\n');
        }
        out.push_str(&(i + 1).to_string());
        out.push('\n');
        out.push_str(&format_srt_time(cue.start));
        out.push_str(" --> ");
        out.push_str(&format_srt_time(cue.end));
        out.push('\n');
        out.push_str(&wrap_text(&cue.text, max_chars, max_lines).join("\n"));
        out.push('\n');
    }
    out
}

/// Render a complete Advanced SubStation Alpha document.
///
/// The style is "Netflix-flavoured": a modern sans-serif, a crisp opaque
/// black outline, a **semi-transparent black drop shadow** (so text stays
/// legible over bright, busy or mid-grey backgrounds alike), bottom-centre
/// alignment, and margins that scale with `PlayRes`.
fn render_ass(cues: &[Cue], style: &AssStyle, max_chars: usize, max_lines: usize) -> String {
    // White primary text.  The second colour is the karaoke fill — unused by
    // these events, but the field is mandatory.
    let primary = ass_colour(0xFF, 0xFF, 0xFF, 0x00);
    let secondary = ass_colour(0xFF, 0xFF, 0xFF, 0x00);
    // Fully opaque black outline: a hard edge on any background.
    let outline_colour = ass_colour(0x00, 0x00, 0x00, 0x00);
    // Semi-transparent black shadow — `shadow_alpha` defaults to 128, i.e.
    // exactly 50 % opacity.
    let back_colour = ass_colour(0x00, 0x00, 0x00, style.shadow_alpha);

    let mut out = String::with_capacity(1024 + cues.len() * 128);

    out.push_str("[Script Info]\n");
    out.push_str(&format!(
        "; Generated by subtitle_forge {}\n",
        env!("CARGO_PKG_VERSION")
    ));
    out.push_str(&format!("Title: {}\n", &style.title));
    out.push_str("ScriptType: v4.00+\n");
    out.push_str("WrapStyle: 0\n"); // smart wrapping, as a fallback
    out.push_str("ScaledBorderAndShadow: yes\n");
    out.push_str("YCbCr Matrix: TV.709\n");
    out.push_str(&format!("PlayResX: {}\n", style.play_res_x));
    out.push_str(&format!("PlayResY: {}\n", style.play_res_y));
    out.push_str("Collision: Normal\n");
    out.push('\n');

    out.push_str("[V4+ Styles]\n");
    out.push_str(
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, \
         OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, \
         ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, \
         MarginR, MarginV, Encoding\n",
    );
    // 23 values for 23 format fields.  Bold/Italic/Underline/StrikeOut are
    // 0 = off (ASS wants -1 for "on").  BorderStyle 1 = outline + shadow.
    out.push_str(&format!(
        "Style: Default,{font},{font_size},{primary},{secondary},\
         {outline_colour},{back_colour},0,0,0,0,100,100,0,0,1,\
         {outline:.1},{shadow:.1},{alignment},{margin_l},{margin_r},{margin_v},1\n",
        font = &style.font,
        font_size = style.font_size,
        primary = primary,
        secondary = secondary,
        outline_colour = outline_colour,
        back_colour = back_colour,
        outline = OUTLINE_PX,
        shadow = SHADOW_PX,
        alignment = ALIGNMENT,
        margin_l = style.margin_l,
        margin_r = style.margin_r,
        margin_v = style.margin_v,
    ));
    out.push('\n');

    out.push_str("[Events]\n");
    out.push_str(
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, \
         Effect, Text\n",
    );

    for cue in cues {
        // Escape *per line*, then join with the hard-break escape — escaping
        // afterwards would be correct today only by accident, and would stop
        // being correct the moment `escape_ass` learned to touch backslashes.
        let text = wrap_text(&cue.text, max_chars, max_lines)
            .iter()
            .map(|line| escape_ass(line))
            .collect::<Vec<String>>()
            .join("\\N");
        out.push_str(&format!(
            "Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n",
            start = format_ass_time(cue.start),
            end = format_ass_time(cue.end),
        ));
    }

    out
}

/// Write `contents` to `path`, creating parent directories.
///
/// The bytes are staged in a sibling `*.part` file first and then renamed
/// into place, so an interrupted run can never leave a half-written subtitle
/// that a later pipeline stage would happily ingest.
fn write_atomic(path: &Path, contents: &str) -> Result<()> {
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        fs::create_dir_all(parent).map_err(|source| ForgeError::Io {
            context: "create directory for",
            path: parent.to_path_buf(),
            source,
        })?;
    }

    let mut tmp_name = path.as_os_str().to_owned();
    tmp_name.push(format!(".{}.part", std::process::id()));
    let tmp = PathBuf::from(tmp_name);

    if let Err(source) = fs::write(&tmp, contents) {
        let _ = fs::remove_file(&tmp); // never leave litter behind
        return Err(ForgeError::Io {
            context: "write",
            path: tmp,
            source,
        });
    }

    // Windows refuses to rename onto an existing file; POSIX replaces it.
    if cfg!(windows) && path.exists() {
        if let Err(source) = fs::remove_file(path) {
            let _ = fs::remove_file(&tmp);
            return Err(ForgeError::Io {
                context: "replace",
                path: path.to_path_buf(),
                source,
            });
        }
    }

    fs::rename(&tmp, path).map_err(|source| {
        let _ = fs::remove_file(&tmp);
        ForgeError::Io {
            context: "finalise",
            path: path.to_path_buf(),
            source,
        }
    })
}

// ─────────────────────────────────────────────────────────────────────────────
// orchestration
// ─────────────────────────────────────────────────────────────────────────────

/// Do the whole job, returning `Ok(())` on success.
///
/// Split out from [`main`] so every error path is testable without spawning
/// the binary.
fn run(cli: &Cli) -> Result<()> {
    let t0 = Instant::now();

    // Validate output arguments before doing any work — a run that writes
    // nothing is always a mistake.
    if cli.srt.is_none() && cli.ass.is_none() {
        return Err(ForgeError::Usage(
            "nothing to write: pass --srt and/or --ass".to_string(),
        ));
    }

    let style = resolve_style(cli)?;
    let max_lines = cli.max_lines.max(1);

    slog!(t0, "reading {}", cli.input.display());
    let raw = fs::read_to_string(&cli.input).map_err(|source| ForgeError::Io {
        context: "read transcript",
        path: cli.input.clone(),
        source,
    })?;

    let segments = parse_transcript(&cli.input, &raw)?;
    let (cues, report) = load(segments);

    // Surface everything the loader had to repair *before* writing output, so
    // a surprisingly short file never looks like success.
    if report.clamped > 0 {
        slog!(
            t0,
            "{} cue(s) had negative timestamps clamped to 0",
            report.clamped
        );
    }
    if report.dropped() > 0 {
        slog!(
            t0,
            "dropped {} cue(s): {} malformed, {} with end <= start",
            report.dropped(),
            report.malformed,
            report.inverted
        );
    }
    if report.empty > 0 {
        slog!(t0, "ignored {} empty cue(s)", report.empty);
    }
    if report.reordered > 0 {
        slog!(t0, "cues were not time-ordered; moved {} of them", report.reordered);
    }
    if cues.is_empty() {
        slog!(
            t0,
            "warning: no usable cues — both outputs will contain zero subtitles"
        );
    }

    if let Some(path) = &cli.srt {
        slog!(t0, "rendering SRT -> {}", path.display());
        write_atomic(path, &render_srt(&cues, cli.max_chars, max_lines))?;
        slog!(t0, "wrote {} cue(s) to {}", cues.len(), path.display());
    }

    if let Some(path) = &cli.ass {
        slog!(t0, "rendering ASS -> {}", path.display());
        write_atomic(path, &render_ass(&cues, &style, cli.max_chars, max_lines))?;
        slog!(
            t0,
            "wrote {} cue(s) to {} (font {}, {} px, margins L/R {} / V {})",
            cues.len(),
            path.display(),
            style.font.as_str(),
            style.font_size,
            style.margin_l,
            style.margin_v
        );
    }

    slog!(t0, "done");
    Ok(())
}

/// Entry point: parse argv and map errors onto process exit codes.
///
/// * `0` — success
/// * `1` — runtime failure (I/O, malformed JSON)
/// * `2` — usage failure, matching clap's own exit code for bad arguments
fn main() -> ExitCode {
    let cli = Cli::parse();
    match run(&cli) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("[subtitle_forge] error: {err}");
            let mut cause = err.source();
            while let Some(inner) = cause {
                eprintln!("  caused by: {inner}");
                cause = inner.source();
            }
            match err {
                ForgeError::Usage(_) => ExitCode::from(2),
                _ => ExitCode::FAILURE,
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// tests
// ─────────────────────────────────────────────────────────────────────────────
//
// Run with `cargo test`.  Every numeric expectation below is independently
// confirmed by `tests/test_subtitle_forge_timecode.py` (timestamp math) and
// `tests/test_subtitle_forge_wrapping.py` (line breaking), which port these
// exact algorithms to Python — a second, independent check that these
// assertions describe the intended behaviour rather than just whatever the
// Rust happens to do.

#[cfg(test)]
mod tests {
    // The trait that provides `err.source()` arrives through the glob below:
    // `use super::*` imports the parent module's `use ... as _` bindings too,
    // so a second import here would be a duplicate (rustc flags it as unused).
    use super::*;

    // -- timecode ---------------------------------------------------------

    #[test]
    fn srt_time_is_plain() {
        assert_eq!(format_srt_time(0.0), "00:00:00,000");
        assert_eq!(format_srt_time(1.5), "00:00:01,500");
        assert_eq!(format_srt_time(61.25), "00:01:01,250");
        assert_eq!(format_srt_time(3661.001), "01:01:01,001");
        assert_eq!(format_srt_time(3_600.0), "01:00:00,000");
        assert_eq!(format_srt_time(3.25), "00:00:03,250");
        assert_eq!(format_srt_time(2.0), "00:00:02,000");
    }

    #[test]
    fn srt_time_rounding_carries_instead_of_overflowing() {
        // Rounding the *total* rolls 999.6 ms up into the next second.
        // Field-by-field rounding would emit the illegal ",1000".
        assert_eq!(format_srt_time(59.9996), "00:01:00,000");
        assert_eq!(format_srt_time(0.9999), "00:00:01,000");
        assert_eq!(format_srt_time(3599.9995), "01:00:00,000");
        assert_eq!(format_srt_time(119.9995), "00:02:00,000");
    }

    #[test]
    fn srt_time_never_goes_negative_or_nan() {
        assert_eq!(format_srt_time(-5.0), "00:00:00,000");
        assert_eq!(format_srt_time(f64::NAN), "00:00:00,000");
        assert_eq!(format_srt_time(f64::INFINITY), "00:00:00,000");
        assert_eq!(format_srt_time(f64::NEG_INFINITY), "00:00:00,000");
    }

    #[test]
    fn srt_time_handles_long_files() {
        // 10 hours: `{:02}` widens rather than truncating.
        assert_eq!(format_srt_time(36_000.0), "10:00:00,000");
        assert_eq!(format_srt_time(36_000.001), "10:00:00,001");
    }

    #[test]
    fn ass_time_is_plain() {
        assert_eq!(format_ass_time(0.0), "0:00:00.00");
        assert_eq!(format_ass_time(1.5), "0:00:01.50");
        assert_eq!(format_ass_time(61.25), "0:01:01.25");
        assert_eq!(format_ass_time(3661.5), "1:01:01.50");
        assert_eq!(format_ass_time(3600.0), "1:00:00.00");
    }

    #[test]
    fn ass_time_rounding_carries_and_keeps_two_digits() {
        assert_eq!(format_ass_time(59.996), "0:01:00.00");
        assert_eq!(format_ass_time(0.996), "0:00:01.00");
        // Hours are unpadded per the ASS spec (`H:MM:SS.cc`) but must not be
        // capped at one digit.
        assert_eq!(format_ass_time(35_999.996), "10:00:00.00");
        // The centisecond field can never reach 100.
        assert_eq!(format_ass_time(1.995), "0:00:02.00");
    }

    #[test]
    fn ass_time_never_goes_negative_or_nan() {
        assert_eq!(format_ass_time(-1.0), "0:00:00.00");
        assert_eq!(format_ass_time(f64::NAN), "0:00:00.00");
    }

    // -- text shaping -----------------------------------------------------

    #[test]
    fn normalise_collapses_whitespace() {
        assert_eq!(normalize_text("  hello\t world \n"), "hello world");
        assert_eq!(normalize_text("\n \t"), "");
    }

    #[test]
    fn wrap_fits_on_one_line() {
        assert_eq!(wrap_text("short cue", 42, 2), vec!["short cue".to_string()]);
    }

    #[test]
    fn wrap_splits_on_word_boundaries_without_losing_words() {
        let text = "one two three four five six seven eight nine ten eleven twelve";
        let lines = wrap_text(text, 20, 10);
        assert!(lines.len() > 1, "expected multiple lines, got {lines:?}");
        for line in &lines {
            assert!(line.chars().count() <= 20, "line too long: {line:?}");
            assert_eq!(line.as_str(), line.trim(), "stray whitespace: {line:?}");
        }
        assert_eq!(lines.join(" "), text, "no word may be lost or duplicated");
    }

    #[test]
    fn wrap_relaxes_width_instead_of_exceeding_max_lines() {
        // 199 chars cannot be told in 2 lines of 42, and words are never
        // dropped — so the width gives way and the line count holds.
        let text = "word ".repeat(40);
        let text = text.trim();
        let lines = wrap_text(text, 42, 2);
        assert_eq!(lines.len(), 2, "expected widening to 2 lines: {lines:?}");
        assert_eq!(lines.join(" "), text, "no word may be lost");
    }

    #[test]
    fn wrap_keeps_the_char_budget_when_it_fits() {
        // 13 four-char words (64 chars): greedy produces two lines of 39 and
        // 24 characters, both comfortably inside the 42-character budget, so
        // no width relaxation is triggered.
        let text = "aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa bbbb bbbb bbbb";
        let lines = wrap_text(text, 42, 10);
        assert!(lines.len() > 1);
        for line in &lines {
            assert!(line.chars().count() <= 42, "line too long: {line:?}");
        }
        assert_eq!(lines.join(" "), text);
    }

    #[test]
    fn wrap_zero_max_chars_disables_wrapping() {
        let text = "a very long cue that would otherwise be split";
        assert_eq!(wrap_text(text, 0, 2), vec![text.to_string()]);
    }

    #[test]
    fn wrap_handles_single_oversized_word() {
        assert_eq!(
            wrap_text("Supercalifragilisticexpialidocious", 10, 2),
            vec!["Supercalifragilisticexpialidocious".to_string()]
        );
    }

    #[test]
    fn wrap_handles_empty_text() {
        assert!(wrap_text("   ", 42, 2).is_empty());
    }

    #[test]
    fn ass_escape_shields_braces_and_drops_controls() {
        // A literal backslash passes through untouched: ASS has no general
        // escape for it, and rewriting it would be guesswork.
        assert_eq!(escape_ass("{\\i1}hi"), "\\{\\i1\\}hi");
        assert_eq!(escape_ass("a\u{7}b"), "ab");
        assert_eq!(escape_ass("plain text"), "plain text");
    }

    #[test]
    fn ass_colours_are_abgr_with_inverted_alpha() {
        assert_eq!(ass_colour(0xFF, 0xFF, 0xFF, 0x00), "&H00FFFFFF");
        assert_eq!(ass_colour(0x00, 0x00, 0x00, 0x00), "&H00000000");
        assert_eq!(ass_colour(0x00, 0x00, 0x00, 128), "&H80000000");
    }

    #[test]
    fn sanitiser_strips_field_delimiters() {
        assert_eq!(sanitize_field("Roboto, Bold\n"), "Roboto Bold");
        assert_eq!(sanitize_field("   "), "");
    }

    // -- parsing ----------------------------------------------------------

    #[test]
    fn parses_wrapped_and_bare_shapes() {
        let wrapped = r#"{"language":"en","segments":[{"start":0.0,"end":1.0,"text":"hi"}]}"#;
        let bare = r#"[{"start":0.0,"end":1.0,"text":"hi"}]"#;
        for json in [wrapped, bare] {
            let raw = parse_transcript(Path::new("t.json"), json).unwrap();
            let (cues, report) = load(raw);
            assert_eq!(cues.len(), 1, "json: {json}");
            assert_eq!(cues[0].text, "hi");
            assert_eq!(report.dropped(), 0, "json: {json}");
        }
    }

    #[test]
    fn rejects_non_transcript_json_with_a_hint() {
        let err = parse_transcript(Path::new("t.json"), r#"{"foo":1}"#).unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("segments"), "unhelpful message: {msg}");
        assert!(msg.contains("t.json"), "missing path: {msg}");
    }

    #[test]
    fn rejects_broken_json_with_path() {
        let err = parse_transcript(Path::new("bad.json"), "{oops").unwrap_err();
        assert!(matches!(err, ForgeError::Json { .. }), "{err:?}");
        assert!(err.to_string().contains("bad.json"));
        assert!(err.source().is_some(), "underlying cause must be chainable");
    }

    #[test]
    fn strips_utf8_bom() {
        let json = "\u{feff}{\"segments\":[]}";
        assert!(parse_transcript(Path::new("t.json"), json).is_ok());
    }

    #[test]
    fn drops_bad_cues_and_keeps_good_ones() {
        let json = r#"{"segments":[
            {"start":0.0,"end":1.0,"text":"keep"},
            {"start":1.0,"end":1.0,"text":"zero length"},
            {"start":2.0,"end":2.5,"text":"   "},
            {"start":3.0,"text":"no end"},
            {"start":4.0,"end":5.0,"text":"keep2"}
        ]}"#;
        let (cues, report) = load(parse_transcript(Path::new("t.json"), json).unwrap());
        let texts: Vec<&str> = cues.iter().map(|c| c.text.as_str()).collect();
        assert_eq!(texts, vec!["keep", "keep2"]);
        assert_eq!(report.inverted, 1);
        assert_eq!(report.empty, 1);
        assert_eq!(report.malformed, 1);
        assert_eq!(report.dropped(), 2);
        assert_eq!(report.clamped, 0);
        assert_eq!(report.reordered, 0);
    }

    #[test]
    fn clamps_negative_timestamps_instead_of_dropping() {
        let json = r#"{"segments":[{"start":-1.0,"end":0.5,"text":"hi"}]}"#;
        let (cues, report) = load(parse_transcript(Path::new("t.json"), json).unwrap());
        assert_eq!(cues.len(), 1);
        assert_eq!(cues[0].start, 0.0);
        assert_eq!(report.clamped, 1);
        assert_eq!(report.dropped(), 0);
    }

    #[test]
    fn recovers_missing_times_from_words() {
        let json = r#"{"segments":[{"text":"hi","words":[
            {"word":"hi","start":10.0,"end":10.4}
        ]}]}"#;
        let (cues, _) = load(parse_transcript(Path::new("t.json"), json).unwrap());
        assert_eq!(cues.len(), 1);
        assert_eq!(cues[0].start, 10.0);
        assert_eq!(cues[0].end, 10.4);
    }

    #[test]
    fn sorts_cues_into_time_order() {
        let json = r#"{"segments":[
            {"start":5.0,"end":6.0,"text":"b"},
            {"start":1.0,"end":2.0,"text":"a"}
        ]}"#;
        let (cues, report) = load(parse_transcript(Path::new("t.json"), json).unwrap());
        let texts: Vec<&str> = cues.iter().map(|c| c.text.as_str()).collect();
        assert_eq!(texts, vec!["a", "b"]);
        // Both cues changed position.
        assert_eq!(report.reordered, 2);
    }

    // -- rendering --------------------------------------------------------

    #[test]
    fn srt_document_layout_matches_python_helper() {
        let cues = vec![
            Cue { start: 0.0, end: 1.5, text: "Hello".into() },
            Cue { start: 2.0, end: 3.25, text: "World".into() },
        ];
        let expected = "1\n00:00:00,000 --> 00:00:01,500\nHello\n\n\
                        2\n00:00:02,000 --> 00:00:03,250\nWorld\n";
        assert_eq!(render_srt(&cues, 42, 2), expected);
        // Empty input yields an empty document, not a stray newline.
        assert_eq!(render_srt(&[], 42, 2), "");
    }

    #[test]
    fn ass_document_has_consistent_field_counts() {
        let cues = vec![
            Cue { start: 0.0, end: 1.5, text: "Hello".into() },
            Cue { start: 1.0, end: 2.5, text: "Overlap".into() },
        ];
        let cli = Cli::parse_from(["subtitle_forge", "--input", "x.json", "--ass", "x.ass"]);
        let style = resolve_style(&cli).unwrap();
        let out = render_ass(&cues, &style, 42, 2);

        // The Style line must have exactly as many values as the Format line.
        let format_line = out
            .lines()
            .find(|l| l.starts_with("Format: Name,"))
            .expect("style Format line");
        let style_line = out
            .lines()
            .find(|l| l.starts_with("Style: "))
            .expect("Style line");
        assert_eq!(
            format_line.trim_start_matches("Format: ").split(',').count(),
            style_line.trim_start_matches("Style: ").split(',').count(),
            "Style/Format column mismatch"
        );

        // Dialogue lines: exactly 10 fields (Text is last, so it may hold
        // commas — `splitn` is the correct tool here).
        for line in out.lines().filter(|l| l.starts_with("Dialogue: ")) {
            assert_eq!(line.trim_start_matches("Dialogue: ").splitn(10, ',').count(), 10);
        }
        assert_eq!(out.lines().filter(|l| l.starts_with("Dialogue: ")).count(), 2);

        // Required structural sections are present.
        for header in ["[Script Info]", "[V4+ Styles]", "[Events]"] {
            assert!(out.contains(header), "missing {header}");
        }
        // The semi-transparent shadow and the proportioned defaults made it in.
        assert!(out.contains("&H80000000"), "expected a 50% black shadow");
        assert_eq!(style.font_size, 54, "5% of 1080");
        assert_eq!(style.margin_v, 108, "10% of 1080");
        assert_eq!(style.margin_l, 120, "6.25% of 1920");
    }

    #[test]
    fn ass_multi_line_cues_use_hard_breaks() {
        let cues = vec![Cue {
            start: 0.0,
            end: 1.0,
            text: "first half second half third half fourth".into(),
        }];
        let cli = Cli::parse_from(["subtitle_forge", "--input", "x.json", "--ass", "x.ass"]);
        let style = resolve_style(&cli).unwrap();
        let out = render_ass(&cues, &style, 20, 2);
        let dialogue = out
            .lines()
            .find(|l| l.starts_with("Dialogue: "))
            .expect("Dialogue line");
        assert!(dialogue.contains("\\N"), "expected a hard line break: {dialogue}");
    }

    #[test]
    fn ass_braces_in_cue_text_are_never_override_blocks() {
        let cues = vec![Cue { start: 0.0, end: 1.0, text: "a {b} c".into() }];
        let cli = Cli::parse_from(["subtitle_forge", "--input", "x.json", "--ass", "x.ass"]);
        let style = resolve_style(&cli).unwrap();
        let out = render_ass(&cues, &style, 42, 2);
        let dialogue = out
            .lines()
            .find(|l| l.starts_with("Dialogue: "))
            .expect("Dialogue line");
        assert!(dialogue.contains("\\{b\\}"), "brace not escaped: {dialogue}");
        assert!(!dialogue.contains(",{b},"), "raw braces leaked: {dialogue}");
    }

    #[test]
    fn style_defaults_scale_with_play_res() {
        let cli = Cli::parse_from([
            "subtitle_forge", "--input", "x.json", "--ass", "x.ass",
            "--play-res-x", "1280", "--play-res-y", "720",
        ]);
        let style = resolve_style(&cli).unwrap();
        assert_eq!(style.font_size, 36); // 5% of 720
        assert_eq!(style.margin_v, 72); // 10% of 720
        assert_eq!(style.margin_l, 80); // 6.25% of 1280
    }

    #[test]
    fn explicit_style_overrides_win() {
        let cli = Cli::parse_from([
            "subtitle_forge", "--input", "x.json", "--ass", "x.ass",
            "--font", "Open Sans", "--font-size", "60", "--margin-v", "90",
        ]);
        let style = resolve_style(&cli).unwrap();
        assert_eq!(style.font, "Open Sans");
        assert_eq!(style.font_size, 60);
        assert_eq!(style.margin_v, 90);
    }

    #[test]
    fn font_defaults_to_roboto_when_absent() {
        let cli = Cli::parse_from(["subtitle_forge", "--input", "x.json", "--ass", "x.ass"]);
        let style = resolve_style(&cli).unwrap();
        assert_eq!(style.font, FALLBACK_FONT);
        assert!(style.font_size > 0);
    }

    #[test]
    fn empty_font_falls_back_instead_of_emitting_a_broken_style() {
        let cli =
            Cli::parse_from(["subtitle_forge", "--input", "x.json", "--ass", "x.ass", "--font", ","]);
        let style = resolve_style(&cli).unwrap();
        assert_eq!(style.font, FALLBACK_FONT);
    }

    #[test]
    fn zero_play_res_is_a_usage_error() {
        let cli = Cli::parse_from([
            "subtitle_forge", "--input", "x.json", "--ass", "x.ass",
            "--play-res-y", "0",
        ]);
        assert!(matches!(resolve_style(&cli), Err(ForgeError::Usage(_))));
    }

    // -- end to end -------------------------------------------------------

    #[test]
    fn missing_outputs_is_a_usage_error() {
        let cli = Cli::parse_from(["subtitle_forge", "--input", "x.json"]);
        let err = run(&cli).unwrap_err();
        assert!(matches!(err, ForgeError::Usage(_)), "{err:?}");
    }

    #[test]
    fn round_trip_through_filesystem() {
        let dir = std::env::temp_dir().join(format!(
            "subtitle_forge_test_{}",
            std::process::id()
        ));
        // Start from a clean slate: a previous aborted run must not be able
        // to fail this test with leftover artefacts.
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let json_path = dir.join("t.json");
        let srt_path = dir.join("nested/out.srt");
        let ass_path = dir.join("out.ass");
        fs::write(
            &json_path,
            r#"{"segments":[{"start":0.0,"end":1.0,"text":"hello world"}]}"#,
        )
        .unwrap();

        let cli = Cli::parse_from([
            "subtitle_forge",
            "--input",
            json_path.to_str().unwrap(),
            "--srt",
            srt_path.to_str().unwrap(),
            "--ass",
            ass_path.to_str().unwrap(),
        ]);
        run(&cli).unwrap();

        let srt = fs::read_to_string(&srt_path).unwrap();
        assert!(srt.contains("00:00:00,000 --> 00:00:01,000"), "{srt}");
        assert!(srt.contains("hello world"), "{srt}");

        let ass = fs::read_to_string(&ass_path).unwrap();
        assert!(ass.contains("Dialogue: 0,0:00:00.00,0:00:01.00,"), "{ass}");
        assert!(ass.contains("hello world"), "{ass}");

        // The staging file must not survive a successful run.
        let leftovers: Vec<_> = fs::read_dir(&dir)
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().contains(".part"))
            .collect();
        assert!(leftovers.is_empty(), "stale .part files: {leftovers:?}");

        let _ = fs::remove_dir_all(&dir);
    }
}
