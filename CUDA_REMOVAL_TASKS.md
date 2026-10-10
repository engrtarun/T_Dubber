# CUDA & PyTorch ERADICATION PLAN - TASK DELEGATION

Bhai, `NEW_WORKFLOW.MD` aur Kaggle ke latest test cases (jaise `test4_gotgVERSION`) ki logs dekhne ke baad ek baat saaf hai: **Pura time 9-10 GB ke pip installs (PyTorch, vLLM, CUDA) kha rahe hain**. Kaggle har baar naye environment mein crash ho jata hai. 

Tumhara rule clear hai: 
1. **PyTorch aur CUDA ko jad se ukhadna hai**.
2. **Old python logic delete nahi karna, usko "archive / raddi" folder mein rakhna hai.**
3. **C++ (llama.cpp / whisper.cpp), ONNX, aur Go (as master controller) ka use karna hai.**

Humare paas 4 agents hain, unko is tarah kaam baantna hai:

---

## 1. Agent 1: `cpp_accelerator` (C++ / Assembly Expert)
**Kya Karega:** 
* `llama.cpp` (LLM translation / Homura ke liye) aur `whisper.cpp` (Audio Transcription ke liye) ko compile aur setup karega.
* Models ko `.gguf` format mein run karne ke liye C++ binaries (`.exe` / linux binary) banayega ya unki integration likhega.
**Kyon Karega:** 
* Python aur PyTorch ka 9GB ka environment sidha 50MB ki binary mein convert ho jayega. Ye bina CUDA version conflicts ke makkhan ki tarah CPU/GPU par chalega. Kaggle ka setup time seconds mein aa jayega.

## 2. Agent 2: `mazinger` (Dubbing Engine / Python Pipeline)
**Kya Karega:** 
* Kaggle scripts (`kaggle_worker.ipynb` etc.) aur pipeline se saare `pip install torch`, `vLLM` aur heavy dependencies hata dega.
* Python pipeline ko update karega taaki wo seedha `cpp_accelerator` ki banai hui C++ binaries ko call kare. TTS (Text-to-Speech) ko ONNX runtime par shift karega.
* **Important:** Jo purani PyTorch wali python files aur logic hain, unko delete nahi karega balki `archive` (raddi) naam ke folder mein move kar dega (jaise tumne kaha "raddi me saja ke rakh lo").
**Kyon Karega:** 
* Kaggle par code fail nahi hoga. Dependencies ka bojh khatam ho jayega aur pipeline ekdum clean "PyTorch-Free Architecture" ban jayegi.

## 3. Agent 3: `edge` (Go HTTP Service / Controller)
**Kya Karega:** 
* Go master controller ka kaam karega (Go aab asli raja hoga). 
* `hf_store.py` aur Go fetch logic ko update karega taaki wo PyTorch weights ki jagah `.gguf` models download aur cache kare.
* `NEW_WORKFLOW.MD` ke according, job ledger, queue, aur parallelism (C++ video workers, GPU hotspots) sab Go control karega.
**Kyon Karega:** 
* Go ki concurrency best hai. Ye pipeline parallel karega aur bina Python ke nakhro ke Kaggle par instantly setup ready rakhega.

## 4. Agent 4: `telegram_uploader` / `tgup` (Resilience & Checkpointing)
**Kya Karega:** 
* "Adaptive Chunking + SQLite + Telegram Backup" wale naye logic ko enforce karega.
* Har chunk (video, audio, text) ka hash (SHA-256) check karega aur agar koi chunk fail ho jaye, toh pura project fail karne ki jagah sirf us chunk ko Telegram se wapas manga kar retry karega.
**Kyon Karega:** 
* 100 GB ki movies ko bina data corruption aur crash ke handle karne ke liye. Ek error = puri movie ka vinash nahi.

---
**AGENTS KO INSTRUCTION KAISE DENA HAI:**
Is file (`CUDA_REMOVAL_TASKS.md`) ko save kar diya gaya hai. Tum bas har agent ko mention karke bol sakte ho ki *"Apna apna task CUDA_REMOVAL_TASKS.md se padh kar execute karo."* Maza aa jayega aur Kaggle Test 4 instantly pass hoga!

