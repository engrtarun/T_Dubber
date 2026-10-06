# 🗺️ AGENTS_MAP -- kis agent ka topic kya hai

Repo me kai agents parallel me kaam kar rahe hain. Ye map
(uncommitted changes + folder contents se banaya gaya hai)
batata hai ki har agent ka **topic** kya hai, taaki kaam
overlap na ho.

| # | Agent / Area | Topic | Evidence (git status / folder) |
|---|---|---|---|
| 1 | **cpp_accelerator** | C++20 + **NASM assembly** kernels: `kernels/normalizer_gate_gain.asm` (AVX2 gate/gain) aur `kernels/stitcher_mix.asm` (AVX2 timeline mixer -- 3 entry points: lay/duck/add_voice). CMake wiring, equivalence tests. | `M cpp_accelerator/CMakeLists.txt`, `?? cpp_accelerator/build_mix/`, `?? cpp_accelerator/kernels/stitcher_mix.asm`, `?? cpp_accelerator/stitcher_mix.{cpp,h}`, `?? cpp_accelerator/tests/mix_equivalence_test.cpp` |
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
| `huggingface/` | HF connector: `models.json` roster, `hf_store.py` (mounted→HF_HOME→edge→hub resolution), `seed_cache.py` (Kaggle cache dataset pre-pull), `.env.example` |
| `lip_sync/` | **NEW**: `lip_sync.py` provider abstraction (MuseTalk default ~4GB VRAM MIT / Wav2Lip torch-only fallback / Fake for tests), `LIP_SYNC.md` (model research + Kaggle fit + test plan) |
| `kaggle_worker.ipynb` | Cell 4 = multitasker, Cell 5 = lip-sync (vLLM stop → GPU free), Cell 6 = legacy single-file path (bypass-guard, **deleted nahi**) |
| `wire_multitasker.py` | Notebook wiring script (idempotent) |
| `pipeline.py` | Stage 2 me multitasker.py + huggingface/ + lip_sync/ input dataset me attach |

## ⚠️ cpp agent ko report karna

`cpp_accelerator/build_mix/mix_equivalence_test.exe` sab cases print
karta hai ("ok") par exit code **-1073740791** (0xC0000409,
STATUS_STACK_BUFFER_OVERRUN) deta hai -- crash test teardown me
lagta hai. `normalizer_kernel_test.exe` clean PASS (exit 0).
Assembly kernels correct hain (saari "ok" lines pass), par mix
test ka teardown crash cpp agent ko dekhna chahiye.

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
