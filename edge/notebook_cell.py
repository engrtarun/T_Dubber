# =============================================================================
# T_Dubber edge cache -- fetch warm artefacts before anything is installed
# =============================================================================
# WHAT THIS CELL IS FOR
# ---------------------
# Run 14 took 1220 s. 903 s of that -- 74% -- was downloading bytes that had not
# changed since the previous run:
#
#     pip install of pylibs         378 s
#     Homura-2B snapshot download   525 s
#
# The GPU work (TTS 221 s, transcribe 40 s) has to stay on Kaggle, because there
# is no GPU anywhere else in the free tier. The downloads do not. This cell pulls
# prebuilt artefacts from the origin -- a public Hugging Face repository, served
# from /resolve/main/ -- content-addressed by sha256, and the next cell's
# find_pylibs() finds them already in place.
#
# WHY THE ORIGIN IS A REPOSITORY AND NOT A SPACE
# ----------------------------------------------
# A Docker Space on Hugging Face costs $9/month: since 2026, "Gradio and Docker
# Spaces run on compute and require a paid plan to create". A public dataset
# repository is free, has no sleep state, no cold start and no volume that is
# wiped on redeploy -- and this cell only ever GETs bytes, so it needs no
# process to talk to. The repository IS the server.
#
# WHAT IT IS NOT
# --------------
# It is not required. If EDGE_URL is unset, or the origin is unreachable, or the
# manifest does not cover what this run needs, this cell prints one line and
# exits. Everything downstream is unchanged. A cache miss must never be worse
# than no cache.
#
# WHY THIS RUNS BEFORE THE INSTALL
# -------------------------------
# Ordering is the entire value. Run after the pip install it would have nothing
# left to save; run before, it removes the install. The cell sits directly after
# the pack cell (which provides the binary) and before the dependency cell.
#
# WHY THIS IS NOT A WARM CACHE, AND WHY THAT MATTERS
# --------------------------------------------------
# An earlier version of this cell fetched into /kaggle/working and advertised a
# 33 ms warm path. That number is unreachable on Kaggle and the cell no longer
# claims it:
#
#   * a NEW Kaggle session starts with an EMPTY /kaggle/working, so nothing this
#     cell writes survives to the next run -- the digest comparison is right but
#     always answers "missing";
#   * /kaggle/working is also cleared whenever the session ends, so the cache
#     cannot outlive the run that built it even in principle;
#   * the durable, free storage Kaggle offers is an attached DATASET under
#     /kaggle/input, and the next cell already looks there first
#     (find_pylibs([INPUT, WORK])).
#
# So this cell is a COLD-RUN accelerator, not a cache. What it actually saves is
# the pip resolver and the per-package install: one tar extract instead of ~110
# wheel installations. It cannot save the download, because the bytes are the
# same either way.
#
# The genuinely free win on repeat runs is attaching the built tree as a Kaggle
# Dataset, which makes it local disk and costs zero seconds to "download". That
# is already implemented downstream; this cell is what makes the FIRST run cheap
# enough to be worth keeping.
#
# WHY THE DIGEST MATTERS
# ----------------------
# pylibs is unpacked into sys.path and then imports and executes code. An archive
# that arrived truncated or substituted is not a failed run, it is arbitrary code
# execution. edge-fetch therefore verifies sha256 against the Space's manifest
# and installs nothing on a mismatch, and writes to a temporary name so a partial
# file is never visible under the real path.

import os
import subprocess
import sys
import time
from pathlib import Path

T_EDGE = time.monotonic()

WORK = Path("/kaggle/working")

# The origin. A Kaggle notebook cannot be handed environment variables by the
# kernel metadata -- there is no field for it -- so the default lives here
# rather than being injected, and EDGE_URL still overrides it for a local run,
# a mirror, or a LAN server during development.
#
# The trailing /resolve/main/ is what makes a plain public Hugging Face
# repository behave like a file server: the client appends /manifest.json and
# /artifact/<name>, and both resolve to real file paths in the repository.
# Point EDGE_URL at anything else with that layout and this cell works
# unchanged -- a Space, an nginx, a laptop on the same network.
DEFAULT_EDGE_URL = "https://huggingface.co/datasets/pocotarun/tdubber-edge/resolve/main"
EDGE_URL = (os.environ.get("EDGE_URL", "") or DEFAULT_EDGE_URL).strip()

