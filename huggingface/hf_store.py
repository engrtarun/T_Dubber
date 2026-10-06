"""T_Dubber <-> Hugging Face connector.

The single module every weight lookup goes through. Resolution
order, cheapest first:

1. **Mounted copy** -- a snapshot already sitting under a Kaggle
   input directory (a cache/pack dataset attached to the kernel).
   Zero bytes, zero seconds.
2. **HF_HOME cache** -- a previous run's download under
   ``/kaggle/working/hf_cache`` (Kaggle saves notebook output,
   so the cache can ride along as next session's input).
3. **edge Space** -- the repo's own 24/7 artifact server
   (``edge/server.go``), when ``TDUBBER_EDGE_URL`` is set. A LAN
   fetch of a 5 GB snapshot beats a hub download on any shared
   Kaggle network.
4. **huggingface_hub** -- ``snapshot_download``. Gated repos need
   ``HF_TOKEN``; public repos do not.

Kaggle-specific behaviour is built in: ``HF_HOME`` moves off
``/root/.cache`` (which dies with the container) into
``/kaggle/working/hf_cache``, and xet is disabled because its
uploads stall on Kaggle's network.

Usage::

    import hf_store
    hf_store.setup_env()
    path = hf_store.ensure_model("IndexTeam/Index-Homura-2B")
    hf_store.warm_status()   # human report: what is already local

Every function degrades instead of raising when the hub is
unreachable: a dubbing run must never die because a *optional*
model could not be fetched -- the caller decides what a missing
model means.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

DEFAULT_HF_HOME = Path("/kaggle/working/hf_cache")
FALLBACK_HF_HOME = Path.home() / ".cache" / "huggingface"

_ROSTER_PATH = Path(__file__).resolve().parent / "models.json"


def setup_env(hf_home: str | Path | None = None) -> Path:
    """Point HF at a durable cache and return it.

    Must run before anything imports ``huggingface_hub`` or
    ``transformers`` -- the same reason the Kaggle notebook sets
    ``HF_HOME`` at the very top of its first cell.
    """
    home = Path(hf_home) if hf_home else None
    if home is None:
        home = DEFAULT_HF_HOME if Path("/kaggle/working").is_dir() else FALLBACK_HF_HOME
    os.environ.setdefault("HF_HOME", str(home))
    # xet uploads stall on Kaggle; hub falls back to regular HTTP.
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    Path(os.environ["HF_HOME"]).mkdir(parents=True, exist_ok=True)
    return Path(os.environ["HF_HOME"])


def load_roster(path: str | Path | None = None) -> dict:
    """The models.json roster (empty list when missing)."""
    roster_path = Path(path) if path else _ROSTER_PATH
    try:
        return json.loads(roster_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 0, "models": []}


# The repo keeps HF credentials in HuggingFace_PaperWork/
# (the HF twin of kaggle_paperWork/). A token file there
# is honored but never committed: .gitignore covers it.
_PAPERWORK_DIR = Path(__file__).resolve().parent.parent / "HuggingFace_PaperWork"


def _token_from_paperwork() -> str | None:
    """Read HF_TOKEN from HuggingFace_PaperWork/, if present.

    Accepts hf.env (KEY=VALUE lines), token.txt (raw token)
    or .env. First match wins; nothing is written back.
    """
    for name in ("hf.env", ".env", "token.txt"):
        candidate = _PAPERWORK_DIR / name
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if name == "token.txt":
            if text and not text.startswith("#"):
                return text.splitlines()[0].strip()
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.upper().startswith("HF_TOKEN="):
                value = line.split("=", 1)[1].strip().strip('"\'')
                if value:
                    return value
    return None


def resolve_token(explicit: str | None = None) -> str | None:
    """Token precedence: explicit arg > HF_TOKEN env >
    HuggingFace_PaperWork/ file."""
    if explicit:
        return explicit
    env_token = os.environ.get("HF_TOKEN", "").strip()
    if env_token:
        return env_token
    return _token_from_paperwork()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _snapshot_dirs(repo_id: str, base: Path):
    """HuggingFace hub cache layout: models--<org>--<name>/snapshots/*."""
    key = "models--" + repo_id.replace("/", "--")
    try:
        for snap in base.glob(f"**/{key}/snapshots/*"):
            if snap.is_dir() and (snap / "config.json").exists():
                yield snap
    except OSError:
        return


def _mounted_snapshot(repo_id: str, roots) -> Path | None:
    for root in roots or []:
        if not root:
            continue
        for snap in _snapshot_dirs(repo_id, Path(root)):
            if any(snap.glob("*.safetensors")) or any(snap.glob("*.bin")):
                return snap
    return None


def _edge_fetch(repo_id: str, dest: Path, edge_url: str) -> bool:
    """Install the origin's weight artefacts by running ``edge-fetch``.

    WHY THIS DELEGATES INSTEAD OF DOWNLOADING
    ------------------------------------------
    The origin speaks one protocol: ``manifest.json`` listing content digests,
    then ``/artifact/<name>`` per entry, with every downloaded byte checked
    against that digest before it is unpacked. ``edge/`` is the tested
    implementation of exactly that contract (``edge/client_test.go`` and
    ``edge/unpack_test.go``), and it is a static binary that the Kaggle pack
    already delivers.

    An earlier version of this function fetched
    ``<url>/artefacts/<repo_id>.tar.gz`` itself. No origin has ever served that
    path -- the Space serves ``/artifact/<name>`` and a Hugging Face repository
    serves ``/resolve/main/artifact/<name>`` -- so the call returned False on
    every run and the "fast path" silently degraded to the hub. Reimplementing a
    second copy of a verified-fetch contract in Python is how that drift goes
    unnoticed: there is no test that fails, only a cache that never engages.

    ``repo_id`` is accepted for the caller's readability and logged; selection
    is by ROLE, not by repository, because that is what the manifest indexes.
    Returns True only when the fetch was verified end to end, so a cache miss
    still falls through to the hub.
    """
    import subprocess

    binary = os.environ.get("EDGE_FETCH_BIN", "").strip() or shutil.which("edge-fetch")
    if not binary:
        return False
    if not edge_url or not edge_url.startswith(("http://", "https://")):
        return False

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    # The weights archive carries `hf_cache/` at its root, so it is extracted
    # one level ABOVE dest: dest is the hf_cache directory itself, and a level
    # of nesting error here shows up as a model that silently re-downloads.
    unpack_dir = dest.parent

    cmd = [
        binary,
        "-url", edge_url,
        "-dest", str(dest),
        "-unpack-dir", str(unpack_dir),
        "-role", "weights",
        # Without -must-fetch a miss exits 0 and looks like success, which is
        # the right behaviour for the notebook cell and exactly wrong here:
        # this function's whole job is to report whether it worked.
        "-must-fetch",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                                check=False)
    except Exception:
        return False
    if result.returncode != 0:
        return False
    print("hf_store: installed %s from the edge origin (%s)"
          % (repo_id, edge_url), flush=True)
    return True


def ensure_model(repo_id: str, revision: str = "",
                 mounted_roots=None,
                 hf_home: str | Path | None = None,
                 edge_url: str | None = None,
                 token: str | None = None) -> Path | None:
    """Resolve ``repo_id`` to a local snapshot directory, or None.

    Never raises for a missing/unreachable model -- returning None
    lets the caller choose a fallback (a smaller model, a different
    engine) instead of losing a GPU run.
    """
    home = Path(hf_home) if hf_home else setup_env()

    # 1. Mounted (Kaggle input dataset): zero cost.
    mounted = _mounted_snapshot(repo_id, mounted_roots)
    if mounted:
        return mounted

    # 2. This machine's HF_HOME cache.
    for snap in _snapshot_dirs(repo_id, home):
        if any(snap.glob("*.safetensors")) or any(snap.glob("*.bin")):
            return snap

    # 3. The repo's own edge Space (fast, optional).
    edge = edge_url or os.environ.get("TDUBBER_EDGE_URL", "").strip()
    if edge:
        dest = home / "edge-snapshots" / repo_id.replace("/", "--")
        if _edge_fetch(repo_id, dest, edge) and any(dest.glob("*.safetensors")):
            return dest

    # 4. The hub itself.
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("[hf_store] huggingface_hub not installed; cannot download.",
              file=sys.stderr)
        return None
    try:
        kwargs = {"repo_id": repo_id, "cache_dir": str(home)}
        if revision:
            kwargs["revision"] = revision
        elif os.environ.get("HF_DEFAULT_REVISION"):
            kwargs["revision"] = os.environ["HF_DEFAULT_REVISION"]
        resolved = resolve_token(token)
        if resolved:
            kwargs["token"] = resolved
        return Path(snapshot_download(**kwargs))
    except Exception as exc:
        print(f"[hf_store] could not fetch {repo_id}: {exc}", file=sys.stderr)
        return None


def warm_status(mounted_roots=None,
                hf_home: str | Path | None = None) -> list[dict]:
    """Per-roster-entry report: where (if anywhere) it is already local.

    The worker prints this at startup so a slow run is never silent
    about WHY it is slow.
    """
    home = Path(hf_home) if hf_home else setup_env()
    rows = []
    for entry in load_roster().get("models", []):
        repo_id = entry.get("repo_id", "")
        source = "missing"
        if _mounted_snapshot(repo_id, mounted_roots):
            source = "mounted"
        elif any(_snapshot_dirs(repo_id, home)):
            source = "hf_home"
        rows.append({"repo_id": repo_id, "role": entry.get("role", ""),
                     "approx_gb": entry.get("approx_gb", 0), "source": source})
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_env()
    mounted = [p for p in (os.environ.get("TDUBBER_MOUNT_ROOTS", "/kaggle/input"),)
               if p]
    rows = warm_status(mounted_roots=mounted)
    warm = sum(1 for row in rows if row["source"] != "missing")
    print(f"warm {warm}/{len(rows)} (HF_HOME={os.environ['HF_HOME']})")
    for row in rows:
        print(f"  {row['source']:8s} {row['repo_id']:40s} ~{row['approx_gb']}GB [{row['role']}]")
    if "--ensure" in argv:
        for row in rows:
            if row["source"] == "missing":
                path = ensure_model(row["repo_id"], mounted_roots=mounted)
                print(f"  ensure {row['repo_id']} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
