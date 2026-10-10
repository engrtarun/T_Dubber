# 🗄️ `archive/pytorch_raddi` — the PyTorch / CUDA scrap heap

Mission rule #2 from `CUDA_REMOVAL_TASKS.md`:

> purana logic delete nahi karna, raddi folder mein rakhna hai
> ("don't delete the old python logic, keep it in the scrap folder")

Nothing here is executed by any code path. It is kept so that a future run
can answer "what did the vLLM cell actually do?" without a `git log` hunt
through a 20-minute-deep notebook cell.

---

## Why this was archived

Kaggle run **`test4_gotgVERSION`** (log: `Kaggle Tests Case/test4_gotgVERSION`):

| | seconds |
|---|---|
| pip-installing `vllm` + `torch` + `nvidia-*-cu13` | **1067** |
| total run | 1258 |
| useful output | 0 |

Then:

```
ImportError: libcudart.so.13: cannot open shared object file
```

**85% of the run, no output, and the error is not even about the thing that
was installed.** vLLM 0.26.0 ships wheels built against CUDA 13. The Kaggle
image carries CUDA 12. Every download succeeds — that is the trap. pip
returns 0, the cell prints "installed", and the failure surfaces 18 minutes
later at `import` time, inside the serving cell, by which point every
downstream stage has already been skipped.

A dependency stack that costs 15 minutes to install and then cannot import is
worse than no stack: it burns the budget *and* the run.

## What replaced it

| Job | Was | Is now |
|---|---|---|
| LLM translation | `vllm serve` (torch + CUDA) | `llama-server` (C++ binary, OpenAI-compatible) |
| ASR | `faster-whisper` / torch Whisper | `whisper-cli` (C++ binary) |
| TTS | torch TTS | `candle` — Rust `any-tts` binary (torch-free); `onnxruntime` was the interim |

Both binaries live in `cpp_accelerator/runtimes/` and are fetched once and
cached as a Kaggle input dataset.

**The drop-in was free.** `mazinger/llm.py::build_client()` passes any
`base_url` straight to `openai.OpenAI`, and `llama-server` serves
`/v1/chat/completions`. No call site in `translate.py` changed. That
generality is now guarded by a regression test
(`mazinger/tests/test_no_torch_import.py`) so a future edit cannot quietly
hard-code a vLLM-only assumption back in.

## What is in here

| File | What it is |
|---|---|
| `notebook_vllm_cell_original.py` | The complete original source of `kaggle_worker.ipynb` cell 2 (21587 chars), verbatim. Kept as `.py` because a 21 KB JSON string is unreadable. |
| `bench_qwen_tts.py` | The torch Qwen3-TTS benchmark script, moved here 2026-10-10 when the `candle` engine (Rust `any-tts`) became the torch-free TTS path. Verbatim; nothing deleted. |

The heavy path is **gated, not deleted**: `kaggle_worker.ipynb` cell 2 keeps
`if backend == "vllm":` with the original pip recipe behind it, reachable via
`TDUBBER_LLM_BACKEND=vllm`. That is the escape hatch for debugging the old
stack, and the reason the archive is a copy rather than a move.

## NOT archived, and why

* `mazinger/mazinger/_vendor/qwen_tts/**` — real torch code, but it is a
  live import target of the `tts` extra. Removing it would be removing a
  feature.
* `edge/pin_torch_cuda.py`, `edge/pin_torch.py` — **moved here by the `edge`
  agent** once it had verified no caller remained (repo-wide grep: no reference
  in `edge/*.go`, `edge/*.py`, `edge/Dockerfile`, `edge/README.md`,
  `kaggle_worker.ipynb`, or `kaggle_worker_local/kaggle_worker.ipynb`; the only
  other hits are their own docstrings and historical notes in `WE_ARE_TEAM.MD`,
  which were left alone). They exist solely to untangle the
  torch/vLLM/CUDA-version pin dance that the GGUF architecture removes, so the
  whole pair is dead under the new default.
* `mazinger/mazinger/tts.py`, `transcribe.py`, `assemble.py` — these still
  have torch inside function bodies for the legacy engines. That is
  deliberate: the engines work, and the point was to make torch *optional*,
  not to amputate a shipped feature. The invariant that is actually enforced
  is **no module-level `import torch` anywhere** — so a torch-free box can
  import every module cleanly.

## When to roll back

Roll back if any of these turn out to be true. All of them are checkable in
under a minute:

1. **`llama-server` cannot serve Homura-2B at the context the pipeline needs.**
   The pipeline asks for `max_tokens=8000` (resegment merge) and 4000+prompt
   (fit check); cell 4's pre-flight proves both against the live server
   before a GPU run is spent. If that pre-flight fails on llama.cpp and
   passes on vLLM, set `TDUBBER_LLM_BACKEND=vllm` and the old path runs
   unchanged.
2. **Translation quality regresses** on a Homura-2B GGUF versus the HF
   safetensors. Different quantisation, different tokenizer artefacts.
   Compare a handful of real segments before blaming the server.
3. **The 5 GB Homura-2B safetensors snapshot has to be downloaded anyway.**
   `llama-server` needs a GGUF, not a safetensors tree — that conversion (or
   finding a pre-quantised GGUF) is a one-time cost and must be measured
   before it is assumed free. **This has not been measured yet.**

The rollback is one env var. That is the entire point of gating rather than
deleting.

## Reinstating the original cell

`notebook_vllm_cell_original.py` is the exact original source. To restore:

```python
import json
src = open("archive/pytorch_raddi/notebook_vllm_cell_original.py", encoding="utf-8").read()
nb = json.load(open("kaggle_worker.ipynb", encoding="utf-8"))
nb["cells"][2]["source"] = src.splitlines(keepends=True)
json.dump(nb, open("kaggle_worker.ipynb", "w", encoding="utf-8"), indent=1)
```

Or just set `TDUBBER_LLM_BACKEND=vllm` and run — that path is still there.