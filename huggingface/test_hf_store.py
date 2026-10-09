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


def main() -> int:
    if not QUIET:
        print("hf_store regression tests")

    for test in (test_edge_fetch,
                 test_edge_unpack_without_filter_kwarg,
                 test_snapshot_discovery,
                 test_warm_status_agrees_with_resolver):
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