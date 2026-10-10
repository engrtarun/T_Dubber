//! `tts_forge` — CPU-only text-to-speech for the T_Dubber pipeline.
//!
//! WHY RUST AND NOT PYTHON
//! -----------------------
//! Kaggle run `test4_gotgVERSION` spent 1067 s pip-installing vLLM +
//! PyTorch + CUDA and then died at `import` time with
//! `ImportError: libcudart.so.13: cannot open shared object file`.
//! This binary is the same bet as the llama.cpp / whisper.cpp binaries
//! agent 1 shipped: no interpreter, no site-packages, no CUDA, one
//! file to place. It is built on the [`any-tts`](https://crates.io/
//! crates/any-tts) crate, which wraps HuggingFace Candle behind one
//! trait API and ships native, torch-free backends for OmniVoice,
//! Kokoro, Qwen3-TTS and VibeVoice.
//!
//! CPU-ONLY, ENFORCED IN TWO PLACES
//! --------------------------------
//! 1. `Cargo.toml` pins `any-tts` with `default-features = false` and
//!    only the four backend families the pipeline knows about. The
//!    `cuda` feature maps straight onto `candle-core/cuda` +
//!    `candle-nn/cuda` + `candle-transformers/cuda` and is never
//!    compiled in.
//! 2. There is no GPU code path anywhere below. A request naming a
//!    GPU device is answered with a loud stderr warning and served on
//!    CPU — the warning goes to the pipeline log, so it is never a
//!    silent downgrade.
//!
//! THE CONTRACT (mirrored verbatim in `mazinger/mazinger/tts.py` and
//! `CUDA_REMOVAL_TASKS.md` PHASE 2 — neither side may drift)
//! ---------------------------------------------------------
//! ```text
//! tts_forge --version
//!     prints `tts_forge <version>` and exits 0.
//!
//! tts_forge --once --text TEXT --output OUT.wav [--reference REF.wav]
//!           [--ref-text TEXT] [--language CODE] [--model FAMILY]
//!           [--device cpu|cuda] [--weights DIR]
//!     Smoke-test mode: writes OUT.wav, exit 0. On failure prints
//!     `error: ...` to stderr and exits non-zero.
//!
//! tts_forge stream
//!     Long-running mode (the one the pipeline uses).
//!     stdin  — one JSON request per line:
//!         {"id":1,"text":"...","output":"out.wav","reference":null,
//!          "ref_text":null,"language":"en","model":"omnivoice",
//!          "device":"cpu"}
//!     stdout — the FIRST line must be {"event":"ready","version":"..."},
//!     then exactly one response per request:
//!         {"id":1,"ok":true,"sample_rate":24000,"duration":1.23}
//!         {"id":1,"ok":false,"error":"..."}
//! ```
//!
//! `stream` is not a nicety: the pipeline synthesises segment by
//! segment, so a process-per-segment would reload a ~1 GB model for
//! every line of dialogue and be SLOWER than the torch path this
//! replaces. One process, one load per family, hundreds of requests.
//!
//! VOICE CLONING — THE DECISIVE RULE
//! ---------------------------------
//! Verified against the any-tts 0.2.0 source (2026-10-10): the
//! OmniVoice and Qwen3-TTS Rust backends REJECT `reference_audio`
//! outright (`TtsError::ModelError`); only the VibeVoice backend
//! implements reference-audio voice cloning. So a reference clip with
//! any other family is a loud failure here, never a silently-dropped
//! reference: a finished dub in the wrong voice is strictly worse than
//! a crashed stage, because every stage downstream reports success.
//!
//! EXIT CODES
//! ----------
//! 0 ok · 2 usage error · 3 synthesis/load failure. Distinct on
//! purpose: "it failed" is not actionable, "the weights are missing"
//! is.

use std::collections::HashMap;
use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Instant;

use any_tts::{
    load_model, AudioSamples, DeviceSelection, ModelType, ReferenceAudio,
    SynthesisRequest, TtsConfig, TtsModel,
};
use serde_json::{json, Value};

