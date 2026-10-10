#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gguf_store regression tests -- proofs, not just coverage.

    python huggingface/test_gguf_store.py         # human output, exit 0 = pass
    python huggingface/test_gguf_store.py --quiet

Nothing here touches the real network or the real HF cache. Every case builds a
throwaway directory tree, and the fetch cases run real HTTP servers on localhost
so the ROUTES are genuinely exercised rather than mocked. Mocking urllib is
exactly what hid the original ``/artefacts/`` vs ``/artifact/`` bug for weeks:
a mocked client agrees with whatever shape the test author believed.

WHAT IS BEING PROTECTED
-----------------------
1. The roster mirror (``models_gguf.json``) still agrees with the Go table in
   ``edge/model_gguf.go``: same ids, same digests, same defaults. A drift here
   is a model that verifies on one side and never verifies on the other.
2. The URL shapes. ``/gguf/<file>`` -- singular, task-shaped -- and
   ``<hub>/<repo>/resolve/<rev>/<file>``. This is pinned against the same
   strings Go's ``TestURLShapes`` pins, from the other language.
3. The four-step precedence chain (mounted -> HF_HOME -> edge -> hub) is the
   SAME chain hf_store uses, in the same order. A cache hit must not be
   re-downloaded, and a miss must fall through rather than fail.
4. Graceful degradation. A dead Space, a 404, a digest the server got wrong:
   each is a clean miss, never a crash -- except at the hub, where a caller that
   asked for a model by name gets GgufError rather than a silent "carry on
   without the translation model".
5. Range resume. A ``.part`` left by a reaped session is continued, not thrown
   away, and the digest still covers the whole file rather than the tail.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gguf_store  # noqa: E402

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
# Fixtures
# ---------------------------------------------------------------------------

def entry(model_id: str, file_name: str, payload: bytes, *,
          repo: str = "Org/Model", task: str = "llm", quant: str = "Q4_K_M",
          digest: str | None = None, size: int | None = None,
          verified: bool | None = None, source: str = "test") -> dict:
    """One roster row whose digest/size are true for ``payload`` by default."""
    sha = hashlib.sha256(payload).hexdigest() if digest is None else digest
    row = {
        "id": model_id,
        "repo": repo,
        "file": file_name,
        "quant": quant,
        "task": task,
        "sha256": sha,
        "sha256_verified": (bool(sha) if verified is None else verified),
        "sha256_source": source,
        "size_bytes": len(payload) if size is None else size,
        "ctx_tokens": 1500,
        "notes": "",
    }
    return row


def roster_of(*rows: dict) -> dict:
    return {"version": 1, "models": list(rows)}


@contextlib.contextmanager
def _tiny_block(size: int = 8):
    """Shrink the download block so a multi-chunk stream is a few bytes.

    The production block is 4 MiB. Keeping it would make every streaming and
    resume test allocate a 4 MB buffer, and would make a "did the loop really
    loop" assertion impossible to write on a small payload.
    """
    saved = gguf_store._BLOCK
    gguf_store._BLOCK = size
    try:
        yield
    finally:
        gguf_store._BLOCK = saved


