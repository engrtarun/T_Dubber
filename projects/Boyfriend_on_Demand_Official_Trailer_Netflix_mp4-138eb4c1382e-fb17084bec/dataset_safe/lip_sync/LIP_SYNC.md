# 👄 LIP-SYNC -- best HF model, usage, implementation, tests

## 1. Model choice (2026 web research)

| Model | VRAM | License | Speed | Quality | Verdict |
|---|---|---|---|---|---|
| **MuseTalk** (TMElyralab/Tencent) | ~4 GB | **MIT** (commercial OK) | 30fps+ on V100 | Good (256×256 face) | **DEFAULT** |
| Wav2Lip (Rudrabha) | ~2–3 GB | Apache-2.0 | fast | Soft mouth region | **FALLBACK** |
| LatentSync (ByteDance) | ~12–24 GB | Apache-2.0 | slow | Best | Too heavy for T4 alongside the stack |
| SadTalker / LivePortrait | varies | varies | varies | Talking-head, not video dub | Wrong tool: needs a still image, not a video face donor |

**Why MuseTalk wins for T_Dubber:**
* Latent-space face **inpainting** -- re-renders only the mouth region of the *existing* video (exactly the dubbing use case).
* ~4 GB VRAM → fits a Kaggle T4 (16 GB) next to nothing else.
* MIT license, weights on HF (`TMElyralab/MuseTalk`), 100+ Spaces prove it runs.
* `bbox_shift` knob controls mouth openness -- wide-mouth languages (Hindi, Arabic) need +3..+7.
* Audio encoder is frozen **whisper-tiny** -- multilingual by construction.

**Why Wav2Lip stays:** it is torch-only. MuseTalk needs `mmcv>=2.0.1`/`mmpose` prebuilt wheels, and Kaggle's torch (2.13+cu128) moves faster than mmcv's wheel matrix. If `mim install mmcv` cannot resolve, MuseTalk setup fails **loudly** and the provider auto-falls back to Wav2Lip. The right trade: softer mouth, pipeline still moves.

## 2. Kaggle per kaam karega? KYA + KYUN

| Requirement | Kaggle T4 | Fit |
|---|---|---|
| VRAM | 16 GB | MuseTalk ~4 GB ✅ (vLLM stopped first, GPU 0 free) |
| Disk | /kaggle/working 20 GB output quota | MuseTalk repo+weights ~2 GB, cached ✅ |
| Internet | notebook setting | weights download on first run only ✅ |
| CUDA | T4 (sm_75) | pure inference, no custom kernels ✅ |
| Time | long-running kernel | 30fps+ → 2h movie ≈ 2–4h sync, chunked by the multitasker ✅ |
| **Timing trick** | GPU vs network are separate hardware | while GPU syncs job N, uploader ships job N-1 to Telegram (the unlimited slow cloud) ✅ |

**vLLM ordering is the key insight:** the translation LLM and the sync model both want the GPU, but a dub never needs them at the same time. So the lip-sync phase runs in its own notebook cell AFTER `vllm.terminate()` -- GPU 0 goes to MuseTalk, zero contention.

## 3. How to use

### Enable (Kaggle notebook Settings → Secrets)
```
TDUBBER_MULTITASKER = 1
TDUBBER_LIP_SYNC    = 1
TDUBBER_BBOX_SHIFT  = 0        # Hindi/audio wide-mouth: try 3..7
TDUBBER_TG_CHANNEL  = @your_backup_channel
TDUBBER_TGUP_CREDS  = /kaggle/working/tgup_creds.json
HF_TOKEN            = hf_...     # gated models only
```

### Batch manifest (input dataset) -- `dub_batch.json`
```json
{"jobs": [
  {"job_id": "ep01", "video_path": "/kaggle/input/ds/ep01.mp4",
   "target_language": "Hindi", "lip_sync": true, "bbox_shift": 5},
  {"job_id": "ep02", "video_path": "/kaggle/input/ds/ep02.mp4",
   "target_language": "Hindi", "lip_sync": true}
]}
```

### Single job -- `dub_job.json`
```json
{"target_language": "Hindi", "lip_sync": true, "bbox_shift": 0}
```

### Provider override
```
TDUBBER_LIP_SYNC_PROVIDER = musetalk | wav2lip | fake
```
Auto mode: MuseTalk unless its setup already failed once (`.musetalk_failed` marker), then Wav2Lip.

