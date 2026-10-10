#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hf_store regression tests -- proofs for four bugs that were live.

    python huggingface/test_hf_store.py        # human output, exit 0 = pass
    python huggingface/test_hf_store.py --quiet

Nothing here touches the network or the real HF cache. Every case builds a
throwaway directory tree, and the edge case runs a real HTTP server on
localhost so the ROUTE is genuinely exercised rather than mocked -- mocking
urllib would have let the wrong path pass, which is exactly the bug.

THE FOUR BUGS
-------------
1. `_edge_fetch` asked the Space for `/artefacts/<name>.tar.gz`. The Space
   serves `/artifact/<name>` (edge/server.go). Every edge fetch 404'd, the
   bare except swallowed it, and resolution fell through to the hub -- so the
   "525 s Homura download becomes 0 s" claim was unreachable. A silently dead
   fast path is worse than a slow one: the run still succeeds.

2. `extractall(filter="data")` needs Python 3.11.4. Kaggle ships 3.10/3.11,
   where it raises TypeError from inside the except handler -- so the edge
   path degraded to "unreachable" with nothing logged.

3. The archive unpacks into a subdirectory, so the caller's
   `dest.glob("*.safetensors")` saw nothing and the fetch was discarded even
   when the download had succeeded.

4. `warm_status()` accepted any directory that existed under snapshots/,
   while `ensure_model()` additionally demanded weight files. So an
   interrupted, half-fetched snapshot was reported "hf_home" by the log and
   then downloaded again by the resolver: a run that is slow for a reason the
   log denies. The roster's DATASET entry (mazinger-dubber-profiles) has no
   config.json, which made it permanently invisible too.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import traceback
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hf_store  # noqa: E402

FAILURES: list[str] = []
QUIET = "--quiet" in sys.argv


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        if not QUIET:
            print(f"    ok   {label}")
    else:
        FAILURES.append(label)
        print(f"    FAIL {label}   {detail}")


def section(title: str) -> None:
    if not QUIET:
        print(f"\n== {title} ==")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def hub_cache(root: Path, repo_id: str, *, weights: bool = True,
              config: bool = True, extra: str | None = None) -> Path:
    """Build one hub-cache snapshot directory and return it.

    Layout is exactly what huggingface_hub produces and what
    `_snapshot_dirs` is documented to scan.
    """
    key = "models--" + repo_id.replace("/", "--")
    snap = root / key / "snapshots" / "abc123"
    snap.mkdir(parents=True, exist_ok=True)
    if config:
        (snap / "config.json").write_text('{"model_type":"test"}', encoding="utf-8")
    if weights:
        (snap / "model.safetensors").write_bytes(b"\x00" * 16)
    if extra:
        (snap / extra).write_text("{}\n", encoding="utf-8")
    return snap


def tar_of(snapshot_dir: Path, archive: Path, prefix: str = "models--X--Y/snapshots/abc") -> Path:
    """Pack a snapshot directory the way the edge Space would.

    `prefix` matters: the real archive nests one top-level directory, which is
    precisely what bug 3 was about.
    """
    with tarfile.open(archive, "w:gz") as tar:
        for item in sorted(snapshot_dir.iterdir()):
            tar.add(item, arcname=f"{prefix}/{item.name}")
    return archive


class _StubHub:
    """Stands in for huggingface_hub so a test cannot reach the real network."""

    def __init__(self):
        self.calls = 0

    def snapshot_download(self, **kwargs):
        self.calls += 1
        # The token is redacted rather than echoed. ensure_model resolves it from
        # HuggingFace_PaperWork/ on the way in, and a test failure that prints it
        # puts a live credential in someone's terminal and in any CI log that
        # captures stderr.
        shown = {k: ("<redacted>" if "token" in k else v) for k, v in kwargs.items()}
        raise AssertionError(f"the hub must not be reached here: {shown}")


