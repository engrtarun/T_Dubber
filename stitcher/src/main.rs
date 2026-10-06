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

// ---------------------------------------------------------------------------
// Timeline mixing: three loops, three kernels
// ---------------------------------------------------------------------------
//
// These are the only CPU-bound loops in the stitcher, and every one of them
// runs over the WHOLE timeline, once per dub. The AVX2 versions live in
// cpp_accelerator/kernels/stitcher_mix.asm, are assembled by build.rs, and are
// bit-identical to the scalar code here -- cpp_accelerator's
// mix_equivalence_test compares them sample for sample -- so the only decision
// here is speed, never accuracy.
//
// The scalar fallback is deliberately NOT the code this replaced. The original
// background loop was:
//
//     for (i, sample) in mix.iter_mut().enumerate() {
//         *sample = bg[i % bg.len()] * bg_vol;
//     }
//
// `i % bg.len()` is one 64-bit integer division per sample: 2.57M of them for
// the 107 s trailer this was written against, 172.8M for a two-hour feature
// film, each blocking a divider that neither pipelines nor overlaps the
// multiply beside it. The fallback below carries the read index and wraps it --
// the same wrap the kernel performs -- so a build without nasm is slower but no
// less correct, and does not pay for a division either.

#[cfg(td_stitcher_asm)]
mod mix_asm {
    /// Mirror of `TdMixSpan`: two u64s, 16 bytes.
    #[repr(C)]
    #[derive(Clone, Copy)]
    pub struct MixSpan {
        pub start: u64,
        pub end: u64,
    }

    /// Mirror of `TdStitcherLayParams`: out@0, bg@8, bg_len@16, total@24,
    /// bg_vol@32, 40 bytes total. The header's static_asserts pin those offsets;
    /// if a field is reordered here the kernel reads the wrong number silently.
    #[repr(C)]
    pub struct LayParams {
        pub out: *mut f32,
        pub bg: *const f32,
        pub bg_len: u64,
        pub total: u64,
        pub bg_vol: f32,
    }

    /// Mirror of `TdStitcherDuckParams`: out@0, duck_gain@8, spans@16,
    /// span_count@24, 32 bytes total.
    #[repr(C)]
    pub struct DuckParams {
        pub out: *mut f32,
        pub duck_gain: f32,
        pub spans: *const MixSpan,
        pub span_count: u64,
    }

    /// Mirror of `TdStitcherAddVoiceParams`: out@0, voice@8, count@16,
    /// dest@24, 32 bytes total.
    #[repr(C)]
    pub struct AddVoiceParams {
        pub out: *mut f32,
        pub voice: *const f32,
        pub count: u64,
        pub dest: u64,
    }

    extern "C" {
        pub fn td_stitcher_lay(p: *const LayParams);
        pub fn td_stitcher_duck(p: *const DuckParams);
        pub fn td_stitcher_add_voice(p: *const AddVoiceParams);
    }
}

/// Fill `mix` with the background, looped and scaled by `bg_vol`.
///
/// An empty background means silence, and the caller hands in a zeroed buffer,
/// so there is nothing to write.
fn lay_background(mix: &mut [f32], bg: &[f32], bg_vol: f32) {
    if bg.is_empty() {
        return;
    }
    #[cfg(td_stitcher_asm)]
    unsafe {
        mix_asm::td_stitcher_lay(&mix_asm::LayParams {
            out: mix.as_mut_ptr(),
            bg: bg.as_ptr(),
            bg_len: bg.len() as u64,
            total: mix.len() as u64,
            bg_vol,
        });
        return;
    }
    #[cfg(not(td_stitcher_asm))]
    {
        let mut j = 0usize;
        for sample in mix.iter_mut() {
            *sample = bg[j] * bg_vol;
            j += 1;
            if j == bg.len() {
                j = 0;
            }
        }
    }
}

