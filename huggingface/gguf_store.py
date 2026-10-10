"""Resolve a .gguf model blob to a local file, in the order hf_store.py uses.

WHY THIS EXISTS
---------------
Kaggle run ``test4_gotgVERSION`` spent 1067 s of 1258 s pip-installing vLLM,
PyTorch and CUDA, and then crashed with::

    ImportError: libcudart.so.13: cannot open shared object file

vLLM 0.26.0 ships CUDA 13 wheels; the Kaggle image ships CUDA 12. There is no
pin that fixes that from the inside, so the torch stack is going away and
llama.cpp / whisper.cpp take over. Those load a single file each -- a .gguf for
the translation model, a ggml .bin for whisper -- and that file is this
module's job.

SAME PRECEDENCE CHAIN, ON PURPOSE
---------------------------------
Mounted Kaggle input -> HF_HOME cache -> edge Space -> huggingface hub, in that
order, cheapest first. Not a similar order -- the SAME one, implemented on top
of hf_store's helpers rather than beside them. Four independent implementations
of a resolution order is four places for the fast path to be silently dead, and
the previous one of those (/artefacts/ instead of /artifact/) cost 525 s per run
while the log claimed it cost nothing.

WHAT IS DIFFERENT FROM ensure_model(), AND WHY
----------------------------------------------
ensure_model returns a snapshot DIRECTORY, because a torch repo is many files.
A .gguf is ONE file, so this returns a Path to that file and every caller in the
pipeline wants exactly that. Everything else -- discovery, the hub fallback, the
token -- is delegated.

THE VERIFY-THEN-DELETE RULE
---------------------------
A model is hashed AFTER it lands on disk, streaming, and a mismatch DELETES the
file and raises. Both halves matter:

  * After, because a digest of a file still being written is a digest of
    whatever had been written so far.
  * Streaming, because Q4_K_M is 1.31 GB and large-v3-turbo is 1.62 GB;
    read_bytes() on either is more RAM than the rest of the resolver.
  * Delete-and-raise, because leaving a corrupt model on disk is the worst of
    the three outcomes: the next run finds a file of the right SIZE, the size
    check passes, and the run is slow for a reason the log does not mention.

    Deleting means the next run re-downloads 1.6 GB. Keeping means every future
    run re-downloads 1.6 GB AND re-verifies it. Deleting is also the honest
    thing: we do not know what the bytes are, so we must not let anything load
    them.

A MISSING DIGEST IS NOT A FAILURE
---------------------------------
If the roster has no verified digest for a model, the file is size-checked and
used. That is deliberately weaker, and it is the correct fallback: an unverified
model that is 99% right beats no model at all on a run with a fixed time budget.
It is logged every time so the weakening is never invisible.

Usage::

    import gguf_store
    path = gguf_store.ensure_gguf("whisper-large-v3-turbo")     # default asr
    path = gguf_store.ensure_gguf("homura-2b-q4_k_m")
    print(gguf_store.warm_status())
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hf_store  # noqa: E402

_ROSTER_PATH = Path(__file__).resolve().parent / "models_gguf.json"

# The Space's model route. SINGULAR /gguf/ -- see edge/server.go handleGguf.
#
# It is not /artifact/ and it is not /artefacts/. Getting this wrong is the
# failure this whole connector is named after: the route 404s, the bare except
# swallows it, and resolution quietly falls through to the hub, so the run
# succeeds, the "525 s becomes 0 s" claim stays false, and nothing in the log
# says why.
GGUF_ROUTE = "/gguf/"

# Header carrying the blob's digest. The client reads this BEFORE transferring,
# so it knows what it is verifying against rather than discovering it after.
SHA_HEADER = "X-Content-Sha256"

# Read block for the streaming download and hash. Matches hf_store.sha256_file
# and the Go side, so all three spend the same on the same file.
_BLOCK = 4 * 1024 * 1024


class GgufError(RuntimeError):
    """A model could not be resolved, or resolved to bytes that are wrong.

    Separate from None. ``ensure_model`` returns None for "not found" because a
    caller can always pick a smaller model; a gguf that downloads and then fails
    its digest is a different thing entirely -- the bytes exist and are wrong --
    and swallowing that into a None would send the pipeline to the hub for the
    same corrupt file.
    """


def log(line: str) -> None:
    print(f"[gguf_store] {line}", file=sys.stderr, flush=True)


def load_roster(path: str | Path | None = None) -> dict:
    """The models_gguf.json roster (empty list when missing).

    This file is the Python mirror of edge/model_gguf.go. The Go test
    TestPythonRosterMirrorMatchesGo compares the two field-for-field and fails
    the build if a digest drifts, so editing one without the other does not
    silently ship.
    """
    roster_path = Path(path) if path else _ROSTER_PATH
    try:
        return json.loads(roster_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"roster unreadable ({exc}); no model can be resolved by name")
        return {"version": 0, "models": []}


def lookup(model_id: str, roster: dict | None = None) -> dict:
    """One roster entry by id, or {}.

    An empty dict rather than None so callers can use ``entry.get(...)``
    throughout and there is no ``if entry is None`` in every call site.
    """
    doc = roster if roster is not None else load_roster()
    for entry in doc.get("models", []):
        if entry.get("id") == model_id:
            return entry
    return {}


def default_model(task: str, roster: dict | None = None) -> dict:
    """The roster's default for a task: ``llm`` or ``asr``.

    Mirrors Go's DefaultGguf (edge/model_gguf.go). The choices are not arbitrary:
    Q4_K_M because the publisher validated it against the F16 conversion on an
    A100 and the run is throughput-bound; large-v3-turbo because dropping 24 of
    32 decoder layers costs no accuracy that matters for dubbing.
    """
    doc = roster if roster is not None else load_roster()
    for entry in doc.get("models", []):
        if entry.get("task") == task:
            return entry
    raise GgufError(f"no {task!r} model in the roster")


def hub_url(entry: dict, revision: str = "") -> str:
    """The hub's /resolve URL for one file.

    ``https://huggingface.co/<repo>/resolve/<rev>/<file>`` -- what
    ``download-ggml-model.sh`` itself uses for the whisper models. The hub
    redirects to a CDN, so this is a URL to GET, not a path inside a snapshot
    directory.

    ``HF_ENDPOINT`` overrides the origin, which is the standard Hugging Face
    mirror knob (hf-mirror.com, an internal proxy). It is also the seam a test
    uses to point the hub step at a localhost server and exercise the real
    request path rather than mocking urllib -- the mocking that let the original
    ``/artefacts/`` bug pass.
    """
    rev = revision or os.environ.get("HF_DEFAULT_REVISION") or "main"
    base = (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")
    return f"{base}/{entry['repo']}/resolve/{rev}/{entry['file']}"


# ---------------------------------------------------------------------------
# Local discovery -- steps 1 and 2
# ---------------------------------------------------------------------------


def _mounted_file(file_name: str, mounted_roots) -> Path | None:
    """A .gguf already sitting under a Kaggle input directory. Zero bytes."""
    for root in mounted_roots or []:
        if not root:
            continue
        base = Path(root)
        try:
            hits = sorted(base.glob(f"**/{file_name}"))
        except OSError:
            continue
        for hit in hits:
            if hit.is_file():
                return hit
    return None


def _cached_file(entry: dict, home: Path) -> Path | None:
    """A .gguf in this machine's HF_HOME.

    Two layouts are checked, because two different code paths put one there:

      1. the hub cache: ``models--<org>--<name>/snapshots/<rev>/<file>``
      2. our own flat copy: ``gguf/<file>``, where the edge fetch and the hub
         fallback below both land it

    Checking only (1) is how a resolved model gets re-downloaded on every run
    even though it is sitting on disk -- the file is there, the report says
    missing, and the log blames the network.
    """
    file_name = entry.get("file", "")
    key = "models--" + str(entry.get("repo", "")).replace("/", "--")
    if key.startswith("models--"):
        try:
            for hit in sorted(home.glob(f"**/{key}/snapshots/*/{file_name}")):
                if hit.is_file():
                    return hit
        except OSError:
            pass
    flat = home / "gguf" / file_name
    if flat.is_file():
        return flat
    edge_copy = home / "edge-gguf" / file_name
    if edge_copy.is_file():
        return edge_copy
    return None


# ---------------------------------------------------------------------------
# Download + verify
# ---------------------------------------------------------------------------


def _resume_prefix(part: Path, expect_bytes: int, digest) -> int:
    """Bytes of an existing ``.part`` that can be reused, fed into ``digest``.

    Returns 0 when there is nothing safe to reuse. A ``.part`` already at or past
    the expected length is not a prefix of the file we want -- it is stale, or
    foreign, or from a different build -- so it is discarded rather than appended
    to. That check is what stops a reaped download of the OLD model being spliced
    onto the new one and producing a file of exactly the right size and wrong in
    the middle.

    Resume is only attempted when the total length is known. Without it there is
    no way to tell "a prefix of the right file" from "the whole of the wrong
    one", and a Range request past a shorter file's end is a 416 rather than
    useful bytes.
    """
    if expect_bytes <= 0:
        return 0
    try:
        if not part.is_file():
            return 0
        size = part.stat().st_size
    except OSError:
        return 0
    if size <= 0 or size >= expect_bytes:
        return 0
    # The bytes already on disk are hashed into the same digest the tail will
    # extend, so "sha256 of what we wrote" stays "sha256 of the whole file".
    try:
        with open(part, "rb") as handle:
            for chunk in iter(lambda: handle.read(_BLOCK), b""):
                digest.update(chunk)
    except OSError:
        return 0
    return size


def _download(url: str, dest: Path, expect_bytes: int, headers: dict | None = None,
              timeout: int = 60) -> tuple[int, str]:
    """Stream url into dest, hashing as it goes. Returns (bytes, sha256).

    Downloads to a ``.part`` next to the target and renames only after the whole
    body has arrived, so a reaped session can never leave a half file under the
    real name. The hash is computed on the same stream that is written.

    A ``.part`` left by an earlier session is RESUMED with an HTTP Range request
    rather than thrown away: the edge Space and the hub both serve ``bytes=N-``,
    so a run reaped 1.6 GB into a download pays only for the tail. If the server
    ignores the Range and answers 200, the digest is restarted from scratch --
    otherwise "verified" would describe only the part of the file that arrived
    this time.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    request_headers = dict(headers or {})

    written = _resume_prefix(part, expect_bytes, digest)
    if written:
        log(f"resuming {part.name} at {written} bytes")
        request_headers["Range"] = f"bytes={written}-"

    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = response.status
        if status not in (200, 206):
            raise GgufError(f"{url} answered {status}")
        if status == 200 and written:
            # Range ignored: the body is the whole file and what is on disk is
            # not a prefix of it. Start the digest over rather than append.
            log(f"{part.name}: server ignored Range; downloading from 0")
            digest = hashlib.sha256()
            written = 0
        mode = "ab" if (status == 206 and written) else "wb"
        try:
            with open(part, mode) as handle:
                while True:
                    chunk = response.read(_BLOCK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
            # A server that sends more than it advertised gets truncated here
            # instead of silently inflating the file past what was expected.
            if expect_bytes and written != expect_bytes:
                raise GgufError(
                    f"{dest.name}: got {written} bytes, roster says {expect_bytes}")
            # Same filesystem, so the rename is atomic: "complete" and "visible"
            # are the same instant and nothing can load a partial model.
            os.replace(part, dest)
        finally:
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass
    return written, digest.hexdigest()


def _reject(candidate: Path, reason: str) -> None:
    """Delete a model whose bytes did not match, and say so loudly.

    Deleting is the point. A wrong-size or wrong-digest file left in place is
    accepted by the next run's size check, re-verified, re-rejected, and
    re-downloaded every single time -- and in the meantime every reader of that
    path has to wonder whether it is safe to load. We do not know what these
    bytes are, so we must not let anything use them.
    """
    try:
        size = candidate.stat().st_size
        candidate.unlink()
        log(f"DELETED {candidate} ({size} bytes): {reason}")
    except OSError as exc:
        log(f"could not delete {candidate} ({exc}); it must be removed by hand "
            f"before this model will fetch again")


def _compare(path: Path, entry: dict, actual_size: int, actual_sha: str) -> None:
    """Size and digest check for a resolved .gguf, or raise GgufError.

    Always checks SIZE first, even when a digest is known: it is free, and it
    turns "the download was truncated" into a sentence that says so instead of a
    digest mismatch that names 1.6 GB of perfectly good bytes as corrupt.

    The digest is only enforced when the roster says it is VERIFIED. That flag
    exists because an absent digest and a digest nobody confirmed must not look
    alike, and a wrong digest is far worse than a missing one: it fails
    verification forever and names a value nobody recognises.

    A missing digest is a warning, not a failure. The file is size-checked and
    used, and the weakening is logged every time -- an unverified model that is
    99% right beats no model at all on a run with a fixed time budget, but the
    reader should know that is the trade being made.
    """
    expect_size = int(entry.get("size_bytes", 0) or 0)
    if expect_size and actual_size != expect_size:
        _reject(path, f"size {actual_size}, roster says {expect_size}")
        raise GgufError(
            f"{path.name}: size {actual_size}, roster says {expect_size} "
            f"(file removed; re-run to fetch it again)")

    want = str(entry.get("sha256", "") or "")
    if not entry.get("sha256_verified") or not want:
        log(f"{path.name}: no verified digest in the roster; size-checked only")
        return

    if str(actual_sha).lower() != want.lower():
        _reject(path, f"digest {str(actual_sha)[:16]}..., roster says {want[:16]}...")
        raise GgufError(
            f"{path.name}: sha256 {actual_sha}, roster says {want} "
            f"(file removed; re-run to fetch it again)")
    log(f"{path.name}: sha256 verified")


def verify(path: Path, entry: dict) -> None:
    """Verify a model that was ALREADY on disk. Raises GgufError on mismatch.

    Used for the mounted and cache hits, which are the cases where skipping the
    check is most tempting and most dangerous: the file was correct when it was
    written, and "correct when it was written" is exactly the claim a truncated
    download makes. The read is streamed; a 1.6 GB model is never held in RAM.
    """
    _compare(path, entry, path.stat().st_size, hf_store.sha256_file(path))


def _verify_downloaded(path: Path, entry: dict, written: int, digest: str) -> None:
    """Verify using the digest computed DURING the transfer.

    Same check as :func:`verify`, one pass instead of two. Re-reading 1.6 GB off
    the disk immediately after writing it costs real seconds on a Kaggle volume
    and can find bytes that changed underneath us if anything else on the box is
    writing there.
    """
    _compare(path, entry, written, digest)


def _fetch_from_edge(entry: dict, home: Path, edge_url: str) -> Path | None:
    """Pull one .gguf from the T_Dubber edge Space, or None on any miss.

    Reads X-Content-Sha256 off the response and uses it as the expected digest
    when the roster has none. That is the whole reason the route exists: the
    client decides what to verify against BEFORE it spends the bandwidth, rather
    than after.

    Any failure is a miss, not an exception. A Space that is asleep, absent, or
    simply does not carry this model is a normal condition and the hub is right
    there. The one thing this function never does is fail silently without a
    log line -- that is the bug the connector exists to prevent.
    """
    dest = home / "edge-gguf" / entry["file"]
    url = f"{edge_url.rstrip('/')}{GGUF_ROUTE}{entry['file']}"
    log(f"edge {url}")
    try:
        # HEAD first: one small round trip buys the digest, so a file that is on
        # the Space but wrong on disk is refused in milliseconds instead of
        # after a 1.6 GB transfer.
        head = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(head, timeout=30) as response:
            if response.status != 200:
                log(f"edge HEAD {entry['file']} answered {response.status}")
                return None
            served_sha = (response.headers.get(SHA_HEADER) or "").strip()
            served_len = int(response.headers.get("Content-Length") or 0)
    except Exception as exc:  # noqa: BLE001 - a miss must stay a miss
        log(f"edge miss {url}: {exc}")
        return None

    expected = dict(entry)
    if served_sha:
        expected["sha256"] = served_sha
        expected["sha256_verified"] = True
        if expected.get("sha256_source") in (None, ""):
            expected["sha256_source"] = f"edge {SHA_HEADER} header"
    if served_len:
        expected["size_bytes"] = served_len

    try:
        written, digest = _download(
            url, dest, int(entry.get("size_bytes", 0) or 0))
        _verify_downloaded(dest, expected, written, digest)
        return dest
    except GgufError as exc:
        log(f"edge fetch of {entry['file']} failed: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        log(f"edge fetch of {entry['file']} failed: {exc}")
        return None


def _fetch_from_hub(entry: dict, home: Path, token: str | None,
                    revision: str = "") -> Path:
    """Last resort: the hub. Raises GgufError rather than returning None.

    By this point the mounted copy, the cache and the edge Space have all said
    no, and the caller asked for this model by name. A None here would be a
    silent "carry on without it", which for a translation model means a run that
    produces no subtitles and reports success.
    """
    dest = home / "gguf" / entry["file"]
    url = hub_url(entry, revision)
    log(f"hub {url}")
    headers = {}
    resolved = hf_store.resolve_token(token)
    if resolved:
        headers["Authorization"] = f"Bearer {resolved}"
    try:
        written, digest = _download(
            url, dest, int(entry.get("size_bytes", 0) or 0), headers)
    except urllib.error.HTTPError as exc:
        raise GgufError(
            f"{url} answered {exc.code}. A gated repo needs HF_TOKEN "
            f"(see huggingface/.env.example).") from exc
    except Exception as exc:  # noqa: BLE001
        raise GgufError(f"could not fetch {url}: {exc}") from exc
    _verify_downloaded(dest, entry, written, digest)
    return dest


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ensure_gguf(model_id: str = "", *, task: str = "", mounted_roots=None,
                hf_home: str | Path | None = None, edge_url: str | None = None,
                token: str | None = None, revision: str = "",
                roster: dict | None = None) -> Path:
    """Resolve a .gguf to a local file, verified. Raises GgufError on failure.

    ``model_id`` wins over ``task``. With neither, ``task`` defaults to "llm",
    because the translation model is the one a dubbing run cannot proceed
    without.

    The four steps, cheapest first -- the same order hf_store.ensure_model uses,
    for the same reasons:

      1. mounted  -- a Kaggle input dataset, already on local disk, costs zero
      2. hf_home  -- this session's or a previous session's cache
      3. edge     -- the repo's own Space, via /gguf/<file>
      4. hub      -- huggingface.co/resolve/<rev>/<file>

    Steps 1 and 2 still VERIFY before returning. A cache hit is the case where
    skipping that is most tempting and most dangerous: the file was correct when
    it was written, and "correct when it was written" is exactly the claim a
    truncated download makes.
    """
    doc = roster if roster is not None else load_roster()
    entry = lookup(model_id, doc) if model_id else default_model(task or "llm", doc)
    if not entry:
        known = ", ".join(sorted(m.get("id", "?") for m in doc.get("models", [])))
        raise GgufError(
            f"no gguf model {model_id!r} in the roster. Known: {known or '(none)'}")

    home = Path(hf_home) if hf_home else hf_store.setup_env()
    file_name = entry.get("file", "")
    if not file_name:
        raise GgufError(f"roster entry {entry.get('id')!r} has no file")

    # 1. Mounted.
    hit = _mounted_file(file_name, mounted_roots)
    if hit:
        log(f"mounted {hit}")
        verify(hit, entry)
        return hit

    # 2. HF_HOME.
    hit = _cached_file(entry, home)
    if hit:
        log(f"hf_home {hit}")
        verify(hit, entry)
        return hit

    # 3. edge Space.
    edge = edge_url or os.environ.get("TDUBBER_EDGE_URL", "").strip()
    if edge:
        fetched = _fetch_from_edge(entry, home, edge)
        if fetched:
            return fetched
        log(f"edge did not have {file_name}; falling through to the hub")

    # 4. hub.
    return _fetch_from_hub(entry, home, token, revision)


def warm_status(*, mounted_roots=None, hf_home: str | Path | None = None,
                roster: dict | None = None) -> list[dict]:
    """Where each roster entry already is, so a slow run says WHY.

    Same shape as hf_store.warm_status so the worker can print one table for
    torch models and one for gguf models without special-casing either.

    "missing" here does not mean "will be downloaded from the hub": the edge
    Space may well have it. It means "not on local disk", and the resolver's next
    two steps are cheap.
    """
    home = Path(hf_home) if hf_home else hf_store.setup_env()
    rows = []
    for entry in (roster if roster is not None else load_roster()).get("models", []):
        file_name = entry.get("file", "")
        source, note = "missing", ""
        hit = _mounted_file(file_name, mounted_roots) if file_name else None
        if hit:
            source = "mounted"
        else:
            hit = _cached_file(entry, home) if file_name else None
            if hit:
                source = "hf_home"
                # The report may disagree with the resolver; it must not. A cache
                # hit whose digest does not match is reported as missing, because
                # that is what ensure_gguf will do about it.
                want = str(entry.get("sha256", "") or "")
                if entry.get("sha256_verified") and want:
                    try:
                        if hf_store.sha256_file(hit).lower() != want.lower():
                            source = "missing"
                            note = f"{hit.name} present but digest differs -- will re-fetch"
                    except OSError as exc:
                        note = f"unreadable: {exc}"
        rows.append({
            "id": entry.get("id", ""),
            "repo": entry.get("repo", ""),
            "file": file_name,
            "task": entry.get("task", ""),
            "quant": entry.get("quant", ""),
            "size_bytes": entry.get("size_bytes", 0),
            "sha256_verified": bool(entry.get("sha256_verified")),
            "source": source,
            "path": str(hit) if hit and source != "missing" else "",
            "note": note,
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    hf_store.setup_env()
    mounted = [p for p in (os.environ.get("TDUBBER_MOUNT_ROOTS", "/kaggle/input"),)
               if p]

    if argv and argv[0] not in ("--status", "--ensure", "--help", "-h"):
        try:
            path = ensure_gguf(argv[0], mounted_roots=mounted)
        except GgufError as exc:
            log(f"FAILED {exc}")
            return 1
        print(path)
        return 0

    rows = warm_status(mounted_roots=mounted)
    warm = sum(1 for row in rows if row["source"] != "missing")
    print(f"gguf warm {warm}/{len(rows)}")
    for row in rows:
        gb = row["size_bytes"] / (1 << 30)
        flag = "verified" if row["sha256_verified"] else "NO DIGEST"
        print(f"  {row['source']:8s} {row['id']:24s} {gb:5.2f}GiB "
              f"[{row['task']}/{row['quant']}] {flag}")
        if row.get("note"):
            print(f"           note: {row['note']}")

    if "--ensure" in argv:
        for row in rows:
            if row["source"] == "missing":
                try:
                    print(f"  ensure {row['id']} -> {ensure_gguf(row['id'], mounted_roots=mounted)}")
                except GgufError as exc:
                    print(f"  ensure {row['id']} FAILED: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
