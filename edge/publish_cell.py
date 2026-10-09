# =============================================================================
# T_Dubber edge publish -- push the built pylibs tree to the HF repository
# PUBLISH_VARIANT: late
# =============================================================================
# WHY THIS CELL EXISTS TWICE
# -------------------------
# This file is injected twice per notebook: the early copy runs right after the
# dependency install, the late copy runs last. inject_cell.py tells them apart by
# the banner line above -- and never by repeating that line in this prose, because
# a marker that appears twice inside one cell cannot be used to find that cell.
#
# Measured reason, from Kaggle run `letest_test3.txt` (2026-10-09): the install
# finished at ~274 s and the tree was complete, but `import torchaudio` raised
# a CUDA mismatch at 545 s, so the kernel stopped -- and a publish cell that only
# runs LAST never ran. The artefact existed and the repository stayed empty, so
# the next cold run would pay the full 458 s pip install again.
#
# Publishing early costs one extra minute of hashing when the run is healthy; the
# late copy then finds the digest already on the Hub and prints "already
# published". A publish attempt is never allowed to fail the run either way.
#
# WHAT THIS CELL IS FOR
# --------------------
# The fetch side (edge/notebook_cell.py) proves the origin works, but a
# repository with nothing published in it saves nothing: the worker prints one
# line and falls back to the 458 s pip install. The artefact can only be built
# by a run that completed its install -- on Kaggle, with Kaggle's linux wheels --
# so THIS run is the only place the tree will ever exist. Publishing at the end
# of the run is what turns the next cold run into a warm one.
#
# WHY PYTHON AND NOT edge-publish.exe
# -----------------------------------
# edge-publish (Go) is the reference publisher and stays byte-compatible with
# edge-fetch, but shipping a second binary through the pack -- and keeping a
# linux build of it current -- costs more than the tar it writes. huggingface_hub
# is already installed for model downloads, and the Hub handles git-lfs for us,
# so this cell needs no binary, no git and no lfs client. It writes the SAME
# layout (manifest.json + artifact/pylibs/pylibs.tar) and the SAME refusal rules
# (.tdubber_ready + vllm/__init__.py), and edge-fetch verifies the digest the
# same way either way.
#
# THE ONE RULE THIS CELL OBEYS
# ----------------------------
# A cache failure is never fatal. No HF_TOKEN secret, no tree, no network, a
# digest that does not match -- each prints one line and exits 0. The run that
# just succeeded must not be failed by the attempt to make the NEXT one faster.
#
# WHY THE UPLOAD ORDER IS TAR FIRST, MANIFEST SECOND
# --------------------------------------------------
# The manifest is what the client trusts. A manifest that names a tar which is
# not on the Hub yet is a window in which every fetch fails verification. The
# tar lands first; the manifest, last. On a digest mismatch against what is
# already published, neither is uploaded at all -- that is the common case, and
# it costs one small GET.
#
# WHY THE MTIME IS FIXED AT ZERO
# ------------------------------
# A re-install of identical wheels produces identical bytes with new mtimes.
# Hashing the file mtimes into the tar would change the digest on every run and
# re-upload gigabytes that did not change. Zeroing mtimes makes the digest a
# function of CONTENT, which is what "already published, skipping" compares.

import argparse
import hashlib
import io
import json
import os
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_REPO = "pocotarun/tdubber-edge"
DEFAULT_TREE = "/kaggle/working/pylibs"
MANIFEST_REMOTE = "https://huggingface.co/datasets/%s/resolve/main/manifest.json"


def log(msg):
    print("edge-publish-py: %s" % msg, flush=True)


def refuse(msg):
    """The same refusals edge-publish (Go) makes, for the same reason: a tree
    find_pylibs() would ignore still gets downloaded, unpacked and wasted --
    and a half-installed tree published as warm would be worse than no cache
    at all, because the run would skip the install and crash on the import."""
    log("refusing to publish: %s" % msg)
    return 0


