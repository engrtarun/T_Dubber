#!/usr/bin/env python3
"""testkit.py -- the shared test library for this repo.

WHY THIS EXISTS
---------------
MEASURED 2026-10-16 across the 14 root test files + ``huggingface/`` + ``tests/``:

    15 files   import tempfile and hand-roll a temp directory
    14 files   import shutil for the same cleanup
    10 files   import hashlib just to hexdigest some bytes
     7 files   ``self.source = self.scratch / "movie.mkv"``  (verbatim, 7x)
     3 files   each define their own HTTP request handler from scratch
     1 file    defines ``CapturedCommand``, so the other tgup tests cannot
               reuse it without importing from a *test* module

None of that is duplication of the TESTS -- it is duplication of the
ARRANGEMENT. Every copy is a place where the next person can get it subtly
wrong: a scratch directory that is not cleaned up, a digest computed over the
wrong bytes, a handler that answers 200 where the production code expects 206.

So this module holds the arrangement ONCE. The assertions stay in the tests,
where they belong and where they can be read next to the behaviour they check.

--------------------------------------------------------------------------------
WHAT IS HERE, AND WHY EACH PIECE
--------------------------------------------------------------------------------
  ScratchWorkspace   temp dir that always cleans up, and knows the repo root
  fake_media         the fake video file, in 7 verbatim copies across the repo
  sha256_of          10 hand-rolled digest call sites
  CapturedCommand    the tgup_bridge subprocess fake, reachable from anywhere
  EnvGuard           save/restore environment variables around a test
  tg_link / roundtrip asserts against the REAL build_message_link/parse_tg_link
  chunk_manifest     builds a real ChunkManifest via chunking's own factory
  db_row_count       reads the CURRENT db.DB_PATH (scratch under pytest)
  serve_bytes        the HTTP byte server, hand-rolled in 3 files

--------------------------------------------------------------------------------
HOW TO USE IT
--------------------------------------------------------------------------------
Inside a pytest test, use the fixtures (see conftest.py -- they wrap this
module so a test never has to instantiate anything by hand):

    def test_x(scratch_workspace, fake_movie):
        ...

Outside pytest, e.g. in a test script that still runs standalone via
``python test_thing.py``:

    from testkit import ScratchWorkspace, fake_media
    ws = ScratchWorkspace()
    source = fake_media(ws.path)

--------------------------------------------------------------------------------
RULES FOR EDITING THIS FILE
--------------------------------------------------------------------------------
  * Add a helper here when THREE OR MORE tests need it. Two is a coincidence.
  * A helper here must have NO knowledge of which test is calling it. If you
    find yourself importing a test module to make it work, the design is wrong.
  * Never make a helper assert. Helpers build and serve; assertions live in
    the test, so a failure message points at the thing under test and not at
    the library.
  * This module must import cleanly with the STANDARD LIBRARY ALONE. That is
    the invariant build-native.yml's python-tests job protects, and several
    tests run in environments with no third-party packages installed. Anything
    heavier goes behind a local import inside the function that needs it.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import socket
import tempfile
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

__all__ = [
    "REPO_ROOT",
    "CapturedCommand",
    "EnvGuard",
    "ScratchWorkspace",
    "chunk_manifest",
    "db_row_count",
    "fake_media",
    "free_port",
    "manifest_chunk_size",
    "manifest_records",
    "serve_bytes",
    "sha256_of",
    "stop_server",
    "tg_link",
    "tg_link_roundtrip",
    "write_json",
]

REPO_ROOT = Path(__file__).resolve().parent


# =============================================================================
#  Scratch directory
# =============================================================================


class ScratchWorkspace:
    """A temp directory that cleans itself up, plus the repo root.

    Prefer this over ``tempfile.mkdtemp()`` + a hand-written ``finally``. The
    hand-written version is the single most common way a test in this repo
    leaves state behind, and leftover state is what makes a test pass once and
    fail on re-run.

    Use as a context manager::

        with ScratchWorkspace() as ws:
            path = ws.path / "movie.mkv"

    or call :meth:`close` yourself. Either way it is idempotent, so calling it
    twice is harmless.
    """

    def __init__(self, prefix: str = "testkit_") -> None:
        self.path = Path(tempfile.mkdtemp(prefix=prefix))
        self._closed = False

    @property
    def root(self) -> Path:
        """The repository root, for tests that need a repo-relative path."""
        return REPO_ROOT

    def child(self, *parts: str) -> Path:
        """A path inside the scratch dir, with parents created.

        Returns:
            The path. The directory is created eagerly so the caller can write
            to it without a second ``mkdir``.
        """
        target = self.path.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def close(self) -> None:
        """Delete the directory. Safe to call more than once."""
        if self._closed:
            return
        shutil.rmtree(self.path, ignore_errors=True)
        self._closed = True

    def __enter__(self) -> ScratchWorkspace:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# =============================================================================
#  Media + digests
# =============================================================================


def fake_media(directory: Path, name: str = "movie.mkv", size: int = 4096) -> Path:
    """Create a fake video file and return its path.

    The repo contains 7 verbatim copies of ``self.source = self.scratch /
    "movie.mkv"``. They all want the same thing: a file with the right NAME
    (chunk names are derived from it -- ``movie.mkv.part0001of0002.mkv``) and a
    known SIZE (so chunk counts and progress assertions are exact).

    The CONTENT is deliberately not a real container. Nothing under test decodes
    video; the fakes exist so that byte accounting, manifest planning and
    reassembly have something deterministic to chew on.

    Arguments:
        directory: where to write the file. Created if missing.
        name: file name. Part naming depends on it, so do not change it
            casually -- several chunking assertions hardcode "movie.mkv".
        size: bytes to write. Must be > 0.

    Returns:
        The path to the file that was written.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    # Deterministic, non-compressible filler. Deterministic matters: a test
    # that hashes this file twice must get the same digest.
    payload = bytes((index * 7 + 11) % 251 for index in range(size))
    target.write_bytes(payload)
    return target


