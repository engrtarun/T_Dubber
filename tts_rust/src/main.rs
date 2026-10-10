//! `tdub_tts` -- CPU-only text-to-speech for the T_Dubber dubbing pipeline.
//!
//! WHY RUST AND NOT PYTHON
//! -----------------------
//! Kaggle run `test4_gotgVERSION` spent 1067 s pip-installing vLLM + PyTorch +
//! CUDA and then died at `import` time with
//! `ImportError: libcudart.so.13: cannot open shared object file`. This binary
//! is the same bet as the llama.cpp / whisper.cpp binaries agent 1 shipped:
//! no interpreter, no site-packages, no CUDA, one file to place.
//!
//! CPU-ONLY, ENFORCED IN TWO PLACES
//! --------------------------------
//! 1. `Cargo.toml` pins `any-tts` with `default-features = false` and only
//!    `["vibevoice", "download"]`. any-tts' `cuda` feature maps straight onto
//!    `candle-core/cuda` + `candle-nn/cuda` + `candle-transformers/cuda`.
//! 2. `--device` below accepts exactly one value, `cpu`. Anything else is a
//!    usage error, not a fallback.
//!
//! WHY VIBEVOICE-1.5B AND NOTHING ELSE
//! -----------------------------------
//! mazinger's TTS is voice *cloning*: `create_voice_clone_prompt(ref_audio=...)`,
//! `--voice-sample`. In the any-tts README, the "What does not work yet in the
//! Rust backend" section says, for OmniVoice, "Reference-audio voice cloning.";
//! for Qwen3-TTS, "reference-audio cloning is not implemented in this crate
//! yet"; VibeVoice-Realtime has "Reference-audio input" listed as absent; Voxtral
//! has no reference-audio encoder weights. VibeVoice-1.5B is the only backend
//! whose "What works in any-tts today" list includes `reference_audio`. That is
//! the whole selection criterion. Do not substitute a backend to save bytes.
//!
//! LOAD THE MODEL ONCE
//! -------------------
//! `--text-file` takes one segment per line and loads VibeVoice exactly once.
//! Reloading a 1.5B checkpoint per line is the difference between a dubbing
//! pipeline and a denial of service, so this is a correctness property, not a
//! nicety. `test_tts_bridge.py` asserts the process count.
//!
//! EXIT CODES ARE PART OF THE CONTRACT
//! -----------------------------------
//! A silent success carrying an empty WAV is the exact failure that dubbed two
//! hours of silence once already, so nothing here can succeed vacuously.

use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Instant;

use any_tts::{
    load_model, AudioSamples, DeviceSelection, ModelType, ReferenceAudio, SynthesisRequest,
    TtsConfig, TtsError,
};
use serde_json::{json, Value};

/// Everything worked. Not "worked unless you look closely".
const EX_OK: u8 = 0;
/// Bad command line. Nothing was loaded, nothing was written.
const EX_USAGE: u8 = 2;
/// Weights missing, unreadable, or rejected by any-tts. Distinct from EX_SYNTH:
/// retrying a synthesis will not fix a bad path.
const EX_MODEL_LOAD: u8 = 3;
/// `--ref-audio` absent when required, or undecodable.
const EX_REF_AUDIO: u8 = 4;
/// The model loaded and then failed to produce audio.
const EX_SYNTH: u8 = 5;
/// Synthesis succeeded but the WAV could not be written. Treated as failure on
/// purpose: a synthesized buffer that never reached disk is not a dub.
const EX_WRITE: u8 = 6;
/// Synthesis "succeeded" and produced zero samples. Never a success by default.
const EX_EMPTY_AUDIO: u8 = 7;

const USAGE: &str = "\
tdub_tts -- CPU-only VibeVoice TTS (no CUDA, no PyTorch)

USAGE
  tdub_tts --model <dir> --text <string> --out <wav> [options]
  tdub_tts --model <dir> --text-file <path> --out-dir <dir> [options]
  tdub_tts --model <dir> --probe --json

INPUT
  --model <dir>          VibeVoice-1.5B snapshot directory. Required.
                         Must contain config.json, tokenizer.json and
                         model*.safetensors.
  --text <string>        One segment to speak.
  --text-file <path>     One segment per line; blank lines are skipped.
                         Loaded model is reused for every line.
  --out <wav>            Output WAV. Required with --text.
  --out-dir <dir>        Output directory. Required with --text-file.
  --out-prefix <name>    Filename stem inside --out-dir (default: seg).
  --ref-audio <wav|mp3>  Reference clip for zero-shot voice cloning.
                         Decoded ONCE and reused for every segment.