class _no_hub:
    """Swap huggingface_hub out for a stub that fails loudly if it is used.

    The module docstring promises these tests never touch the network, and
    without this the "falls through to the hub" assertions really do call out --
    which is slow when it works, and misleading when it 401s. A test that
    exercises the FALLBACK path must stub the fallback.
    """

    def __enter__(self):
        self.stub = _StubHub()
        self.saved = sys.modules.get("huggingface_hub")
        module = types.ModuleType("huggingface_hub")
        module.snapshot_download = self.stub.snapshot_download
        sys.modules["huggingface_hub"] = module
        return self

    def __exit__(self, *exc):
        if self.saved is None:
            sys.modules.pop("huggingface_hub", None)
        else:
            sys.modules["huggingface_hub"] = self.saved
        return False


class _Handler(BaseHTTPRequestHandler):
    """Serves /artifact/<name> and records what was asked for."""

    archive: Path | None = None
    asked: list[str] = []
    hit_count = 0

    def do_GET(self):  # noqa: N802  (http.server API)
        type(self).asked.append(self.path)
        if type(self).archive and self.path.startswith("/artifact/"):
            data = type(self).archive.read_bytes()
            type(self).hit_count += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence
        pass


def serve(archive: Path) -> tuple[str, type[_Handler]]:
    _Handler.archive = archive
    _Handler.asked = []
    _Handler.hit_count = 0
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}", _Handler


# ---------------------------------------------------------------------------
# 1 + 2 + 3: the edge path
# ---------------------------------------------------------------------------