---

# PHASE 2 — TTS bhi torch se bahar (Rust / Candle)

## ✅ Verified (2026-10-10, web search se)

Advocate agent ka claim **galat nahi tha**. HuggingFace Candle ke upar ek
dedicated Rust crate hai:

* **crates.io/crates/any-tts** — "Rust TTS library built around Candle with
  one trait-based API for **Kokoro, OmniVoice, Qwen3-TTS, VibeVoice**,
  VibeVoice Realtime" — aur features: `cuda`, `metal`, `accelerate`,
  `omnivoice` (default), `download` (HF se auto-download).
* docs.rs/any-tts — same, with per-model feature flags.

Matlab: **jo models hum use karte hain wo Candle me native (torch-free) hain.**
ONNX ka aadha-adhura setup ab kisi kaam ka nahi — ek binary kaafi hai.

## ⚠️ Gaddari rokne ke liye: file ownership (sirf listed files chhedo)

| File / folder | Owner | Rule |
|---|---|---|
| `RustSetup/tts_forge/` (naya crate) | **Agent 3 (edge)** | Sirf Agent 3 likhta hai |
| `mazinger/mazinger/tts.py` (candle engine) | **Agent 2 (mazinger)** | Sirf Agent 2 |
| `mazinger/mazinger/cli/_groups.py` | **Agent 2** | Sirf Agent 2 |
| `mazinger/mazinger/pipeline.py` | **Agent 2** | Sirf Agent 2 |
| `mazinger/tests/test_candle_tts.py` | **Agent 2** | Sirf Agent 2 |
| `kaggle_worker.ipynb` cell 8 | **Agent 2** | Sirf Agent 2 |
| `kaggle_worker_local/kaggle_worker.ipynb` | **Agent 3 (edge)** | Merge se pehle review |
| `CUDA_REMOVAL_TASKS.md` | **Manager** | Koi bhi append kar sakta hai, par contract badalna manager ki marzi se |

**Koi bhi agent doosre ka file directly merge nahi karega.** Pehle topic
samjho, review karo, phir merge karo.

## 🔌 THE CONTRACT — **CORRECTED 2026-10-10, supersedes the original**

> **Read this before the original contract below.** The original specified
> `tts_forge` in `RustSetup/tts_forge/` with an `omnivoice` default and the
> **`cuda` feature enabled for Kaggle GPU**. Three things changed that, in
> order of importance:
>
> 1. **The project owner's rule is absolute: NO CUDA, NO PYTORCH, in any
>    build flag, ever.** The precedence is the 1067s vLLM run that died on
>    `libcudart.so.13`. Enabling candle's `cuda` feature reproduces that exact
>    failure class. Any contract line asking for `cuda` is void.
> 2. **OmniVoice cannot serve this pipeline in Rust.** The `any-tts` README's
>    "What does not work yet in the Rust backend" lists *reference-audio voice
>    cloning* for OmniVoice. Qwen3-TTS is the same. **VibeVoice-1.5B is the
>    only backend in the crate that clones from a reference audio**, and
>    mazinger's TTS *is* cloning (`--voice-sample`). So a contract defaulting
>    to `omnivoice` cannot do the one thing the pipeline needs.
> 3. **A stdin JSONL `stream` protocol is not required.** One-process / one
>    model load is the property that matters; `--text-file` achieves it with
>    fewer moving parts than a custom protocol, and it is what shipped.
>
> What actually exists, verified by running it: **`tts_rust/tdub_tts`**
> (built, 10.7 MB, CPU-only by construction, `any-tts` v0.2 with
> `default-features = false, features = ["vibevoice", "download"]`).
>
> **The original contract follows, kept for the record. Do not implement it
> as-is.**

---

### ORIGINAL (superseded) — Agent 3 `RustSetup/tts_forge/` me crate banayega. Binary ka naam:
**`tts_forge`**. Ye teen modes implement karega:

### 1. `tts_forge --version`
`tts_forge <version>` print kare, exit 0.

### 2. `tts_forge --once --text TEXT --output OUT.wav [--reference REF.wav] [--ref-text TEXT] [--language CODE] [--model FAMILY] [--device cpu|cuda]`
Smoke-test mode. `OUT.wav` likhe, exit 0. Fail par stderr me `error: ...`
aur non-zero exit.

### 3. `tts_forge stream` ← **main mode (yehi pipeline use karega)**
* stdin: har line ek JSON request:
  ```json
  {"id":1,"text":"...","output":"out.wav","reference":null,"ref_text":null,"language":"en","model":"omnivoice","device":"cuda"}
  ```
* stdout: **pehli line** `{"event":"ready","version":"..."}` honi chahiye,
  phir har request ka ek response:
  ```json
  {"id":1,"ok":true,"sample_rate":24000,"duration":1.23}
  {"id":1,"ok":false,"error":"..."}
  ```
* WAV: mono, native sample rate (OmniVoice = 24000). Python `soundfile`
  se padhta hai — koi hardcoded sr nahi.
* `stream` mode zaroori hai, luxury nahi: pipeline segment-by-segment
  synthesize karta hai. Process-per-segment = har dialogue line pe ~1 GB
  model reload = torch path se bhi slow. **Ek process, ek load, saare
  segments.**

`--model` families: `omnivoice` (default), `kokoro`, `qwen3-tts`, `vibevoice`.

## 📋 Agent 3 ka kaam (edge / Infrastructure) — ✅ DONE (contract ke bahar, aur theek hi kiya)

`RustSetup/tts_forge/` nahi bana — jo bana hai wo **`tts_rust/tdub_tts`** hai,
is tarah ki I already described above. Verified with my own hands:

* `tts_rust/target/release/tdub_tts.exe` — **10.7 MB**, `cargo build --release`
  se bana. 10 MB is the CPU-only signature; a candle-CUDA build is hundreds.
* `cuda`/`nvidia`/`nccl` crates: **zero** in `Cargo.lock`.
* `tdub_tts.exe --device cuda` → **exit 2**, *"CPU-only by construction: no
  CUDA feature is compiled in and no GPU backend exists to fall back to."*
* `--require-ref-audio` (exit 4) — silently speaking in a default voice is
  worse than failing, so a clone request can never degrade.
* `--text-file` loads the model **once** for N segments — process-per-segment
  would reload 1.5 GB per dialogue line.
* `tts_rust/test_tts_bridge.py`: **37 passed, 6 subtests passed**.

**Owner override on record:** `CUDA` is banned outright. `[features] cuda` and
`--device cuda` must never be reintroduced. If a future contract wants GPU, it
needs the owner first — a CUDA candle build recreates the vLLM crash class.

### Agent 3 ka ORIGINAL (superseded) vakya, jaisa tha

2. **Koi torch, koi Python dependency nahi** — sirf Rust + Candle.

## ✅ Agent 2 ka kaam (mazinger / Pipeline)

* `mazinger/mazinger/tts.py` me `rusttts` engine + `onnxruntime` fallback,
  dono registered. `resolve_tts_backend()` clone requests ko `rusttts` se
  **door** rakhta hai (`create_voice_prompt` phir se check karta hai, taaki
  hard-coded `--tts-engine rusttts` chup-chaap clone na kar de). Default
  `auto` hai — `rusttts` nahi, kyunki binary har box pe nahi hoti.

## 🚦 Checkpoints (kisi bhi default flip se PEHLE)