const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Everything worked. Not "worked unless you look closely".
const EX_OK: u8 = 0;
/// Bad command line. Nothing was loaded, nothing was written.
const EX_USAGE: u8 = 2;
/// Weights missing/unreadable, or synthesis failed. Retrying a
/// synthesis will not fix a bad path, so one code covers both.
const EX_SYNTH: u8 = 3;

/// The families this binary can serve, in the spelling the pipeline
/// sends. `CANDLE_FAMILIES` in mazinger/mazinger/tts.py must stay a
/// subset of this list — the registry on the Python side is what
/// routes requests here.
const FAMILIES: &[&str] = &["omnivoice", "kokoro", "qwen3-tts", "vibevoice"];

/// What `--model` defaults to when a request carries no `model`
/// field. Matches `_candle_family()`'s default on the Python side.
const FAMILY_DEFAULT: &str = "omnivoice";

/// The only family whose any-tts 0.2.0 backend implements
/// reference-audio voice cloning. See the module docs.
const CLONING_FAMILY: &str = "vibevoice";

/// Env var holding the weights root directory. Inside it, a
/// subdirectory named after the family is preferred (`omnivoice/`,
/// `qwen3-tts/`, …); the root itself is the fallback. Unset, the
/// any-tts resolution applies: HF cache (`$HF_HOME/hub` or
/// `~/.cache/huggingface/hub`), then Hub download — which is why the
/// pipeline's per-request timeout is 900 s: the first call may
/// download weights.
const WEIGHTS_ENV: &str = "TDUBBER_TTS_FORGE_WEIGHTS";

const USAGE: &str = "\
tts_forge — CPU-only any-tts (Candle) TTS for the T_Dubber pipeline

USAGE
  tts_forge --version
  tts_forge --once --text TEXT --output OUT.wav [options]
  tts_forge stream

MODES
  --version          Print `tts_forge <version>` and exit 0.
  --once             Synthesise exactly one segment (smoke test).
  stream             Long-running JSONL server (what the pipeline
                     spawns). First stdout line is
                     {\"event\":\"ready\",\"version\":\"...\"}; then one
                     JSON response per stdin request line.

OPTIONS (--once)
  --text TEXT        Text to synthesise. Required with --once.
  --output PATH      WAV to write. Required with --once.
  --reference PATH   Reference clip for zero-shot voice cloning.
                     Only honoured with --model vibevoice; any other
                     family is a loud error (the reference is never
                     dropped — see the module docs).
  --ref-text TEXT    Transcript of the reference clip. Accepted for
                     contract compatibility; any-tts 0.2.0's
                     ReferenceAudio carries no transcript, so it is
                     unused (and says so on stderr).
  --language CODE    ISO 639-1 code or language name.
  --model FAMILY     omnivoice | kokoro | qwen3-tts | vibevoice
                     (default: omnivoice).
  --device cpu|cuda  Accepted for contract compatibility. This binary
                     is CPU-only by construction; a GPU device is a
                     loud stderr warning, never a silent path.
  --weights DIR      Weights root. Family subdirectories are
                     preferred. Unset: $TDUBBER_TTS_FORGE_WEIGHTS,
                     then the any-tts default (HF cache / download).

ENV
  TDUBBER_TTS_FORGE_WEIGHTS  Weights root for `stream` mode.

EXIT CODES
  0 ok
  2 usage error
  3 model load / synthesis failure
";

// --------------------------------------------------------------------------- //
// Arguments
// --------------------------------------------------------------------------- //

#[derive(Debug, Default)]
struct Args {
    version: bool,
    once: bool,
    stream: bool,
    text: Option<String>,
    output: Option<PathBuf>,
    reference: Option<PathBuf>,
    ref_text: Option<String>,
    language: Option<String>,
    model: Option<String>,
    device: Option<String>,
    weights: Option<PathBuf>,
}

/// A clean, coded failure. No `Result` with a `String` error: every
/// exit path has to name its own code, which is the part callers
/// actually branch on.
struct UsageError(String);

fn parse_args(argv: &[String]) -> Result<Args, UsageError> {
    let mut args = Args::default();
    let mut cursor = 0usize;

    while cursor < argv.len() {
        let token = &argv[cursor];
        cursor += 1;

        // `--flag value` and `--flag=value` are both accepted.
        let (flag, inline) = match token.split_once('=') {
            Some((name, value)) if name.starts_with("--") => {
                (name.to_string(), Some(value.to_string()))
            }
            _ => (token.clone(), None),
        };

        macro_rules! value {
            () => {{
                if let Some(value) = inline.clone() {
                    value
                } else if cursor < argv.len() {
                    let value = argv[cursor].clone();
                    cursor += 1;
                    value
                } else {
                    return Err(UsageError(format!("{} requires a value", flag)));
                }
            }};
        }

        match flag.as_str() {
            "--version" => args.version = true,
            "--once" => args.once = true,
            "stream" => args.stream = true,
            "--text" => args.text = Some(value!()),
            "--output" => args.output = Some(PathBuf::from(value!())),
            "--reference" => args.reference = Some(PathBuf::from(value!())),
            "--ref-text" => args.ref_text = Some(value!()),
            "--language" => args.language = Some(value!()),
            "--model" => args.model = Some(value!()),
            "--device" => args.device = Some(value!()),
            "--weights" => args.weights = Some(PathBuf::from(value!())),
            "--help" | "-h" => {
                println!("{}", USAGE);
                std::process::exit(i32::from(EX_OK));
            }
            other => {
                return Err(UsageError(format!(
                    "unknown argument '{}'\n\n{}",
                    other, USAGE
                )));
            }
        }
    }
    Ok(args)
}

// --------------------------------------------------------------------------- //
// Families
// --------------------------------------------------------------------------- //

/// Canonical family name for a `--model` / request `model` value.
/// Accepts the aliases the pipeline's `_FAMILY_TOKENS` scanner can
/// produce ("qwen" → qwen3-tts) and rejects everything else loudly —
/// a typo'd family must not silently become the default voice.
fn canonical_family(family: &str) -> Result<&'static str, String> {
    match family.trim().to_ascii_lowercase().as_str() {
        "omnivoice" => Ok("omnivoice"),
        "kokoro" => Ok("kokoro"),
        "qwen3-tts" | "qwen3" | "qwen" => Ok("qwen3-tts"),
        "vibevoice" => Ok("vibevoice"),
        other => Err(format!(
            "unknown model family {other:?}. Valid families: {}.",
            FAMILIES.join(", ")
        )),
    }
}