@contextlib.contextmanager
def _env(**pairs):
    """Set/unset environment variables, restoring the exact prior state."""
    saved = {k: os.environ.get(k) for k in pairs}
    try:
        for key, value in pairs.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _Handler(BaseHTTPRequestHandler):
    """One configurable blob server: edge (/gguf/) or hub (/resolve/)."""

    payload: bytes = b""
    digest: str = ""
    prefixes: tuple[str, ...] = ()
    ignore_range = False
    advertise_sha: str | None = None  # None -> the true digest
    asked: list[str] = []
    ranges: list[str] = []
    hits = 0

    def log_message(self, *args):  # silence
        pass

    def do_GET(self):  # noqa: N802  (http.server API)
        self._serve(head=False)

    def do_HEAD(self):  # noqa: N802
        self._serve(head=True)

    def _serve(self, head: bool) -> None:
        type(self).asked.append(self.path)
        if not any(self.path.startswith(p) for p in type(self).prefixes):
            self.send_response(404)
            self.end_headers()
            return

        data = type(self).payload
        sha = type(self).digest if type(self).advertise_sha is None else type(self).advertise_sha
        start = 0
        status = 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes=") and not type(self).ignore_range:
            type(self).ranges.append(rng)
            start = int(rng[len("bytes="):].split("-")[0])
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return
            status = 206

        body = data[start:]
        type(self).hits += 1
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        # The header under test: the client reads this BEFORE the body so it
        # knows what it will verify against.
        self.send_header("X-Content-Sha256", sha)
        self.send_header("Content-Length", str(len(body)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        self.end_headers()
        if not head and body:
            self.wfile.write(body)


@contextlib.contextmanager
def blob_server(payload: bytes, *, prefixes, digest=None, advertise_sha=None,
                ignore_range=False):
    _Handler.payload = payload
    _Handler.digest = digest if digest is not None else hashlib.sha256(payload).hexdigest()
    _Handler.prefixes = tuple(prefixes)
    _Handler.advertise_sha = advertise_sha
    _Handler.ignore_range = ignore_range
    _Handler.asked = []
    _Handler.ranges = []
    _Handler.hits = 0
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# 1: the mirror and its defaults
# ---------------------------------------------------------------------------

def test_shipped_roster() -> None:
    section("models_gguf.json: the mirror the Go test also reads")

    doc = gguf_store.load_roster()
    models = doc.get("models", [])
    check("roster has the eight shipped rows", len(models) == 8, f"got {len(models)}")
    check("roster version is 1", doc.get("version") == 1, str(doc.get("version")))

    ids = [m["id"] for m in models]
    check("ids are unique", len(ids) == len(set(ids)), str(ids))
    check("tasks are only llm/asr",
          all(m["task"] in ("llm", "asr") for m in models),
          str(sorted({m["task"] for m in models})))

    for m in models:
        sha = m.get("sha256", "")
        check(f"{m['id']}: digest is 64 lowercase hex",
              len(sha) == 64 and all(c in "0123456789abcdef" for c in sha), sha)
        check(f"{m['id']}: digest is marked verified", bool(m.get("sha256_verified")))
        check(f"{m['id']}: digest has a recorded source", bool(m.get("sha256_source")))
        check(f"{m['id']}: size is positive", int(m.get("size_bytes", 0)) > 0)

    got = gguf_store.lookup("homura-2b-q4_k_m")
    check("lookup finds a row by id", got.get("repo") == "IndexTeam/Index-Homura-2B-GGUF", str(got))
    check("lookup of an unknown id is an empty dict",
          gguf_store.lookup("no-such-model") == {})

    llm = gguf_store.default_model("llm")
    check("default llm is Q4_K_M", llm.get("id") == "homura-2b-q4_k_m" and llm.get("quant") == "Q4_K_M",
          str(llm.get("id")))
    asr = gguf_store.default_model("asr")
    check("default asr is large-v3-turbo", asr.get("id") == "whisper-large-v3-turbo",
          str(asr.get("id")))
    try:
        gguf_store.default_model("tts")
        check("an unknown task raises instead of guessing", False)
    except gguf_store.GgufError:
        check("an unknown task raises instead of guessing", True)


# ---------------------------------------------------------------------------
# 2: the URL shapes, pinned from the Python side
# ---------------------------------------------------------------------------

def test_url_shapes() -> None:
    section("URL shapes: the same strings edge/model_gguf_test.go pins")

    check("the edge route is singular /gguf/", gguf_store.GGUF_ROUTE == "/gguf/",
          gguf_store.GGUF_ROUTE)
    check("the digest header is X-Content-Sha256",
          gguf_store.SHA_HEADER == "X-Content-Sha256", gguf_store.SHA_HEADER)

    m = gguf_store.lookup("homura-2b-q4_k_m")
    want = ("https://huggingface.co/IndexTeam/Index-Homura-2B-GGUF/resolve/main/"
            "Index-Homura-2B.Q4_K_M.gguf")
    check("hub_url matches Go HubURL(\"\")", gguf_store.hub_url(m) == want,
          gguf_store.hub_url(m))
    want_rev = want.replace("/resolve/main/", "/resolve/abc123/")
    check("hub_url honours a revision", gguf_store.hub_url(m, "abc123") == want_rev,
          gguf_store.hub_url(m, "abc123"))

    asr = gguf_store.lookup("whisper-large-v3-turbo")
    check("an ASR .bin gets a hub URL too",
          gguf_store.hub_url(asr).endswith(
              "/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"),
          gguf_store.hub_url(asr))


# ---------------------------------------------------------------------------
# 3: local discovery -- mounted and every HF_HOME layout
# ---------------------------------------------------------------------------

def test_local_discovery() -> None:
    section("discovery: mounted, flat cache, hub-cache and edge-gguf layouts")

    work = Path(tempfile.mkdtemp(prefix="ggufstore_local_"))
    try:
        payload = b"GGUF-LOCAL-BYTES-0123456789"
        file_name = "Index-Homura-2B.Q4_K_M.gguf"
        roi = roster_of(entry("m", file_name, payload))
        home = work / "home"

        # 1. Mounted Kaggle input, possibly nested.
        mounted_root = work / "mnt"
        mounted_file = _write(mounted_root / "some" / "deep" / file_name, payload)
        got = gguf_store.ensure_gguf("m", mounted_roots=[mounted_root], hf_home=home,
                                     edge_url="http://127.0.0.1:1", roster=roi)
        check("mounted copy wins, zero bytes", got == mounted_file, str(got))

        # 2. Flat HF_HOME/gguf/<file>, where the edge fetch and hub land it.
        mounted_file.unlink()
        flat = _write(home / "gguf" / file_name, payload)
        got = gguf_store.ensure_gguf("m", mounted_roots=[mounted_root], hf_home=home,
                                     edge_url="http://127.0.0.1:1", roster=roi)
        check("flat HF_HOME/gguf cache hit", got == flat, str(got))

        # 3. A previous edge fetch, under edge-gguf/.
        flat.unlink()
        edge_copy = _write(home / "edge-gguf" / file_name, payload)
        got = gguf_store.ensure_gguf("m", mounted_roots=[], hf_home=home,
                                     edge_url="http://127.0.0.1:1", roster=roi)
        check("edge-gguf cache hit", got == edge_copy, str(got))

        # 4. The huggingface_hub cache layout.
        edge_copy.unlink()
        cached = _write(home / "models--Org--Model" / "snapshots" / "rev1" / file_name, payload)
        got = gguf_store.ensure_gguf("m", mounted_roots=[], hf_home=home,
                                     edge_url="http://127.0.0.1:1", roster=roi)
        check("hub-cache snapshots layout is found", got == cached, str(got))

        # A cache hit is verified, not trusted: a corrupt file is rejected and
        # deleted, and the resolver then fails (no hub reachable here).
        _write(cached, b"corrupt" + payload[7:])
        try:
            gguf_store.ensure_gguf("m", mounted_roots=[], hf_home=home,
                                   edge_url="http://127.0.0.1:1", roster=roi)
            check("a corrupt cache hit is refused", False, "it was returned")
        except gguf_store.GgufError:
            check("a corrupt cache hit is refused", True)
        check("the corrupt cached file was deleted, not left to be loaded",
              not cached.exists())
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4: the edge Space
# ---------------------------------------------------------------------------

def test_edge_fetch() -> None:
    section("edge fetch: /gguf/ route, digest header, opaque miss")

    work = Path(tempfile.mkdtemp(prefix="ggufstore_edge_"))
    try:
        payload = bytes(range(256)) * 9  # 2304 bytes, several small blocks
        file_name = "Index-Homura-2B.Q4_K_M.gguf"
        # NO digest in the roster: the whole point of /gguf/ is that the header
        # supplies one the client can verify against.
        roi = roster_of(entry("m", file_name, payload, digest="", verified=False))
        home = work / "home"

        with _tiny_block(), blob_server(payload, prefixes=("/gguf/",)) as url:
            got = gguf_store.ensure_gguf("m", mounted_roots=[], hf_home=home,
                                         edge_url=url, roster=roi)
            check("edge fetch returns the file", got == home / "edge-gguf" / file_name,
                  str(got))
            check("the fetched bytes are correct",
                  got is not None and got.read_bytes() == payload)
            # The route: singular /gguf/ and a bare filename. This is the exact
            # string Go's GgufHTTPPath builds.
            check("asked the singular /gguf/<file> route",
                  _Handler.asked and _Handler.asked[0] == f"/gguf/{file_name}",
                  str(_Handler.asked))
            check("never asked /artefact(s)/ on the edge",
                  not any("artefact" in p for p in _Handler.asked), str(_Handler.asked))
            check("HEAD-then-GET: two round trips, no body on the HEAD",
                  _Handler.hits == 2, f"hits={_Handler.hits}")

        # A 404 for a model the Space does not have. _fetch_from_edge directly,
        # because ensure_gguf would fall through to the hub and we are not
        # touching the network here.
        with blob_server(payload, prefixes=("/gguf/",)) as url:
            _Handler.prefixes = ("/nothing/",)
            miss = gguf_store._fetch_from_edge(roi["models"][0], home, url)
            check("a 404 is a clean None", miss is None, str(miss))

        # A dead host is also a clean None.
        miss = gguf_store._fetch_from_edge(roi["models"][0], home, "http://127.0.0.1:1")
        check("a dead host is a clean None", miss is None, str(miss))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_edge_wrong_digest_is_refused() -> None:
    section("edge: a wrong X-Content-Sha256 deletes the file instead of installing it")

    work = Path(tempfile.mkdtemp(prefix="ggufstore_badsha_"))
    try:
        payload = b"GGUF-REAL-BYTES"
        file_name = "Index-Homura-2B.Q4_K_M.gguf"
        roi = roster_of(entry("m", file_name, payload, digest="", verified=False))
        home = work / "home"
        dest = home / "edge-gguf" / file_name

        # The bytes are perfect; the header lies about them.
        with blob_server(payload, prefixes=("/gguf/",), advertise_sha="0" * 64) as url:
            got = gguf_store._fetch_from_edge(roi["models"][0], home, url)
        check("a digest the server got wrong is a miss", got is None, str(got))
        check("and the file is not left on disk to be loaded", not dest.exists())

        # A roster length that does not match the served body is also refused.
        roi2 = roster_of(entry("m", file_name, payload, size=len(payload) + 5,
                               digest="", verified=False))
        with blob_server(payload, prefixes=("/gguf/",)) as url:
            got = gguf_store._fetch_from_edge(roi2["models"][0], home, url)
        check("a wrong advertised size is a miss", got is None, str(got))
        check("no partial file survives a size failure", not dest.exists())
        check("no .part survives a failed fetch either",
              not (dest.parent / (file_name + ".part")).exists())
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5: resume + streaming
# ---------------------------------------------------------------------------

def test_download_resume() -> None:
    section("_download: Range resume, whole-file digest, size check")

    work = Path(tempfile.mkdtemp(prefix="ggufstore_resume_"))
    try:
        payload = bytes(range(64))  # 64 bytes, block size 8 -> 8 chunks
        want = hashlib.sha256(payload).hexdigest()
        dest = work / "dest.gguf"
        part = dest.with_name(dest.name + ".part")

        with _tiny_block(8), blob_server(payload, prefixes=("/blob",)) as url:
            # Seed three bytes' worth of a reaped download.
            _write(part, payload[:24])
            written, got = gguf_store._download(url + "/blob", dest, len(payload))
            check("resumed to the full length", written == len(payload), str(written))
            check("the resumed file is byte-correct", dest.read_bytes() == payload)
            check("the digest covers the whole file, prefix included", got == want, got)
            check("a Range request was actually sent",
                  _Handler.ranges and _Handler.ranges[0] == "bytes=24-",
                  str(_Handler.ranges))
            check("the .part is gone after the rename", not part.exists())

        # A server that ignores Range must not corrupt the result: the client
        # restarts the digest from zero rather than appending a full body to a
        # prefix of it.
        dest.unlink(missing_ok=True)
        with _tiny_block(8), blob_server(payload, prefixes=("/blob",), ignore_range=True) as url:
            _write(part, payload[:24])
            written, got = gguf_store._download(url + "/blob", dest, len(payload))
            check("Range ignored: still the right length", written == len(payload), str(written))
            check("Range ignored: still the right bytes", dest.read_bytes() == payload)
            check("Range ignored: digest restarted from zero, not appended", got == want, got)

        # A body shorter than advertised is a failure, not a truncated install.
        dest.unlink(missing_ok=True)
        with _tiny_block(8), blob_server(payload, prefixes=("/blob",)) as url:
            try:
                gguf_store._download(url + "/blob", dest, len(payload) + 8)
                check("a short body raises", False, "it returned")
            except gguf_store.GgufError:
                check("a short body raises", True)
            check("a short body leaves no final file", not dest.exists())
            check("a short body leaves no .part", not part.exists())

        # A .part longer than the file cannot be a prefix of it: start over.
        dest.unlink(missing_ok=True)
        with _tiny_block(8), blob_server(payload, prefixes=("/blob",)) as url:
            _write(part, payload + b"EXTRA-STALE-BYTES")
            written, got = gguf_store._download(url + "/blob", dest, len(payload))
            check("an oversized stale .part is discarded, not appended",
                  written == len(payload) and dest.read_bytes() == payload)
            check("and the digest is the real file's", got == want, got)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6: the hub fallback
# ---------------------------------------------------------------------------

def test_hub_fallback() -> None:
    section("hub: last resort, via HF_ENDPOINT")

    work = Path(tempfile.mkdtemp(prefix="ggufstore_hub_"))
    try:
        payload = b"GGUF-FROM-THE-HUB"
        file_name = "Index-Homura-2B.Q4_K_M.gguf"
        roi = roster_of(entry("m", file_name, payload))
        home = work / "home"

        with blob_server(payload, prefixes=("/",)) as url, _env(HF_ENDPOINT=url,
                                                                TDUBBER_EDGE_URL=None):
            # No edge at all: force the hub step.
            got = gguf_store.ensure_gguf("m", mounted_roots=[], hf_home=home,
                                         edge_url="", roster=roi)
            check("hub fetch installs the file", got == home / "gguf" / file_name, str(got))
            check("the hub bytes are correct", got is not None and got.read_bytes() == payload)
            check("asked the /resolve/<rev>/<file> path",
                  _Handler.asked and _Handler.asked[0] ==
                  f"/Org/Model/resolve/main/{file_name}", str(_Handler.asked))

        # A hub that cannot serve the model raises GgufError: a caller asked for
        # this model BY NAME, and silently continuing is the failure that
        # produces a dub with no subtitles and a green log. A FRESH home, or the
        # successful fetch above would be a cache hit and hide the failure.
        cold = work / "cold_home"
        with blob_server(payload, prefixes=("/nope/",)) as url, _env(HF_ENDPOINT=url,
                                                                     TDUBBER_EDGE_URL=None):
            try:
                gguf_store.ensure_gguf("m", mounted_roots=[], hf_home=cold,
                                       edge_url="", roster=roi)
                check("an unreachable hub raises GgufError", False, "it returned")
            except gguf_store.GgufError:
                check("an unreachable hub raises GgufError", True)

        # A missing model NAME is its own error, before any network is touched.
        try:
            gguf_store.ensure_gguf("no-such-model", mounted_roots=[], hf_home=cold,
                                   edge_url="", roster=roi)
            check("an unknown model id raises", False, "it returned")
        except gguf_store.GgufError:
            check("an unknown model id raises", True)

        # task= picks the default row when no id is given.
        with blob_server(payload, prefixes=("/",)) as url, _env(HF_ENDPOINT=url,
                                                                TDUBBER_EDGE_URL=None):
            got = gguf_store.ensure_gguf(task="llm", mounted_roots=[], hf_home=cold,
                                         edge_url="", roster=roi)
            check("task without an id resolves the default", got is not None, str(got))
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7: warm_status must agree with the resolver
# ---------------------------------------------------------------------------

def test_warm_status() -> None:
    section("warm_status: reports what ensure_gguf would really do")

    work = Path(tempfile.mkdtemp(prefix="ggufstore_warm_"))
    try:
        good = b"GOOD-BYTES" * 4
        bad = b"BAD-BYTES-" * 4
        a = "a.gguf"
        b = "b.gguf"
        c = "c.gguf"
        d = "d.gguf"
        roi = roster_of(
            entry("roi-a", a, good, repo="Org/A"),
            entry("roi-b", b, good, repo="Org/B"),
            entry("roi-c", c, good, repo="Org/C"),
            entry("roi-d", d, good, repo="Org/D"),
        )
        home = work / "home"
        _write(home / "gguf" / a, good)          # verified cache hit
        _write(home / "gguf" / b, bad)           # present but wrong digest
        # c: nowhere. d: mounted.
        mounted = work / "mnt"
        _write(mounted / d, good)

        rows = {r["id"]: r for r in gguf_store.warm_status(
            mounted_roots=[mounted], hf_home=home, roster=roi)}

        check("a verified cache hit reports hf_home", rows["roi-a"]["source"] == "hf_home",
              str(rows["roi-a"]))
        check("a corrupt cache hit reports missing",
              rows["roi-b"]["source"] == "missing", str(rows["roi-b"]))
        check("and says why",
              "digest" in rows["roi-b"].get("note", ""), str(rows["roi-b"]))
        check("a never-seen model is missing with no note",
              rows["roi-c"]["source"] == "missing" and not rows["roi-c"].get("note"),
              str(rows["roi-c"]))
        check("a mounted model reports mounted", rows["roi-d"]["source"] == "mounted",
              str(rows["roi-d"]))
        check("every row carries its planning fields",
              all(r["size_bytes"] and r["task"] and r["quant"] for r in rows.values()),
              str(rows))
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8: the CLI
# ---------------------------------------------------------------------------

def test_cli_ensure() -> None:
    section("CLI: --ensure fetches every missing model")

    work = Path(tempfile.mkdtemp(prefix="ggufstore_cli_"))
    try:
        payload = b"GGUF-CLI-BYTES"
        file_name = "Index-Homura-2B.Q4_K_M.gguf"
        roi = roster_of(entry("m", file_name, payload))
        roster_path = work / "models_gguf.json"
        roster_path.write_text(json.dumps(roi), encoding="utf-8")
        home = work / "home"
        empty_mount = work / "mnt"
        empty_mount.mkdir(parents=True, exist_ok=True)

        saved_roster = gguf_store._ROSTER_PATH
        gguf_store._ROSTER_PATH = roster_path
        try:
            with _tiny_block(), blob_server(payload, prefixes=("/",)) as url, \
                    _env(HF_HOME=str(home), HF_ENDPOINT=url, TDUBBER_EDGE_URL=None,
                         TDUBBER_MOUNT_ROOTS=str(empty_mount)):
                rc = gguf_store.main(["--ensure"])
                check("--ensure exits 0", rc == 0, str(rc))
                check("--ensure installed the model",
                      (home / "gguf" / file_name).read_bytes() == payload)

                rc = gguf_store.main(["m"])
                check("id form exits 0 and prints the path", rc == 0, str(rc))
        finally:
            gguf_store._ROSTER_PATH = saved_roster
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    if not QUIET:
        print("gguf_store regression tests")

    for test in (test_shipped_roster,
                 test_url_shapes,
                 test_local_discovery,
                 test_edge_fetch,
                 test_edge_wrong_digest_is_refused,
                 test_download_resume,
                 test_hub_fallback,
                 test_warm_status,
                 test_cli_ensure):
        try:
            test()
        except Exception:
            FAILURES.append(test.__name__)
            print(f"    FAIL {test.__name__} raised:")
            traceback.print_exc()

    print("=" * 72)
    if FAILURES:
        print(f"  gguf_store tests: FAIL ({len(FAILURES)})")
        for name in FAILURES:
            print(f"    - {name}")
        print("=" * 72)
        return 1
    print("  gguf_store tests: PASS")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
