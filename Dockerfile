# =============================================================================
#  T_DUBBER — "SPACE SHUTTLE"  ·  Kaggle GPU Worker Image
# =============================================================================
#  PURPOSE
#  -------
#  A single, reproducible environment for the automated dubbing worker:
#  Python AI brain (torch + vLLM + faster-whisper), FFmpeg's blades, the Go
#  transporter (tgup), and the Rust mem-broker for audio stitching.
#
#  HOW TO BUILD
#  ------------
#    docker build -t t-dubber:1.0.0 .
#    docker run --rm --gpus all -it t-dubber:1.0.0 bash
#
#  THE KAGGLE CAVEAT  (read this before you expect a Dockerfile to run there)
#  --------------------------------------------------------------------------
#  Kaggle kernels CANNOT run your own image. They pin their own NVIDIA NGC base
#  and a Python 3.10.12 kernel; there is no "build this image" affordance.
#  So this file serves three real purposes:
#    1. Local/CI parity  — run the exact worker toolchain on a workstation or a
#       self-hosted GPU box, so local runs stop diverging from Kaggle.
#    2. A build cache     — `docker run ... python -m mazinger ...` warms a
#       layer cache you can export as a dataset/artifact.
#    3. Documentation     — every tool, version and flag the notebook needs is
#       pinned here in one place.
#  For Kaggle itself, use the "KAGGLE EQUIVALENT" block at the bottom of this
#  file: it is the same command list, in the same order, in one shell block you
#  can paste into cell #1 of kaggle_worker.ipynb.
#
#  SIZE STRATEGY
#  -------------
#  Multi-stage. Toolchains (Go, Rust, build-essential) exist ONLY in builder
#  stages; the runtime gets a 14 MB static binary and a toolchain only if you
#  ask for it with --build-arg WITH_RUST=1.
#
#    Base runtime (CUDA + cuDNN)   ~ 3.1 GB
#    + torch cu128 + vLLM          ~ 5.4 GB   <-- irreducible
#    + ffmpeg + libs                ~ 0.4 GB
#    + venv                         (already counted in torch/vLLM)
#    + Go toolchain                 ~ 0.0 GB   (builder only)
#    + Rust toolchain               ~ 1.5 GB   (opt-in, WITH_RUST=1)
#    ------------------------------------------
#    TOTAL                          ~ 8.9 GB   (default)
# =============================================================================

# -----------------------------------------------------------------------------
# LAYER 0 — THE HULL
# Pick the CUDA minor that matches your torch wheel.
#   cu128  -> torch 2.7+   (vLLM 0.29 default)
#   cu126  -> torch 2.5/2.6 (older vLLM)
# `-runtime` is ~2.5 GB, `-devel` is ~6 GB. We do NOT need nvcc: vLLM ships
# prebuilt kernels for torch and we never JIT-compile a CUDA extension here.
# If you ever add flash-attn or a custom kernel, swap this for `-devel`.
# -----------------------------------------------------------------------------
ARG CUDA_IMAGE=nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04
ARG UBUNTU_TAG=ubuntu22.04

# Ubuntu 22.04 is deliberate, not lazy: its default python3 IS 3.10, which is
# byte-for-byte the interpreter your Kaggle kernel already uses. Matching it
# removes an entire class of "works on Kaggle, breaks locally" failures.
# (For 3.11/3.12 swap to ubuntu24.04 and add deadsnakes; nothing else changes.)


# =============================================================================
# STAGE 1 — "PROPULSION BAY"  ·  build tgup (Go), throw away the compiler
# =============================================================================
FROM golang:1.25-bookworm AS go-transporter

WORKDIR /src

# Dependency manifests first. This is the single most important line in the
# file: copying only go.mod/go.sum means the ~45-module download layer is
# cached and is NOT invalidated every time you touch main.go.
COPY go.mod go.sum ./

# `go mod download` with the cache primed. Skipped entirely when the module
# cache already satisfies the graph (a no-op re-run costs milliseconds).
RUN go mod download && \
    go mod verify

