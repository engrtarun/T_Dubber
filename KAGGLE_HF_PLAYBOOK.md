# ⚡ KAGGLE + HUGGING FACE PLAYBOOK

T_Dubber me Kaggle aur Hugging Face kaise fit hote hain --
ek page me poora system.

```
                 LOCAL PC (pipeline.py)
                 │  compress → bundle → upload input dataset
                 ▼
        ┌────────────────────────────────┐
        │  KAGGLE DATASETS               │
        │  ├─ dubber-video-<key>  (job)  │  source_video.mp4 + dub_job.json
        │  │                    + multitasker.py + huggingface/
        │  ├─ engrtarun/tdubber-pack     │  5 static Linux binaries
        │  └─ (optional) hf cache ds     │  pre-pulled HF weights
        └────────────────────────────────┘
                 │ mount read-only
                 ▼
        ┌────────────────────────────────┐
        │  KAGGLE GPU WORKER             │
        │  kaggle_worker.ipynb           │
        │  0. pack extract + pin         │
        │  1. pylibs + vLLM (Homura-2B)  │
        │  2. pre-flight (8000 tok)      │
        │  3. find video + dub_job.json  │
        │  4. MULTITASKER (3 threads) ◄──┼── TDUBBER_MULTITASKER=1
        │  5. legacy single-file (fallback)
        └────────────────────────────────┘
        GPU 0: vLLM Homura-2B (translation LLM)
        GPU 1: ASR + TTS (faster-whisper, OmniVoice)
        CPU:   ffmpeg staging + tgup upload (overlapped)
                 │ outputs
                 ▼
        /kaggle/working/output.mp4 + report.json
                 │  kaggle kernels output
                 ▼
        LOCAL PC → telegram_uploader.py (backup)
```

## 1. Hugging Face ka role

| Model | Role | Kaggle me kaise aata hai |
|---|---|---|
| `IndexTeam/Index-Homura-2B` | Translation LLM | `vllm serve` GPU 0 -- mounted snapshot (0 download) ya HF_HOME cache ya hub |
| `k2-fsa/OmniVoice` | TTS (default) | mazinger stage, hub se |
| `Qwen/Qwen3-TTS-12Hz-1.7B-Base` | Voice-clone TTS | mazinger stage, hub se |
| `Systran/faster-whisper-large-v3` | ASR | faster-whisper, hub se |
| `bakrianoo/mazinger-dubber-profiles` | Voice profiles (dataset) | `mazinger.profiles.fetch_profile()` |

**Gated**: CohereX models -- `HF_TOKEN` chahiye
(Kaggle notebook Settings → Secrets → `HF_TOKEN`).

### `huggingface/` folder ka upyog

```bash
# 1. roster dekho (kaun sa local hai)
python huggingface/hf_store.py

# 2. ek cache dataset banao (ek baar, ~15 GB)
python huggingface/seed_cache.py --out ./hf_seed
kaggle datasets create -p ./hf_seed --dir-mode tar

# 3. us dataset ko kaggle_worker.ipynb me attach karo
#    → agle run me hf_store mounted snapshot dhoondh lega
#    → Homura download 525s → 0s
```

`hf_store.ensure_model()` resolution order:
**mounted /kaggle/input → HF_HOME cache → edge Space → hub**.

## 2. Multitasker -- 3-thread pipelining

**Funda**: GPU aur network alag hardware hain. Serial flow me
GPU download ke time idle baithta hai. Multitasker overlap karta hai:

```
Network/CPU : [ stage N+1 ] ───────► [ upload N-1 ]
GPU         :            [ dub N ] ──► [ dub N+1 ]
```

**Threads** (`multitasker.py`):
1. **DownloaderWorker** -- ffmpeg voice-reference extraction,
   job staging → `gpu_queue` (maxsize 2)
2. **GpuWorker** -- `python -m mazinger dub` subprocess →
   `upload_queue` (maxsize 2)
3. **UploaderWorker** -- tgup → Telegram checkpoint +
   `/kaggle/working/output.mp4` staging + validation gate

**Enable**: `TDUBBER_MULTITASKER=1` (Kaggle notebook Settings →
Secrets, ya cell me env var). Legacy cell **bypass** hota hai,
delete nahi hota.

**Batch**: input dataset me `dub_batch.json` dal do:
```json
{"jobs": [
  {"job_id": "ep01", "video_path": "/kaggle/input/ds/ep01.mp4",
   "target_language": "Hindi", "source_title": "Episode 1"},
  {"job_id": "ep02", "video_path": "/kaggle/input/ds/ep02.mp4",
   "target_language": "Hindi"}
]}
```
Poora season ek kernel run me pipeline hoga.

