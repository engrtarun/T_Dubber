# `tts_rust/` — CPU-only Rust TTS (VibeVoice-1.5B), no PyTorch, no CUDA

A single binary that speaks text in a **cloned** voice, built on the real
[`any-tts`](https://crates.io/crates/any-tts) crate, plus a stdlib-only Python
bridge so mazinger can call it without ever importing torch.

| File | What it is |
|---|---|
| `Cargo.toml` | Pins `any-tts 0.2` CPU-only. This file *is* the CUDA policy. |
| `src/main.rs` | The `tdub_tts` CLI. Exit codes are part of its contract. |
| `tdub_tts_bridge.py` | Python API: `TtsRunner`, `resolve()`, `available()`. Stdlib only. |
| `test_tts_bridge.py` | 43 checks, no binary and no weights required. |
| `BUILD.md` | Exact reproducible build command, toolchain, and a Windows gotcha. |

---

## Backend choice: VibeVoice-1.5B, and nothing else

mazinger's TTS is fundamentally **voice cloning** — `create_voice_clone_prompt(ref_audio=...)`,
`--voice-sample`. So the only question that matters is: *which any-tts backend
actually implements reference-audio cloning?*

From the upstream README's own "What does not work yet in the Rust backend"
sections:

| Backend | Reference-audio cloning |
|---|---|
| **OmniVoice** (mazinger's current default) | **No.** Listed verbatim as "Reference-audio voice cloning." |
| Qwen3-TTS | **No.** "Upstream Base-model voice cloning exists, but reference-audio cloning is not implemented in this crate yet." |
| Kokoro | Only with separate style-encoder weights. |
| VibeVoice-Realtime-0.5B | **No.** "Reference-audio input" is on its not-working list; it uses cached `voices/*.pt` presets instead. |
| Voxtral-4B | **No.** "The open checkpoint does not ship reference-audio encoder weights." |
| **VibeVoice-1.5B** | **Yes.** Its "What works in any-tts today" list includes `reference_audio`. |

VibeVoice-1.5B is the only row that survives the filter, and that is the entire
reason for the choice. **Do not substitute another backend to save download
size.** A backend that cannot clone produces a dub in the wrong voice, which is
worse than a stage that failed.

Two things the crate will *not* let you do with VibeVoice, both enforced at
runtime by `VibeVoiceModel::validate_request()`, and therefore deliberately not
exposed by this CLI: `--voice` (named presets) and `--speed`.

## CPU-only rationale

The project owner's rule is absolute: **no CUDA, no PyTorch, in any build flag,
ever.** It is enforced in two independent places.

**1. Cargo features.** `Cargo.toml` pins:

```toml
any-tts = { version = "0.2", default-features = false, features = ["vibevoice", "download"] }
```

`default-features = false` is the load-bearing half. Verified against the live
crates.io API on 2026-10-10, any-tts 0.2.0's feature table is:

```json
"cuda":     ["candle-core/cuda", "candle-nn/cuda", "candle-transformers/cuda"],
"metal":    ["candle-core/metal", ...],
"accelerate":["candle-core/accelerate", ...],
"default":  ["qwen3-tts","kokoro","omnivoice","vibevoice","voxtral","download"]
```

`cuda` is **not** default — but it is one typo away. `Cargo.lock` for this
project contains **zero** CUDA-related crates (`cudarc`, `nvidia-*`, none), which
is checkable at any time:

```
grep -iE "cuda|cudarc|nvidia" tts_rust/Cargo.lock     # -> no matches
```

**2. Runtime.** `tdub_tts --device` accepts exactly one value, `cpu`. Anything
else is a usage error (exit 2), **not** a silent downgrade. The Python bridge
refuses `device="cuda"` at construction for the same reason.

Why this matters concretely: Kaggle run `test4_gotgVERSION` spent **1067 s of a
1258 s run** pip-installing vLLM + PyTorch + CUDA, then died at `import` with
`ImportError: libcudart.so.13: cannot open shared object file`. Every download
had succeeded. pip returned 0. The failure surfaced 18 minutes later, after
every downstream stage had already been skipped.

---

## Build

See `BUILD.md` for the full recipe, the measured wall time, and one
Windows-specific trap that costs an hour if you hit it blind.

```
cd tts_rust
cargo build --release
# -> tts_rust/target/release/tdub_tts.exe
```

Weights (`microsoft/VibeVoice-1.5B`, ~5.4 GB) are **not** vendored. See
"Feasibility" below.

## CLI

```
tdub_tts --model <dir> --text <string> --out <wav> [options]
tdub_tts --model <dir> --text-file <path> --out-dir <dir> [options]
tdub_tts --model <dir> --probe --json
```

| Flag | Meaning |
|---|---|
| `--model <dir>` | VibeVoice-1.5B snapshot directory. Required. |
| `--text <s>` / `--text-file <path>` | One segment, or one segment per line. |
| `--out <wav>` / `--out-dir <dir>` | Where audio lands. `--out-prefix` names the stem (default `seg`). |
| `--ref-audio <wav\|mp3>` | Reference clip for cloning. Decoded **once**, reused for every segment. |
| `--require-ref-audio` | Exit 4 if `--ref-audio` is absent. Use whenever a clone was requested. |
| `--language`, `--instruct`, `--max-tokens`, `--temperature`, `--cfg-scale`, `--seed` | Passed through to the backend. |
| `--device cpu` | The only accepted value. |
| `--probe` | Load the model, print metadata, synthesize nothing. The feasibility check. |
| `--json` | Machine-readable report on stdout. |
| `--keep-going` | Finish remaining segments after a failure; **still exits non-zero**. |
| `--allow-empty` | Permit zero-sample output. Off by default. |

### Exit codes

| Code | Meaning | Bridge exception |
|---|---|---|
| 0 | ok | — |
| 2 | usage error | `TtsUsageError` |
| 3 | model load failure | `TtsModelError` |
| 4 | missing / undecodable reference audio | `TtsReferenceAudioError` |
| 5 | synthesis failure | `TtsSynthesisError` |
| 6 | output write failure | `TtsOutputError` |
| 7 | empty audio (unless `--allow-empty`) | `TtsEmptyAudioError` |

Codes are distinct on purpose. "It failed" is not actionable; "the weights are
missing" is. The bridge maps each one to its own exception class, and an
unmapped code (a crash) still raises rather than being mistaken for success.

### One model load per run

`--text-file` loads VibeVoice **once** and loops over the lines. This is a
correctness property, not an optimisation: reloading a 1.5B checkpoint per
subtitle line is the difference between a dubbing pipeline and a denial of
service. `test_tts_bridge.py::test_many_segments_run_the_binary_exactly_once`
asserts the process count, because a regression here does not raise — it just
gets slower, and then it does not finish.

---

## Python

```python
from tdub_tts_bridge import TtsRunner, resolve, available

runner = TtsRunner(
    resolve(),                                  # or "tts_rust/target/release/tdub_tts.exe"
    "/kaggle/working/models/VibeVoice-1.5B",
    ref_audio="voice_sample.wav",               # optional: pass it and the voice is cloned
    language="hi",
)

runner.probe()                                 # load only, no synthesis
results = runner.synthesize_many(
    [{"text": "Pehla segment."}, {"text": "Doosra segment."}],
    out_dir="stage8/",
)                                              # ONE process, ONE model load
```

`require_clone` defaults to **False** because of the call that
actually arrives from mazinger: the `rusttts` engine is registered
`clones=False`, its wrapper refuses a reference at three gates, and
`TtsRunner(binary, model_dir).synthesize(text, out_wav)` must work
without a voice sample. Defaulting to `True` made every plain
segment raise `TtsReferenceAudioError` (fixed 2026-10-10). An
explicit `ref_audio` still clones; the flag only changes what a
*missing* reference means. Pass `require_clone=True` to make a
missing clip a hard failure again.

## Tests

```
python tts_rust/test_tts_bridge.py
```

43 checks. No binary, no weights, no network. `tdub_tts` is replaced by a Python
fake run as a **real process** (and through a `.bat` shim on Windows, so
CreateProcess is exercised the same way production exercises it). See the module
docstring for why each failure mode gets its own fake.

---

## Feasibility status — read this before trusting the above

### ✅ Verified here

* **`cargo build --release` succeeds.** Real output and wall time in `BUILD.md`.
* **Zero CUDA crates in `Cargo.lock`.** Greppable proof, above.
* **The API surface compiles against the real crate.** Written against
  `any-tts 0.2.0`'s source, not its docs — see the correction below.
* **The bridge's contract is proven.** 43/43 tests pass against a real process.

### ⚠️ A correction to the brief I was given

The brief documented the API as
`SynthesisRequest::with_reference_audio(path)`. **That is not the signature.**
In any-tts 0.2.0 it is:

```rust
pub fn with_reference_audio(mut self, audio: ReferenceAudio) -> Self   // a struct, not a path
```

`ReferenceAudio::new(Vec<f32> /* samples */, u32 /* sample_rate */)`. So the
binary decodes the clip itself via `AudioSamples::from_audio_file(path)` (WAV
and MP3, downmixed to mono) and constructs the struct. Anyone porting this code
from the brief rather than from the crate will not compile.

Related: `SynthesisRequest` has **no** `.with_seed()`. Seeding is env-var only —
`VIBEVOICE_SEED`, read per `synthesize()` call — which is why `--seed` calls
`std::env::set_var` before loading.

### ⚠️ `microsoft/VibeVoice-1.5B` is NOT self-contained

The upstream repo serves `config.json`, `preprocessor_config.json`,
`model.safetensors.index.json` and three safetensors shards. It has **no
`tokenizer.json`** — even though any-tts lists `tokenizer.json` as a required
asset for VibeVoice.

That is not a bug on either side: `preprocessor_config.json` declares
`"language_model_pretrained_name": "Qwen/Qwen2.5-1.5B"`, and the decoder config
carries `vocab_size: 151936`, which is the Qwen2.5 tokenizer. **You must also
fetch `tokenizer.json`, `tokenizer_config.json`, `vocab.json` and `merges.txt`
from `Qwen/Qwen2.5-1.5B`** (about 11 MB total) into the same directory, or
`load_model` has no vocabulary.

Sizes, as served by the HF tree API on 2026-10-10:

| File | Bytes |
|---|---|
| `model-00001-of-00003.safetensors` | 1,975,317,828 |
| `model-00002-of-00003.safetensors` | 1,983,051,688 |
| `model-00003-of-00003.safetensors` | 1,449,832,938 |
| `config.json` | 2,762 |
| `preprocessor_config.json` | 351 |
| `model.safetensors.index.json` | 122,616 |
| `tokenizer.json` (+3 from Qwen2.5-1.5B) | ~11,500,000 |
| **total** | **~5,408,000,000** |

Any future `edge` roster row must carry **all seven** files, not one — this
model is a sharded tree, not a single blob.

### ❌ NOT DONE / NOT VERIFIED — the honest list

1. **No end-to-end synthesis has been run.** See the report for the current
   status of the `--probe` run. No WAV in this repo was produced by this
   binary, and none is claimed.
2. **No audio-quality judgement.** Nothing here has been listened to. Clone
   fidelity against the reference clip is unknown.
3. **CPU throughput is unmeasured.** VibeVoice-1.5B is a 20-step DDPM diffusion
   decoder over a 1.5B LM, running with no GPU. `--max-tokens` defaults to up to
   2048. Whether a two-hour film finishes in a Kaggle run budget is **not known
   and must be measured before this is scheduled for real work.**
4. **Hindi/Hinglish is unverified.** The backend reports `languages:
   ["auto", "multilingual"]` — that is the crate's claim, not a measurement.
   The project is Hindi-first; verify before trusting.
5. **Multi-speaker / long-form is untested.** The crate's own README calls the
   backend "still early" and "optimized for correctness and parity work rather
   than streaming performance."
6. **`edge/model_gguf.go` was deliberately NOT extended.** See below.

### Why `edge/model_gguf.go` was left alone

The brief permitted adding TTS rows there. I did not, because that file has
invariants that TTS cannot satisfy without edits to files nobody assigned me:

* `validGGUFFile()` accepts **only** `.gguf` and `.bin`, and rejects
  `.safetensors` in a comment that says *"if a torch weight ends up in this
  table the PyTorch-free architecture has failed"* — which is a wrong rule for a
  crate that reads safetensors natively and has no torch.
* `GgufTask` is a closed set (`llm`, `asr`) wired into `queue.go`'s concurrency
  guarantee — a file I do not own.
* `model_gguf_test.go` contains `TestPythonRosterMirrorMatchesGo`, which
  compares field-for-field against `huggingface/models_gguf.json`. Adding rows
  in Go without adding them in Python **breaks the build**.

A correct TTS roster needs its own file with its own task constant, its own
size/digest rules for *sharded* trees, and a matching Python mirror. That is a
change to files outside my ownership; I am flagging it rather than doing it.

---

## Rollback

This directory is additive and nothing imports it yet. Deleting `tts_rust/`
returns the repo to its previous state. There is no env var to unset, no
feature to disable, and no code path in `mazinger/` or `kaggle_worker.ipynb`
that knows this directory exists.