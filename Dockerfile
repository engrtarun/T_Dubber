# =============================================================================
#  T_DUBBER — "SPACE SHUTTLE"  ·  Kaggle GPU Worker Image
# =============================================================================
#  PURPOSE
#  -------
#  A single, reproducible environment for the automated dubbing worker:
#  Python AI brain (torch + vLLM + faster-whisper), FFmpeg's blades, and the
#  native arsenal -- tgup (Go), stitcher + subtitle_forge + havaldar_core
#  (Rust, musl-static) and normalizer (C++, static).
#
#  HOW TO BUILD
#  ------------
#    docker build -t t-dubber:1.0.0 .
#    docker run --rm --gpus all -it t-dubber:1.0.0 bash
#    .\build_kaggle_pack.ps1        <- the Kaggle pack: builds ONLY the
#                                      pack-exporter target (Go/Rust/C++
#                                      stages -- the 5 GB GPU brain is never
#                                      pulled) and exports ./pack/ to disk.
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
#    + Native arsenal (5 tools)     ~ 0.04 GB  (static; baked via 3f)
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
# Ubuntu tag for the C++ builder (stage 1c): `FROM ubuntu:${UBUNTU_TAG}` must
# resolve to a real image, so the tag is plain 22.04 -- the same release as
# the Kaggle kernel (glibc 2.35). The old value `ubuntu22.04` was never used
# by any FROM line; it is not a valid image reference, which is why the
# correction and this comment live together.
ARG UBUNTU_TAG=22.04
# Builder image for the Rust arsenal (stage 1b).
#
# NOT `rust:stable-bookworm`: the official Rust image publishes no `stable` tag.
# Its tags are the Rust RELEASE version -- 1.99.0, 1.99, 1.98.1, ... -- plus the
# floating `bookworm`, `slim-bookworm` and `latest`. `1` is the tag that actually
# means "newest stable 1.x", which is what this line always intended and exactly
# what RUST_TOOLCHAIN=stable below asks rustup for.
#
# This is not a cosmetic tag. Docker resolves metadata for every stage up front,
# so a bad tag fails the whole buildx invocation in about a second, before a
# single line of Rust is compiled:
#
#   ERROR: Failed to solve: rust:stable-bookworm: failed to resolve source
#   metadata for docker.io/library/rust:stable-bookworm: not found
ARG RUST_PACK_IMAGE=rust:1-bookworm

# Rust target triple for the static musl builds. Defined ONCE because the name
# appears in the `rustup target add`, in three `cargo build --target`, in three
# output paths and in six `COPY --from` paths across two stages -- twelve places
# that all have to agree. Spelling it out literally in twelve places is exactly
# how a wrong name survives this long.
#
# The triple is `x86_64-unknown-linux-musl`, NOT `x86_64-unknown-musl`. There is
# no bare `x86_64-unknown-musl` target in Rust at all. Checked against the
# official channel manifests for 1.60, 1.70, 1.75, 1.80, 1.85, 1.90, 1.95, 1.98
# and 1.99: the short form is absent from every one, and
# `x86_64-unknown-linux-musl` is present with `available = true` in every one.
# rustup reports that as a missing target rather than as a typo:
#
#   error: toolchain '1.99.0-x86_64-unknown-linux-gnu' does not support target
#   'x86_64-unknown-musl'
#
# So this stage has never built, in any release, for the whole life of the file.
ARG MUSL_TARGET=x86_64-unknown-linux-musl

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
# STAGE 1b — "THE ARMORY"  ·  build the three Rust tools, statically
# =============================================================================
# stitcher (timeline mixing), subtitle_forge (faster-whisper JSON -> styled
# .srt/.ass) and havaldar_core (the telemetry daemon) build in ONE stage, so
# the cargo registry cache is fetched once and shared by all three crates.
#
# WHY the musl target, NOT the default gnu target: Kaggle kernels run
# Ubuntu 22.04 (glibc 2.35) while this builder is Debian bookworm (glibc
# 2.36). A gnu-target binary linked here would demand GLIBC_2.36 symbols on
# Kaggle and die with a version error -- discovered 20 minutes into a run.
# The musl target links its libc in statically instead: no glibc
# negotiation on the far side at all, which is what "static" has to mean to
# survive the Docker -> Dataset -> Kaggle trip.
#
# musl-tools ships musl-gcc, which havaldar_core's bundled SQLite (rusqlite)
# needs to compile its C amalgamation against musl headers.
FROM ${RUST_PACK_IMAGE} AS rust-arsenal