VOICE CLONING
  --require-ref-audio    Fail with exit 4 if --ref-audio is absent. Use this
                         whenever the caller asked for a clone: silently
                         speaking in a default voice is worse than failing.

SYNTHESIS
  --language <code>      e.g. en, hi, zh. Backend exposes auto|multilingual.
  --instruct <text>      Style instruction passed to the backend.
  --max-tokens <n>       Generation cap.
  --temperature <f64>    Sampling temperature (default: backend default).
  --cfg-scale <f64>      Classifier-free guidance scale (default: 1.3).
  --seed <u64>           Reproducibility seed (sets VIBEVOICE_SEED).
  --keep-going           On a per-segment failure, finish the remaining
                         segments and report the failures in --json. OFF by
                         default: a half-dubbed film is not a result.

RUNTIME
  --device <cpu>         Only 'cpu' is accepted. There is no GPU path in this
                         binary, by design.
  --probe                Load the model, report its metadata, synthesize
                         nothing. The feasibility check.
  --json                 Machine-readable report on stdout.
  --allow-empty          Permit zero-sample output. Off by default.

EXIT CODES
  0 ok
  2 usage error
  3 model load failure
  4 missing / undecodable reference audio
  5 synthesis failure
  6 output write failure
  7 empty audio (unless --allow-empty)
";

// --------------------------------------------------------------------------- //
// Arguments
// --------------------------------------------------------------------------- //

#[derive(Debug)]
struct Args {
    model: Option<PathBuf>,
    text: Option<String>,
    text_file: Option<PathBuf>,
    out: Option<PathBuf>,
    out_dir: Option<PathBuf>,
    out_prefix: String,
    ref_audio: Option<PathBuf>,
    require_ref_audio: bool,
    language: Option<String>,
    instruct: Option<String>,
    max_tokens: Option<usize>,
    temperature: Option<f64>,
    cfg_scale: Option<f64>,
    seed: Option<u64>,
    keep_going: bool,
    probe: bool,
    json: bool,
    allow_empty: bool,
}

impl Default for Args {
    fn default() -> Self {
        Self {
            model: None,
            text: None,
            text_file: None,
            out: None,
            out_dir: None,
            out_prefix: "seg".to_string(),
            ref_audio: None,
            require_ref_audio: false,
            language: None,
            instruct: None,
            max_tokens: None,
            temperature: None,
            cfg_scale: None,
            seed: None,
            keep_going: false,
            probe: false,
            json: false,
            allow_empty: false,
        }
    }
}

/// A clean, coded failure. No `Result` with a `String` error: every exit path
/// has to name its own code, which is the part callers actually branch on.
struct Failure {
    code: u8,
    message: String,
}