fn model_type_for(family: &str) -> ModelType {
    match family {
        "kokoro" => ModelType::Kokoro,
        "qwen3-tts" => ModelType::Qwen3Tts,
        "vibevoice" => ModelType::VibeVoice,
        _ => ModelType::OmniVoice,
    }
}

// --------------------------------------------------------------------------- //
// Weights discovery
// --------------------------------------------------------------------------- //

/// The weights root: an explicit `--weights` wins, then the env var.
fn weights_root(explicit: Option<&Path>) -> Option<PathBuf> {
    if let Some(path) = explicit {
        return Some(path.to_path_buf());
    }
    std::env::var_os(WEIGHTS_ENV).map(PathBuf::from)
}

/// The directory to load *family* from: `<root>/<family>/` when that
/// exists (a box may hold several families under one root), otherwise
/// the root itself. `None` means "no local directory" and hands
/// resolution to any-tts: HF cache first, then Hub download.
fn weights_dir_for(root: Option<&Path>, family: &str) -> Option<PathBuf> {
    let root = root?;
    let family_dir = root.join(family);
    if family_dir.is_dir() {
        Some(family_dir)
    } else {
        Some(root.to_path_buf())
    }
}

// --------------------------------------------------------------------------- //
// Model loading
// --------------------------------------------------------------------------- //