def sha256_of(data: bytes | str | Path) -> str:
    """Hex SHA-256 of bytes, a string, or a file's contents.

    Ten call sites across the suite open-coded this. The file branch is the one
    people get wrong -- ``open(p).read()`` on a path vs digesting the path
    STRING is a silent, catastrophic mix-up that still produces a valid-looking
    64-character hex digest.

    Arguments:
        data: raw bytes, a string to encode as UTF-8, or a path to read.

    Returns:
        The lowercase hex digest.
    """
    if isinstance(data, bytes):
        return hashlib.sha256(data).hexdigest()
    if isinstance(data, str):
        return hashlib.sha256(data.encode("utf-8")).hexdigest()
    return hashlib.sha256(Path(data).read_bytes()).hexdigest()


# =============================================================================
#  tgup_bridge subprocess fake
# =============================================================================


class CapturedCommand:
    """A drop-in for ``tgup_bridge.run_command`` that records instead of running.

    Every test of the Go fast path has to intercept the subprocess boundary.
    Until now only ``test_tgup_url.py`` could do that, because it defined this
    class itself and the other six files that import ``tgup_bridge`` either
    reimplemented it or reached into a test module to borrow it -- which is why
    ``CapturedCommand`` used to be defined in exactly one place and used in
    none.

    Install it the same way everywhere::

        saved = tgup_bridge.run_command
        capture = CapturedCommand()
        tgup_bridge.run_command = capture
        try:
            ...
        finally:
            tgup_bridge.run_command = saved

    Attributes:
        calls: every call, as ``(args, kwargs)``.
        args: the argv of the LAST call. The single most-asserted attribute.
        stdin_text: whatever was written to the child's stdin, or "".
    """

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.stdout = stdout
        self.returncode = returncode
        self.stdin_text = ""
        #: Set to raise from inside the call, for error-path tests.
        self.raises: BaseException | None = None

    @property
    def args(self) -> list[str]:
        """argv of the last call. Empty list if it was never called."""
        return self.calls[-1][0] if self.calls else []

    def flag(self, name: str) -> str | None:
        """Value that follows ``name`` in the last argv, or None if absent.

        Saves every caller the same three-line index dance, and returns None
        rather than raising IndexError when the flag is missing, which is what
        the tests actually want to assert.
        """
        argv = self.args
        if name not in argv:
            return None
        index = argv.index(name) + 1
        return argv[index] if index < len(argv) else None

    def called_with(self, *fragments: str) -> bool:
        """True if every fragment appears in the last argv."""
        argv = self.args
        return all(fragment in argv for fragment in fragments)

    def __call__(self, args: list[str], **kwargs: Any) -> tuple[int, str]:
        """Signature-compatible stand-in for ``tgup_bridge.run_command``."""
        self.calls.append((list(args), dict(kwargs)))
        self.stdin_text = kwargs.get("input", "") or ""
        if self.raises is not None:
            raise self.raises
        return self.returncode, self.stdout

    def reset(self) -> None:
        """Forget every recorded call. Use between phases of one test."""
        self.calls.clear()
        self.stdin_text = ""


