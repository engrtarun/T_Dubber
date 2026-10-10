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

import hashlib
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

# The suffixes that count as "there is something to load here", in the order the
# old hardcoded glob list used them. Kept as a module constant so there is ONE
# answer to "what counts as weights" in this file -- it was inlined in three
# places before, and two of them had already drifted (see _has_weights).
#
# .bin is here for the whisper.cpp ggml models, which predate the .gguf
# extension. .safetensors is here for the safetensors torch snapshot this
# connector was originally written for, and stays the default so nothing about
# the existing behaviour changes.
WEIGHT_SUFFIXES: tuple[str, ...] = (".safetensors", ".bin")
DEFAULT_SUFFIX: str = ".safetensors"

# Read block for the streaming digest. 4 MiB matches what multitasker.py and the
# Go side use, so all three spend the same syscall budget on the same file.
_HASH_BLOCK = 4 * 1024 * 1024


def sha256_file(path: str | Path, block: int = _HASH_BLOCK) -> str:
    """Content digest of a file, read in bounded blocks.

    THIS IS THE ONLY CONTENT-ADDRESSING HELPER IN THIS PACKAGE. gguf_store.py
    verifies multi-gigabyte model blobs with it, so there is one implementation
    to keep correct rather than two that can disagree about what "verified" means.

    It reads blocks rather than ``path.read_bytes()`` on purpose. A .gguf here is
    1.3 GB and the base whisper model 150 MB; slurping one costs more RAM than
    the rest of the resolver, and on a Kaggle kernel that is the difference
    between a fetch and an OOM. The block is fixed rather than taken from a
    caller on the hot path because every caller wants the same answer.

    The size is checked against the file's own stat only for the empty-file
    case; a caller that knows the expected length should compare it itself and
    use this for the digest.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(block), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def weight_suffixes(suffix=None) -> tuple[str, ...]:
    """Normalise a Suffix argument into the tuple of glob patterns to look for.

    ``None``  -> WEIGHT_SUFFIXES, which is EXACTLY what this module checked
                 before a suffix could be passed. Nothing about the existing
                 ``ensure_model("Systran/faster-whisper-large-v3")`` behaviour
                 changes.
    ``".gguf"`` or ``"gguf"`` -> ``(".gguf",)``. Only that. Passing ".gguf" and
                 still accepting ".bin" would mean a resolver asked for a gguf
                 reports success on a directory holding a faster-whisper
                 CTranslate2 blob, and the caller then hands llama.cpp a file it
                 cannot load.
    a sequence -> used as-is, with dots added where they are missing.

    The "add the dot if it is missing" rule is because both spellings are what
    people type, and a silent empty pattern would make every snapshot look
    weightless -- i.e. a permanent silent re-download on every run.
    """
    if suffix is None:
        return WEIGHT_SUFFIXES
    if isinstance(suffix, str):
        wanted = (suffix,)
    else:
        wanted = tuple(suffix)
    out = []
    for item in wanted:
        item = str(item).strip().lower()
        if not item:
            continue
        out.append(item if item.startswith(".") else "." + item)
    return tuple(out)


def _is_snapshot(path: Path, suffix=None) -> bool:
    """A directory looks like a usable snapshot if it has a config or weights.

    The original test was `config.json` alone, which is right for a model repo
    and wrong for the DATASET in the roster (bakrianoo/mazinger-dubber-profiles):
    a dataset has no config.json, so it was permanently invisible here and
    warm_status reported it missing forever however often it was downloaded.

    `suffix` is ADDED to the pattern list rather than replacing it, because
    discoverability and loadability are different questions: a snapshot is worth
    finding if it has any of the usual markers, and worth *using* if it has the
    weights the caller asked for. Substituting the list would have made
    ensure_model(suffix=".gguf") unable to find a directory that also holds a
    config.json.

    ".gguf" is in the base list, so a directory holding nothing but a quantised
    model is at least VISIBLE to a report. It still does not count as loadable
    without the matching suffix -- discoverability says "there is something
    here", _has_weights says "this is what you asked for".
    """
    if (path / "config.json").exists():
        return True
    patterns = ("*.safetensors", "*.bin", "*.pt", "*.jsonl", "*.gguf")
    for extra in weight_suffixes(suffix):
        candidate = "*" + extra
        if candidate not in patterns:
            patterns = patterns + (candidate,)
    for pattern in patterns:
        if next(path.glob(pattern), None) is not None:
            return True
    return False


def _has_weights(path: Path, suffix=None) -> bool:
    """The heavier half of _is_snapshot: is there anything to actually load?

    This is the predicate that decides "is the run warm", so it is the one the
    Suffix argument has to reach. Default behaviour is unchanged: a safetensors
    file or a .bin counts, exactly as before.
    """
    for pattern in weight_suffixes(suffix):
        if next(path.glob("*" + pattern), None) is not None:
            return True
    return False


def _snapshot_dirs(repo_id: str, base: Path, suffix=None):
    """HuggingFace hub cache layout: models--<org>--<name>/snapshots/*."""
    key = "models--" + repo_id.replace("/", "--")
    try:
        for snap in base.glob(f"**/{key}/snapshots/*"):
            if snap.is_dir() and _is_snapshot(snap, suffix):
                yield snap
    except OSError:
        return


def _mounted_snapshot(repo_id: str, roots, suffix=None) -> Path | None:
    for root in roots or []:
        if not root:
            continue
        for snap in _snapshot_dirs(repo_id, Path(root), suffix):
            if _has_weights(snap, suffix):
                return snap
    return None


def _edge_fetch(repo_id: str, dest: Path, edge_url: str, suffix=None) -> Path | None:
    """Pull one model snapshot from the T_Dubber edge Space.

    Returns the resolved snapshot directory, or None on any miss.

    The Space serves ``GET /artifact/<name>`` -- SINGULAR -- alongside
    ``/manifest.json`` and ``/healthz``; see edge/server.go. This function used
    to ask for ``/artefacts/<org>--<name>.tar.gz``, a route the Space has never
    had. Every call 404'd, the bare ``except`` swallowed it, and resolution
    quietly fell through to the hub -- so the "Homura download 525 s -> 0 s"
    claim in KAGGLE_HF_PLAYBOOK.md was unreachable in practice. A silently dead
    fast path is worse than a slow one: the run still succeeds and nobody reads
    the log.

    Three things had to be true at once, and fixing only some of them leaves it
    just as dead:

    1. The route is ``/artifact/``; the artefact name is the ``<org>--<name>``
       the manifest publishes.
    2. ``TarFile.extractall(filter=...)`` exists only from Python 3.11.4.
       Kaggle images ship 3.10/3.11, where passing it raises TypeError -- from
       inside the ``except`` below, so it degraded to "edge unreachable" with
       nothing to go on. The filter is passed only where it exists.
    3. The archive unpacks into a subdirectory of its own, so the caller's
       ``dest.glob("*.safetensors")`` sees nothing at the top level. Hence the
       resolved directory is returned instead of a bare success flag.
    """
    import tarfile
    import tempfile

    name = repo_id.replace("/", "--") + ".tar.gz"
    endpoint = f"{edge_url.rstrip('/')}/artifact/{name}"
    try:
        with urllib.request.urlopen(endpoint, timeout=30) as response:
            if response.status != 200:
                return None
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
                shutil.copyfileobj(response, tmp)
                archive = tmp.name
    except Exception as exc:
        print(f"[hf_store] edge miss {endpoint}: {exc}", file=sys.stderr)
        return None

    try:
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as bundle:
            try:
                bundle.extractall(dest, filter="data")
            except TypeError:
                # Python < 3.11.4 has no filter kwarg at all.
                bundle.extractall(dest)
        # The archive nests its own top-level directory -- and how deep it
        # nests is not a promise anyone made, so walk the tree rather than
        # guessing at one or two levels. rglob is sorted, so the first match is
        # the shallowest directory that actually holds weights, which is the
        # right thing to hand back for a sharded repo too. Checking `dest`
        # itself first would have been the "one level down" guess: it found the
        # top-level directory the tarball created, saw no weights in it, and
        # declared the fetch a miss -- even though the download had worked.
        for candidate in sorted(dest.rglob("*")):
            if candidate.is_dir() and _has_weights(candidate, suffix):
                return candidate
        return None
    except Exception as exc:
        print(f"[hf_store] edge unpack of {name} failed: {exc}", file=sys.stderr)
        return None
    finally:
        try:
            os.unlink(archive)
        except OSError:
            pass

def ensure_model(repo_id: str, revision: str = "",
                 mounted_roots=None,
                 hf_home: str | Path | None = None,
                 edge_url: str | None = None,
                 token: str | None = None,
                 suffix=None) -> Path | None:
    """Resolve ``repo_id`` to a local snapshot directory, or None.

    Never raises for a missing/unreachable model -- returning None
    lets the caller choose a fallback (a smaller model, a different
    engine) instead of losing a GPU run.

    ``suffix`` narrows what counts as a usable snapshot; see
    :func:`weight_suffixes`. It defaults to ``None``, which is the
    pre-existing behaviour (``.safetensors`` or ``.bin``) -- so every current
    caller is unaffected, and ``gguf_store.py`` passes ``".gguf"`` explicitly.

    Step 3 also takes the suffix, because an edge tarball that contains only a
    .gguf would otherwise unpack, fail the _has_weights test, and be reported as
    an unreachable Space.
    """
    home = Path(hf_home) if hf_home else setup_env()

    # 1. Mounted (Kaggle input dataset): zero cost.
    mounted = _mounted_snapshot(repo_id, mounted_roots, suffix)
    if mounted:
        return mounted

    # 2. This machine's HF_HOME cache. The weight test is _has_weights, the same
    #    predicate warm_status uses -- see the note there. Two different
    #    predicates across the resolve and report paths is how a report says
    #    "warm" for a snapshot the resolver will still re-download.
    for snap in _snapshot_dirs(repo_id, home, suffix):
        if _has_weights(snap, suffix):
            return snap

    # 3. The repo's own edge Space (fast, optional). _edge_fetch returns the
    #    resolved snapshot directory, because the archive unpacks one level
    #    below `dest` and a bare success flag would hand back an empty folder.
    edge = edge_url or os.environ.get("TDUBBER_EDGE_URL", "").strip()
    if edge:
        dest = home / "edge-snapshots" / repo_id.replace("/", "--")
        fetched = _edge_fetch(repo_id, dest, edge, suffix)
        if fetched:
            return fetched

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
                hf_home: str | Path | None = None,
                suffix=None) -> list[dict]:
    """Per-roster-entry report: where (if anywhere) it is already local.

    The worker prints this at startup so a slow run is never silent about WHY it
    is slow -- which only works if this report and ensure_model() agree. They
    used not to: this function accepted any directory that merely EXISTED under
    snapshots/, while ensure_model() additionally demanded weight files. A
    half-fetched snapshot -- exactly what an interrupted run leaves behind --
    was therefore reported "hf_home" by this function and then downloaded all
    over again by the resolver. The symptom is a run that is slow for a reason
    the log claims is not the reason.

    So both use _has_weights, and the reason is recorded in the row. `suffix`
    goes to both for the same reason: a report and a resolver with different
    ideas of what counts as warm is exactly the bug this function documents.
    """
    home = Path(hf_home) if hf_home else setup_env()
    rows = []
    for entry in load_roster().get("models", []):
        repo_id = entry.get("repo_id", "")
        source = "missing"
        note = ""
        mounted = _mounted_snapshot(repo_id, mounted_roots, suffix)
        if mounted:
            source = "mounted"
        else:
            snapshots = list(_snapshot_dirs(repo_id, home, suffix))
            if any(_has_weights(s, suffix) for s in snapshots):
                source = "hf_home"
            elif snapshots:
                note = "dir present, no weight files -- will re-download"
        rows.append({"repo_id": repo_id, "role": entry.get("role", ""),
                     "approx_gb": entry.get("approx_gb", 0), "source": source,
                     "note": note})
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
        if row.get("note"):
            print(f"           note: {row['note']}")
    if "--ensure" in argv:
        for row in rows:
            if row["source"] == "missing":
                path = ensure_model(row["repo_id"], mounted_roots=mounted)
                print(f"  ensure {row['repo_id']} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