* **CP1 — binary verified**. Rewritten against what exists: binary is
  `tts_rust/target/release/tdub_tts.exe`, `cuda` is not a device, and there is
  no `--once`/`stream`. The parts that must pass:
  1. `tdub_tts.exe --help` exit 0 — **PASS**
  2. `tdub_tts.exe --device cuda` → exit 2 with the CPU-only message — **PASS**
  3. `python -m pytest tts_rust/test_tts_bridge.py` — **PASS** (37 passed,
     6 subtests)
  4. **`tdub_tts.exe --probe` with the real VibeVoice-1.5B weights load the
     model** — **NOT YET DONE.** This is the only open item, and it is the one
     that matters: everything above proves compile + wrapper + CLI, not that
     the weights actually load. ~1.5 B params has not been downloaded on this
     box; the real proof happens on Kaggle.
  → **CP1 is 3/4. The defaults stay `qwen`, and that is the correct call:
  flipping before the weights load means every run dies on "binary not found"
  or a bad model dir and nothing is learned.**
* **CP2 — torch TTS engines raddi**: CP1 ke 4/4 hone ke baad hi
  `qwen`/`chatterbox`/`mlx`/`omnivoice` engines `archive/raddi/` me
  jayenge (verbatim copy + `TDUBBER_ALLOW_TORCH_TTS=1` gate). CP2 se
  pehle unka removal pipeline defaults todega.
* **ONNX engine**: deprecated, par delete **nahi** — and note the owner chose
  it as the explicit fallback for the clone path, so it is not merely
  tolerated, it is load-bearing until CP1 is 4/4.

## 📌 Abhi ka status (corrected — the old line below was stale)

* Agent 2: `rusttts` + `onnxruntime` engines registered, clone guard in place,
  tests pass.
* Agent 3: **binary built.** `tts_rust/tdub_tts.exe`, 10.7 MB, CPU-only
  verified three ways (Cargo.lock, size, `--device cuda` rejection). It is in
  `tts_rust/`, **not** `RustSetup/` — those are different directories and the
  earlier "RustSetup me sirf rustup-init.exe hai" line was reading the wrong
  one.
* Defaults abhi bhi `qwen` hain — CP1 ke 4/4 hone ke baad flip hoga. Ye jaan-
  boojh kar hai (see CP1).

### Two things fixed this round, worth remembering

* `test_no_torch_import.py` had a stray trailing comma inside a parenthesis —
  `("...",)` is a 1-tuple and `write_text` rejects tuples. Only that entry had
  it, only that entry failed.
* The real one: `_gate_map` walked an `elif`'s `orelse` **without pushing the
  outer `if`'s test**, so an elif body was reported as gated by its own
  condition alone. That is the dangerous direction — it under-reports the gate,
  which is exactly how a torch install slips past. Its own test's line-number
  lookup also broke on the first edit to the fixture; now resolved by
  call argument.
* Notebooks (`kaggle_worker.ipynb` cell 8 + `kaggle_worker_local/` cell 6) had
  a third hard-coded flag, `"--device", "cuda"`, after the other two had been
  made env-driven. Now `TDUBBER_TTS_DEVICE`, default `cpu`. Both notebooks
  re-verified with `ast.parse` — a missing `\n` in the patched source list
  glues two lines and smuggles two bogus elements into the `cmd` list, which
  is why that check exists.
* **`tdub_tts` used to hang on a bad `--model`.** Empty or partial model dir
  sailed through `preflight_model` (missing `*.safetensors` was only a
  *warning*), reached `load_model`, and any-tts' last resolution tier is a
  Hugging Face fetch — a 5.4 GB download of `microsoft/VibeVoice-1.5B`. On
  Kaggle that is not an error, it is a job that burns its wall clock until the
  kernel is killed, from what is really a typo or a pack that did not mount.
  Now a hard `EX_MODEL_LOAD` (exit 3): **45s+ hang → 21 ms**. Runaway
  downloads are a hang, not a slow path.
* **`os error 32` was misattributed to `panic = "abort"`.** It fired again with
  no `panic` setting at all, on a different file each time
  (`libgemm_c32-*.rmeta`, `darling_core-*-cgu.05.rcgu.o`,
  `libany_tts-*.rmeta`) — a lock race, not a profile option. Retry fixes it;
  `tts_rust/build_release.ps1` does the retry and re-proves the CPU-only
  guarantee afterwards.