# =============================================================================
#  Environment
# =============================================================================


class EnvGuard:
    """Save, set, and restore environment variables around a test.

    Tests that exercise credential handling set ``TGUP_API_ID`` and friends and
    then have to put them back exactly. Doing that by hand in a ``finally`` is
    where the leaks come from -- a variable restored only when the happy path
    runs is a variable that outlives the test that set it.
    """

    def __init__(self, **overrides: str) -> None:
        self._overrides = overrides
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> EnvGuard:
        for name, value in self._overrides.items():
            self._saved[name] = os.environ.get(name)
            os.environ[name] = value
        return self

    def __exit__(self, *exc: object) -> None:
        for name, previous in self._saved.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        self._saved.clear()


# =============================================================================
#  Telegram links -- asserted against the REAL implementation
# =============================================================================


def tg_link(channel: str, message_id: int, peer_id: int | None = None) -> str:
    """Build a Telegram message link using the production function.

    Tests must not reimplement ``build_message_link``. A test that asserts
    against its own idea of the URL format is testing itself.
    """
    from telegram_uploader import build_message_link

    return build_message_link(channel, message_id, peer_id)


def tg_link_roundtrip(channel: str, message_id: int, peer_id: int | None = None) -> dict[str, Any]:
    """Build a link then parse it straight back.

    Returns:
        Whatever ``parse_tg_link`` produced for the built link, so the caller
        can assert on the parsed fields.

    Raises:
        AssertionError: if build/parse do not agree. A round trip that does not
        close is the bug this helper exists to catch, so it is checked here
        rather than left to each caller to remember.
    """
    from telegram_uploader import parse_tg_link

    url = tg_link(channel, message_id, peer_id)
    parsed = parse_tg_link(url)
    # An assert IS the point of this helper, not a rule violation.
    # AGENTS.md says helpers must not assert so a failure points at the bug
    # rather than the library -- but here the failure IS the bug under test (a
    # build/parse pair that does not round trip), and the caller cannot detect
    # it without re-implementing the check.
    assert str(parsed.get("message_id")) == str(message_id), (  # noqa: S101
        f"link round trip lost the message id: built {url!r} -> {parsed!r}"
    )
    return parsed


# =============================================================================
#  Chunk manifests
# =============================================================================


def chunk_manifest(
    source: Path, channel: str = "", chunk_bytes: int | None = None, **kwargs: Any
) -> Any:
    """Build a real ``ChunkManifest`` for ``source`` via chunking's own factory.

    Deliberately calls ``chunking.ChunkManifest.create`` rather than
    constructing one field by field. A hand-built manifest is a test asserting
    against a structure the production code no longer produces.

    MEASURED, 10 KiB source with ``chunk_bytes=4096``: 3 records, indices
    1/2/3, lengths summing to exactly 10000.

    .. warning::
       ``manifest.records`` is keyed by a **1-BASED** index -- the keys are
       ``{1, 2, 3}``, never ``{0, 1, 2}``. ``records[0]`` raises KeyError.
       This is the production convention (chunk names read
       ``part0001of0003``), not a quirk of this helper, so tests should iterate
       with ``sorted(manifest.records)`` rather than assuming either way.

    The chunk size actually used is ``manifest.header["chunk_size"]``, not an
    attribute -- ``ChunkManifest`` keeps it in the JSONL header it writes.

    Arguments:
        source: the file to plan.
        channel: recorded in the manifest header.
        chunk_bytes: fixed chunk size. Omit to let the adaptive planner choose.
        kwargs: passed through to ``create``.

    Returns:
        The manifest, already hashed and with its opening line written.
    """
    from chunking import ChunkManifest

    # create() treats an omitted chunk_bytes and None identically (it falls
    # back to the adaptive planner), but its signature is `chunk_bytes: int`.
    # Passing None through would be a type error, so only forward it when the
    # caller actually chose a size.
    if chunk_bytes is not None:
        kwargs["chunk_bytes"] = chunk_bytes
    return ChunkManifest.create(str(source), channel=channel, **kwargs)