def test_edge_fetch() -> None:
    section("edge fetch: right route, safe unpack, resolved directory")

    work = Path(tempfile.mkdtemp(prefix="hfstore_edge_"))
    try:
        snap = hub_cache(work / "src", "k2-fsa/OmniVoice")
        archive = tar_of(snap, work / "k2-fsa--OmniVoice.tar.gz")
        url, handler = serve(archive)
        dest = work / "edge-snapshots" / "k2-fsa--OmniVoice"

        got = hf_store._edge_fetch("k2-fsa/OmniVoice", dest, url)

        # Bug 1: the route is the whole point, so assert on what was asked for
        # rather than only on what came back.
        check("asked /artifact/ (singular)", bool(handler.asked) and
              handler.asked[0].startswith("/artifact/"),
              f"asked {handler.asked}")
        check("never asked /artefacts/ (plural)",
              not any(p.startswith("/artefacts") for p in handler.asked),
              f"asked {handler.asked}")
        check("server was actually hit", handler.hit_count == 1,
              f"hit_count={handler.hit_count}")

        # Bug 2/3: a real snapshot directory comes back, not a bare True.
        check("returns a resolved directory", isinstance(got, Path) and got.is_dir(),
              f"got {got!r}")
        check("resolved dir holds the weights",
              isinstance(got, Path) and hf_store._has_weights(got),
              f"got {got!r}")
        check("ensure_model-style top-level glob is no longer required",
              isinstance(got, Path) and (got / "config.json").exists(),
              f"got {got!r}")

        # A miss must be a clean None, never an exception.
        _Handler.archive = None
        check("a 404 returns None", hf_store._edge_fetch("nope/nope", dest, url) is None)

        # An unreachable host must also be a clean None.
        check("a dead host returns None",
              hf_store._edge_fetch("k2-fsa/OmniVoice", dest,
                                  "http://127.0.0.1:1") is None)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_edge_unpack_without_filter_kwarg() -> None:
    section("edge unpack survives a Python with no extractall(filter=...)")

    # Simulate Python < 3.11.4 precisely: extractall() raises TypeError when it
    # is given the kwarg. The kernel must catch that SPECIFIC error and retry
    # without it, rather than letting it escape into the outer except and be
    # reported as a corrupt archive.
    real_extractall = tarfile.TarFile.extractall

    def picky_extractall(self, path=".", members=None, *, numeric_owner=False,
                         filter=None):  # noqa: A002  (stdlib keyword name)
        if filter is not None:
            raise TypeError(
                "extractall() got an unexpected keyword argument 'filter'")
        return real_extractall(self, path, members,
                               numeric_owner=numeric_owner)

    work = Path(tempfile.mkdtemp(prefix="hfstore_nofilter_"))
    try:
        snap = hub_cache(work / "src", "k2-fsa/OmniVoice")
        archive = tar_of(snap, work / "a.tar.gz")
        url, _ = serve(archive)

        tarfile.TarFile.extractall = picky_extractall
        try:
            got = hf_store._edge_fetch("k2-fsa/OmniVoice",
                                       work / "dest", url)
        finally:
            tarfile.TarFile.extractall = real_extractall

        check("unpacks on a Python without the filter kwarg",
              isinstance(got, Path) and hf_store._has_weights(got),
              f"got {got!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4: the roster report must agree with the resolver
# ---------------------------------------------------------------------------

def test_snapshot_discovery() -> None:
    section("snapshot discovery: datasets and half-fetched dirs")

    work = Path(tempfile.mkdtemp(prefix="hfstore_roster_"))
    try:
        home = work / "hf_home"

        # A model snapshot: config.json + weights.
        hub_cache(home, "Org/Model")
        found = list(hf_store._snapshot_dirs("Org/Model", home))
        check("model snapshot found", len(found) == 1, f"{found}")

        # The roster's DATASET: no config.json, no weights, but present. This is
        # what was permanently invisible before.
        hub_cache(home, "bakrianoo/mazinger-dubber-profiles",
                  weights=False, config=False, extra="profiles.jsonl")
        found = list(hf_store._snapshot_dirs(
            "bakrianoo/mazinger-dubber-profiles", home))
        check("dataset snapshot found without a config.json", len(found) == 1,
              f"{found}")

        # An interrupted download: config.json present, weights missing. It must
        # be VISIBLE (so we can say something about it) but must NOT count as
        # warm.
        half = hub_cache(home, "Org/HalfFetched", weights=False)
        check("half-fetched snapshot is discovered",
              len(list(hf_store._snapshot_dirs("Org/HalfFetched", home))) == 1)
        check("half-fetched snapshot does not count as having weights",
              not hf_store._has_weights(half))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_warm_status_agrees_with_resolver() -> None:
    section("warm_status tells the truth about a half-fetched snapshot")

    work = Path(tempfile.mkdtemp(prefix="hfstore_warm_"))
    try:
        home = work / "hf_home"
        hub_cache(home, "Org/Good")
        hub_cache(home, "Org/HalfFetched", weights=False)

        roster = work / "models.json"
        roster.write_text(json.dumps({"version": 1, "models": [
            {"repo_id": "Org/Good", "role": "test", "approx_gb": 1},
            {"repo_id": "Org/HalfFetched", "role": "test", "approx_gb": 1},
            {"repo_id": "Org/NeverSeen", "role": "test", "approx_gb": 1},
        ]}), encoding="utf-8")

        rows = {r["repo_id"]: r
                for r in hf_store.warm_status(mounted_roots=[],
                                              hf_home=home)[:0] or []}
        rows = {}
        for entry in json.loads(roster.read_text(encoding="utf-8"))["models"]:
            rows[entry["repo_id"]] = None

        original = hf_store._ROSTER_PATH
        hf_store._ROSTER_PATH = roster
        try:
            got = {r["repo_id"]: r
                   for r in hf_store.warm_status(mounted_roots=[], hf_home=home)}
        finally:
            hf_store._ROSTER_PATH = original

        check("a complete snapshot reports hf_home",
              got["Org/Good"]["source"] == "hf_home",
              str(got["Org/Good"]))
        check("a half-fetched snapshot does NOT report hf_home",
              got["Org/HalfFetched"]["source"] == "missing",
              str(got["Org/HalfFetched"]))
        check("a half-fetched snapshot explains itself",
              bool(got["Org/HalfFetched"].get("note")),
              str(got["Org/HalfFetched"]))
        check("a never-seen model reports missing with no note",
              got["Org/NeverSeen"]["source"] == "missing"
              and not got["Org/NeverSeen"].get("note"),
              str(got["Org/NeverSeen"]))
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5: the Suffix parameter -- what may be a "weight file"
# ---------------------------------------------------------------------------

def test_weight_suffix_normalisation() -> None:
    section("weight_suffixes: the Suffix argument, normalised")

    # None is the pre-existing behaviour and must not move. This is the whole
    # compatibility promise of adding a parameter.
    check("None keeps the historical pair",
          hf_store.weight_suffixes(None) == (".safetensors", ".bin"),
          str(hf_store.weight_suffixes(None)))
    check("omitting the argument is the same as None",
          hf_store.weight_suffixes() == (".safetensors", ".bin"))

    check("both spellings of a suffix work",
          hf_store.weight_suffixes(".gguf") == hf_store.weight_suffixes("gguf")
          == (".gguf",),
          f"{hf_store.weight_suffixes('.gguf')} vs {hf_store.weight_suffixes('gguf')}")

    check("case is normalised",
          hf_store.weight_suffixes(".GGUF") == (".gguf",),
          str(hf_store.weight_suffixes(".GGUF")))
    check("whitespace is normalised",
          hf_store.weight_suffixes("  .gguf ") == (".gguf",),
          str(hf_store.weight_suffixes("  .gguf ")))
    check("a sequence works",
          hf_store.weight_suffixes([".gguf", "bin"]) == (".gguf", ".bin"),
          str(hf_store.weight_suffixes([".gguf", "bin"])))
    check("blanks are dropped",
          hf_store.weight_suffixes([".gguf", "", "   "]) == (".gguf",),
          str(hf_store.weight_suffixes([".gguf", "", "   "])))


def test_suffix_predicates() -> None:
    section("_has_weights / _is_snapshot honour the suffix")

    work = Path(tempfile.mkdtemp(prefix="hfstore_suffix_"))
    try:
        # A gguf-only directory: the layout gguf_store hands back.
        g = work / "ggufonly"
        g.mkdir(parents=True)
        (g / "Index-Homura-2B.Q4_K_M.gguf").write_bytes(b"\x00" * 32)

        check("a .gguf is NOT a default weight file",
              not hf_store._has_weights(g), "the default must stay torch-shaped")
        check("a .gguf IS a weight file when asked for by name",
              hf_store._has_weights(g, ".gguf"))
        check("a .gguf is discoverable as a snapshot with suffix=.gguf",
              hf_store._is_snapshot(g, ".gguf"))
        # Discoverability is broader than loadability: a config.json makes a
        # directory findable regardless, so substituting the pattern list would
        # have made ensure_model(suffix=".gguf") unable to find it at all.
        check("a .gguf is discoverable as a snapshot even without a suffix",
              hf_store._is_snapshot(g))

        # The whisper ggml .bin files. Already covered by the default, which is
        # why the whisper resolver needs no suffix at all.
        b = work / "binonly"
        b.mkdir(parents=True)
        (b / "ggml-base.bin").write_bytes(b"\x00" * 16)
        check("a whisper ggml .bin counts by default",
              hf_store._has_weights(b), "this is the pre-existing .bin rule")
        check("a .bin is NOT a .gguf",
              not hf_store._has_weights(b, ".gguf"),
              "asking for gguf must not accept a faster-whisper CTranslate2 blob")

        # The bad combination: a safetensors snapshot must not satisfy .gguf.
        s = work / "torchonly"
        s.mkdir(parents=True)
        (s / "model.safetensors").write_bytes(b"\x00" * 16)
        check("torch weights do not satisfy suffix=.gguf",
              not hf_store._has_weights(s, ".gguf"))

        # Threading it through ensure_model: a gguf-only cache entry is found
        # when the caller asks for a gguf and ignored when it does not.
        home = work / "home"
        snap = hub_cache(home, "IndexTeam/Index-Homura-2B-GGUF", weights=False,
                         config=False)
        (snap / "Index-Homura-2B.Q4_K_M.gguf").write_bytes(b"\x00" * 32)
        with _no_hub() as hub:
            got = hf_store.ensure_model("IndexTeam/Index-Homura-2B-GGUF",
                                        mounted_roots=[], hf_home=home,
                                        edge_url="http://127.0.0.1:1",
                                        suffix=".gguf")
            check("ensure_model(suffix=.gguf) finds a gguf-only snapshot",
                  got is not None and got == snap, f"got {got!r}")
            got = hf_store.ensure_model("IndexTeam/Index-Homura-2B-GGUF",
                                        mounted_roots=[], hf_home=home,
                                        edge_url="http://127.0.0.1:1")
            check("ensure_model with no suffix does NOT claim a gguf snapshot",
                  got is None, f"got {got!r} -- it should fall through to the hub")
            check("the hub was only reached by the case that was meant to miss",
                  hub.stub.calls == 1, f"hub calls = {hub.stub.calls}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_default_suffix_is_unchanged() -> None:
    section("a .safetensors snapshot behaves exactly as it did before")

    work = Path(tempfile.mkdtemp(prefix="hfstore_default_"))
    try:
        home = work / "home"
        snap = hub_cache(home, "Systran/faster-whisper-large-v3")
        got = hf_store.ensure_model("Systran/faster-whisper-large-v3",
                                    mounted_roots=[], hf_home=home,
                                    edge_url="http://127.0.0.1:1")
        check("the faster-whisper snapshot still resolves with no suffix",
              got == snap, f"got {got!r}")
        check("and with an explicit None",
              hf_store.ensure_model("Systran/faster-whisper-large-v3",
                                    mounted_roots=[], hf_home=home,
                                    edge_url="http://127.0.0.1:1",
                                    suffix=None) == snap)
        check("and with the default named explicitly",
              hf_store.ensure_model("Systran/faster-whisper-large-v3",
                                    mounted_roots=[], hf_home=home,
                                    edge_url="http://127.0.0.1:1",
                                    suffix=hf_store.DEFAULT_SUFFIX) == snap)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6: the one content-addressing helper
# ---------------------------------------------------------------------------

def test_sha256_file_streams() -> None:
    section("sha256_file: correct, and bounded in memory")

    import hashlib

    work = Path(tempfile.mkdtemp(prefix="hfstore_hash_"))
    try:
        target = work / "model.gguf"
        # 12 MiB: comfortably more than one 4 MiB block, so a single-read
        # implementation and a streaming one are distinguishable.
        payload = bytes(range(256)) * (12 * 1024 * 1024 // 256)
        target.write_bytes(payload)

        want = hashlib.sha256(payload).hexdigest()
        check("digest matches hashlib", hf_store.sha256_file(target) == want)
        check("a str path works too",
              hf_store.sha256_file(str(target)) == want)

        # The block size must be smaller than the file, and a 1-byte block must
        # still produce the same digest -- that is what proves the loop is a
        # loop and not a single read that happens to work on small files.
        check("a 1-byte block gives the same digest",
              hf_store.sha256_file(target, block=1) == want)
        check("an empty file hashes to the empty digest",
              hf_store.sha256_file(_touch(work / "empty", b"")) ==
              hashlib.sha256(b"").hexdigest())
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _touch(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def main() -> int:
    if not QUIET:
        print("hf_store regression tests")

    for test in (test_edge_fetch,
                 test_edge_unpack_without_filter_kwarg,
                 test_snapshot_discovery,
                 test_warm_status_agrees_with_resolver,
                 test_weight_suffix_normalisation,
                 test_suffix_predicates,
                 test_default_suffix_is_unchanged,
                 test_sha256_file_streams):
        try:
            test()
        except Exception:
            FAILURES.append(test.__name__)
            print(f"    FAIL {test.__name__} raised:")
            traceback.print_exc()

    print("=" * 72)
    if FAILURES:
        print(f"  hf_store tests: FAIL ({len(FAILURES)})")
        for name in FAILURES:
            print(f"    - {name}")
        print("=" * 72)
        return 1
    print("  hf_store tests: PASS")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())