/// Load (or download) a family's weights and construct the model.
///
/// `TtsConfig::resolve_files()` — called inside every backend's
/// `load()` — resolves in this order: explicit files → auto-discovery
/// in `model_path` → HuggingFace Hub download (the `download`
/// feature is compiled in, so a Kaggle box with no weights yet gets
/// them on the first request; that is what the 900 s timeout is for).
fn load_family(family: &str, weights: Option<&Path>) -> Result<Box<dyn TtsModel>, String> {
    let model_type = model_type_for(family);
    let mut config = TtsConfig::new(model_type).with_device(DeviceSelection::Cpu);
    if let Some(dir) = weights {
        config = config.with_model_path(dir.to_string_lossy().to_string());
    }
    let started = Instant::now();
    let model = load_model(config)
        .map_err(|err| format!("could not load {family} model: {err}"))?;
    eprintln!(
        "tts_forge: loaded {family} in {} ms (cpu)",
        started.elapsed().as_millis()
    );
    Ok(model)
}

// --------------------------------------------------------------------------- //
// Synthesis
// --------------------------------------------------------------------------- //

/// Build the request. THE CLONE RULE lives here: a reference clip with
/// a family that cannot clone is a loud error naming the fix, because
/// dropping it would dub the whole film in a default voice.
fn build_request(
    text: &str,
    language: Option<&str>,
    reference: Option<&Path>,
    family: &str,
) -> Result<SynthesisRequest, String> {
    let mut request = SynthesisRequest::new(text);
    if let Some(language) = language.map(str::trim).filter(|lang| !lang.is_empty()) {
        request = request.with_language(language);
    }
    let Some(path) = reference else {
        return Ok(request);
    };
    if family != CLONING_FAMILY {
        return Err(format!(
            "reference audio was supplied for family '{family}', which cannot \
             clone: any-tts 0.2.0 implements reference-audio voice cloning \
             for '{CLONING_FAMILY}' only — the OmniVoice and Qwen3-TTS Rust \
             backends reject reference audio outright. The reference was NOT \
             dropped. Fix: pass --model {CLONING_FAMILY}, or drop the \
             reference only if a default voice is genuinely intended."
        ));
    }
    let decoded = AudioSamples::from_audio_file(path).map_err(|err| {
        format!(
            "could not decode reference audio {}: {err}",
            path.display()
        )
    })?;
    if decoded.is_empty() {
        return Err(format!(
            "reference audio {} decoded to zero samples",
            path.display()
        ));
    }
    Ok(request.with_reference_audio(ReferenceAudio::new(
        decoded.samples,
        decoded.sample_rate,
    )))
}

/// Write the WAV. Zero samples is a failure, never a file: a header-only
/// WAV is exactly how two hours of silence shipped once already.
fn write_wav(output: &Path, audio: &AudioSamples) -> Result<(), String> {
    if audio.is_empty() {
        return Err(
            "synthesis produced zero samples; refusing to write an empty WAV".to_string(),
        );
    }
    let bytes = audio.get_wav();
    if bytes.len() <= 44 {
        return Err(format!(
            "synthesis encoded to {} bytes (a bare WAV header); refusing",
            bytes.len()
        ));
    }
    if let Some(parent) = output.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent).map_err(|err| {
                format!("could not create {}: {err}", parent.display())
            })?;
        }
    }
    std::fs::write(output, &bytes)
        .map_err(|err| format!("could not write {}: {err}", output.display()))?;
    Ok(())
}

// --------------------------------------------------------------------------- //
// --once
// --------------------------------------------------------------------------- //

fn run_once(args: &Args) -> Result<(), String> {
    let text = args.text.as_deref().unwrap_or("").trim();
    if text.is_empty() {
        return Err("--text is empty; refusing to speak nothing".to_string());
    }
    let output = args
        .output
        .as_ref()
        .ok_or_else(|| "--output is required with --once".to_string())?;

    let family = args
        .model
        .as_deref()
        .unwrap_or(FAMILY_DEFAULT);
    let family = canonical_family(family).map_err(|err| {
        format!("--model {err}")
    })?;

    // A device string that names a GPU is contract-compatible but
    // impossible here: there is no GPU code in this binary at all.
    // Warn loudly (to the pipeline log), serve on CPU — never a
    // silent downgrade, because there is nothing to downgrade from.
    if let Some(device) =
        args.device.as_deref().map(str::trim).filter(|d| !d.is_empty())
    {
        if device != "cpu" && device != "auto" {
            eprintln!(
                "tts_forge: --device '{device}' was requested; this binary is \
                 CPU-only by construction (no CUDA feature is compiled in), so \
                 it runs on CPU. The libcudart.so.13 trap stays dead."
            );
        }
    }

    let weights = weights_dir_for(weights_root(args.weights.as_deref()).as_deref(), family);
    let model = load_family(family, weights.as_deref())?;
    let request = build_request(
        text,
        args.language.as_deref(),
        args.reference.as_deref(),
        family,
    )?;
    if args.ref_text.is_some() {
        ref_text_note();
    }
    let started = Instant::now();
    let audio = model
        .synthesize(&request)
        .map_err(|err| format!("synthesis failed: {err}"))?;
    write_wav(output, &audio)?;
    eprintln!(
        "tts_forge: {} -> {} ({:.2}s audio, {} Hz, {} ms)",
        truncate(text, 60),
        output.display(),
        audio.duration_secs(),
        audio.sample_rate,
        started.elapsed().as_millis()
    );
    Ok(())
}

