# Hugging Face setup — what it does in this project, and what you have to do

## The short version

Hugging Face is the **origin** for the two things a Kaggle run re-downloads
every session. It is not the worker, not the queue, and not the GPU.

```
  Hugging Face dataset repo (FREE, public)
    manifest.json                 <- what exists, with a sha256 per entry
    artifact/pylibs/pylibs.tar    <- prebuilt dependency tree
    artifact/weights/weights.tar  <- prebuilt model cache
            |
            |  fetched by the static `edge-fetch` binary that now ships
            |  inside the Kaggle pack
            v
  /kaggle/working/pylibs  +  /kaggle/working/hf_cache
            |
            v
  Kaggle GPU worker (vLLM + Mazinger)  ->  dubbed.mp4  ->  Telegram (tgup)
```

A Kaggle session starts with an **empty** `/kaggle/working`, so this is a
*cold-run* saving. For a genuinely warm run, mount the same tree as a Kaggle
Dataset instead — `find_pylibs()` checks `/kaggle/input` first, and mounted disk
costs zero seconds. Both paths exist; they do not conflict.

## Why a repository and not a Space

Since 2026, Gradio and Docker Spaces require a paid plan (PRO, $9/month). Free
accounts get unlimited **static** Spaces and free public repository storage. The
worker only ever does GETs, so it needs no process to talk to: a public dataset
repository served from `/resolve/main/` answers `/manifest.json` and
`/artifact/<name>` exactly as `edge/server.go` does.

One repository therefore works as an origin with **or** without the Go server:
set `EDGE_URL` to either and nothing else changes. That is deliberate — the
server exists for a stable private origin, not because the fetch side needs it.

## What you have to do (about 5 minutes, once)

1. **Create the dataset repo.** On huggingface.co → *New* → *Dataset* →
   name it `tdubber-edge` → visibility **Public**. A write token cannot create
   repos it cannot see, and a public repo means the Kaggle kernel needs no HF
   token at all.

2. **Create a write token.** *Settings* → *Access Tokens* → *Write* role, and
   send it to me. It is used only to `git push`; nothing else in the repo reads
   it, and the publisher removes it from the git remote before it exits.

3. **Publish once, from a machine that has the tree.** The tree only exists on
   a Kaggle run (or on the machine that produced it), so the first publish is a
   separate step from this document — see below.

4. **Point the kernel at it.** Nothing to do: `edge/notebook_cell.py` defaults
   `DEFAULT_EDGE_URL` to `https://huggingface.co/datasets/pocotarun/tdubber-edge/resolve/main`.
   Override with `EDGE_URL` in a local run.

## Publishing, and why it is a separate step

The artefact is the *installed* tree from a completed run, not something the
repository can synthesise: it contains `.tdubber_ready`, which is written only
after the install **and** its import probe both pass, and `pylibs/vllm`, which
only exists once vLLM has been installed. `edge-publish` refuses to package a
tree without them, because a tree that `find_pylibs()` would ignore would still
be downloaded, unpacked and then wasted.

```powershell
# from a tree produced by a successful run
edge-publish.exe -stage hf-repo -add pylibs=<...>/kaggle/working/pylibs `
                -add weights=<...>/kaggle/working/hf_cache -as weights=hf_cache `
                -push -repo pocotarun/tdubber-edge -token $env:HF_TOKEN
```

On Kaggle the same command runs with `$HF_TOKEN` set from a notebook secret.
Uploading ~9 GB through git-lfs is a one-off; after that the repository is only
re-pushed when a digest actually changes.

## Where the seconds went, measured

Numbers from the successful Kaggle run (932.7 s) and from this machine:

| thing | cost | what changed |
|---|---|---|
| cold `pip install` of pylibs | 458 s | becomes one verified extract |
| model weights download | 525 s | becomes one verified extract |
| gzip decompression of those artefacts | **74 s** | none: artefacts are stored as plain `.tar` |
| second sha256 pass over the same bytes | ~2 s | removed (`edge-fetch` trusts the digest it just verified) |
| re-verifying an artefact from an earlier run | varies | still verified — that one was written by a different process |
| stitcher background loop, 43.2M samples | 0.21 s → 0.07 s | AVX2 kernel (`kernels/stitcher_mix.asm`) |

The 74 s row is the one worth keeping: a 400 MB sample of the payload actually
ships compressed at a ratio of **1.000** and decompresses at 121 MB/s, so
`tar.gz` would have cost a minute and a quarter of CPU to move the same bytes.

## Verification, in the order worth doing

```powershell
go test ./...                      # edge: 60+ tests incl. traversal/symlink/unpack
edge-publish -stage hf-repo -add pylibs=<tree>       # digest printed
edge-server (EDGE_ROOT=...)                          # optional: local origin
edge-fetch -url ... -dest ... -unpack-dir ... -role pylibs
```

The log line that proves it worked on Kaggle:

```
edge: unpacked pylibs/pylibs.tar -> /kaggle/working (4 files, ...)
role pylibs: tree at /kaggle/working/pylibs (..., ready_marker=yes)
```

`ready_marker=yes` is the one that matters. Without it `find_pylibs()` skips the
tree and the run quietly does the 458 s pip install anyway.

## What is deliberately not here

- **No model inference on HF.** A Space has no CUDA device; TTS (221 s) and
  whisper (40 s) stay on Kaggle. Moving them would mean not dubbing at all.
- **No assembly in the fetch path.** It is pure I/O. The assembly in this repo
  earns its place in audio inner loops, which is where the measurements above
  are from.
- **No long-lived cache on Kaggle.** `/kaggle/working` is wiped between
  sessions, so the repository is an origin, not a cache. If you want zero
  download time on warm runs, mount a Kaggle Dataset — that is the other
  branch, and it is the better one for repeated runs.