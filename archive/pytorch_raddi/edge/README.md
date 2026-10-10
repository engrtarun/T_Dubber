# `archive/pytorch_raddi/edge/` — the torch pin scripts

These two files used to live in `edge/`. They are dead under the GGUF
architecture and have been moved here, not deleted, per mission rule #2 in
`CUDA_REMOVAL_TASKS.md`:

> purana logic delete nahi karna, raddi folder mein rakhna hai

| File | What it did |
|---|---|
| `pin_torch.py` | Pinned `torch==2.13.0` to vLLM's own choice in the speech pip pass, so two passes into one `--target` directory would not download a second, different torch (~554 MB) and then fail to overwrite the first. |
| `pin_torch_cuda.py` | Patched both notebook copies to install vLLM against the `cu128` index as an atomic triple, and added a `DEEP_PROBE` that *imports* `torchaudio` — because the CUDA version check only fires at import time, which is 18 minutes into a run instead of 1 second. |

## Why they are dead

Both exist to make one pip resolve agree with itself across a PyTorch + CUDA
build. That whole problem disappears when the torch stack does:

```
Kaggle run test4_gotgVERSION
  1067 s of 1258 s   pip install vllm + torch + nvidia-*-cu13
       0 s           usable output
  ImportError: libcudart.so.13: cannot open shared object file
```

vLLM 0.26.0 ships CUDA 13 wheels; the Kaggle image ships CUDA 12. **There is no
pin that fixes that from the inside** — the mismatch is between what pip wants
to install and what the image provides, and the closest these scripts got was
turning an 18-minute failure into a 1-second one, while still paying the
15-minute install.

The replacement is `llama-server` (llama.cpp) for translation and `whisper-cli`
(whisper.cpp) for ASR: two static binaries, no interpreter, no dependency
resolve. See the parent `README.md` for that story.

The one thing these scripts were genuinely good at — failing fast instead of
failing at minute 18 — survives in the new architecture in a better form: the
edge Space verifies every model blob against a published sha256 **before** it is
installed (`edge/model_gguf.go`, `huggingface/gguf_store.py`). A wrong or
truncated model is refused in milliseconds and the bad bytes are deleted, rather
than surfacing halfway through a chunk.

## Callers

**None.** Verified across the whole repo at the time of the move:

| searched | result |
|---|---|
| `edge/*.go`, `edge/*.py`, `edge/Dockerfile`, `edge/README.md` | no reference |
| `kaggle_worker.ipynb` | no reference |
| `kaggle_worker_local/kaggle_worker.ipynb` | no reference |
| repo-wide (`rg pin_torch`) | only the scripts' own docstrings, plus historical notes in `WE_ARE_TEAM.MD` |

They were already unreferenced before the move: both are one-shot idempotent
notebook patchers that were run once by hand and pasted into the notebooks.
`WE_ARE_TEAM.MD` records that run (`8/8 anchors patched`, then `8/8 already
applied` on rerun) — that log is the only remaining reference and it is left
untouched on purpose.

## If you are reading this because something is missing

You are probably looking for the torch pin that used to sit here. It is not
needed: nothing in the current path pins torch, because nothing in the current
path installs torch. If you are debugging an old notebook run, the original
pip recipe is preserved verbatim in
`../notebook_vllm_cell_original.py` and is still reachable at runtime via
`TDUBBER_LLM_BACKEND=vllm`.