fn ref_text_note() {
    eprintln!(
        "tts_forge: --ref-text was supplied but any-tts 0.2.0's \
         ReferenceAudio carries no transcript; it is unused, not dropped \
         silently"
    );
}

// --------------------------------------------------------------------------- //
// stream
// --------------------------------------------------------------------------- //

/// The long-running mode. One process, one model load per family,
/// hundreds of requests. Every request gets exactly one response line,
/// even when the request is garbage — a silent stream is the failure
/// mode the pipeline cannot recover from.
fn run_stream() -> ExitCode {
    let stdout = io::stdout();
    let mut out = stdout.lock();

    // Contract: the FIRST stdout line is the ready event, before any
    // model load. The wrapper spawns, waits for exactly this line, and
    // only then starts sending requests — so loading happens lazily on
    // the first request, inside the 900 s per-request timeout.
    if respond(&mut out, &json!({"event": "ready", "version": VERSION})).is_err() {
        return ExitCode::from(EX_SYNTH);
    }

    let stdin = io::stdin();
    let mut models: HashMap<String, Box<dyn TtsModel>> = HashMap::new();
    let mut warned_device: Option<String> = None;
    let weights = weights_root(None);

    for line in stdin.lock().lines() {
        let line = match line {
            Ok(line) => line,
            Err(err) => {
                let _ = respond(
                    &mut out,
                    &json!({"id": Value::Null, "ok": false,
                             "error": format!("could not read stdin as UTF-8: {err}")}),
                );
                continue;
            }
        };
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let request: Value = match serde_json::from_str(trimmed) {
            Ok(value) => value,
            Err(err) => {
                let _ = respond(
                    &mut out,
                    &json!({"id": Value::Null, "ok": false,
                             "error": format!("invalid JSON request: {err}")}),
                );
                continue;
            }
        };
        let id = request.get("id").cloned().unwrap_or(Value::Null);
        let mut response = handle_request(
            &request,
            &mut models,
            &mut warned_device,
            weights.as_deref(),
        );
        // The wrapper matches responses by id, so the echo is load-bearing.
        response["id"] = id;
        if respond(&mut out, &response).is_err() {
            // stdout is gone; the wrapper is gone too.
            break;
        }
    }
    ExitCode::from(EX_OK)
}