impl Failure {
    fn new(code: u8, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    fn usage(message: impl Into<String>) -> Self {
        Self::new(EX_USAGE, message)
    }
}

fn parse_args(argv: Vec<String>) -> Result<Option<Args>, Failure> {
    let mut args = Args::default();
    let mut cursor = 0usize;

    while cursor < argv.len() {
        let token = argv[cursor].clone();
        cursor += 1;

        // `--flag value` and `--flag=value` are both accepted. The second form
        // is what a shell-quoted segment containing a trailing '=' produces.
        let (flag, inline) = match token.split_once('=') {
            Some((name, value)) if name.starts_with("--") => {
                (name.to_string(), Some(value.to_string()))
            }
            _ => (token, None),
        };

        // Taking the value without a nested iterator borrow: the closure form
        // does not compile against `cursor`, and a hand-rolled cursor is the
        // cheapest way to keep `--flag=value` and `--flag value` in one place.
        macro_rules! value {
            () => {{
                if let Some(value) = inline.clone() {
                    value
                } else if cursor < argv.len() {
                    let value = argv[cursor].clone();
                    cursor += 1;
                    value
                } else {
                    return Err(Failure::usage(format!("{} requires a value", flag)));
                }
            }};
        }

        match flag.as_str() {
            "--model" => args.model = Some(PathBuf::from(value!())),
            "--text" => args.text = Some(value!()),
            "--text-file" => args.text_file = Some(PathBuf::from(value!())),
            "--out" => args.out = Some(PathBuf::from(value!())),
            "--out-dir" => args.out_dir = Some(PathBuf::from(value!())),
            "--out-prefix" => args.out_prefix = value!(),
            "--ref-audio" => args.ref_audio = Some(PathBuf::from(value!())),
            "--language" => args.language = Some(value!()),
            "--instruct" => args.instruct = Some(value!()),
            "--max-tokens" => args.max_tokens = Some(parse_number(&flag, value!())?),
            "--temperature" => args.temperature = Some(parse_number(&flag, value!())?),
            "--cfg-scale" => args.cfg_scale = Some(parse_number(&flag, value!())?),
            "--seed" => args.seed = Some(parse_number(&flag, value!())?),
            // A GPU request is a usage error, never a silent downgrade to CPU.
            // Falling back quietly is how a box ends up burning 1067 s on a
            // stack it was told not to install.
            "--device" => {
                let value = value!();
                if value != "cpu" {
                    return Err(Failure::usage(format!(
                        "--device '{}' is not available in this binary. \
                         tdub_tts is CPU-only by construction: no CUDA feature is \
                         compiled in and no GPU backend exists to fall back to.",
                        value
                    )));
                }
            }
            "--require-ref-audio" => args.require_ref_audio = true,
            "--keep-going" => args.keep_going = true,
            "--probe" => args.probe = true,
            "--json" => args.json = true,
            "--allow-empty" => args.allow_empty = true,
            "--help" | "-h" => return Ok(None),
            other => {
                return Err(Failure::usage(format!(
                    "unknown argument '{}'\n\n{}",
                    other, USAGE
                )))
            }
        }
    }
    Ok(Some(args))
}

fn parse_number<T: std::str::FromStr>(flag: &str, raw: String) -> Result<T, Failure> {
    raw.trim()
        .parse::<T>()
        .map_err(|_| Failure::usage(format!("{} expects a number, got '{}'", flag, raw)))
}

// --------------------------------------------------------------------------- //
// Preflight
// --------------------------------------------------------------------------- //

/// Assets the any-tts README lists as required for VibeVoice. Reported as
/// warnings rather than enforced, so the crate stays the authority on what it
/// actually needs -- but an operator gets told in one line instead of reading
/// a candle error 40 lines deep.
const REQUIRED_ASSETS: &[&str] = &["config.json", "tokenizer.json"];

fn preflight_model(model: &Path) -> Result<Vec<String>, Failure> {
    if !model.exists() {
        return Err(Failure::new(
            EX_MODEL_LOAD,
            format!(
                "model directory not found: {}\n\
                 Expected a microsoft/VibeVoice-1.5B snapshot: config.json, \
                 tokenizer.json and model*.safetensors in one directory.",
                model.display()
            ),
        ));
    }
    if !model.is_dir() {
        return Err(Failure::new(
            EX_MODEL_LOAD,
            format!("model path is not a directory: {}", model.display()),
        ));
    }
    let mut warnings = Vec::new();
    for asset in REQUIRED_ASSETS {
        if !model.join(asset).is_file() {
            warnings.push(format!("{} not found in {}", asset, model.display()));
        }
    }
    // No weights is a HARD error, not a warning. It used to be a warning, on
    // the principle "let the crate stay the authority on what it needs" --
    // and that principle is what made this binary hang for 5+ minutes on an
    // empty model dir.
    //
    // Why: any-tts resolves model files in tiers, and the LAST tier is a
    // Hugging Face fetch (the `download` feature). A dir with no
    // *.safetensors therefore passes preflight, reaches load_model, and the
    // crate goes to the hub for microsoft/VibeVoice-1.5B -- 5.4 GB, three
    // shards. On a Kaggle worker with the pack mounted wrong, a typo in
    // --model, or a half-copied cache, that is not an error message, it is a
    // job that quietly burns its wall clock until the kernel is killed.
    //
    // A missing weight is unambiguous: there is nothing to warn about and
    // continue with. config.json / tokenizer.json stay warnings, because
    // any-tts can derive or default those and the published snapshot's
    // layout is the crate's call, not ours.
    let has_weights = model
        .read_dir()
        .map(|entries| {
            entries.flatten().any(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .ends_with(".safetensors")
            })
        })
        .unwrap_or(false);
    if !has_weights {
        return Err(Failure::new(
            EX_MODEL_LOAD,
            format!(
                "no *.safetensors in {}\n\
                 VibeVoice-1.5B needs model.safetensors or \
                 model-*-of-*.safetensors in the same directory.\n\
                 Refusing to fall back to a Hugging Face download: that is a \
                 5.4 GB fetch which would hang the worker instead of \
                 failing it.\n\
                 Expected files: config.json, tokenizer.json, \
                 model-00001-of-00003.safetensors .. model-00003-of-00003.safetensors.",
                model.display()
            ),
        ));
    }
    Ok(warnings)
}