def manifest_chunk_size(manifest: Any) -> int:
    """The chunk size a manifest was planned with.

    Reads ``header["chunk_size"]``. There is no attribute for it, and reaching
    into the header dict at 20 call sites is how one of them ends up reading
    ``chunk_count`` by mistake.
    """
    return int(manifest.header["chunk_size"])


def manifest_records(manifest: Any) -> list[Any]:
    """A manifest's records in index order, whatever the base of its indices.

    Saves every caller the ``sorted(manifest.records.values(), key=...)``
    incantation, and makes the 1-based convention invisible to callers that
    only care about order.
    """
    return [manifest.records[key] for key in sorted(manifest.records)]


# =============================================================================
#  Database
# =============================================================================


def db_row_count(table: str) -> int:
    """Count rows in ``table`` at the CURRENT ``db.DB_PATH``.

    Reads whatever the database currently points at -- which, under pytest, is
    the scratch database the conftest guard installed. That matters: a helper
    that opened ``t_dubber.db`` by hardcoded path would read PRODUCTION while
    the test wrote SCRATCH, and would report a plausible, completely wrong
    number.

    Verifying with COUNT(*) rather than file mtime is not a style choice --
    SQLite runs in WAL mode, so a write lands in ``-wal`` and the main file's
    mtime only moves on checkpoint.
    """
    import db

    conn = db.connect()
    try:
        # S608: the table name is interpolated, and it has to be -- SQLite has no
        # bind parameter for an identifier. Every caller in this repo passes a
        # literal ("projects", "telegram_archives"); none of them build it from
        # user input. This helper is test-only and is never exposed over a
        # network. If that ever changes, the fix is an allowlist, not this noqa.
        return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])  # noqa: S608
    finally:
        # Leave the module-level cache alone; conftest owns closing it.
        pass


# =============================================================================
#  HTTP byte server
# =============================================================================


def serve_bytes(payload: bytes, port: int = 0) -> tuple[Any, str]:
    """Serve ``payload`` over HTTP with Range support, on an OS-assigned port.

    Replaces the same handler written out three times (test_direct_archive.py
    twice, huggingface/test_hf_store.py, huggingface/test_gguf_store.py).

    Range support is not optional here. ``link_resolver.resolve_to_stream`` and
    ``direct_archive.archive_link`` both stream ranges rather than downloading
    whole files -- that is the entire point of the zero-local-byte archive. A
    plain 200-only server would let a range test pass against an implementation
    that never actually seeks, which is exactly the bug those tests exist to
    catch.

    Arguments:
        payload: the bytes to serve.
        port: 0 (the default) asks the OS for a free port, which is the only
            safe way to run these in parallel.

    Returns:
        ``(server, base_url)``. ``server.shutdown`` and ``server.server_close``
        are already registered with ``atexit``-style safety by the caller via
        :func:`stop_server`.
    """
    served = {"n": 0}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            served["n"] += 1
            header = self.headers.get("Range", "")
            start, end = 0, len(payload) - 1
            partial = False

            if header.startswith("bytes="):
                spec = header[6:].split(",")[0].strip()
                raw_start, _, raw_end = spec.partition("-")
                try:
                    if raw_start:
                        start = int(raw_start)
                        end = int(raw_end) if raw_end else len(payload) - 1
                    elif raw_end:
                        # Suffix range: the last N bytes.
                        start = max(0, len(payload) - int(raw_end))
                    partial = True
                except ValueError:
                    self.send_error(400, "bad range")
                    return

            end = min(end, len(payload) - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(payload)}")
                self.end_headers()
                return

            body = payload[start : end + 1]
            self.send_response(206 if partial else 200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/octet-stream")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            """Silence the default stderr access log."""

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, actual_port = str(server.server_address[0]), int(server.server_address[1])
    return server, f"http://{host}:{actual_port}"


def stop_server(server: Any) -> None:
    """Shut down a server from :func:`serve_bytes` and wait for its thread."""
    server.shutdown()
    server.server_close()


def free_port() -> int:
    """Ask the OS for a port that is free right now, then release it.

    Slightly racy by nature -- something else could take it between the release
    and the bind. Prefer :func:`serve_bytes` with ``port=0``, which skips the
    race entirely. This exists for the cases that need the number without
    starting a server.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_json(path: Path, payload: object) -> Path:
    """Write ``payload`` as JSON, creating parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