### Local dry-run (no GPU, no download)
```bash
python multitasker_test.py        # 5 tests, ALL PASS
python multitasker.py --dry-run
```

## 4. Implementation map

```
multitasker.py
├── DubJob.lip_sync / .bbox_shift        # job flags
├── LipSyncWorker (thread)               # GPU: provider.sync per job
│     face donor = SOURCE video
│     audio      = dubbed track (ffmpeg -vn extract)
│     out        = synced.mp4
└── run_lip_sync_phase()                 # post-dub phase:
      LipSyncWorker ‖ UploaderWorker     # sync N ‖ upload N-1

lip_sync/lip_sync.py
├── LipSyncProvider (contract)           # setup() + sync()
├── MuseTalkProvider                     # git clone + mmcv + 5 weight packs
├── Wav2LipProvider                      # torch-only fallback
└── FakeProvider                         # dry-run orchestration tests

kaggle_worker.ipynb
├── cell 4: multitasker (dub phase, vLLM up)
├── cell 5: lip-sync (stops vLLM, runs the phase)
└── cell 6: legacy single-file path (guarded, kept)
```

**Weights MuseTalk fetches** (cached under `/kaggle/working/lip_sync/MuseTalk/models/`):
`TMElyralab/MuseTalk` (musetalk.json + pytorch_model.bin), `stabilityai/sd-vae-ft-mse`, `yzd-v/DWPose` (dw-ll_ucoco_384.pth), whisper `tiny.pt`, face-parse-bisent `79999_iter.pth`, `resnet18`.

## 5. Test plan -- kya prove ho chuka, kya nahi

| Test | Status | Proves |
|---|---|---|
| `test_lip_sync_phase_overlaps_sync_with_upload` | ✅ PASS | 7.0s wall vs ≥9s serial -- sync/upload overlap real |
| `test_lip_sync_phase_skips_unflagged_jobs` | ✅ PASS | per-job flag honored |
| Real-path plumbing (real ffmpeg extract + FakeProvider + staging) | ✅ PASS | ffmpeg audio extraction, job dirs, output staging all real |
| Notebook cells 4/5/6 compile | ✅ PASS | wiring is valid Python |
| **MuseTalk inference on a real clip** | ⏳ needs 1 Kaggle run | model quality + mmcv build |

**The one thing only a Kaggle run can prove:** the actual mmcv/mmpose wheel resolution against Kaggle's torch, and the visual quality. That is the scheduled next step: `TDUBBER_LIP_SYNC=1` + a 30-second clip, watch cell 5. If MuseTalk setup fails → Wav2Lip auto-fallback (by design).

## 6. Confidence (honest)

| Layer | Confidence | Why |
|---|---|---|
| Orchestration (threads, queues, ledger, overlap) | **HIGH** | same proven pattern as the dub multitasker; 5 automated tests pass |
| MuseTalk fits Kaggle T4 | **HIGH** | ~4 GB VRAM, MIT, pure inference, 100+ HF Spaces run it |
| End-to-end quality on movie content | **MEDIUM** | 256×256 face render, jitter, mustache/lip-color drift are documented MuseTalk limits; side-profile/turned faces are the known weak spot |
| mmcv build on Kaggle's torch | **MEDIUM** | the one real setup risk; Wav2Lip fallback exists exactly for this |

## 7. Known limits (MuseTalk, from its own card)
* Face region renders at 256×256 (optional GFPGAN upscale if needed)
* Single-frame generation → slight jitter
* Identity details (mustache, lip color) can drift
* Trained on HDTF (frontal talking heads) -- side profiles degrade

## 8. Doosre agents ki nigrani (other agents' work)

Ye change **purely additive** hai -- cpp_accelerator, mazinger submodule, edge/ kisi ko touch nahi hua:
* `git status` se weekly check karo (AGENTS_MAP.md update karte raho)
* ~~cpp agent ka `mix_equivalence_test.exe` exit-code crash (-1073740791) abhi bhi open hai~~ **RESOLVED** -- re-ran it: `PASS` + exit 0 after the rebuild
* kisi ka kaam rukna nahi chahiye: meri files alag folders me hain (`lip_sync/`, `multitasker.py`, `huggingface/`, `wire_multitasker.py`)
