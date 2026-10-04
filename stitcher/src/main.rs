//! stitcher — fast WAV timeline mixer for the T_Dubber pipeline.
//!
//! Reads a `timeline.json` describing a background track plus voice
//! segments, mixes them into one 24 kHz mono WAV with broadcast-style
//! ducking, and writes the result.
//!
//! Timeline schema:
//! ```json
//! {
//!   "duration": 60.5,
//!   "background_audio": "/path/to/bg.wav",
//!   "background_volume": 0.2,
//!   "segments": [
//!     {"start": 1.2, "end": 4.5, "file": "/path/to/seg1.wav"},
//!     {"start": 5.0, "end": 7.1, "file": "/path/to/seg2.wav"}
//!   ],
//!   "output": "/path/to/final_dubbed.wav"
//! }
//! ```
//!
//! Mixing order (per output sample):
//!   out = ducked_background + voice
//!
//! Any number of segments is supported, so this scales to the "dozens of
//! WAV segments over a ducked background" case that makes the numpy path
//! slow and memory-heavy.

use std::env;
use std::fs;
use std::path::Path;
use std::process::ExitCode;
use std::time::Instant;

use serde::Deserialize;

/// Output sample rate. Inputs at other rates are linearly resampled
/// to this (fast path: no-op when already 24 kHz).
const TARGET_SAMPLE_RATE: u32 = 24_000;
/// Background gain while any voice segment is active (50% dip).
const DUCK_FACTOR: f32 = 0.5;
/// Sanity cap: 12 h (the Kaggle session limit). Bounds the mix buffer.
const MAX_DURATION_SECS: f64 = 12.0 * 3600.0;

macro_rules! slog {
    ($t0:expr, $($arg:tt)*) => {
        eprintln!(
            "[stitcher] [{:>7.2}s] {}",
            $t0.elapsed().as_secs_f32(),
            format!($($arg)*)
        )
    };
}

#[derive(Debug, Deserialize)]
struct Segment {
    start: f64,
    end: f64,
    file: String,
}

#[derive(Debug, Deserialize)]
struct Timeline {
    duration: f64,
    #[serde(default)]
    background_audio: String,
    background_volume: f64,
    #[serde(default)]
    segments: Vec<Segment>,
    output: String,
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();
    if args.len() != 2 {
        eprintln!("usage: stitcher <timeline.json>");
        return ExitCode::from(1);
    }
    match run(&args[1]) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("[stitcher] ERROR: {err}");
            ExitCode::from(2)
        }
    }
}

