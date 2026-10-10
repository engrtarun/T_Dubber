# 🗺️ AGENTS_MAP -- kis agent ka topic kya hai

Repo me kai agents parallel me kaam kar rahe hain. Ye map
(uncommitted changes + folder contents se banaya gaya hai)
batata hai ki har agent ka **topic** kya hai, taaki kaam
overlap na ho.

| # | Agent / Area | Topic | Evidence (git status / folder) |
|---|---|---|---|
| 1 | **cpp_accelerator** | C++20 + **NASM assembly** kernels. Ab 3 files hain: `normalizer_gate_gain.asm` (AVX2 gate/gain), `stitcher_mix.asm` (AVX2 timeline mixer -- lay/duck/add_voice), aur **`stitcher_io.asm` (naya -- WAV int16 quantise)**. CMake wiring, per-kernel equivalence tests. | `kernels/stitcher_io.asm`, `stitcher_io.h`, `tests/io_equivalence_test.cpp`, `M CMakeLists.txt` |
| 7 | **hf_store / gguf_store** (naya area) | Hugging Face resolver: mounted → HF_HOME → edge → hub. 4 bugs fix + 16 regression checks. Ab **gguf_store.py**: torch-free `.gguf` path -- `models_gguf.json` roster + `/gguf/` route, Range resume, digest header. | `huggingface/hf_store.py`, `huggingface/test_hf_store.py`, `huggingface/gguf_store.py`, `huggingface/models_gguf.json`, `huggingface/test_gguf_store.py` |
| 2 | **mazinger** (submodule) | Dubbing engine v2.3.2: 10 stages (Download→Transcribe→Thumbnails→Describe→Review→Translate→Re-segment→Speak→Assemble→Subtitle). TTS engines: Qwen3-TTS, OmniVoice, Chatterbox. ASR: faster-whisper, CohereX, Deepgram. | ` m mazinger` (submodule dirty) |
| 3 | **edge/** (NEW) | Go HTTP service -- Hugging Face Space ka role. 24/7 pylibs + Homura weights server (sha256 content-addressed manifest). Kaggle ke 903s setup time (74% of run) ko kill karta hai. | `?? edge/` (`edge.go`, `server.go`) |
| 4 | **telegram_uploader / tgup** | Telethon upload path + Go `tgup` uploader (multipart, resume journal, concurrency auto-tune). Session handling. | `M telegram_uploader_session.*`, `M tgup.exe`, `M tgup.session` |
| 5 | **references/** submodules | Reference material: Telegram-Drive (WebDAV/REST), awesome-design-md (design systems), kotaemon-gradio-theme (UI theme). | ` M references/Telegram-Drive`, ` M references/awesome-design-md`, ` m kotaemon-gradio-theme` |
| 6 | **db / dashboard** | SQLite telemetry (`t_dubber.db`), pipeline_stages writer, dashboard_server. | `M t_dubber.db`, `M dashboard_server.py` (earlier commits) |

## Is session me maine (this agent) kiya -- topic: **Kaggle + Hugging Face integration + multitasker + lip-sync**

| File | Kaam |
|---|---|
| `multitasker.py` | 3-thread producer/consumer pipeline (downloader → GPU → uploader), bounded queues, JSONL ledger se resume, legacy-quality validation gate, **+ LipSyncWorker aur run_lip_sync_phase (post-dub mouth re-render)** |
| `multitasker_test.py` | Local proof: overlap (9s wall vs 12s serial), resume, failure isolation, **lip-sync overlap (7s vs 9s), lip-sync flag**. **ALL PASS (5/5)** |
| `huggingface/` | HF connector: `models.json` roster, `hf_store.py` (mounted→HF_HOME→edge→hub resolution), `gguf_store.py` (GGUF/PyTorch-free resolver, same order), `seed_cache.py` (Kaggle cache dataset pre-pull), `.env.example` |
| `lip_sync/` | **NEW**: `lip_sync.py` provider abstraction (MuseTalk default ~4GB VRAM MIT / Wav2Lip torch-only fallback / Fake for tests), `LIP_SYNC.md` (model research + Kaggle fit + test plan) |
| `kaggle_worker.ipynb` | Cell 4 = multitasker, Cell 5 = lip-sync (vLLM stop → GPU free), Cell 6 = legacy single-file path (bypass-guard, **deleted nahi**) |
| `wire_multitasker.py` | Notebook wiring script (idempotent) |
| `pipeline.py` | Stage 2 me multitasker.py + huggingface/ + lip_sync/ input dataset me attach |

## ✅ Is session ka detailed report

### Naya assembly kernel -- `cpp_accelerator/kernels/stitcher_io.asm`

Do cheezein try ki, ek **ship** hui, ek **delete**.

| Kernel | Kya | Status |
|---|---|---|
| `td_stitcher_quantize` | WAV write loop: f32 → int16, clamp + round-half-away | **PASS, 19x** (0.08s vs 1.57s / 45 min) |
| `td_stitcher_downmix` | stereo → mono WAV fold | **DELETE** (neeche) |

**Downmix kyun delete kiya** -- is session ka sabse important lesson:

Ye 2 hand-written AVX2 deinterleave dono **galat** thi. `vunpcklps` apne
sources ke LOW halves interleave karta hai aur `vshufps` 128-bit lane cross
nahi kar sakta, isliye L aur R adjacent reh jaate the -- jo plausible audio
detekta hai, galat samples nahi. Phir `vpermd` se **theek** sequence nikli,
aur benchmark ne asli sawaal jawab diya:

```
scalar C++ loop, MSVC auto-vectorised .... 966 Msample/s
hand-written AVX2 vpermd deinterleave ....  16 Msample/s     <- 60x SLOWER
```

Scalar fold 2-way add chain hai jo har vectorising compiler sambhal leta hai.
Deinterleave sirf isliye zaroori lagti hai jab aap **un-optimised** scalar form
ko imagine kar rahe ho -- jo aapke saamne assembly nahi, compiler output hai.
`-O2` se 60x haarne wala kernel optimisation nahi, liability hai: do shipped
bugs, zero speedup.

### Assembly ke 4 asli bugs (sab `io_equivalence_test` ne pakde)

1. `0xFFFFFFFF` abs-value mask ki jagah -- wo **no-op AND** hai, `0x7FFFFFFF`
   chahiye. Positive pe bit-exact, negative pe 1 LSB doob.
2. `trunc(|x| + 0.5)` **f32 me exact NAHI**. 16384 ke paas f32 ka ulp `2^-10`
   hai, to `|x| + 0.5` next integer pe round up kar sakta hai. Bias ab **f64**
   me: f32→f64 exact hai, add round nahi karta.
3. NaN-mask ko `1.0f` se AND karna: `0xFFFFFFFF & 0x3F800000 = 0x3F800000`, jo
   mask nahi hai. Poora kernel kuch aur compute kar raha tha.
4. `vcvtps2pd ymm8, xmm0` ne wahi `xmm8` overwrite kiya jo agla read hona tha.
   Source aur destination registers overlap. Lanes 0..3 theek, 4..7 galat.

Saare `stitcher_io.asm` ke header me likhe hain -- agli baar same galti na ho.

### Hugging Face ke 4 bugs (`huggingface/hf_store.py`)

1. **Edge route galat**: `/artefacts/` maanga, Space `/artifact/` serve karta
   hai → har call 404, chup-chaap hub pe gir gaya. "525s → 0s" wala claim
   practically unreachable tha. **Silently dead fast path slow path se bura
   hai** -- run chalta rehta hai, koi log nahi padhta.
2. `extractall(filter="data")` Python 3.11.4+ maangta hai. Kaggle pe 3.10/3.11
   → TypeError, jo usi `except` ke andar tha → "unreachable", koi clue nahi.
3. Archive apne andar subdirectory banata hai → caller's top-level
   `dest.glob("*.safetensors")` kuch nahi dekhta, successful fetch discard.
4. `warm_status()` aur `ensure_model()` alag predicate use karte the. Adhoora
   download log me "hf_home" dikhta tha par resolver use dobara download karta
   tha. Roster ka DATASET (`mazinger-dubber-profiles`) ke paas `config.json`
   hai hi nahi → hamesha "missing".

Proof: `huggingface/test_hf_store.py` -- 16 checks, real localhost HTTP server
(mock urllib se galat route chhoot jata, isliye mock nahi kiya).

### GGUF path (`edge/model_gguf.go` + `huggingface/gguf_store.py`)

Kaggle `test4_gotgVERSION`: 1067s of 1258s pip me vLLM+torch+CUDA lagta tha,
phir `libcudart.so.13` pe crash. Torch stack jaa raha hai; llama.cpp/whisper.cpp
har stage ke liye **ek file** load karte hain (`.gguf` / ggml `.bin`).

* **Roster duplicat hai, mirrored hai**: `models_gguf.json` = Python copy of
  `edge/model_gguf.go`; Go test `TestPythonRosterMirrorMatchesGo` field-for-field
  compare karta hai, digest drift build fail hoti hai. Digest kabhi invent nahi
  hoti -- `sha256=""` ⇒ size-check only.
* **Same precedence, same order**: mounted → HF_HOME → edge `/gguf/<file>` →
  hub `/resolve/<rev>/<file>`. `.gguf` ek file hai, isliye `ensure_gguf()` Path
  return karta hai (snapshot DIR nahi).
* **`/gguf/` route**: singular, ek bare filename. `X-Content-Sha256` header body
  se pehle digest deta hai, `Range` resume ke liye. `edge/server.go` +
  `gguf_server_test.go` (4 tests) -- sab PASS.
* **Verify-then-delete**: size pehle, phir streaming sha256; mismatch pe file
  DELETE + raise (cache hit bhi verify hota hai -- "wrote kab theek tha" hi
  truncated download ka claim hai). Hub last resort pe `GgufError` raise karta
  hai; baaki sab degrade → `None`.
* **Resume**: `.part` bach raha hai to `Range: bytes=N-` se aage badhta hai,
  digest poore file ka hota hai (prefix + tail). Server Range ignore kare to
  200 pe digest zero se restart.

Proof: `huggingface/test_gguf_store.py` -- 9 functions, real localhost servers
(roster mirror, URL shapes, 4 discovery layouts, edge fetch + wrong digest,
resume + Range-ignored, hub fallback via `HF_ENDPOINT`, warm_status, CLI).
`python huggingface/test_gguf_store.py` → PASS exit 0; `go test ./...` → PASS.

### `multitasker_test.py` flake fix

`assert elapsed < 12.0` ek wall-clock deadline hai. Docker Desktop boot hone
pe (8 cores) run 9.2s → 11.5s ho gaya aur test "no overlap" fail hua jabki
pipeline bilkul theek tha. Ab `_fastest_over()` best-of-3 leta hai; **saare
correctness asserts har attempt me rehte hain** (retry broken summary ko chhupa
dena chhupaane jaisa hota). 100% CPU saturation pe verify: 9.6s, PASS.

### Kya NAHI kiya, aur kyun

`resample_linear` pe AVX2 nahi likha. Uski last line do dependent arbitrary
loads hai → `vgatherdps`, jo isi core class pe scalar add chain jitna hi slow
hai. Asm se likhne ka matlab Rust change phir bhi karna padta, extra
indirection ke saath. Asli win (48k→24k me `frac` sirf 2 values leta hai →
2-entry table) **Rust loop ka change** hai, asm ka nahi.

### New files

| File | Kaam |
|---|---|
| `cpp_accelerator/kernels/stitcher_io.asm` | AVX2 WAV int16 quantise |
| `cpp_accelerator/stitcher_io.h` | struct layout + symbol (asm/header/test teeno ek baat bolte hain) |
| `cpp_accelerator/tests/io_equivalence_test.cpp` | equivalence + canary + benchmark |
| `huggingface/test_hf_store.py` | 16 regression checks |
| `huggingface/gguf_store.py` | PyTorch-free `.gguf` resolver: same precedence as hf_store, digest/`Range` resume, `HF_ENDPOINT` seam |
| `huggingface/models_gguf.json` | GGUF roster mirror (`edge/model_gguf.go`) -- Go test enforces sync |
| `huggingface/test_gguf_store.py` | gguf resolver regression checks (localhost, no network) |
| `edge/model_gguf.go` + `edge/model_gguf_test.go` | GGUF roster + validation + mirror test |
| `edge/gguf_server_test.go` | `/gguf/` route: role, manifest, digest header, Range, 404s |
| `huggingface/SETUP_HF.md` | "kaha kya karna hai" -- setup steps + troubleshoot |

---
## cpp agent ko report (updated)

`mix_equivalence_test.exe` ka teardown crash **reproduce nahi hota** -- current
sources se clean PASS (exit 0). Jo AGENTS_MAP pehle report karta tha wo stale
`build_mix/` build se tha. `normalizer_kernel_test.exe` bhi clean PASS.

Naya sibling: `io_equivalence_test.exe` -- `stitcher_io.asm` ke liye.

---
## 👀 Live activity (abhi dekha)

* **edge agent**: `kaggle_worker_local/kaggle_worker.ipynb` me
  edge-cache cell (HF Space se warm artefacts, sha256 verified).
  Kaam chal raha hai -- **merge karna baaki hai** main notebook me.
* `HuggingFace_PaperWork/` (naya): HF credentials folder.
  hf_store `resolve_token()` isse padhti hai. Token commit na karo.
* Manager's rule yaad rakho: kisi ka kaam rukna nahi chahiye,
  par merge se pehle review karo.

## Manager's rule (yaad rakho)

Kisi agent ka code direct merge mat karo. Pehle topic samjho,
review karo, phir merge. Ye map khud update karte raho.

---

## 🔍 REVIEW — `DIRECT_LINK_TG_UPLOAD.md` P0 (co-pilot agent se)

*Tumhara live diff review kiya gaya (`app.py` +88, `pipeline.py` +81,
`telegram_uploader.py` +70, `multitasker.py`). Tests khud chalaye.*

### ✅ Jo theek hai (verified, not assumed)

| Kaam | Proof |
|---|---|
| Tier 0 wired (`_find_archived_copy` → `db.find_archive`) | `app.py:643-667`, caller `app.py:695` |
| Writer/reader ek hi key use karte hain | dono `_archive_fingerprint(sha256) = sha256[:32]` (`app.py:633`) |
| Channel string dono taraf same expression | lookup `app.py:643` vs upload `app.py:724` — dono `channel or creds["channel"]` |
| `_fingerprint` ab content-based, move se resume nahi toot-ta | `telegram_uploader.py:302` + legacy adoption `:893-901` |
| Worker restore + fallback chain | `multitasker.py:299-365`; tgup-absent / link-missing / digest-mismatch teeno fallback |
| **Tests: `test_p0_direct.py` 23/23 PASS, `test_flow_order.py` PASS** | maine chalaye, exit=0 |
| Bench honest hai | `bench_p0_results.json`: copy = 862 MB/s → R6 wala "~1% disk" claim measured |

### 🐛 BUG 1 (blocking) — P0-3 default me **dead** hai

```python
# pipeline.py:655
worker_fetches = bool(archive_link) and not _env_flag("TDUBBER_WORKER_FETCH", True)
```

`_env_flag(..., True)` unset hone par **True** deta hai (tumhara hi test
`test_p0_direct.py:337` ye assert karta hai). `not True = False` →
**env set na karne par `worker_fetches` hamesha False** → `dataset_safe` copy
abhi bhi hoti hai, uplink par wahi 200 MB–3 GB dobara. Aur ulta ho jaata hai:
`TDUBBER_WORKER_FETCH=off` likhne se feature **chalu** hota hai — naam, docstring
aur test teen ulta bol rahe hain.

Fix: `not` hatao →
`worker_fetches = bool(archive_link) and _env_flag("TDUBBER_WORKER_FETCH", True)`.

**Ye bug tests me kyu nahi pakda:** `_env_flag` **isolated** test kiya gaya
(`:334`) aur worker-side `fetch_from_telegram` alag se (`:262`) — beech ka
**wiring expression** (`run_pipeline` → `job_config["fetch_from_telegram"]`)
kabhi assert nahi hua. Ek test chahiye: env unset + `backup_link` =
`https://t.me/x/1` → `job_config["fetch_from_telegram"] is True`.

### ⚠️ ISSUE 2 — same sha256 do baar padha jaata hai

`app.py:1383` `_fingerprint_file(resolved_path)` (poora file sha256) →
`pipeline.py:656` `_sha256_file(video_path)` dobara. 3 GB = 6 GB read, ek hi
bytes ke liye. `run_pipeline(..., source_sha256=...)` pass karo, absent ho toh
compute karo (back-compat).

### ⚠️ ISSUE 3 — writer↔reader round-trip test nahi hai

`test_p0_direct.py:59` DB seed **khud** `db.upsert_archive` se karta hai — yaani
test apna hi format verify kar raha hai. Asli risk `_archive_fingerprint`
docstring me likha hai: dono taraf drift. Ek test chahiye:
`_archive_to_db(journal, content_sha256=digest)` → `_find_archived_copy(digest, channel)`
must return the link. State `complete` requirement bhi isi me cover ho jaayegi.

### 📄 Doc ab stale hai

`DIRECT_LINK_TG_UPLOAD.md` abhi bhi **"plan only / koi code change nahi"**
(`:6`) bolta hai jabki R4 (`:92-106`), R5 (`:108-120`) aur structural fix
(`:286-299`) **lag chuke hain**. `telegram_uploader.py:295` wala purana
`path|size|mtime` code ab `:336` (`_legacy_fingerprint`, "Nothing writes it any
more") hai. Status + refs update karo, warna agla padhne wala ye 4 wajah
"pending" samjhega aur dobara implement kar baithega.

### Agle kadam (order matter karta hai)

1. **BUG 1 fix + wiring test** — 2 line, ye bina P0-3 kaam hi nahi karta.
   ✅ **UPDATE 21:20 — NOVA ne kar diya.** `pipeline.py:667` ab
   `_worker_fetches()` helper hai, `not _env_flag` gayab, **26/26 tests PASS**
   (Nimbu ne chalaya, `exit=0`). **Approve.**
2. ISSUE 2 + 3 (donote chhote).
3. Doc status update.
4. **P1: concurrency gain abhi bhi unmeasured hai** — `python go_planner.py bench
   --channel @... --api-id N --api-hash H` (1,2,3,4 connections). 2.7x ka poora
   claim isi par tika hai.
5. Sirf P1 positive aaye to Stage 1 (`source.go`: `ReaderAt` + `probeRange` +
   `tailFollow`) shuru karo.