/// One request in, one response object out (without the id — the caller
/// echoes it). Every failure shape returns `{"ok": false, "error": …}`
/// rather than panicking: a crashed child costs the pipeline a restart,
/// a malformed response costs it the run.
fn handle_request(
    request: &Value,
    models: &mut HashMap<String, Box<dyn TtsModel>>,
    warned_device: &mut Option<String>,
    weights_root: Option<&Path>,
) -> Value {
    // Device: CPU-only by construction. Anything else is a loud warning
    // (once per distinct device, so a GPU-labelled stream does not fill
    // the log), never a silent path — and never a silent downgrade,
    // because there is no GPU code here to downgrade from.
    if let Some(device) = request.get("device").and_then(Value::as_str) {
        let device = device.trim();
        if !device.is_empty()
            && device != "cpu"
            && device != "auto"
            && warned_device.as_deref() != Some(device)
        {
            *warned_device = Some(device.to_string());
            eprintln!(
                "tts_forge: request asked for device '{device}'; this binary \
                 is CPU-only by construction (no CUDA feature is compiled \
                 in), so it runs on CPU. The libcudart.so.13 trap stays dead."
            );
        }
    }

    let text = match request.get("text").and_then(Value::as_str) {
        Some(text) if !text.trim().is_empty() => text.trim(),
        _ => {
            return json!({"ok": false,
                          "error": "request field 'text' is empty or missing"})
        }
    };
    let output = match request.get("output").and_then(Value::as_str) {
        Some(path) if !path.trim().is_empty() => PathBuf::from(path.trim()),
        _ => {
            return json!({"ok": false,
                          "error": "request field 'output' is empty or missing"})
        }
    };
    let family = request
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or(FAMILY_DEFAULT);
    let family = match canonical_family(family) {
        Ok(family) => family,
        Err(err) => return json!({"ok": false, "error": err}),
    };
    let language = request.get("language").and_then(Value::as_str);
    let reference = request
        .get("reference")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|path| !path.is_empty())
        .map(PathBuf::from);
    if request.get("ref_text").and_then(Value::as_str).is_some() {
        ref_text_note();
    }

    // One load per family for the whole stream. A reload per segment
    // would be slower than the torch path this replaces, which is the
    // entire reason `stream` exists.
    let key = family.to_string();
    if !models.contains_key(&key) {
        let weights = weights_dir_for(weights_root, family);
        match load_family(family, weights.as_deref()) {
            Ok(model) => {
                models.insert(key.clone(), model);
            }
            Err(err) => return json!({"ok": false, "error": err}),
        }
    }
    let model = models.get(&key).expect("just inserted");

    let request = match build_request(
        text,
        language,
        reference.as_deref(),
        family,
    ) {
        Ok(request) => request,
        Err(err) => return json!({"ok": false, "error": err}),
    };
    let started = Instant::now();
    let audio = match model.synthesize(&request) {
        Ok(audio) => audio,
        Err(err) => return json!({"ok": false, "error": format!("synthesis failed: {err}")}),
    };
    if let Err(err) = write_wav(&output, &audio) {
        return json!({"ok": false, "error": err});
    }
    json!({
        "ok": true,
        "sample_rate": audio.sample_rate,
        "duration": audio.duration_secs() as f64,
        "elapsed_ms": started.elapsed().as_millis() as u64,
    })
}

/// One JSON line, flushed. The wrapper reads line-by-line with
/// `bufsize=1`; an unflushed line is a hang, not a delay.
fn respond(out: &mut impl Write, payload: &Value) -> io::Result<()> {
    writeln!(out, "{payload}")?;
    out.flush()
}

// --------------------------------------------------------------------------- //
// Main
// --------------------------------------------------------------------------- //

fn main() -> ExitCode {
    // A panic escaping main() is still a non-zero exit, but the
    // pipeline cannot classify it against the contract. One
    // identifiable line on stderr turns "exit code 101" into "this is
    // a bug in tts_forge, not a missing model".
    std::panic::set_hook(Box::new(|info| {
        eprintln!(
            "tts_forge: internal panic -- this is a bug in tts_forge, not a \
             model or environment problem: {info}"
        );
    }));

    let argv: Vec<String> = std::env::args().skip(1).collect();
    let args = match parse_args(&argv) {
        Ok(args) => args,
        Err(UsageError(message)) => {
            eprintln!("tts_forge: {message}");
            return ExitCode::from(EX_USAGE);
        }
    };

    if args.version {
        println!("tts_forge {VERSION}");
        return ExitCode::from(EX_OK);
    }
    if args.stream {
        return run_stream();
    }
    if !args.once {
        eprintln!("tts_forge: no mode given; pass --once, --version or stream\n\n{}", USAGE);
        return ExitCode::from(EX_USAGE);
    }
    match run_once(&args) {
        Ok(()) => ExitCode::from(EX_OK),
        Err(err) => {
            // Contract: `error: ...` on stderr, non-zero exit.
            eprintln!("error: {err}");
            ExitCode::from(EX_SYNTH)
        }
    }
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