fn run(timeline_path: &str) -> Result<(), Box<dyn std::error::Error>> {
    let t0 = Instant::now();
    slog!(t0, "reading timeline {timeline_path}");

    let raw = fs::read_to_string(timeline_path)
        .map_err(|e| format!("cannot read timeline '{timeline_path}': {e}"))?;
    let tl: Timeline = serde_json::from_str(&raw)
        .map_err(|e| format!("invalid timeline JSON in '{timeline_path}': {e}"))?;

    // ---- Validate -------------------------------------------------------
    if !tl.duration.is_finite() || tl.duration <= 0.0 {
        return Err(format!("bad duration: {}", tl.duration).into());
    }
    if tl.duration > MAX_DURATION_SECS {
        return Err(format!(
            "duration {}s exceeds cap of {}s",
            tl.duration, MAX_DURATION_SECS
        )
        .into());
    }
    if !tl.background_volume.is_finite() || !(0.0..=1.0).contains(&tl.background_volume) {
        return Err(format!(
            "background_volume must be in [0, 1], got {}",
            tl.background_volume
        )
        .into());
    }
    if tl.output.trim().is_empty() {
        return Err("output path is empty".into());
    }

    let total_samples = (tl.duration * f64::from(TARGET_SAMPLE_RATE)).round() as usize;
    if total_samples == 0 {
        return Err("duration rounds to zero samples".into());
    }
    slog!(
        t0,
        "timeline: {:.2}s -> {} samples @ {} Hz, {} segment(s), bg vol {}",
        tl.duration,
        total_samples,
        TARGET_SAMPLE_RATE,
        tl.segments.len(),
        tl.background_volume
    );

    // ---- Background (looped to `duration`, empty path = silence) --------
    let bg_vol = tl.background_volume as f32;
    let bg: Vec<f32> = if tl.background_audio.trim().is_empty() {
        slog!(t0, "no background track, mixing voice over silence");
        Vec::new()
    } else {
        let (pcm, rate) = read_wav_mono(&tl.background_audio)?;
        slog!(
            t0,
            "background: {} samples ({} Hz -> {} Hz)",
            pcm.len(),
            rate,
            TARGET_SAMPLE_RATE
        );
        resample_linear(&pcm, rate, TARGET_SAMPLE_RATE)
    };

    // Pre-sized mix buffer (~960 KB per minute @ 24 kHz f32 mono).
    let mut mix = vec![0f32; total_samples];
    if !bg.is_empty() {
        for (i, sample) in mix.iter_mut().enumerate() {
            *sample = bg[i % bg.len()] * bg_vol;
        }
        slog!(t0, "background laid (looped to {:.2}s)", tl.duration);
    }

    // ---- Ducking spans: union of voice ranges, background only ----------
    let mut spans: Vec<(usize, usize)> = Vec::with_capacity(tl.segments.len());
    for seg in &tl.segments {
        if !seg.start.is_finite() || !seg.end.is_finite() {
            return Err(format!("non-finite span in '{}'", seg.file).into());
        }
        if seg.start < 0.0 || seg.end <= seg.start {
            return Err(format!("bad span [{}, {}] in '{}'", seg.start, seg.end, seg.file).into());
        }
        let start_idx =
            ((seg.start * f64::from(TARGET_SAMPLE_RATE)).round() as usize).min(total_samples);
        let end_idx =
            ((seg.end * f64::from(TARGET_SAMPLE_RATE)).round() as usize).min(total_samples);
        if start_idx < end_idx {
            spans.push((start_idx, end_idx));
        }
    }
    spans.sort_unstable();
    let mut ducked: Vec<(usize, usize)> = Vec::with_capacity(spans.len());
    for (start, end) in spans {
        match ducked.last_mut() {
            Some(last) if start <= last.1 => {
                if end > last.1 {
                    last.1 = end;
                }
            }
            _ => ducked.push((start, end)),
        }
    }
    for (start, end) in &ducked {
        for sample in &mut mix[*start..*end] {
            *sample *= DUCK_FACTOR;
        }
    }
    slog!(
        t0,
        "ducking applied over {} span(s) x {:.2}",
        ducked.len(),
        DUCK_FACTOR
    );

    // ---- Voice overlay (unity gain, truncated to span + timeline) -------
    for (n, seg) in tl.segments.iter().enumerate() {
        let (pcm, rate) = read_wav_mono(&seg.file)?;
        let voice = resample_linear(&pcm, rate, TARGET_SAMPLE_RATE);
        let start_idx =
            ((seg.start * f64::from(TARGET_SAMPLE_RATE)).round() as usize).min(total_samples);
        let window = ((seg.end - seg.start) * f64::from(TARGET_SAMPLE_RATE)).round() as usize;
        let take = voice.len().min(window).min(total_samples - start_idx);
        for (i, &v) in voice.iter().take(take).enumerate() {
            mix[start_idx + i] += v;
        }
        slog!(
            t0,
            "segment {}/{}: {} samples @ {:.2}s ({} Hz src)",
            n + 1,
            tl.segments.len(),
            take,
            seg.start,
            rate
        );
    }

    // ---- Write 16-bit PCM (hard-clipped, parent dirs created) -----------
    if let Some(parent) = Path::new(&tl.output).parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)
                .map_err(|e| format!("cannot create output dir '{}': {e}", parent.display()))?;
        }
    }
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: TARGET_SAMPLE_RATE,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut writer = hound::WavWriter::create(&tl.output, spec)
        .map_err(|e| format!("cannot create '{}': {e}", tl.output))?;
    for &s in &mix {
        let q = (s.clamp(-1.0, 1.0) * 32767.0).round() as i16;
        writer
            .write_sample(q)
            .map_err(|e| format!("write failed for '{}': {e}", tl.output))?;
    }
    writer
        .finalize()
        .map_err(|e| format!("finalize failed for '{}': {e}", tl.output))?;
    slog!(t0, "wrote {} ({} samples)", tl.output, total_samples);
    Ok(())
}

/// Read any PCM/float WAV as mono f32 in [-1.0, 1.0].
/// Multi-channel input is downmixed by averaging; the source rate is
/// returned so the caller can resample to the target rate.
fn read_wav_mono(path: &str) -> Result<(Vec<f32>, u32), Box<dyn std::error::Error>> {
    let mut reader =
        hound::WavReader::open(path).map_err(|e| format!("cannot open WAV '{path}': {e}"))?;
    let spec = reader.spec();
    if spec.channels == 0 {
        return Err(format!("WAV '{path}' has 0 channels").into());
    }
    let raw: Vec<f32> = reader
        .samples::<f32>()
        .collect::<Result<Vec<f32>, hound::Error>>()
        .map_err(|e| format!("cannot decode samples in '{path}': {e}"))?;
    let channels = usize::from(spec.channels);
    if channels == 1 {
        return Ok((raw, spec.sample_rate));
    }
    let frames = raw.len() / channels;
    let mut mono = Vec::with_capacity(frames);
    for f in 0..frames {
        let mut acc = 0.0f32;
        for c in 0..channels {
            acc += raw[f * channels + c];
        }
        mono.push(acc / channels as f32);
    }
    Ok((mono, spec.sample_rate))
}

/// Linear-interpolation resampler. Exact no-op when rates match.
fn resample_linear(input: &[f32], src_rate: u32, dst_rate: u32) -> Vec<f32> {
    if input.is_empty() || src_rate == dst_rate || src_rate == 0 || dst_rate == 0 {
        return input.to_vec();
    }
    let step = src_rate as f64 / dst_rate as f64; // src samples per dst sample
    let out_len = ((input.len() as f64 / step).round() as usize).max(1);
    let mut out = Vec::with_capacity(out_len);
    let last = input.len() - 1;
    for i in 0..out_len {
        let pos = i as f64 * step;
        let idx = pos.floor() as usize;
        let frac = (pos - idx as f64) as f32;
        let a = input[idx.min(last)];
        let b = input[(idx + 1).min(last)];
        out.push(a + (b - a) * frac);
    }
    out
}
