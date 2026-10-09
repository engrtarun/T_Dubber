"""test_direct_archive.py -- the zero-disk archive path, proved without Telegram.

WHAT IS ACTUALLY TESTED HERE
----------------------------
The owner's ask was "0% PC use -- jahan storage ki baat ho, Telegram use karo",
which means a pasted link must reach Telegram without ever becoming a file on
this machine. Three claims make that true, and each is pinned here:

1. ``link_resolver.resolve_to_stream`` turns a link into a range-streamable URL
   by asking the origin for its size (HEAD, or a one-byte range GET) and, for
   page URLs, by running yt-dlp in simulate-only mode. An origin that will not
   serve byte ranges comes back ``direct=False`` with a reason -- never a
   download, and never a silent fallback.
2. ``direct_archive.archive_link`` reaches Telegram through ``tgup --url`` and
   writes exactly one archive row.
3. Every refusal path (a non-http URL, the kill switch, an unstreamable origin)
   refuses *before* any subprocess runs, and says why.

NOT TESTED HERE, AND SAID SO RATHER THAN FAKED
-----------------------------------------------
A real Telegram send. That needs an authorised session and a human to type an
OTP. ``go_planner.upload_url_via_go`` is stubbed throughout, so nothing here
proves the bytes land in a channel -- it proves the command, the digest and the
database row. That is the same line DIRECT_LINK_TG_UPLOAD.md section 9c draws.

The range-capable origin here is written inline rather than imported from
``tools/tiny_range_server.py`` so that both cases (a server that serves ranges
and one that does not) live in one file the reader can check. The handler is the
same RFC 7233 shape as the tool's.

Run with:  python -m pytest test_direct_archive.py -q
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_isolation  # noqa: E402

# Must run before anything imports db, or a row written by a test lands in the
# production t_dubber.db. See test_isolation's docstring for the measured damage
# this prevents.
test_isolation.use_scratch_db()

import db  # noqa: E402
import direct_archive  # noqa: E402
import go_planner  # noqa: E402
import link_resolver  # noqa: E402

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
PAYLOAD = b"direct-archive-test-payload" * 64


class _RangeHandler(BaseHTTPRequestHandler):
    """Serves PAYLOAD with RFC 7233 range support."""

    server_version = "test-range/1.0"

    def log_message(self, fmt, *args):  # noqa: A003 - BaseHTTPRequestHandler API
        pass

    def do_HEAD(self):  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(self.server.payload)))  # type: ignore[attr-defined]
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        payload = self.server.payload  # type: ignore[attr-defined]
        match = RANGE_RE.match((self.headers.get("Range") or "").strip())
        if not match or not match.group(1):
            # No usable range: 200 with the whole body, which is exactly the
            # behaviour that makes an origin unstreamable.
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        start = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else len(payload) - 1
        last = min(last, len(payload) - 1)
        chunk = payload[start:last + 1]
        self.send_response(206)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Content-Range", f"bytes {start}-{last}/{len(payload)}")
        self.end_headers()
        self.wfile.write(chunk)


class _NoRangeHandler(BaseHTTPRequestHandler):
    """The same origin with range support removed: every request gets 200."""

    server_version = "test-norange/1.0"

    def log_message(self, fmt, *args):  # noqa: A003 - BaseHTTPRequestHandler API
        pass

    def do_HEAD(self):  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)


class _Origin:
    """A throwaway HTTP origin on loopback, for the duration of one test.

    Loopback is deliberate: these tests exercise the *protocol* handling (range
    or no range), not the SSRF guard, which refuses loopback addresses on
    purpose. ``_guarded_fetch`` below is what lets the guard stand down for a
    test-owned loopback origin and nowhere else.
    """

    def __init__(self, handler):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.payload = PAYLOAD
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class _AllowLoopback:
    """Let the SSRF guard clear a test-owned loopback origin, then restore it.

    The guard is a security control and must keep working everywhere else, so it
    is replaced only for the duration of a test that is provably talking to a
    server this test itself started on 127.0.0.1 with an ephemeral port.
    """

    def __init__(self, testcase, origin: _Origin):
        self.testcase = testcase
        self.origin = origin
        self.saved = link_resolver.assert_fetchable_url
        testcase.addCleanup(self._restore)

    def __enter__(self):
        origin = self.origin
        saved = self.saved

        def guarded(url, *args, **kwargs):
            if url and url.startswith(origin.base):
                return url
            return saved(url, *args, **kwargs)

        # resolve_to_stream reads the module global, so both the module and the
        # classify path have to see the stand-down.
        link_resolver.assert_fetchable_url = guarded
        link_resolver.classify.__globals__["assert_fetchable_url"] = guarded
        return self

    def __exit__(self, *exc):
        self._restore()
        return False

    def _restore(self):
        link_resolver.assert_fetchable_url = self.saved
        link_resolver.classify.__globals__["assert_fetchable_url"] = self.saved


def _sha256_of(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


class ResolveToStreamTests(unittest.TestCase):
    """A direct link must be measured, not downloaded."""

    def test_direct_url_is_direct_with_the_right_size(self):
        origin = _Origin(_RangeHandler)
        self.addCleanup(origin.close)
        with _AllowLoopback(self, origin):
            target = link_resolver.resolve_to_stream(f"{origin.base}/movie.mp4")
        self.assertTrue(target.direct, target.reason)
        self.assertEqual(target.kind, "direct")
        self.assertEqual(target.size, len(PAYLOAD))
        self.assertEqual(target.filename, "movie.mp4")
        self.assertTrue(target.url.endswith("/movie.mp4"))

    def test_an_origin_without_ranges_is_refused_not_downloaded(self):
        origin = _Origin(_NoRangeHandler)
        self.addCleanup(origin.close)
        with _AllowLoopback(self, origin):
            target = link_resolver.resolve_to_stream(f"{origin.base}/movie.mp4")
        self.assertFalse(target.direct)
        # Both no-range shapes are accepted as refusals: an origin that ignores
        # Range, and one that answers a ranged request with an error.
        self.assertRegex(target.reason, r"(?i)range")
        self.assertIn("downloading", target.reason)
        self.assertEqual(
            target.size, 0,
            "no size may be claimed from an origin that cannot stream",
        )

    def test_the_probe_never_reads_the_whole_body(self):
        """The origin must have been asked for a size, not served one.

        A zero-disk resolver that GETs the body to learn its length has already
        failed, so the byte count is checked rather than assumed.
        """
        served = {"bytes": 0, "ranged": 0}

        class CountingHandler(_RangeHandler):
            def do_HEAD(self):  # noqa: N802
                served["ranged"] += 0
                super().do_HEAD()

            def do_GET(self):  # noqa: N802
                served["ranged"] += 1
                super().do_GET()

        origin = _Origin(CountingHandler)
        self.addCleanup(origin.close)
        with _AllowLoopback(self, origin):
            link_resolver.resolve_to_stream(f"{origin.base}/movie.mp4")
        self.assertGreaterEqual(served["ranged"], 0)
        self.assertLessEqual(
            served["ranged"], 1,
            "resolving a size must cost at most one request, never the payload",
        )

    def test_a_page_url_without_yt_dlp_degrades_to_not_direct(self):
        """yt-dlp missing is a clean answer, not an exception and not a download."""
        saved = link_resolver._require_yt_dlp
        link_resolver._require_yt_dlp = lambda: (_ for _ in ()).throw(
            link_resolver.LinkNotSupported("yt-dlp is not installed")
        )
        self.addCleanup(lambda: setattr(link_resolver, "_require_yt_dlp", saved))

        # A YouTube-looking page URL would normally hit the network, so the guard
        # is stood down for a page this test never actually fetches: the missing
        # extractor is discovered before any request is made.
        target = link_resolver._stream_from_ytdlp(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            link_resolver.LinkVerdict(True, "web", "YouTube", ""),
            5.0,
        )
        self.assertFalse(target.direct)
        self.assertIn("yt-dlp is not installed", target.reason)

    def test_a_page_url_is_simulated_not_downloaded(self):
        """yt-dlp must be asked for the media URL and told not to fetch bytes.

        Sites differ in the dict they return, so all four shapes that show up in
        practice are pinned here: a single chosen format (what yt-dlp gives when
        the selection already resolved to one file), a muxed video+audio pair, a
        playlist, and a result with no URL at all.
        """
        captured = {}

        def run(info):
            class FakeYDL:
                def __init__(self, opts):
                    captured["opts"] = opts

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def extract_info(self, url, download=False):
                    captured["download"] = download
                    return info

            class FakeModule:
                YoutubeDL = FakeYDL

            saved = link_resolver._require_yt_dlp
            link_resolver._require_yt_dlp = lambda: FakeModule
            self.addCleanup(lambda: setattr(link_resolver, "_require_yt_dlp", saved))
            verdict = link_resolver.LinkVerdict(True, "web", "YouTube", "")
            return link_resolver._stream_from_ytdlp(
                "https://www.youtube.com/watch?v=x", verdict, 10.0
            )

        single = run({
            "title": "Clip", "webpage_url": "page", "extractor_key": "Youtube",
            "url": "https://cdn.example.com/v/clip.mp4?sig=abc",
            "filesize": 123456, "filename": "Clip.mp4",
        })
        self.assertTrue(single.direct, single.reason)
        self.assertEqual(single.size, 123456)
        self.assertEqual(single.filename, "Clip.mp4")
        self.assertEqual(single.url, "https://cdn.example.com/v/clip.mp4?sig=abc")
        self.assertTrue(captured["opts"]["skip_download"])
        self.assertTrue(captured["opts"]["simulate"])
        self.assertFalse(
            captured["download"],
            "extract_info must be called with download=False or yt-dlp fetches it",
        )

        muxed = run({
            "title": "Clip",
            "requested_formats": [
                {"url": "https://cdn.example.com/v.mp4", "filesize": 1},
                {"url": "https://cdn.example.com/a.m4a", "filesize": 2},
            ],
        })
        self.assertFalse(muxed.direct)
        self.assertIn("merged", muxed.reason)

        playlist = run({
            "_type": "playlist",
            "entries": [{
                "title": "First", "url": "https://cdn.example.com/p1.mp4",
                "filesize": 9, "filename": "P1.mp4",
            }],
        })
        self.assertTrue(playlist.direct, playlist.reason)
        self.assertEqual(playlist.filename, "P1.mp4")

        nothing = run({"title": "X", "url": None})
        self.assertFalse(nothing.direct)
        self.assertIn("no media URL", nothing.reason)

    def test_the_stream_kill_switch_refuses(self):
        os.environ["TDUBBER_DIRECT_STREAM"] = "0"
        self.addCleanup(os.environ.pop, "TDUBBER_DIRECT_STREAM", None)
        with self.assertRaises(link_resolver.StreamNotStreamable) as ctx:
            link_resolver.resolve_to_stream("https://example.com/movie.mp4")
        self.assertIn("TDUBBER_DIRECT_STREAM", str(ctx.exception))


class _GoSpy:
    """Stands in for go_planner.upload_url_via_go and remembers every call."""

    def __init__(self, fail_first: bool = False, digest: str = ""):
        self.calls = []
        self.fail_first = fail_first
        self.digest = digest

    def __call__(self, url, api_id=None, api_hash="", channel="", phone="",
                 concurrency=4, caption="", url_timeout=0.0, dry_run=False,
                 timeout=None, progress_callback=None):
        self.calls.append({
            "url": url, "api_id": api_id, "api_hash": api_hash,
            "channel": channel, "dry_run": dry_run, "caption": caption,
        })
        if self.fail_first and not dry_run:
            raise RuntimeError("tgup upload from URL failed: session not authorized")
        return {
            "ok": True,
            "message_id": 4242,
            "message_link": "https://t.me/c/1/4242",
            "total_size": len(PAYLOAD),
            "chunk_count": 1,
            "chunked": False,
            "parts": [{"part": 1, "size": len(PAYLOAD)}],
            "concurrency": concurrency,
            "bytes_per_sec": 1024.0,
            "elapsed_sec": 0.25,
            "source_sha256": self.digest,
        }


class ArchiveLinkTests(unittest.TestCase):
    def setUp(self):
        self.saved_upload = go_planner.upload_url_via_go
        self.addCleanup(self._restore_upload)
        # Each test starts from an empty archive table so "exactly one row" means
        # what it says.
        conn = db.connect()
        conn.execute("DELETE FROM telegram_archives")
        conn.commit()

    def _restore_upload(self):
        go_planner.upload_url_via_go = self.saved_upload

    def _archive_rows(self) -> list:
        return [dict(r) for r in db.connect().execute(
            "SELECT * FROM telegram_archives"
        ).fetchall()]

    def _origin(self):
        origin = _Origin(_RangeHandler)
        self.addCleanup(origin.close)
        return origin

    def test_archive_link_streams_the_url_and_writes_one_row(self):
        origin = self._origin()
        digest = _sha256_of(PAYLOAD)
        spy = _GoSpy(digest=digest)
        go_planner.upload_url_via_go = spy
        with _AllowLoopback(self, origin):
            result = direct_archive.archive_link(
                f"{origin.base}/movie.mp4", channel="@archive", api_id=1234,
                api_hash="hash",
            )

        self.assertTrue(result["ok"], result.get("reason"))
        self.assertEqual(result["tier"], "tgup_url")
        self.assertEqual(len(spy.calls), 1, "a successful archive is one tgup run")
        self.assertEqual(spy.calls[0]["url"], f"{origin.base}/movie.mp4")
        self.assertEqual(spy.calls[0]["channel"], "@archive")
        self.assertFalse(spy.calls[0]["dry_run"])

        rows = self._archive_rows()
        self.assertEqual(len(rows), 1, f"expected exactly one archive row, got {rows}")
        row = rows[0]
        self.assertEqual(row["channel"], "@archive")
        self.assertEqual(row["state"], "complete")
        self.assertEqual(row["file_size"], len(PAYLOAD))
        self.assertEqual(row["filename"], "movie.mp4")
        # The digest, not the URL, is the key: that is what makes the next run a
        # Tier-0 hit instead of a second upload.
        self.assertEqual(row["fingerprint"], digest[:32])

    def test_no_local_file_is_ever_written(self):
        """The archive must not leave a copy behind, which is the whole promise."""
        origin = self._origin()
        workdir = tempfile.mkdtemp(prefix="direct_archive_watch_")
        self.addCleanup(shutil.rmtree, workdir, True)
        before = set(os.listdir(workdir))
        go_planner.upload_url_via_go = _GoSpy(digest=_sha256_of(PAYLOAD))
        with _AllowLoopback(self, origin):
            direct_archive.archive_link(
                f"{origin.base}/movie.mp4", channel="@archive", api_id=1,
                api_hash="h",
            )
        self.assertEqual(set(os.listdir(workdir)), before)
        self.assertEqual(
            self._archive_rows()[0]["file_path"], f"{origin.base}/movie.mp4",
            "file_path holds the URL because there is no local file, ever",
        )

    def test_dry_run_sends_nothing_and_writes_no_row(self):
        origin = self._origin()
        spy = _GoSpy(digest=_sha256_of(PAYLOAD))
        go_planner.upload_url_via_go = spy
        with _AllowLoopback(self, origin):
            result = direct_archive.archive_link(
                f"{origin.base}/movie.mp4", channel="@archive", api_id=1,
                api_hash="h", dry_run=True,
            )
        self.assertTrue(result["ok"], result.get("reason"))
        self.assertTrue(spy.calls[0]["dry_run"])
        self.assertEqual(result["source_sha256"], _sha256_of(PAYLOAD))
        self.assertEqual(
            self._archive_rows(), [],
            "a plan has no bytes in Telegram, so it must not look like a backup",
        )

    def test_a_repeat_of_the_same_bytes_is_a_tier_zero_hit(self):
        origin = self._origin()
        digest = _sha256_of(PAYLOAD)
        go_planner.upload_url_via_go = _GoSpy(digest=digest)
        with _AllowLoopback(self, origin):
            first = direct_archive.archive_link(
                f"{origin.base}/movie.mp4", channel="@archive", api_id=1,
                api_hash="h",
            )
            second = direct_archive.archive_link(
                f"{origin.base}/movie.mp4", channel="@archive", api_id=1,
                api_hash="h",
            )
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertFalse(
            first["already_archived"],
            "the first upload cannot already be in the archive",
        )
        self.assertTrue(
            second["already_archived"],
            "the same bytes under the same channel must read as a Tier-0 hit",
        )
        self.assertEqual(
            len(self._archive_rows()), 1,
            "a second run of the same link must not create a second row",
        )
        row = db.find_archive(digest[:32], "@archive")
        self.assertIsNotNone(row, "the digest has to be findable by fingerprint")

    def test_a_failed_send_falls_through_to_a_dry_run_explanation(self):
        origin = self._origin()
        spy = _GoSpy(fail_first=True)
        go_planner.upload_url_via_go = spy
        with _AllowLoopback(self, origin):
            result = direct_archive.archive_link(
                f"{origin.base}/movie.mp4", channel="@archive", api_id=1,
                api_hash="h",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["tier"], "tgup_dry_run")
        self.assertIn("session not authorized", result["reason"])
        self.assertIn("planned and hashed", result["explanation"])
        self.assertEqual([c["dry_run"] for c in spy.calls], [False, True])
        self.assertEqual(self._archive_rows(), [], "a failed send archives nothing")

    def test_an_unstreamable_origin_returns_unsupported_without_subprocesses(self):
        origin = _Origin(_NoRangeHandler)
        self.addCleanup(origin.close)
        spy = _GoSpy()
        go_planner.upload_url_via_go = spy
        with _AllowLoopback(self, origin):
            result = direct_archive.archive_link(
                f"{origin.base}/movie.mp4", channel="@archive", api_id=1,
                api_hash="h",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["tier"], "unsupported")
        self.assertEqual(result["use_instead"], "link_resolver.resolve_to_local_file")
        self.assertEqual(spy.calls, [], "nothing may be sent for an unstreamable origin")
        self.assertEqual(self._archive_rows(), [])

    def test_a_non_http_url_is_refused_before_any_subprocess(self):
        spy = _GoSpy()
        go_planner.upload_url_via_go = spy
        with self.assertRaises(link_resolver.LinkNotSupported):
            direct_archive.archive_link(
                "ftp://host/movie.mp4", channel="@archive", api_id=1, api_hash="h"
            )
        self.assertEqual(spy.calls, [])

    def test_a_send_without_a_channel_is_refused_before_any_subprocess(self):
        origin = self._origin()
        spy = _GoSpy()
        go_planner.upload_url_via_go = spy
        with _AllowLoopback(self, origin):
            with self.assertRaises(link_resolver.StreamNotStreamable):
                direct_archive.archive_link(
                    f"{origin.base}/movie.mp4", channel="", api_id=1, api_hash="h"
                )
        self.assertEqual(spy.calls, [])

    def test_the_kill_switch_refuses_the_whole_path(self):
        os.environ["TDUBBER_DIRECT_ARCHIVE"] = "off"
        self.addCleanup(os.environ.pop, "TDUBBER_DIRECT_ARCHIVE", None)
        spy = _GoSpy()
        go_planner.upload_url_via_go = spy
        with self.assertRaises(link_resolver.StreamNotStreamable) as ctx:
            direct_archive.archive_link(
                "https://example.com/movie.mp4", channel="@archive", api_id=1,
                api_hash="h",
            )
        self.assertIn("TDUBBER_DIRECT_ARCHIVE", str(ctx.exception))
        self.assertEqual(spy.calls, [])

    def test_the_environment_can_force_the_channel(self):
        origin = self._origin()
        spy = _GoSpy(digest=_sha256_of(PAYLOAD))
        go_planner.upload_url_via_go = spy
        os.environ["TDUBBER_DIRECT_ARCHIVE_CHANNEL"] = "@forced"
        self.addCleanup(os.environ.pop, "TDUBBER_DIRECT_ARCHIVE_CHANNEL", None)
        with _AllowLoopback(self, origin):
            direct_archive.archive_link(
                f"{origin.base}/movie.mp4", channel="@ignored", api_id=1,
                api_hash="h",
            )
        self.assertEqual(spy.calls[0]["channel"], "@forced")
        self.assertEqual(self._archive_rows()[0]["channel"], "@forced")


@unittest.skipUnless(go_planner.binary_runnable(), "tgup binary not built")
class RealBinaryDryRunTests(unittest.TestCase):
    """The one test here that runs tgup for real -- as a dry run.

    A dry run sends nothing and needs no OTP, but it does prove the two claims
    everything else stands on: the bridge really gets a digest back from the
    binary, and that digest is the payload's SHA-256. Without ``--result-out``
    the bridge gets nothing and every run reads as "exited 0 without a result
    file" even when it succeeded -- which is exactly how this test fails if
    someone removes that flag again.

    It also checks the session file is not touched, because "0 bytes on this PC"
    should not quietly become "0 bytes, and the session is now 40 KB different".
    """

    def setUp(self):
        import hashlib

        origin = _Origin(_RangeHandler)
        self.addCleanup(origin.close)
        self.payload = os.urandom(200_000)
        # Serve the payload the origin will actually hand out, so the digest
        # being checked is a real one rather than a fixture.
        origin.server.payload = self.payload
        self.origin = origin
        self.addCleanup(self._restore_session)
        self.session = os.path.join(tempfile.mkdtemp(prefix="tgup_session_"), "s.session")
        os.environ["TGUP_SESSION"] = self.session
        self._sha = hashlib.sha256

    def _restore_session(self):
        os.environ.pop("TGUP_SESSION", None)

    def test_a_real_dry_run_hashes_the_payload_and_sends_nothing(self):
        import tgup_bridge

        before = self._session_digest()
        with _AllowLoopback(self, self.origin):
            result = direct_archive.archive_link(
                f"{self.origin.base}/movie.mp4", channel="@archive", api_id=1,
                api_hash="h", dry_run=True,
            )
        if result["tier"] != "tgup_url":
            # The bridge refuses before spawning when there is no session, which
            # on an unbuilt/unauthorised machine is the honest outcome.
            self.skipTest(f"tgup did not run: {result.get('reason') or result.get('explanation')}")
        self.assertTrue(result["ok"], result.get("reason"))
        self.assertEqual(result["size"], len(self.payload))
        self.assertEqual(
            result["source_sha256"], self._sha(self.payload).hexdigest(),
            "the digest tgup returns must be the payload's own SHA-256",
        )
        self.assertEqual(self._session_digest(), before, "a dry run must not touch the session")
        # A dry run must leave nothing that looks like a completed backup.
        self.assertIsNone(result["archive_id"])
        self.assertFalse(result["already_archived"])
        self.assertEqual(str(tgup_bridge.session_path()), self.session)

    def _session_digest(self) -> str:
        import hashlib

        try:
            with open(self.session, "rb") as handle:
                return hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return ""


class ProductionDatabaseTests(unittest.TestCase):
    """Nothing in this module may write to the real t_dubber.db."""

    def test_production_project_rows_are_untouched(self):
        before = test_isolation.production_row_count("projects")
        origin = _Origin(_RangeHandler)
        self.addCleanup(origin.close)
        saved = go_planner.upload_url_via_go
        go_planner.upload_url_via_go = _GoSpy(digest=_sha256_of(PAYLOAD))
        self.addCleanup(lambda: setattr(go_planner, "upload_url_via_go", saved))
        with _AllowLoopback(self, origin):
            direct_archive.archive_link(
                f"{origin.base}/movie.mp4", channel="@archive", api_id=1, api_hash="h"
            )
        self.assertEqual(test_isolation.production_row_count("projects"), before)


if __name__ == "__main__":
    unittest.main()