/// Read the reference clip ONCE. Per-segment re-decoding of a 10 s WAV is
/// wasteful, and worse, re-decoding can disagree with itself if the source is
/// being written concurrently.
fn load_reference(path: Option<&PathBuf>) -> Result<Option<ReferenceAudio>, Failure> {
    let Some(path) = path else {
        return Ok(None);
    };
    if !path.is_file() {
        return Err(Failure::new(
            EX_REF_AUDIO,
            format!(
                "reference audio not found: {}\n\
                 Voice cloning needs a real clip. Point this at a WAV/MP3 of \
                 3-10 s of clean single-speaker audio, or drop --require-ref-audio \
                 only if a default voice is genuinely intended.",
                path.display()
            ),
        ));
    }
    let decoded = AudioSamples::from_audio_file(path).map_err(|err| {
        Failure::new(
            EX_REF_AUDIO,
            format!("could not decode reference audio {}: {}", path.display(), err),
        )
    })?;
    if decoded.is_empty() {
        return Err(Failure::new(
            EX_REF_AUDIO,
            format!("reference audio {} decoded to zero samples", path.display()),
        ));
    }
    Ok(Some(ReferenceAudio::new(decoded.samples, decoded.sample_rate)))
}

fn read_segments(args: &Args) -> Result<Vec<(usize, String)>, Failure> {
    let mut segments: Vec<(usize, String)> = Vec::new();

    if let Some(text) = &args.text {
        let trimmed = text.trim();
        if trimmed.is_empty() {
            return Err(Failure::usage("--text is empty; refusing to speak nothing"));
        }
        segments.push((0, text.clone()));
    }

    if let Some(path) = &args.text_file {
        if !path.is_file() {
            return Err(Failure::usage(format!(
                "--text-file not found: {}",
                path.display()
            )));
        }
        let body = std::fs::read_to_string(path).map_err(|err| {
            Failure::usage(format!("could not read {}: {}", path.display(), err))
        })?;
        for line in body.lines() {
            let text = line.trim();
            // A dubbing ledger is full of blank separators. Skipping them is
            // correct; synthesising them would emit N seconds of silence.
            if text.is_empty() {
                continue;
            }
            segments.push((segments.len(), text.to_string()));
        }
        if segments.is_empty() {
            return Err(Failure::usage(format!(
                "{} contained no non-empty segments",
                path.display()
            )));
        }
    }

    Ok(segments)
}

fn output_path(args: &Args, index: usize) -> PathBuf {
    match (&args.out_dir, &args.out) {
        (Some(dir), _) => dir.join(format!("{}-{:04}.wav", args.out_prefix, index)),
        (None, Some(out)) => out.clone(),
        (None, None) => PathBuf::from(format!("{}-{:04}.wav", args.out_prefix, index)),
    }
}

fn build_request(text: &str, args: &Args, reference: Option<&ReferenceAudio>) -> SynthesisRequest {
    let mut request = SynthesisRequest::new(text);
    if let Some(language) = &args.language {
        request = request.with_language(language.clone());
    }
    if let Some(instruct) = &args.instruct {
        request = request.with_instruct(instruct.clone());
    }
    if let Some(max_tokens) = args.max_tokens {
        request = request.with_max_tokens(max_tokens);
    }
    if let Some(temperature) = args.temperature {
        request = request.with_temperature(temperature);
    }
    if let Some(cfg_scale) = args.cfg_scale {
        request = request.with_cfg_scale(cfg_scale);
    }
    if let Some(reference) = reference {
        // NOTE: this takes an already-decoded `ReferenceAudio`, NOT a path.
        // The mission brief said `with_reference_audio(path)`; the crate's
        // signature is `with_reference_audio(impl Into<ReferenceAudio>)`
        // only in spirit -- it takes the struct. Hence the decode above.
        request = request.with_reference_audio(reference.clone());
    }
    // `with_voice` and `with_speed` are deliberately NOT wired: VibeVoiceModel
    // ::validate_request() rejects both outright, so exposing them would be an
    // option that always fails at synthesis time.
    request
}

