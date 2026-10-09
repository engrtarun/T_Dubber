"""tiny_range_server.py -- serve one file over HTTP with real Range support.

WHY THIS EXISTS
---------------
``tgup upload --url ...`` refuses to run against a server that cannot serve byte
ranges, and Python's stock ``http.server`` cannot: it answers every request with
200 and the whole body. So the feature could not be tried by hand, only from Go
tests, where a purpose-built test server stood in.

This is that server, for humans. It is deliberately small and dependency-free.

    python tools/tiny_range_server.py --root . --port 8765
    tgup plan --url http://127.0.0.1:8765/some/video.mp4

It also prints one line per request, so you can *see* that tgup asks for ranges
and not for the whole file every part -- which is the property that makes a
zero-disk upload worth having.

NOT FOR PRODUCTION. Loopback only, no TLS, no auth.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

STATS = {"requests": 0, "ranged": 0, "bytes": 0}
STATS_LOCK = threading.Lock()


class RangeHandler(BaseHTTPRequestHandler):
    """Serves one file under ``server.root`` with RFC 7233 range support."""

    server_version = "tiny-range/1.0"

    def log_message(self, fmt, *args):  # noqa: A003 - BaseHTTPRequestHandler API
        sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers ----------------------------------------------------------

    def _resolve(self) -> tuple[str | None, int]:
        raw = self.path.split("?", 1)[0].split("#", 1)[0]
        rel = os.path.normpath(raw.lstrip("/")).replace("\\", "/")
        if rel in (".", "/"):
            return None, 403
        full = os.path.abspath(os.path.join(self.server.root, rel))  # type: ignore[attr-defined]
        root = os.path.abspath(self.server.root)  # type: ignore[attr-defined]
        # Path traversal is the one thing a demo server must not teach by example.
        if not (full == root or full.startswith(root + os.sep)):
            return None, 403
        if not os.path.isfile(full):
            return None, 404
        return full, os.path.getsize(full)

    def _count(self, ranged: bool, nbytes: int) -> None:
        with STATS_LOCK:
            STATS["requests"] += 1
            STATS["ranged"] += 1 if ranged else 0
            STATS["bytes"] += nbytes

    def _common(self) -> tuple[str | None, int]:
        path = self.path.split("?", 1)[0]
        return self._resolve() + (path,)  # type: ignore[return-value]

    # -- verbs ------------------------------------------------------------

    def do_HEAD(self):  # noqa: N802 - BaseHTTPRequestHandler API
        full, size = self._resolve()
        if full is None:
            self.send_error(size)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        # /__stats answers "how many bytes did the client actually pull?", which
        # is the only honest way to check that a run streamed the payload
        # instead of quietly downloading it to disk first.
        if self.path.split("?", 1)[0] == "/__stats":
            payload = json.dumps(STATS, sort_keys=True).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        full, size = self._resolve()
        if full is None:
            self.send_error(size)
            return
        spec = self.headers.get("Range")
        start, end = 0, size - 1
        ranged = False
        if spec:
            match = RANGE_RE.match(spec.strip())
            if match:
                first, last = match.group(1), match.group(2)
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                    if start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    ranged = True
                elif last:
                    # Suffix range: the last N bytes.
                    length = min(int(last), size)
                    start, end = size - length, size - 1
                    ranged = True
        if end >= size:
            end = size - 1
        length = end - start + 1

        self.send_response(206 if ranged else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if ranged:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(full, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1 << 20, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # A client that hung up mid-part is normal on a cancel.
                    break
        self._count(ranged, length)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.getcwd(), help="directory to serve (default: cwd)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="loopback by default; do not expose this")
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), RangeHandler)
    server.root = args.root  # type: ignore[attr-defined]
    print(f"serving {os.path.abspath(args.root)} at "
          f"http://{args.host}:{args.port}/  (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\nrequests={STATS['requests']} ranged={STATS['ranged']} "
              f"bytes={STATS['bytes']}")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())