**Resume**: har transition `multitasker_ledger.jsonl` me jati hai.
Kernel re-run → done jobs skip.

**Quality gate**: har dub pe legacy cell ke same 3 checks
(timeline duration ±0.2%, video+audio streams, subtitle
coverage ≥80% + Devanagari ratio for Hindi).

## 3b. Lip-sync -- mouth re-render (post-dub phase)

```
DUB PHASE (vLLM up)        LIP-SYNC PHASE (vLLM STOPPED)
gpu: mazinger dub          gpu: MuseTalk (~4GB VRAM)
                                    │
network: (idle)            network: upload synced N-1 → Telegram
```

* **MuseTalk** default (latent-space inpainting, MIT,
  30fps+, ~4 GB VRAM -- T4 par aaram se fit)
* **Wav2Lip** auto-fallback (torch-only, jab mmcv/mmpose
  build na ho Kaggle ke naye torch ke saath)
* Face donor = SOURCE video, audio = dubbed track
* `TDUBBER_LIP_SYNC=1` + `TDUBBER_BBOX_SHIFT=3..7`
  (Hindi jaisi wide-mouth languages ke liye)
* Cell 5 pe run hota hai -- vLLM terminate ke BAAD
  (GPU 0 sync model ko milta hai, zero contention)
* Batch me har job `"lip_sync": true` flag rakhe
* Full doc: `lip_sync/LIP_SYNC.md`

## 4. Setup checklist (ek naye Kaggle run ke liye)

1. `kaggle_paperWork/kaggle.json` -- Kaggle credentials
   (local, git-tracked nahi)
   `HuggingFace_PaperWork/` -- HF credentials folder
   (hf.env ya token.txt with HF_TOKEN; git-tracked nahi)
2. Pack: `.\build_kaggle_pack.ps1` → `.\update_kaggle_pack.ps1`
   (ya GitHub Actions artifact) → `engrtarun/tdubber-pack`
3. HF cache dataset (optional, ek baar): seed_cache.py flow upar
4. Notebook secrets:
   `TDUBBER_MULTITASKER=1`, `TDUBBER_LIP_SYNC=1`,
   `TDUBBER_BBOX_SHIFT=0`, `TDUBBER_TG_CHANNEL` + `TDUBBER_TGUP_CREDS`
   (Telegram checkpoint ke liye), `HF_TOKEN` (gated ke liye)
5. `python app.py` → Gradio UI → video select → run

## 5. Live agent activity (najar rakhna)

* **edge agent** ne `kaggle_worker_local/kaggle_worker.ipynb`
  me "edge cache" cell add kiya (172 lines) -- HF Space se
  warm pylibs + Homura weights sha256-verified fetch karta
  hai, pip install se PEHLE. Isse 903s setup → seconds.
  **Merge todo**: us cell ko main `kaggle_worker.ipynb` me
  bhi aana chahiye (cell 1 ke pehle).
* `HuggingFace_PaperWork/` -- HF credentials folder
  (kisi ne banaya; screenshots hain abhi). hf_store.py
  isse HF_TOKEN padhti hai (hf.env / token.txt / .env).
* `kaggle_worker_local/` vs `kaggle_worker.ipynb` -- do
  notebook variants alive hain; ek merge point decide karo.

## 4. Local dry-run (GPU ke bina)

```bash
python multitasker_test.py     # overlap + resume + isolation proofs
python multitasker.py --dry-run
python huggingface/hf_store.py # roster + warm status
```

## 5. Files reference

| File | Owner | Purpose |
|---|---|---|
| `multitasker.py` | this agent | pipelined worker engine + lip-sync phase |
| `huggingface/hf_store.py` | this agent | HF resolution connector |
| `huggingface/seed_cache.py` | this agent | cache dataset pre-pull |
| `lip_sync/lip_sync.py` | this agent | MuseTalk/Wav2Lip providers |
| `kaggle_worker.ipynb` | notebook (wired) | Kaggle worker (7 cells) |
| `pipeline.py` | orchestrator | local → Kaggle push |
| `edge/` | edge agent | 24/7 artifact Space (Go) |
| `cpp_accelerator/` | cpp agent | AVX2 assembly kernels |
| `mazinger/` | mazinger agent | dubbing engine (submodule) |