/// Scale every span of `mix` in place by `gain`.
///
/// Spans are clamped to the timeline even though the caller already clamped
/// them: the kernel carries no timeline length, so a span reaching past the end
/// would write past the buffer. The slice range this replaced would have
/// panicked in that case, which is louder but only on the way down.
fn duck_spans(mix: &mut [f32], spans: &[(usize, usize)], gain: f32) {
    #[cfg(td_stitcher_asm)]
    unsafe {
        let total = mix.len() as u64;
        let raw: Vec<mix_asm::MixSpan> = spans
            .iter()
            .map(|&(start, end)| mix_asm::MixSpan {
                start: (start as u64).min(total),
                end: (end as u64).min(total),
            })
            .collect();
        if raw.is_empty() {
            return;
        }
        mix_asm::td_stitcher_duck(&mix_asm::DuckParams {
            out: mix.as_mut_ptr(),
            duck_gain: gain,
            spans: raw.as_ptr(),
            span_count: raw.len() as u64,
        });
    }
    #[cfg(not(td_stitcher_asm))]
    for &(start, end) in spans {
        let end = end.min(mix.len());
        if start < end {
            for sample in &mut mix[start..end] {
                *sample *= gain;
            }
        }
    }
}

/// `mix[dest..dest + count] += voice[..count]`, in place.
///
/// `count` must already be clamped by the caller: the kernel writes exactly the
/// count it is given rather than shortening an overlay silently, because a
/// truncated overlay drops the end of a spoken line.
fn overlay_voice(mix: &mut [f32], voice: &[f32], dest: usize, count: usize) {
    #[cfg(td_stitcher_asm)]
    unsafe {
        if count == 0 {
            return;
        }
        mix_asm::td_stitcher_add_voice(&mix_asm::AddVoiceParams {
            out: mix.as_mut_ptr(),
            voice: voice.as_ptr(),
            count: count as u64,
            dest: dest as u64,
        });
    }
    #[cfg(not(td_stitcher_asm))]
    {
        for (dst, &v) in mix[dest..dest + count].iter_mut().zip(voice.iter().take(count)) {
            *dst += v;
        }
    }
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
    lay_background(&mut mix, &bg, bg_vol);
    if !bg.is_empty() {
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
    duck_spans(&mut mix, &ducked, DUCK_FACTOR);
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
        overlay_voice(&mut mix, &voice, start_idx, take);
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
///
/// hound gates every `Sample` impl on what the *header* declares: `f32::read`
/// returns `InvalidSampleFormat` for integer PCM, and the integer types reject
/// float files symmetrically.  The element type therefore has to be chosen
/// from `spec.sample_format` — decoding everything as `f32` (the natural first
/// attempt) fails outright on the `PCM_16` WAVs that soundfile writes by
/// default, which is every file mazinger hands to this tool.
fn read_wav_mono(path: &str) -> Result<(Vec<f32>, u32), Box<dyn std::error::Error>> {
    let mut reader =
        hound::WavReader::open(path).map_err(|e| format!("cannot open WAV '{path}': {e}"))?;
    let spec = reader.spec();
    if spec.channels == 0 {
        return Err(format!("WAV '{path}' has 0 channels").into());
    }

    let raw: Vec<f32> = match spec.sample_format {
        // IEEE float: stored in [-1, 1] already, so pass straight through.
        hound::SampleFormat::Float => reader
            .samples::<f32>()
            .collect::<Result<Vec<f32>, hound::Error>>()
            .map_err(|e| format!("cannot decode float samples in '{path}': {e}"))?,
        hound::SampleFormat::Int => {
            // hound documents that `S` needs *at least* `bits_per_sample`
            // bits, and `i32::read` accepts every integer layout it can
            // parse: 8, 16, 24 and 32 bits, in either container width.
            // Scaling by 2^(bits-1) puts the result back in [-1, 1].
            let bits = spec.bits_per_sample;
            if !matches!(bits, 8 | 16 | 24 | 32) {
                return Err(format!("unsupported bit depth {bits} in '{path}'").into());
            }
            let scale = (1u64 << (bits - 1)) as f32;
            reader
                .samples::<i32>()
                .map(|s| s.map(|v| v as f32 / scale))
                .collect::<Result<Vec<f32>, hound::Error>>()
                .map_err(|e| format!("cannot decode integer samples in '{path}': {e}"))?
        }
    };

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

#[cfg(test)]
mod tests {
    use super::*;

    // -- helpers ----------------------------------------------------------

    fn scratch(name: &str) -> std::path::PathBuf {
        let mut p = env::temp_dir();
        p.push(format!("stitcher_ut_{}_{}", std::process::id(), name));
        p
    }

    fn write_i16(path: &Path, samples: &[i16], rate: u32) {
        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: rate,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut w = hound::WavWriter::create(path, spec).expect("create wav");
        for &s in samples {
            w.write_sample(s).expect("write sample");
        }
        w.finalize().expect("finalize wav");
    }

    fn write_f32(path: &Path, samples: &[f32], rate: u32) {
        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: rate,
            bits_per_sample: 32,
            sample_format: hound::SampleFormat::Float,
        };
        let mut w = hound::WavWriter::create(path, spec).expect("create wav");
        for &s in samples {
            w.write_sample(s).expect("write sample");
        }
        w.finalize().expect("finalize wav");
    }

    fn write_stereo_i16(path: &Path, left: &[i16], right: &[i16], rate: u32) {
        let spec = hound::WavSpec {
            channels: 2,
            sample_rate: rate,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut w = hound::WavWriter::create(path, spec).expect("create wav");
        for (&l, &r) in left.iter().zip(right.iter()) {
            w.write_sample(l).expect("write l");
            w.write_sample(r).expect("write r");
        }
        w.finalize().expect("finalize wav");
    }

    fn mean(samples: &[f32], lo_frac: f32, hi_frac: f32) -> f32 {
        let lo = (samples.len() as f32 * lo_frac) as usize;
        let hi = (samples.len() as f32 * hi_frac) as usize;
        let slice = &samples[lo..hi.max(lo + 1)];
        slice.iter().sum::<f32>() / slice.len() as f32
    }

    // -- WAV decoding -----------------------------------------------------

    /// Regression: integer PCM must decode.  `hound`'s `f32` sample type
    /// rejects `SampleFormat::Int` outright, so decoding every file as `f32`
    /// fails on the PCM_16 WAVs soundfile writes by default — i.e. on every
    /// input the pipeline actually produces.  This test is the reason the
    /// decode path branches on `spec.sample_format`.
    #[test]
    fn reads_integer_pcm_wav() {
        let path = scratch("int16.wav");
        // -1.0, -0.5, 0.0, 0.5, and just under +1.0
        let src: Vec<i16> = [-32768, -16384, 0, 16384, 32767].to_vec();
        write_i16(&path, &src, 24_000);

        let (pcm, rate) = read_wav_mono(&path.to_string_lossy()).expect("int16 must decode");
        let _ = fs::remove_file(&path);

        assert_eq!(rate, 24_000);
        assert_eq!(pcm.len(), src.len());
        for (got, want) in pcm.iter().zip(src.iter()) {
            let want = *want as f32 / 32768.0;
            assert!(
                (got - want).abs() < 1e-6,
                "int16 sample decoded as {got}, want {want}"
            );
        }
    }

    /// Float WAVs must keep decoding too — the branch must not favour one
    /// format at the expense of the other.
    #[test]
    fn reads_float_wav() {
        let path = scratch("float32.wav");
        let src: Vec<f32> = [-1.0, -0.25, 0.0, 0.25, 0.9].to_vec();
        write_f32(&path, &src, 24_000);

        let (pcm, rate) = read_wav_mono(&path.to_string_lossy()).expect("float must decode");
        let _ = fs::remove_file(&path);

        assert_eq!(rate, 24_000);
        assert_eq!(pcm.len(), src.len());
        for (got, want) in pcm.iter().zip(src.iter()) {
            assert!((got - want).abs() < 1e-6, "float sample {got} != {want}");
        }
    }

    #[test]
    fn downmixes_stereo_by_averaging() {
        let path = scratch("stereo.wav");
        let left: Vec<i16> = vec![16384; 8]; // ~+0.5
        let right: Vec<i16> = vec![0; 8]; //  0.0
        write_stereo_i16(&path, &left, &right, 24_000);

        let (pcm, _) = read_wav_mono(&path.to_string_lossy()).expect("stereo must decode");
        let _ = fs::remove_file(&path);

        assert_eq!(pcm.len(), 8, "channel count must be folded away");
        for got in &pcm {
            assert!((got - 0.25).abs() < 1e-3, "downmix {got}, want ~0.25");
        }
    }

    #[test]
    fn missing_file_is_an_error() {
        let err = read_wav_mono("Z:\\definitely\\not\\here.wav");
        assert!(err.is_err(), "a missing file must not decode as silence");
    }

    // -- resampler --------------------------------------------------------

    #[test]
    fn resample_is_a_noop_at_matching_rate() {
        let input: Vec<f32> = (0..100).map(|i| i as f32 / 100.0).collect();
        let out = resample_linear(&input, 24_000, 24_000);
        assert_eq!(out, input);
    }

    #[test]
    fn resample_halves_the_length_when_downsampling() {
        let input = vec![0.5f32; 96_000]; // 4 s at 48 kHz
        let out = resample_linear(&input, 48_000, 24_000);
        assert!((out.len() as i64 - 48_000).abs() <= 1, "len {}", out.len());
        for v in &out {
            assert!((v - 0.5).abs() < 1e-6, "constant signal must stay constant");
        }
    }

    // -- end-to-end mix ---------------------------------------------------

    /// Drive `run()` with a real timeline: background laid first, halved
    /// under the voice, voice added at unity.
    #[test]
    fn run_mixes_background_ducking_and_voice() {
        let work = env::temp_dir().join(format!("stitcher_ut_{}_mix", std::process::id()));
        let _ = fs::remove_dir_all(&work);
        fs::create_dir_all(&work).expect("scratch dir");

        let seg = work.join("seg.wav");
        let bg = work.join("bg.wav");
        let out = work.join("out.wav");
        let tl = work.join("timeline.json");

        // 0.5 for 1 s of voice; 0.4 for 4 s of background.
        write_i16(&seg, &vec![16_384i16; 24_000], 24_000);
        write_i16(&bg, &vec![13_107i16; 96_000], 24_000);

        let json = serde_json::json!({
            "duration": 4.0,
            "background_audio": bg.to_string_lossy().into_owned(),
            "background_volume": 0.5,
            "segments": [{
                "start": 1.0,
                "end": 2.0,
                "file": seg.to_string_lossy().into_owned(),
            }],
            "output": out.to_string_lossy().into_owned(),
        });
        fs::write(&tl, json.to_string()).expect("write timeline");

        run(&tl.to_string_lossy()).expect("run must succeed");

        let (pcm, rate) = read_wav_mono(&out.to_string_lossy()).expect("output readable");
        let _ = fs::remove_dir_all(&work);

        assert_eq!(rate, 24_000);
        assert_eq!(pcm.len(), 96_000, "4 s at 24 kHz");

        let bg_only = 0.4 * 0.5; // declared volume
        let bg_ducked = bg_only * DUCK_FACTOR;

        let before = mean(&pcm, 0.05, 0.20); // background, no voice
        let during = mean(&pcm, 0.30, 0.45); // voice over background
        let after = mean(&pcm, 0.75, 0.95); // background again

        assert!((before - bg_only).abs() < 0.01, "bg {before}, want {bg_only}");
        assert!((after - bg_only).abs() < 0.01, "bg {after}, want {bg_only}");
        assert!(
            (during - (0.5 + bg_ducked)).abs() < 0.01,
            "during {during}, want {}",
            0.5 + bg_ducked
        );
    }
}