# Re-declared inside the stage: an ARG defined before the first FROM is in scope
# only for FROM lines. Without this line ${MUSL_TARGET} would expand to nothing,
# `cargo build --target ""` would quietly fall back to the host target, and the
# build would sail on until it failed much later as a baffling static-link
# assertion instead of erroring here.
ARG MUSL_TARGET

RUN apt-get update && apt-get install -y --no-install-recommends \
        musl-tools \
        binutils \
    && rm -rf /var/lib/apt/lists/* \
    && rustup target add "${MUSL_TARGET}"

WORKDIR /arsenal

# One crate per RUN: touching stitcher's sources must not recompile the
# other two, and the crates.io download layer is shared by all three.
#
# Every crate RUN ends with two assertions, because a green `cargo build` is
# not the same thing as a shippable binary:
#   1. the binary STARTS -- its help/usage line, matched in the captured output;
#   2. the binary is STATIC -- verified by `readelf -l` finding no PT_INTERP.
# Wrong link = red build HERE, not a dead session on Kaggle.
#
# WHY readelf and NOT ldd, which is the important part:
#
# `ldd` looks like the obvious way to ask "is this static?" and it is not. ldd
# works by setting LD_TRACE_LOADED_OBJECTS=1 and RUNNING the file, expecting the
# dynamic loader to intercept that and print the dependency list instead of
# starting the program. A statically linked binary has no dynamic loader to
# intercept anything, so ldd's probe runs the program for real and the
# PROGRAM'S exit code comes back out as ldd's own.
#
# That is not theoretical. In the pack-manifest stage, `ldd` reported a
# statically linked musl `stitcher` as dynamic, and the build died with:
#     PACK FAIL: ldd exit 0, so stitcher is not static
# ...even though the very same binary had already passed a readelf INTERP check
# two stages earlier. `ldd` also executed havaldar_core, which boots a telemetry
# daemon. Anything inferred from ldd's exit code is an inference about a program
# that may have run, not a fact about the file.
#
# PT_INTERP is the fact: a dynamically linked ELF carries a PT_INTERP program
# header naming its interpreter (ld-linux.so.1 or the musl loader), and a static
# one does not. So readelf is required, not optional -- a missing readelf is a
# hard failure, because silently skipping the assertion would let a dynamically
# linked binary ship to Kaggle and die there with a version error instead.
#
# The probes below are deliberately verbose, because a silent assertion is worse
# than no assertion. The original one-liner was:
#
#     "$B" 2>&1 | grep -qi "usage"
#
# and when it failed the build died with `exit code: 1` and NOTHING else. The
# binary's own output went down the pipe into grep and was discarded, so a
# missing binary, a binary that could not start, and a binary that simply did not
# contain the word all looked identical from the outside. Each step below
# therefore (a) checks the binary exists and says so, (b) prints what the binary
# actually printed, and (c) tests for staticness from the ELF program headers.
COPY stitcher/ ./stitcher/
RUN cd stitcher && \
    cargo build --release --target "${MUSL_TARGET}" && \
    B="./target/${MUSL_TARGET}/release/stitcher" && \
    { \
        if [ ! -x "$B" ]; then \
            echo "ASSERT FAIL: $B was not produced" >&2; \
            echo "--- what cargo actually emitted under target/ ---" >&2; \
            find ./target -maxdepth 3 -name 'stitcher*' >&2 || true; \
            exit 1; \
        fi; \
        "$B" > /tmp/probe.txt 2>&1; \
        echo "=== probe: $B ==="; cat /tmp/probe.txt; echo "=== end probe ==="; \
        if ! grep -qi "usage" /tmp/probe.txt; then \
            echo "ASSERT FAIL: no 'usage' in the probe output above" >&2; exit 1; \
        fi; \
        if ! command -v readelf > /dev/null 2>&1; then \
            echo "ASSERT FAIL: readelf is missing, so staticness cannot be verified" >&2; exit 1; \
        fi; \
        if readelf -l "$B" 2>/dev/null | grep -q INTERP; then \
            echo "ASSERT FAIL: $B carries PT_INTERP, so it is dynamically linked" >&2; \
            readelf -l "$B" 2>/dev/null | sed -n '1,12p' >&2; \
            exit 1; \
        fi; \
        echo "static: no PT_INTERP -> OK"; \
    }

COPY subtitle_forge/ ./subtitle_forge/
RUN cd subtitle_forge && \
    cargo build --release --target "${MUSL_TARGET}" && \
    B="./target/${MUSL_TARGET}/release/subtitle_forge" && \
    { \
        if [ ! -x "$B" ]; then \
            echo "ASSERT FAIL: $B was not produced" >&2; \
            echo "--- what cargo actually emitted under target/ ---" >&2; \
            find ./target -maxdepth 3 -name 'subtitle_forge*' >&2 || true; \
            exit 1; \
        fi; \
        "$B" --version > /tmp/probe.txt 2>&1; \
        echo "=== probe: $B --version ==="; cat /tmp/probe.txt; echo "=== end probe ==="; \
        if ! grep -qi "subtitle_forge" /tmp/probe.txt; then \
            echo "ASSERT FAIL: no 'subtitle_forge' in the probe output above" >&2; exit 1; \
        fi; \
        if ! command -v readelf > /dev/null 2>&1; then \
            echo "ASSERT FAIL: readelf is missing, so staticness cannot be verified" >&2; exit 1; \
        fi; \
        if readelf -l "$B" 2>/dev/null | grep -q INTERP; then \
            echo "ASSERT FAIL: $B carries PT_INTERP, so it is dynamically linked" >&2; \
            readelf -l "$B" 2>/dev/null | sed -n '1,12p' >&2; \
            exit 1; \
        fi; \
        echo "static: no PT_INTERP -> OK"; \
    }

COPY havaldar_core/ ./havaldar_core/
RUN cd havaldar_core && \
    cargo build --release --target "${MUSL_TARGET}" && \
    B="./target/${MUSL_TARGET}/release/havaldar_core" && \
    { \
        if [ ! -x "$B" ]; then \
            echo "ASSERT FAIL: $B was not produced" >&2; \
            echo "--- what cargo actually emitted under target/ ---" >&2; \
            find ./target -maxdepth 3 -name 'havaldar_core*' >&2 || true; \
            exit 1; \
        fi; \
        # --help, never bare: bare would BOOT the telemetry daemon and hang. \
        "$B" --help > /tmp/probe.txt 2>&1; \
        echo "=== probe: $B --help ==="; cat /tmp/probe.txt; echo "=== end probe ==="; \
        if ! grep -qi "havaldar" /tmp/probe.txt; then \
            echo "ASSERT FAIL: no 'havaldar' in the probe output above" >&2; exit 1; \
        fi; \
        if ! command -v readelf > /dev/null 2>&1; then \
            echo "ASSERT FAIL: readelf is missing, so staticness cannot be verified" >&2; exit 1; \
        fi; \
        if readelf -l "$B" 2>/dev/null | grep -q INTERP; then \
            echo "ASSERT FAIL: $B carries PT_INTERP, so it is dynamically linked" >&2; \
            readelf -l "$B" 2>/dev/null | sed -n '1,12p' >&2; \
            exit 1; \
        fi; \
        echo "static: no PT_INTERP -> OK"; \
    }


# =============================================================================
# STAGE 1c — "THE FOUNDRY"  ·  build the C++ normalizer, fully static
# =============================================================================
# ubuntu:${UBUNTU_TAG} = 22.04, the Kaggle kernel's release (glibc 2.35) --
# same parity reasoning as the runtime base. gcc-12 where the archive has it
# (complete C++20), plain g++ as the fallback; CXX is resolved at configure
# time so CMake never sees an empty compiler variable.
#
# The tool links NOTHING but the C++ standard library -- no libsndfile, no
# FFTW, see cpp_accelerator/CMakeLists.txt -- so -static is a one-flag
# affair. CMakeLists warnings are not -Werror, so the clang->gcc warning
# dialect difference cannot turn a warning into a failed pack.
FROM ubuntu:${UBUNTU_TAG} AS cpp-forge

RUN apt-get update && \
    { apt-get install -y --no-install-recommends g++-12 cmake make binutils || \
      apt-get install -y --no-install-recommends g++ cmake make binutils; } && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY cpp_accelerator/ ./

# Same two assertions as the Rust stage: it starts, and it is provably static.
#
# readelf rather than ldd, for the reason spelled out in the rust-arsenal stage
# above: ldd executes a static binary and reports the PROGRAM's exit code as its
# own, so it is not a measurement of the file. binutils is installed above so
# readelf is a guaranteed dependency rather than something inherited by luck.
RUN CXX="$(command -v g++-12 || command -v g++ || echo g++)" \
        cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_EXE_LINKER_FLAGS="-static" && \
    cmake --build build --config Release -j"$(nproc)" && \
    ./build/normalizer --help > /tmp/probe.txt 2>&1; \
    echo "=== probe: normalizer --help ==="; cat /tmp/probe.txt; echo "=== end probe ==="; \
    if ! command -v readelf > /dev/null 2>&1; then \
        echo "ASSERT FAIL: readelf is missing, so staticness cannot be verified" >&2; exit 1; \
    fi; \
    if readelf -l build/normalizer 2>/dev/null | grep -q INTERP; then \
        echo "ASSERT FAIL: build/normalizer carries PT_INTERP, so it is dynamically linked" >&2; \
        readelf -l build/normalizer 2>/dev/null | sed -n '1,12p' >&2; \
        exit 1; \
    fi; \
    echo "static: no PT_INTERP -> OK"


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

# Needed by the three COPY lines below, which pull the musl binaries out of
# rust-arsenal by target directory name. Without this the paths would expand to
# /arsenal/stitcher/target//release/stitcher and fail with a bare "not found".
ARG MUSL_TARGET

LABEL org.opencontainers.image.title="T_Dubber Worker" \
      org.opencontainers.image.description="AI video dubbing pipeline worker: vLLM + faster-whisper + FFmpeg + native arsenal (tgup, stitcher, normalizer, subtitle_forge, havaldar_core)" \
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
    TGUP_BIN=/usr/local/bin/tgup \
    STITCHER_BIN=/usr/local/bin/stitcher \
    NORMALIZER_BIN=/usr/local/bin/normalizer

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
# The three Rust binaries are BAKED into this image by STAGE 3f below; they
# need no toolchain at runtime. This stage is the on-demand compiler:
# WITH_RUST=1 (the default) installs a minimal rustup so `cargo build` still
# works inside a running container when you are hacking on stitcher,
# subtitle_forge or havaldar_core without a full rebuild loop.
# (History, for honesty: this comment used to claim there was no Rust in
# T_Dubber at all. There was -- stitcher/ had been written; it simply had
# never been compiled. Stage 1b fixed that half, 3f ships the result.)
#
# --profile minimal  = no rust-docs, no clippy, no rustfmt. Full rustup is
#                      ~1.5 GB; minimal is ~350 MB. Add the rest with
#                      `rustup component add clippy rustfmt` when you lint.
# --no-modify-path  = we manage PATH explicitly in ENV above, so the image has
#                     one source of truth for it.
# chmod -R a+w       : the venv and any pip-installed helper need to write into
#                     these dirs when a non-root user runs the worker.
#
# Baking a Rust binary is no longer hypothetical: stage 1b compiles all
# three crates to musl-static, and 3f copies them in. Keep this toolchain
# for interactive rebuilds only.
RUN if [ "$WITH_RUST" = "1" ]; then \
        set -eux; \
        RUSTUP_HOME=/usr/local/rustup; \
        CARGO_HOME=/usr/local/cargo; \
        export RUSTUP_HOME CARGO_HOME; \
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
            | sh -s -- -y --profile minimal --default-toolchain "${RUST_TOOLCHAIN}" --no-modify-path; \
        chmod -R a+w "$RUSTUP_HOME" "$CARGO_HOME"; \
        touch /usr/local/cargo/.rust-present; \
        rustc --version; cargo --version; \
    fi

# To rebuild a crate on demand inside a running container:
#   bind-mount or `docker cp` the crate, then e.g.
#   cargo build --release --manifest-path /build/stitcher/Cargo.toml
# The baked binaries in /usr/local/bin stay untouched until 3f re-copies
# them, so an in-container experiment never shadows the shipped arsenal.
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
# STAGE 3f — THE ARMORY (four more static binaries, zero toolchains)
# -----------------------------------------------------------------------------
# The pack's five, minus tgup (already in 3c): all baked so local/CI parity
# matches what build_kaggle_pack.ps1 ships to Kaggle. Discovery is PATH-based
# in mazinger.assemble; STITCHER_BIN / NORMALIZER_BIN (ENV above) pin the two
# that have Python bridges, shutil.which() finds subtitle_forge and
# havaldar_core when anything asks for them by name.
#
# Nothing here -- and nothing in the HEALTHCHECK below -- ever EXECUTES
# havaldar_core: starting it would boot the telemetry HTTP daemon. Presence
# checks only.
COPY --from=rust-arsenal /arsenal/stitcher/target/${MUSL_TARGET}/release/stitcher            /usr/local/bin/stitcher
COPY --from=rust-arsenal /arsenal/subtitle_forge/target/${MUSL_TARGET}/release/subtitle_forge /usr/local/bin/subtitle_forge
COPY --from=rust-arsenal /arsenal/havaldar_core/target/${MUSL_TARGET}/release/havaldar_core   /usr/local/bin/havaldar_core
COPY --from=cpp-forge    /src/build/normalizer                            /usr/local/bin/normalizer
RUN chmod 755 /usr/local/bin/stitcher /usr/local/bin/normalizer \
              /usr/local/bin/subtitle_forge /usr/local/bin/havaldar_core

# -----------------------------------------------------------------------------
# STAGE 3g — CONTRACTS
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
# The Rust check is gated on the marker file rather than `command -v cargo`, so a
# deliberately slimmed `--build-arg WITH_RUST=0` image is not reported unhealthy
# for the one tool you asked it not to install.
HEALTHCHECK --interval=30s --timeout=8s --start-period=90s --retries=3 \
    CMD ffmpeg -version > /dev/null 2>&1 || exit 1; \
        python -c "import torch, vllm, faster_whisper" > /dev/null 2>&1 || exit 1; \
        command -v tgup > /dev/null 2>&1 || exit 1; \
        command -v stitcher > /dev/null 2>&1 || exit 1; \
        command -v normalizer > /dev/null 2>&1 || exit 1; \
        command -v subtitle_forge > /dev/null 2>&1 || exit 1; \
        command -v havaldar_core > /dev/null 2>&1 || exit 1; \
        { [ ! -f /usr/local/cargo/.rust-present ] || command -v cargo > /dev/null 2>&1; } || exit 1; \
        exit 0

# tini as PID 1: signal forwarding to vLLM's engine subprocesses + zombie reaping.
ENTRYPOINT ["/usr/bin/tini", "--"]
# No CMD without a shell: tini needs the program to run spelled out, and this
# is the image's whole interactive contract (`docker run -it t-dubber bash`).
# This line predates the pack stages and was lost when the competing
# pack-build block was cut out of the file -- restored, not newly invented.
CMD ["bash"]

# =============================================================================
# STAGE 4 — "THE MANIFEST"  ·  gather the five, prove each one runs
# =============================================================================
# debian:bookworm-slim only for its shell, coreutils and ldd -- the binaries
# are already static, so nothing here links against the base. This stage
# produces the exact byte set the Kaggle dataset ships, plus two documents:
#   MANIFEST.txt  -- what each tool says about itself (help/version line)
#                    and how big it is, human-readable at a glance;
#   SHA256SUMS    -- the same five, hashed, for the exporter script (and
#                    anyone downstream) to verify the bytes it received.
#
# Help commands are per-tool on purpose: stitcher has no --help flag (its
# usage line IS the help), while running havaldar_core bare would BOOT its
# telemetry daemon -- so every invocation below is the non-starting one.
FROM debian:bookworm-slim AS pack-manifest

# Same reason as in the runtime stage: the COPY paths below name the musl target
# directory.
ARG MUSL_TARGET

# binutils, for readelf. NOT an optimisation and NOT optional.
#
# debian:bookworm-slim ships no binutils, so an earlier version of this stage had
# to fall back to `ldd` to answer "is this binary static?" -- and ldd gave the
# wrong answer, reporting a statically linked musl stitcher as dynamic:
#
#     PACK FAIL: ldd exit 0, so stitcher is not static
#
# ldd works by setting LD_TRACE_LOADED_OBJECTS=1 and running the file, expecting
# the dynamic loader to intercept and print dependencies instead of starting the
# program. A static binary has no dynamic loader, so the probe runs the program
# for real and the PROGRAM's exit code comes back as ldd's own. Whether that
# number means "dynamic" or "static" therefore depends on what the tool does
# with no arguments -- which is why tgup happened to look static and stitcher
# happened to look dynamic, despite both being static.
#
# `readelf -l | grep INTERP` is a measurement of the file instead: a dynamically
# linked ELF carries a PT_INTERP program header, a static one does not. Installing
# binutils here costs one small layer and buys a check that cannot be fooled by
# exit codes.
RUN apt-get update && \
    apt-get install -y --no-install-recommends binutils && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /pack/bin

COPY --from=go-transporter /out/tgup                                         ./tgup
COPY --from=rust-arsenal   /arsenal/stitcher/target/${MUSL_TARGET}/release/stitcher            ./stitcher
COPY --from=rust-arsenal   /arsenal/subtitle_forge/target/${MUSL_TARGET}/release/subtitle_forge ./subtitle_forge
COPY --from=rust-arsenal   /arsenal/havaldar_core/target/${MUSL_TARGET}/release/havaldar_core   ./havaldar_core
COPY --from=cpp-forge      /src/build/normalizer                             ./normalizer

# NO `set -e` here, and that is the whole point of this rewrite.
#
# This one RUN used to do a dozen unrelated jobs in a single shell: chmod, five
# binary probes, manifest generation, checksums, and five static-link checks.
# Under `set -e`, ANY non-zero anywhere aborted the build with a bare
# "exit code: 1" that named none of the dozen steps -- including non-zero exits
# that are entirely benign, like a probe command legitimately exiting 1, or
# `head -n 1` closing a pipe and its writer taking SIGPIPE. Four CI round trips
# went into guessing which of the twelve it was.
#
# So `set -e` is gone and each step guards itself and SAYS WHICH STEP FAILED.
#
# The distinction that matters: the informational probes can no longer fail the
# build at all (they only fill in a manifest column), while the two conditions
# that actually determine shippability -- every binary present, every binary
# static -- still fail hard, loudly and by name.
RUN set -u; \
    for b in tgup stitcher normalizer subtitle_forge havaldar_core; do \
        if [ ! -f "$b" ]; then \
            echo "PACK FAIL: '$b' is not in $(pwd)" >&2; \
            echo "--- directory contents ---" >&2; ls -la >&2; \
            exit 1; \
        fi; \
        chmod 755 "$b" || { echo "PACK FAIL: chmod $b" >&2; exit 1; }; \
    done; \
    echo "--- all five binaries present ---"; \
    probe() { \
        _n="$1"; shift; \
        _o="$("$@" 2>&1 | head -n 1 || true)"; \
        echo "probe $_n -> ${_o:-<no output>}"; \
        printf '%s' "$_o" > "/tmp/probe_$_n"; \
    }; \
    probe tgup ./tgup help; \
    probe stitcher ./stitcher; \
    probe normalizer ./normalizer --help; \
    probe subtitle_forge ./subtitle_forge --version; \
    probe havaldar_core ./havaldar_core --help; \
    { \
        echo "T_Dubber native arsenal - five static linux/amd64 binaries"; \
        echo "built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"; \
        echo ""; \
        printf '%-16s %12s  %s\n' "tool" "bytes" "identifies as"; \
        printf '%-16s %12s  %s\n' "tgup"           "$(wc -c < tgup)"           "$(cat /tmp/probe_tgup)"; \
        printf '%-16s %12s  %s\n' "stitcher"       "$(wc -c < stitcher)"       "$(cat /tmp/probe_stitcher)"; \
        printf '%-16s %12s  %s\n' "normalizer"     "$(wc -c < normalizer)"     "$(cat /tmp/probe_normalizer)"; \
        printf '%-16s %12s  %s\n' "subtitle_forge" "$(wc -c < subtitle_forge)" "$(cat /tmp/probe_subtitle_forge)"; \
        printf '%-16s %12s  %s\n' "havaldar_core"  "$(wc -c < havaldar_core)"  "$(cat /tmp/probe_havaldar_core)"; \
    } > /pack/MANIFEST.txt || { echo "PACK FAIL: writing MANIFEST.txt" >&2; exit 1; }; \
    sha256sum tgup stitcher normalizer subtitle_forge havaldar_core > /pack/SHA256SUMS \
        || { echo "PACK FAIL: sha256sum" >&2; exit 1; }; \
    command -v readelf > /dev/null 2>&1 \
        || { echo "PACK FAIL: readelf is missing, so staticness cannot be verified" >&2; exit 1; }; \
    for b in tgup stitcher normalizer subtitle_forge havaldar_core; do \
        if readelf -l "$b" 2>/dev/null | grep -q INTERP; then \
            echo "PACK FAIL: $b has PT_INTERP, so it is dynamically linked" >&2; \
            readelf -l "$b" 2>/dev/null | sed -n '1,12p' >&2; \
            exit 1; \
        fi; \
        echo "static: $b has no PT_INTERP"; \
    done; \
    echo "--- all five binaries are static ---"; \
    cat /pack/MANIFEST.txt


# =============================================================================
# STAGE 5 — "PACK-EXPORTER"  ·  scratch-clean: the image IS the /pack folder
# =============================================================================
# FROM scratch on purpose: the exported filesystem contains exactly
#   /pack/bin/{tgup,stitcher,normalizer,subtitle_forge,havaldar_core}
#   /pack/MANIFEST.txt
#   /pack/SHA256SUMS
# and nothing else -- no shell, no libc, no layers to strip afterwards.
#
# build_kaggle_pack.ps1 runs:
#   docker build --target pack-exporter --output type=local,dest=<folder> .
# and BuildKit writes that filesystem straight onto the host. Only the
# stages this target DEPENDS ON get built: the ~5 GB vLLM brain is never
# pulled, so a pack rebuild costs minutes. Kaggle itself cannot run any of
# this image -- the pack exists to be exported as a Dataset (see the
# KAGGLE EQUIVALENT block below for the notebook side).
FROM scratch AS pack-exporter

LABEL org.opencontainers.image.title="T_Dubber Kaggle Pack" \
      org.opencontainers.image.description="Five static linux/amd64 binaries: tgup (Go), stitcher + subtitle_forge + havaldar_core (Rust/musl), normalizer (C++)" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/engrtarun/T_Dubber"

COPY --from=pack-manifest /pack /pack


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
#   # Native arsenal -- ONE-TIME setup, no toolchain ever again:
#   # upload the pack/ folder that build_kaggle_pack.ps1 exported as a
#   # Kaggle dataset, then in Python (Cell 0, before anything else runs):
#   #   import os
#   #   os.environ["PATH"] = "/kaggle/input/<dataset>/pack/bin:" + os.environ["PATH"]
#   # tgup/stitcher/normalizer/subtitle_forge/havaldar_core are then on PATH
#   # for the whole session. The pack binaries are static: no glibc, no
#   # rustup, no go, no gcc -- and no compile at startup.
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