// --------------------------------------------------------------------------- //
// Error mapping
// --------------------------------------------------------------------------- //

/// Map any-tts errors onto this binary's exit-code contract. Load-time and
/// synthesis-time errors are separated by the caller, not guessed at here.
fn describe(err: &TtsError) -> String {
    format!("{}", err)
}

// --------------------------------------------------------------------------- //
// Main
// --------------------------------------------------------------------------- //

fn main() -> ExitCode {
    // A panic escaping main() is still a non-zero exit, but the caller cannot
    // classify it against the contract below. One identifiable line on stderr
    // turns "exit code 101" into "this is our bug, not a missing model".
    std::panic::set_hook(Box::new(|info| {
        eprintln!(
            "tdub_tts: internal panic -- this is a bug in tdub_tts, not a model \
             or environment problem: {}",
            info
        );
    }));

    let argv: Vec<String> = std::env::args().skip(1).collect();

    let args = match parse_args(argv) {
        Ok(None) => {
            println!("{}", USAGE);
            return ExitCode::from(EX_OK);
        }
        Ok(Some(args)) => args,
        Err(failure) => return report(failure, false),
    };

    match run(&args) {
        Ok(outcome) => {
            if args.json {
                println!("{}", outcome.report);
            }
            ExitCode::from(outcome.code)
        }
        Err(failure) => report(failure, args.json),
    }
}

fn report(failure: Failure, as_json: bool) -> ExitCode {
    if as_json {
        // Both channels, on purpose. stdout is what a machine reads; stderr is
        // what a human reading the Kaggle log sees. Emitting the error only on
        // one of them is how it disappears.
        println!(
            "{}",
            json!({
                "ok": false,
                "error": {"code": failure.code, "message": failure.message},
            })
        );
    }
    eprintln!("tdub_tts: {}", failure.message);
    ExitCode::from(failure.code)
}

/// A completed run. `code` is separate from "we got here" because
/// `--keep-going` deliberately finishes the surviving segments after a
/// failure -- and a partial dub must still exit non-zero, or the caller files
/// it as a success.
struct Outcome {
    code: u8,
    report: String,
}