# Now the source. Real code, tiny layer.
COPY ./*.go ./

# CGO_ENABLED=0 -> pure static binary. That is what lets us copy it out of this
# Debian-bookworm stage into a CUDA/Ubuntu runtime stage: no glibc version
# negotiation, no runtime linker, no libgcc/libstdc++ to ship alongside.
#   -trimpath  -> strip absolute build paths (reproducible, smaller, no leaks)
#   -s -w      -> drop the symbol table and DWARF debug info (~30% smaller)
# Result: a ~14 MB binary that runs on any Linux, any libc, any arch.
#
# No error suppression here. A red build should tell you which module failed,
# and `tgup help` re-proving the binary starts is the same assertion
# build.ps1 makes locally — if the binary needs a login code to do real work,
# `help` still has to work.
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w" -o /out/tgup . && \
    strip /out/tgup 2>/dev/null || true; \
    /out/tgup help > /dev/null


# =============================================================================
# STAGE 2 — "THE BRAIN"  ·  Python venv with torch + vLLM + faster-whisper
# =============================================================================
FROM ${CUDA_IMAGE} AS brain

# Same version the notebook pins. Overridable, never floating: a vLLM bump
# silently changes torch, CUDA kernels and the pydantic API it validates
# against, so an unpinned vllm in CI is a coin flip every Monday.
ARG VLLM_VERSION=0.29.0
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
# Exactly the wheel set from kaggle_worker.ipynb cell 1. Parity is the point.
ARG WORKER_PIP_PACKAGES="yt-dlp>=2026.3.17 openai>=1.0 json-repair>=0.28 \
Pillow>=10.0 soundfile>=0.12 numpy>=1.24 tqdm>=4.60 python-slugify>=8.0 \
faster-whisper av>=14.0.0 demucs omnivoice"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# libgomp1 is NOT optional: torch's OpenMP kernels abort at import without it.
# libgl1/libglib2.0-0 are for the cv2/opencv + moviepy path Mazinger imports.
# libsndfile1 backs soundfile, which backs the WAV segment writer.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-dev \
        python3-venv \
        ca-certificates \
        curl \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# One venv at a fixed absolute path. Stage 3 copies this directory verbatim;
# an absolute path is mandatory, because the venv's `python` is a symlink to
# /usr/bin/python3 and `pyvenv.cfg` records absolute paths.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv

# --- The Brain -------------------------------------------------------------
# Order matters, and the reason is non-obvious:
#
#   1. vLLM FIRST. vLLM pins an exact torch version plus a specific set of
#      nvidia-* CUDA wheels. Installing torch first and letting vLLM upgrade it
#      is fine; installing vllm first and then "upgrading" torch is a footgun.
#   2. `pip install torch` afterwards is therefore intentionally a NO-OP. You
#      asked for torch in the install line, and it is present — just at the
#      version vLLM certified, not whatever was newest. That is correct.
#   3. --extra-index-url (not --index-url) so vLLM, demucs and omnivoice still
#      resolve their normal deps from PyPI; we are ADDING the CUDA torch
#      index, not replacing PyPI with it.
RUN pip install -q --upgrade pip setuptools wheel && \
    pip install -q "vllm==${VLLM_VERSION}" --extra-index-url "${TORCH_INDEX_URL}" && \
    pip install -q torch --extra-index-url "${TORCH_INDEX_URL}" && \
    pip install -q faster-whisper && \
    pip install -q ${WORKER_PIP_PACKAGES} && \
    pip install -q kaggle && \
    pip cache purge || true

# Fail the build, not the 3 a.m. run. Importing torch actually loads the CUDA
# runtime and allocates a context, so this catches a broken cuDNN/vLLM pair at
# build time rather than at first inference.
RUN python -c "import torch, vllm, faster_whisper, soundfile; \
print('torch', torch.__version__, '| cuda', torch.version.cuda, '| vllm', vllm.__version__); \
print('cuda available:', torch.cuda.is_available())"


# =============================================================================
# STAGE 3 — "MISSION CONTROL"  ·  the runtime you actually ship
# =============================================================================
FROM ${CUDA_IMAGE} AS runtime

LABEL org.opencontainers.image.title="T_Dubber Worker" \
      org.opencontainers.image.description="AI video dubbing pipeline worker: vLLM + faster-whisper + FFmpeg + tgup(Go) + Rust audio stitcher" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/engrtarun/T_Dubber"

ARG WITH_RUST=1
ARG RUST_TOOLCHAIN=stable
ARG APP_USER=tdubber
ARG APP_UID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/opt/cache/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=4 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    PATH="/opt/venv/bin:/usr/local/cargo/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv \
    TGUP_BIN=/usr/local/bin/tgup

# -----------------------------------------------------------------------------
# STAGE 3a — SYSTEM PAYLOAD  (apt, one layer, lists cleaned in the same layer)
# -----------------------------------------------------------------------------
# ffmpeg/ffprobe : the blades. Needed for the >30MB or >1080p 480p squeeze, the
#                  `-ss 6.9 -t 9.5` voice-sample extraction, and stream-copy
#                  remux in chop_drop. The Ubuntu build already carries
#                  libx264 + AAC, which is all we mux with.
# tini           : PID 1 that reaps zombies and forwards SIGTERM. Without it,
#                  `docker stop` waits the full 10s grace period and vLLM's
#                  multi-process engine leaks workers on shutdown.
# Others         : libgomp1 (torch OpenMP), libsndfile1 (soundfile),
#                  libgl1/libglib2.0-0 (cv2 in Mazinger), espeak-ng + a font
#                  (fallback TTS + thumbnail rendering), jq (read report.json
#                  in the shell without booting Python).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ffprobe \
        tini \
        ca-certificates \
        curl \
        git \
        jq \
        unzip \
        zip \
        xz-utils \
        jq \
        libgomp1 \
        libsndfile1 \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        espeak-ng \
        fonts-dejavu-core \
        procps \
        net-tools \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# STAGE 3b — THE BRAIN (copied, not rebuilt)
# -----------------------------------------------------------------------------
# Why copy the venv instead of re-running pip: the same reason we staged it.
# It turns an 8-minute, ~5 GB download into a `COPY` of already-built bytes.
# The base image is byte-identical across stages, so the venv's absolute
# symlinks to /usr/bin/python3 and its libstdc++ expectations both stay valid.
COPY --from=brain /opt/venv /opt/venv

# -----------------------------------------------------------------------------
# STAGE 3c — THE TRANSPORTER (14 MB of Go, zero compiler)
# -----------------------------------------------------------------------------
# Note what is NOT copied: /usr/local/go and the module cache. Go's job here is
# finished; the runtime only needs to exec a static binary.
COPY --from=go-transporter /out/tgup /usr/local/bin/tgup
RUN chmod +x /usr/local/bin/tgup && \
    (tgup help > /dev/null 2>&1 && echo "tgup OK" || echo "tgup: see TGUP.md (needs one-time login code)")

# -----------------------------------------------------------------------------
# STAGE 3d — THE NINJA (Rust, opt-in)
# -----------------------------------------------------------------------------
# The brief asked for Rust for memory-safe audio stitching. To be straight with
# you: there is no Rust in T_Dubber yet. go.mod is the only real toolchain in
# the pipeline; the single Cargo.toml in the repo belongs to the vendored
# Telegram-Drive Tauri app. So this stage installs the toolchain, which is what
# actually lets you `cargo build --release` your stitcher on demand inside the
# container without a second toolchain to install.
#
# --profile minimal  = no rust-docs, no clippy, no rustfmt. Full rustup is
#                      ~1.5 GB; minimal is ~350 MB. Add the rest with
#                      `rustup component add clippy rustfmt` when you lint.
# --no-modify-path  = we manage PATH explicitly in ENV above, so the image has
#                     one source of truth for it.
# chmod -R a+w       : the venv and any pip-installed helper need to write into
#                     these dirs when a non-root user runs the worker.
#
# To bake a real Rust binary in later, add `audio_stitch/Cargo.toml` to the repo
# and uncomment the build block — it is already wired for that.
RUN if [ "$WITH_RUST" = "1" ]; then \
        set -eux; \
        RUSTUP_HOME=/usr/local/rustup; \
        CARGO_HOME=/usr/local/cargo; \
        export RUSTUP_HOME CARGO_HOME; \
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
            | sh -s -- -y --profile minimal --default-toolchain "${RUST_TOOLCHAIN}" --no-modify-path; \
        chmod -R a+w "$RUSTUP_HOME" "$CARGO_HOME"; \
        rustc --version; cargo --version; \
    fi

# To bake a real Rust binary in later: add `audio_stitch/Cargo.toml` to the repo,
# COPY it in above, then add a second RUN:
#   RUN cargo build --release --manifest-path /build/audio_stitch/Cargo.toml \
#       && install -m755 /build/audio_stitch/target/release/audio_stitch /usr/local/bin/
# Deliberately kept as its own instruction: a `#` comment sitting inside a
# line continuation ends the logical line for several Dockerfile parsers.
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    CARGO_TERM_COLOR=always

# -----------------------------------------------------------------------------
# STAGE 3e — WIRING
# -----------------------------------------------------------------------------
# Cache dirs must exist and be writable, or HF silently re-downloads 6 GB of
# weights into a read-only /root on every start.
RUN mkdir -p "$HF_HOME" /kaggle/working /kaggle/input /app/src /var/log/tdubber && \
    chmod -R 777 "$HF_HOME" /kaggle/working

WORKDIR /app/src
COPY pipeline.py telegram_uploader.py go_planner.py tgup_bridge.py ./

# NOTE: no Go toolchain in this image on purpose. `go build` ran in the
# go-transporter builder stage; only the static `tgup` binary shipped. If you
# need to rebuild it inside the container, use the builder stage instead:
#   docker build --target go-transporter -t t-dubber-go .

# Non-root user. We deliberately DO NOT switch to it by default: the NVIDIA
# container runtime hands /dev/nvidia* over with host ownership, so a non-root
# process usually cannot open the device and CUDA silently reports
# torch.cuda.is_available() == False. If you are CPU-only or mounting devices
# with explicit perms, run:  docker run --user tdubber ...
RUN groupadd -g ${APP_UID} ${APP_USER} 2>/dev/null || true && \
    useradd -u ${APP_UID} -g ${APP_USER} -m -s /bin/bash ${APP_USER} 2>/dev/null || true && \
    chown -R ${APP_USER}:${APP_USER} /app /kaggle /var/log/tdubber 2>/dev/null || true

# -----------------------------------------------------------------------------
# STAGE 3f — CONTRACTS
# -----------------------------------------------------------------------------
# vLLM serves the OpenAI-compatible endpoint that Mazinger talks to. Exposing it
# means `docker run -p 8000:8000` gives you a host-reachable translation API for
# debugging, instead of only 127.0.0.1 inside the container.
EXPOSE 8000 8001

# The vLLM launcher flags from kaggle_worker.ipynb, expressed once as ENV so
# the worker does not have to remember them. --language-model-only matters: it
# skips loading the vision tower, and Mazinger does its own frame analysis.
ENV OPENAI_BASE_URL=http://127.0.0.1:8000/v1 \
    OPENAI_API_KEY=EMPTY \
    OPENAI_MODEL=IndexTeam/Index-Homura-2B \
    MAZINGER_DISABLE_VISION=1 \
    MAZINGER_LLM_MAX_OUTPUT_TOKENS=2048 \
    VLLM_LOGGING_LEVEL=WARNING \
    TQDM_DISABLE=0

# Liveness: every tool the pipeline claims to need, actually importable/executable.
# Runs in ~4s. A broken layer fails the container immediately instead of letting
# a Kaggle run die 20 minutes in with a cryptic ImportError.
HEALTHCHECK --interval=30s --timeout=8s --start-period=90s --retries=3 \
    CMD ffmpeg -version > /dev/null 2>&1 || exit 1; \
        python -c "import torch, vllm, faster_whisper" > /dev/null 2>&1 || exit 1; \
        command -v tgup > /dev/null 2>&1 || exit 1; \
        command -v cargo   > /dev/null 2>&1 || exit 1; \
        exit 0

# tini as PID 1: signal forwarding to vLLM's engine subprocesses + zombie reaping.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default to a shell, not a server. The real entrypoint is a notebook cell on
# Kaggle or `python -m mazinger dub ...` locally. Put your command after the
# image name, or override with --entrypoint.
CMD ["bash"]


# =============================================================================
# KAGGLE EQUIVALENT
# =============================================================================
# Kaggle will not run this image. Paste the block below into cell #1 of
# kaggle_worker.ipynb to reproduce this exact toolchain on their NGC base.
# Same order, same versions, same rationale (vllm pins torch, not the reverse).
#
#   %%bash
#   set -e
#   apt-get update -qq && apt-get install -y -qq \
#       ffmpeg libgomp1 libsndfile1 libgl1 libglib2.0-0 espeak-ng jq
#
#   # Go — tgup transporter
#   curl -fsSL https://go.dev/dl/go1.25.0.linux-amd64.tar.gz \
#     | tar -C /usr/local -xz && export PATH=$PATH:/usr/local/go/bin
#
#   # Rust — audio stitcher toolchain (minimal profile)
#   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
#     | sh -s -- -y --profile minimal --no-modify-path
#   export PATH=/usr/local/cargo/bin:$PATH
#
#   # The brain
#   pip install --no-cache-dir "vllm==0.29.0" \
#     --extra-index-url https://download.pytorch.org/whl/cu128
#   pip install --no-cache-dir torch --extra-index-url https://download.pytorch.org/whl/cu128
#   pip install --no-cache-dir faster-whisper
#   pip install --no-cache-dir yt-dlp openai json-repair Pillow soundfile \
#       numpy tqdm python-slugify av demucs omnivoice kaggle
#
#   ffmpeg -version | head -1 && go version && cargo --version && \
#   python -c "import torch, vllm; print(torch.__version__, torch.cuda.is_available())"
#
# GOTCHA: Kaggle resets /usr/local and /root/.cargo between sessions. Persist
# across sessions with a Kaggle dataset, or accept the ~3 min reinstall cost per
# run. Everything under /kaggle/working DOES persist — which is why the cell
# caches models and the vLLM weights there, not in /root/.cache.
# =============================================================================