def build_tar(tree, out_path):
    """Stream the tree into a deterministic plain .tar and return (size, sha256).

    Plain tar, not tar.gz, is deliberate and matches the Go publisher: the
    payload is wheels and safetensors -- already at the entropy floor -- and a
    measured 400 MB sample of this kind of data compressed to ratio 1.000 while
    costing 74 s of CPU across the 9 GB of a full artefact. Same bytes on the
    wire, a minute and a quarter of CPU saved."""
    h = hashlib.sha256()
    size = 0
    files = sorted(p for p in tree.rglob("*"))
    with open(out_path, "wb") as raw:
        class HashingWriter(io.RawIOBase):
            """A write-only file object that digests every byte through it.

            tarfile calls tell() to track the offset it is writing at, so the
            wrapper counts rather than seeks -- the digest wraps the exact bytes
            that reach the file, tar framing included, which is what edge-fetch
            recomputes after download."""

            def __init__(self):
                self._pos = 0

            def write(self, b):
                nonlocal size
                n = raw.write(b)
                h.update(b[:n])
                size += n
                self._pos += n
                return n

            def tell(self):
                return self._pos

            def writable(self):
                return True

            def seekable(self):
                return False

        with tarfile.open(fileobj=HashingWriter(), mode="w", format=tarfile.GNU_FORMAT) as tf:
            for p in files:
                info = tf.gettarinfo(str(p), arcname="pylibs/" + p.relative_to(tree).as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0  # content-addressed: see the header note
                if info.isreg():
                    with open(p, "rb") as fh:
                        tf.addfile(info, fh)
                else:
                    tf.addfile(info)
    return size, h.hexdigest()


def read_remote_manifest(repo):
    """The published manifest, or None. Unauthenticated on purpose: the
    repository is public and the check must work exactly as the worker sees it."""
    try:
        with urllib.request.urlopen(MANIFEST_REMOTE % repo, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description="publish the built pylibs tree to the HF origin")
    ap.add_argument("--tree", default=os.environ.get("TDUBBER_EDGE_PUBLISH_TREE", DEFAULT_TREE))
    ap.add_argument("--repo", default=os.environ.get("TDUBBER_EDGE_REPO", DEFAULT_REPO))
    ap.add_argument("--token", default=os.environ.get("HF_TOKEN", ""))
    ap.add_argument("--tar-out", default="", help="keep the tar here instead of beside the tree")
    args = ap.parse_args()

    tree = Path(args.tree)
    if not tree.is_dir():
        log("no tree at %s; nothing to publish (the run may not have reached the install)" % tree)
        return 0
    if not (tree / ".tdubber_ready").is_file():
        log("no .tdubber_ready in %s; refusing (find_pylibs() would ignore this tree)" % tree)
        return 0
    if not (tree / "vllm" / "__init__.py").is_file():
        log("no vllm/__init__.py in %s; refusing (find_pylibs() requires it)" % tree)
        return 0
    if not args.token:
        log("HF_TOKEN is not set (add the HF write token as a notebook secret); skipping")
        return 0

    tar_path = Path(args.tar_out) if args.tar_out else tree.parent / "pylibs.tar"
    log("hashing %s into %s (content-addressed, this takes a minute on a full tree)" % (tree, tar_path))
    started = time.monotonic()
    size, digest = build_tar(tree, tar_path)
    log("tar %d bytes, sha256=%s (%.1f s)" % (size, digest[:16], time.monotonic() - started))

    remote = read_remote_manifest(args.repo)
    for entry in (remote or {}).get("entries", []):
        if entry.get("role") == "pylibs" and entry.get("sha256") == digest and entry.get("size_bytes") == size:
            log("already published (sha256 matches); the repository is current")
            tar_path.unlink(missing_ok=True)
            return 0

    from huggingface_hub import HfApi

    api = HfApi(token=args.token)
    log("uploading artifact/pylibs/pylibs.tar to %s (one-off; git-lfs stores it)" % args.repo)
    started = time.monotonic()
    api.upload_file(
        path_or_fileobj=str(tar_path),
        path_in_repo="artifact/pylibs/pylibs.tar",
        repo_id=args.repo,
        repo_type="dataset",
    )
    log("tar uploaded (%.1f s); writing manifest.json last" % (time.monotonic() - started))

    manifest = {
        "version": 1,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entries": [
            {
                "name": "pylibs/pylibs.tar",
                "role": "pylibs",
                "size_bytes": size,
                "sha256": digest,
                "modified_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        ],
        "total_bytes": size,
    }
    api.upload_file(
        path_or_fileobj=json.dumps(manifest, indent=2).encode("utf-8") + b"\n",
        path_in_repo="manifest.json",
        repo_id=args.repo,
        repo_type="dataset",
    )
    log("published; the next cold run fetches this instead of pip-installing")
    tar_path.unlink(missing_ok=True)
    return 0


# No sys.exit(): this file is also a notebook cell, and a SystemExit there would
# surface as an error traceback for a run that did not fail. Every path through
# main() returns 0 by design -- a publish failure is a printed line, not an error.
main()