fn run(args: &Args) -> Result<Outcome, Failure> {
    let Some(model_dir) = &args.model else {
        return Err(Failure::usage(format!("--model is required\n\n{}", USAGE)));
    };

    let model_dir = model_dir.clone();
    let warnings = preflight_model(&model_dir)?;

    // Seed before loading: generation_seed() reads this env var, and it is read
    // per synthesize() call, so setting it here covers the whole run.
    if let Some(seed) = args.seed {
        std::env::set_var("VIBEVOICE_SEED", seed.to_string());
    }

    let load_started = Instant::now();
    let model = load_model(
        TtsConfig::new(ModelType::VibeVoice)
            .with_model_path(model_dir.to_string_lossy().to_string())
            .with_device(DeviceSelection::Cpu),
    )
    .map_err(|err| {
        Failure::new(
            EX_MODEL_LOAD,
            format!(
                "could not load VibeVoice-1.5B from {}: {}",
                model_dir.display(),
                describe(&err)
            ),
        )
    })?;
    let load_ms = load_started.elapsed().as_millis() as u64;

    let info = model.model_info();
    let model_report = json!({
        "name": info.name,
        "variant": info.variant,
        "parameters": info.parameters,
        "sample_rate": info.sample_rate,
        "languages": info.languages,
        "voices": info.voices,
    });

    if args.probe {
        return Ok(Outcome {
            code: EX_OK,
            report: json!({
                "ok": true,
                "mode": "probe",
                "device": "cpu",
                "model_path": model_dir.to_string_lossy(),
                "model": model_report,
                "load_ms": load_ms,
                "warnings": warnings,
            })
            .to_string(),
        });
    }

    // `--require-ref-audio` is checked AFTER a successful load on purpose: the
    // two failures are different codes and a caller that passed neither flag
    // should hear about the model first.
    let reference = load_reference(args.ref_audio.as_ref())?;
    if args.require_ref_audio && reference.is_none() {
        return Err(Failure::new(
            EX_REF_AUDIO,
            "--require-ref-audio was set but --ref-audio was not. \
             A clone was requested; refusing to substitute a default voice."
                .to_string(),
        ));
    }

    let segments = read_segments(args)?;
    if args.out_dir.is_none() && args.out.is_none() {
        return Err(Failure::usage(
            "nowhere to write: pass --out <wav> or --out-dir <dir>".to_string(),
        ));
    }
    if args.out.is_some() && segments.len() > 1 {
        return Err(Failure::usage(
            "--out names exactly one file; use --out-dir with --text-file"
                .to_string(),
        ));
    }

    let reference_report = match (args.ref_audio.as_ref(), &reference) {
        (Some(path), Some(ref_audio)) => json!({
            "path": path.to_string_lossy(),
            "sample_rate": ref_audio.sample_rate,
            "samples": ref_audio.samples.len(),
            "duration_secs": ref_audio.duration_secs(),
        }),
        _ => Value::Null,
    };

    let run_started = Instant::now();
    let mut results: Vec<Value> = Vec::with_capacity(segments.len());
    let mut failures: Vec<Value> = Vec::new();

    for (index, text) in &segments {
        let destination = output_path(args, *index);
        if let Some(parent) = destination.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).map_err(|err| {
                    Failure::new(
                        EX_WRITE,
                        format!("could not create {}: {}", parent.display(), err),
                    )
                })?;
            }
        }

        let started = Instant::now();
        let audio = match model.synthesize(&build_request(text, args, reference.as_ref())) {
            Ok(audio) => audio,
            Err(err) => {
                let failure = Failure::new(
                    EX_SYNTH,
                    format!(
                        "synthesis failed on segment {} (\"{}\"): {}",
                        index,
                        truncate(text, 60),
                        describe(&err)
                    ),
                );
                if !args.keep_going {
                    return Err(failure);
                }
                eprintln!("tdub_tts: {}", failure.message);
                failures.push(json!({
                    "index": index,
                    "text": text,
                    "error": failure.message,
                }));
                continue;
            }
        };
        let elapsed_ms = started.elapsed().as_millis() as u64;

        if audio.is_empty() && !args.allow_empty {
            return Err(Failure::new(
                EX_EMPTY_AUDIO,
                format!(
                    "segment {} (\"{}\") produced zero samples. Refusing to write \
                     an empty WAV: that is how two hours of silence shipped once. \
                     Pass --allow-empty if silence really is the expected answer.",
                    index,
                    truncate(text, 60)
                ),
            ));
        }

        let bytes = audio.get_wav();
        if bytes.len() <= 44 && !args.allow_empty {
            return Err(Failure::new(
                EX_EMPTY_AUDIO,
                format!(
                    "segment {} encoded to {} bytes (a bare WAV header). Refusing.",
                    index,
                    bytes.len()
                ),
            ));
        }
        std::fs::write(&destination, &bytes).map_err(|err| {
            Failure::new(
                EX_WRITE,
                format!("could not write {}: {}", destination.display(), err),
            )
        })?;

        results.push(json!({
            "index": index,
            "text": text,
            "out": destination.to_string_lossy(),
            "sample_rate": audio.sample_rate,
            "samples": audio.len(),
            "duration_secs": audio.duration_secs(),
            "bytes": bytes.len(),
            "elapsed_ms": elapsed_ms,
        }));

        if !args.json {
            eprintln!(
                "tdub_tts: segment {} -> {} ({:.2}s audio, {} ms)",
                index,
                destination.display(),
                audio.duration_secs(),
                elapsed_ms
            );
        }
    }

    // A partial dub is still a failure. `--keep-going` buys the operator the
    // segments that did render, not a green light.
    let code = if failures.is_empty() { EX_OK } else { EX_SYNTH };
    Ok(Outcome {
        code,
        report: json!({
            "ok": failures.is_empty(),
            "mode": "synthesize",
            "device": "cpu",
            "model_path": model_dir.to_string_lossy(),
            "model": model_report,
            "reference_audio": reference_report,
            "load_ms": load_ms,
            "segments": results,
            "failures": failures,
            "elapsed_ms": run_started.elapsed().as_millis() as u64,
            "warnings": warnings,
        })
        .to_string(),
    })
}

fn truncate(text: &str, limit: usize) -> String {
    let mut chars = text.chars();
    let head: String = chars.by_ref().take(limit).collect();
    if chars.next().is_some() {
        format!("{}...", head)
    } else {
        head
    }
}