# Roles to pull. "pack" is not among them: the native pack already arrives via
# the Input dataset, which is faster than any HTTP transfer for 7 small files,
# and unpacking a tar of binaries would cost more than it saves.
#
# "weights" is not among them either. huggingface_hub already fetches the model
# from HF's own CDN, and a second copy of 5 GB served from a Space cannot beat
# that; it can only add a sleep/wake dependency. Only "pylibs" is worth
# replacing, because that is the one artefact pip has to resolve and install
# package by package.
EDGE_ROLES = ("pylibs",)
EDGE_DEST = WORK / "edge_cache"


def _edge_tick(label):
    print("[%6.1fs] edge: %s" % (time.monotonic() - T_EDGE, label), flush=True)


def _find_edge_fetch():
    """Locate the edge-fetch binary the pack provides.

    Same pin-by-absolute-path discipline as the normalizer in the pack cell: a
    PATH-only lookup would resolve whatever the Kaggle base image happens to
    call "edge-fetch", which is the failure mode that once made this pipeline
    silently run a foreign normalizer.
    """
    pinned = os.environ.get("EDGE_FETCH_BIN", "").strip()
    candidates = []
    if pinned:
        candidates.append(Path(pinned))
    bin_dir = WORK / "tdubber_pack" / "bin"
    candidates.append(bin_dir / "edge_fetch")
    candidates.append(bin_dir / "edge-fetch")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def main():
    if not EDGE_URL:
        print("edge: EDGE_URL is not set; skipping the edge cache", flush=True)
        return
    if not EDGE_URL.startswith(("http://", "https://")):
        # Refusing a bare host here keeps the mistake visible in the log instead
        # of surfacing as an opaque dial error from inside the Go client.
        print("edge: EDGE_URL %r is not an http(s) URL; skipping" % EDGE_URL, flush=True)
        return

    binary = _find_edge_fetch()
    if binary is None:
        print(
            "edge: edge_fetch binary not found in the pack; skipping "
            "(the dependency cell will install from origin)",
            flush=True,
        )
        return

    try:
        binary.chmod(0o755)
    except OSError:
        pass

    EDGE_DEST.mkdir(parents=True, exist_ok=True)

    # One invocation per role. A role the Space has not published exits 0 without
    # fetching, so this needs no "is it published" pre-check -- the manifest is
    # the authority and asking it twice would be a second source of truth.
    for role in EDGE_ROLES:
        started = time.monotonic()
        _edge_tick("fetching role %s from %s" % (role, EDGE_URL))
        # -retries 3 rather than the default 4: this cell sits inside a notebook
        # that has its own total budget, and spinning here delays the fallback to
        # origin that actually makes progress.
        result = subprocess.run(
            [
                str(binary),
                "-url", EDGE_URL,
                "-dest", str(EDGE_DEST),
                # Archives are rooted at /kaggle/working: pylibs.tar.gz carries
                # pylibs/ and the weights archive carries hf_cache/, so one
                # extraction directory serves both roles and lands each tree
                # exactly where the next cell and HF_HOME look for it.
                "-unpack-dir", str(WORK),
                "-role", role,
                "-retries", "3",
            ],
            capture_output=True,
            text=True,
            timeout=30 * 60,
            check=False,
        )
        elapsed = time.monotonic() - started
        output = ((result.stdout or "") + (result.stderr or "")).strip()

        for line in output.splitlines():
            print("    edge: %s" % line, flush=True)

        if result.returncode != 0:
            # Non-zero here means -must-fetch semantics were somehow in force.
            # The cell's contract is that a cache failure is never fatal, so the
            # run continues on the origin path.
            print(
                "edge: role %s returned %d; continuing on the origin path"
                % (role, result.returncode),
                flush=True,
            )
        else:
            _edge_tick("role %s settled in %.1fs" % (role, elapsed))

    # Report what actually landed, and -- the part that was wrong before -- say
    # plainly that a second run will NOT be faster because of this cell. Without
    # that line "nothing was downloaded and it was still slow" is
    # indistinguishable from "the cache did not engage", which is how a
    # non-functional cache gets left in place for months.
    for role in EDGE_ROLES:
        role_dir = EDGE_DEST / role
        if not role_dir.is_dir():
            _edge_tick("role %s: nothing on disk (pip will install from origin)" % role)
            continue
        files = [p for p in role_dir.rglob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        _edge_tick(
            "role %s: %d file(s), %.1f MiB at %s"
            % (role, len(files), total / (1 << 20), role_dir)
        )
    _edge_tick(
        "note: /kaggle/working is empty on every NEW session, so this is a "
        "cold-run saving only. For a warm run, attach the built tree as a "
        "Kaggle Dataset (the next cell checks /kaggle/input first)."
    )


main()
