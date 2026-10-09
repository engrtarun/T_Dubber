"""
Source-link resolver for T_Dubber.

Turns a pasted URL into a local video file that the rest of the pipeline can
consume, and answers "is this link even supported?" before anybody waits for a
download that was never going to work.

Two entirely separate paths
--------------------------
* Telegram links (``t.me/...``) are resolved locally through Telethon against
  the project's own session, so our own archives come back byte-for-byte,
  manifest and all.
* Everything else is resolved with ``yt-dlp``, which covers every site with a
  maintained extractor.

Why an explicit allowlist
-------------------------
The app downloads whatever URL it is handed, so the resolver first checks the
scheme, then the host, and finally resolves the hostname and refuses private,
loopback, link-local and metadata addresses. Without that check a pasted link
could be used to pull files off the machine's own network (cloud instance
metadata, a router admin page, a local NAS) and hand them to a third party.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _detect_js_runtimes() -> dict:
    """Find a JavaScript runtime for yt-dlp.

    YouTube increasingly signs its player responses with JavaScript, and
    yt-dlp needs a JS engine to solve that challenge. It only auto-enables
    deno, so a machine that has node installed is needlessly treated as
    incapable -- the symptom is a warning today and outright download failures
    as YouTube tightens this up.
    """
    found = {}
    for name in ("deno", "node", "bun"):
        path = shutil.which(name)
        if path:
            found[name] = {"path": path}
    return found


JS_RUNTIMES = _detect_js_runtimes()

TELEGRAM_HOSTS = frozenset(
    {"t.me", "telegram.me", "telegram.dog", "www.t.me", "www.telegram.me"}
)

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".flv", ".ts")
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus")
DIRECT_EXTENSIONS = VIDEO_EXTENSIONS + AUDIO_EXTENSIONS

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class LinkNotSupported(ValueError):
    """The URL is malformed, unsupported, or points somewhere unsafe."""


@dataclass(frozen=True)
class SiteRule:
    label: str
    hosts: frozenset
    note: str
    needs_cookies: bool = False


# Ordered most-specific first; ``classify`` returns the first host match.
SITE_RULES: tuple = (
    SiteRule(
        "Our Telegram archive",
        TELEGRAM_HOSTS,
        "Any T_Dubber upload link. Chunked archives are rebuilt and checksum-verified.",
    ),
    SiteRule(
        "YouTube",
        frozenset(
            {
                "youtube.com", "www.youtube.com", "m.youtube.com",
                "music.youtube.com", "youtu.be", "www.youtu.be",
                "youtube-nocookie.com", "www.youtube-nocookie.com",
            }
        ),
        "Single video or Shorts. Playlists are treated as their first item.",
    ),
    SiteRule(
        "Instagram",
        frozenset({"instagram.com", "www.instagram.com", "instagr.am", "ddinstagram.com"}),
        "Reels and posts. Private accounts and some posts need a cookies file.",
        needs_cookies=True,
    ),
    SiteRule(
        "X / Twitter",
        frozenset({"twitter.com", "www.twitter.com", "x.com", "www.x.com", "mobile.twitter.com"}),
        "Single post video.",
    ),
    SiteRule(
        "Facebook",
        frozenset({"facebook.com", "www.facebook.com", "fb.watch", "fb.com"}),
        "Public posts and videos only.",
    ),
    SiteRule(
        "Reddit",
        frozenset({"reddit.com", "www.reddit.com", "old.reddit.com", "v.redd.it", "i.redd.it"}),
        "Hosted video is downloaded and remuxed.",
        needs_cookies=True,
    ),
    SiteRule(
        "TikTok",
        frozenset({"tiktok.com", "www.tiktok.com", "vm.tiktok.com"}),
        "Single videos; region-locked posts may fail.",
    ),
    SiteRule(
        "Vimeo",
        frozenset({"vimeo.com", "www.vimeo.com", "player.vimeo.com"}),
        "Public and unlisted videos.",
    ),
    SiteRule(
        "Dailymotion",
        frozenset({"dailymotion.com", "www.dailymotion.com", "dai.ly"}),
        "Public videos.",
    ),
    SiteRule(
        "Twitch",
        frozenset({"twitch.tv", "www.twitch.tv", "clips.twitch.tv"}),
        "VODs and clips.",
    ),
    SiteRule(
        "Bilibili",
        frozenset({"bilibili.com", "www.bilibili.com", "b23.tv"}),
        "Public videos; multi-part BV ids take the first part.",
    ),
    SiteRule(
        "SoundCloud",
        frozenset({"soundcloud.com", "www.soundcloud.com", "on.soundcloud.com"}),
        "Audio-only; kept as-is since dubbing needs speech.",
    ),
    SiteRule(
        "Rumble / Odysee",
        frozenset({"rumble.com", "www.rumble.com", "odysee.com", "www.odysee.com"}),
        "Public embeds.",
    ),
    SiteRule(
        "VK / OK.ru",
        frozenset({"vk.com", "www.vk.com", "ok.ru", "www.ok.ru"}),
        "Public clips.",
    ),
    SiteRule(
        "Bitchute",
        frozenset({"bitchute.com", "www.bitchute.com"}),
        "Public videos.",
    ),
    SiteRule(
        "Pinterest",
        frozenset({"pinterest.com", "www.pinterest.com", "pin.it"}),
        "Pin video links.",
    ),
    SiteRule(
        "Internet Archive",
        frozenset({"archive.org", "www.archive.org"}),
        "Public items.",
    ),
)

# Hosts that must never be dereferenced regardless of DNS answers.
_BLOCKED_HOSTNAMES = frozenset(
    {"localhost", "localhost.localdomain", "metadata.google.internal", "instance-data"}
)
_BLOCKED_SUFFIXES = (".local", ".internal", ".localdomain", ".home.arpa")

_SAFE_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class LinkVerdict:
    ok: bool
    kind: str
    label: str
    reason: str
    url: str = ""
    needs_cookies: bool = False
    warnings: list = field(default_factory=list)


@dataclass
class ResolvedMedia:
    path: str
    kind: str
    url: str
    page_url: str = ""
    title: str = ""
    size_bytes: int = 0
    duration: float = 0.0
    extractor: str = ""
    uploader: str = ""
    thumbnail_path: str = ""
    thumbnail_url: str = ""
    width: int = 0
    height: int = 0
    restored_from_manifest: bool = False
    warnings: list = field(default_factory=list)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def resolution(self) -> str:
        return f"{self.height}p" if self.height else "unknown"

    def to_metadata(self) -> dict:
        """Shape for db.upsert_media_metadata."""
        return {
            "page_url": self.page_url or self.url,
            "extractor": self.extractor,
            "uploader": self.uploader,
            "title": self.title,
            "duration": self.duration,
            "thumbnail": self.thumbnail_url,
        }


def _find_thumbnail(stem: str, workdir: str) -> str:
    """Locate the cover art yt-dlp wrote next to the media, if any."""
    if not os.path.isdir(workdir):
        return ""
    prefix = os.path.basename(stem)
    for name in sorted(os.listdir(workdir)):
        if not name.startswith(prefix):
            continue
        if name.lower().endswith((".webp", ".jpg", ".jpeg", ".png")):
            full = os.path.join(workdir, name)
            if os.path.isfile(full) and os.path.getsize(full) > 0:
                return full
    return ""


def _probe_dimensions(path: str):
    """Return (width, height) via ffprobe, or (0, 0) when it is unavailable.

    Some download shapes (a merged HLS stream, an m3u8 rendition) do not always
    yield a probeable video stream, so yt-dlp's own metadata is used as the
    fallback rather than reporting an unknown resolution.
    """
    if path and os.path.isfile(path):
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "csv=s=x:p=0", path,
                ],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0 and result.stdout.strip():
                width, height = (int(v) for v in result.stdout.strip().split(",")[:2])
                return width, height
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return 0, 0


def _dimensions_from_info(info: dict):
    """Best-effort resolution from yt-dlp metadata when ffprobe cannot tell."""
    if not isinstance(info, dict):
        return 0, 0
    width = info.get("width") or 0
    height = info.get("height") or 0
    if width and height:
        return int(width), int(height)
    # A merged download reports its height on the chosen format but leaves the
    # width implicit for portrait video, so derive it from the aspect ratio when
    # one is known.
    if height and info.get("aspect_ratio"):
        try:
            ratio = float(info["aspect_ratio"])
            if ratio and ratio < 1:
                return int(height * ratio), int(height)
        except (TypeError, ValueError):
            pass
    return 0, int(height or 0)


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


_TRANSLATION_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),      # RFC 6052 NAT64, well-known prefix
    ipaddress.ip_network("64:ff9b:1::/48"),    # RFC 8215 local-use NAT64
    ipaddress.ip_network("2002::/16"),         # 6to4
    ipaddress.ip_network("2001::/32"),         # Teredo
)


def _embedded_ipv4(address):
    """Return the IPv4 address tunnelled inside an IPv6 address, or None.

    IPv6-only networks reach the internet through NAT64, so ``x.com`` can
    legitimately resolve to something inside ``64:ff9b::/96``. Python's
    ``is_private`` flags that whole prefix, which would block legitimate hosts.
    Decoding the tunnelled IPv4 and testing *that* keeps the guard strict where
    it matters (a NAT64 gateway must not relay us to 127.0.0.1 or a LAN address)
    without breaking real connectivity.
    """
    raw = int(address)
    for network in _TRANSLATION_NETWORKS:
        if address not in network:
            continue
        if network.prefixlen == 96:
            embedded = raw & 0xFFFFFFFF
        elif network.prefixlen == 48:
            # Local-use NAT64 embeds IPv4 in the low 32 bits of the /48 prefix
            # only for the well-known layout; fall back to the low bits.
            embedded = raw & 0xFFFFFFFF
        elif network.prefixlen == 16:
            embedded = (raw >> 80) & 0xFFFFFFFF          # 6to4
        else:
            # Teredo stores the server IPv4 inverted in bytes 4..7.
            server = (raw >> 24) & 0xFFFFFFFF
            embedded = server ^ 0xFFFFFFFF
        try:
            return ipaddress.IPv4Address(embedded)
        except ipaddress.AddressValueError:
            return None
    return None


def _non_public_reason(address) -> str:
    """Return why an address is not publicly fetchable, or None if it is fine."""
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return _non_public_reason(mapped)

    for network in _TRANSLATION_NETWORKS:
        if address in network:
            embedded = _embedded_ipv4(address)
            if embedded is not None:
                return _non_public_reason(embedded)
            return None

    # Specific reasons first, because they make a far better error message
    # than a bare "not global".
    if address.is_unspecified:
        return "it is the unspecified address"
    if address.is_loopback:
        return "it is a loopback address"
    if address.is_link_local:
        return "it is a link-local address (includes cloud metadata endpoints)"
    if address.is_multicast:
        return "it is a multicast address"
    if address.is_private:
        return "it is a private network address"

    # ``is_global`` is the strict gate. It also covers the ranges the flags
    # above miss: RFC 6598 shared space (100.64.0.0/10), RFC 2544 benchmarking
    # (198.18.0.0/15), documentation ranges, and reserved space.
    if not address.is_global:
        return "it is not a globally routable address"
    return None


def assert_fetchable_url(url: str) -> str:
    """Reject anything that is not a public http(s) URL.

    Guards the app against being used as a relay into the private network it
    happens to be running on.
    """
    raw = (url or "").strip()
    if not raw:
        raise LinkNotSupported("Paste a link first.")
    if raw.startswith("//"):
        raw = "https:" + raw
    if "://" not in raw:
        raw = "https://" + raw

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise LinkNotSupported(
            f"Only http and https links work. Got '{scheme or 'no scheme'}://'."
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise LinkNotSupported("That link has no hostname in it.")
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise LinkNotSupported(
            f"Refusing to fetch '{host}'. Local and internal addresses are blocked."
        )

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if scheme == "https" else 80))
    except socket.gaierror as exc:
        raise LinkNotSupported(f"'{host}' could not be resolved: {exc}") from exc

    if not infos:
        raise LinkNotSupported(f"'{host}' did not resolve to any address.")

    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError as exc:
            raise LinkNotSupported(f"'{host}' resolved to an unusable address.") from exc
        reason = _non_public_reason(address)
        if reason:
            raise LinkNotSupported(
                f"'{host}' resolves to {address} and {reason}. Refusing to fetch it."
            )
    return raw


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _hostname(url: str) -> str:
    raw = (url or "").strip()
    if "://" not in raw:
        raw = "https://" + raw
    return (urlparse(raw).hostname or "").lower()


def _looks_like_direct_media(url: str) -> bool:
    path = urlparse(
        (url or "").strip() if "://" in (url or "") else "https://" + (url or "")
    ).path.lower()
    return path.endswith(DIRECT_EXTENSIONS)


def _guarded(url: str, kind: str, label: str, note: str, needs_cookies=False, warnings=None):
    """Run the SSRF guard, then return an accepting verdict.

    ``classify`` is what the UI shows the user as "can I use this link?", so the
    network guard has to run here too. Deferring it to the download step would
    let a pasted ``http://127.0.0.1/...`` URL report as supported and only fail
    once the user waited for the round trip.
    """
    try:
        assert_fetchable_url(url)
    except LinkNotSupported as exc:
        return LinkVerdict(False, "unsafe", label, str(exc), url)
    return LinkVerdict(
        True,
        kind,
        label,
        note,
        url,
        needs_cookies=needs_cookies,
        warnings=list(warnings or []),
    )


def classify(url: str) -> LinkVerdict:
    """Decide whether a link can be resolved, and say why not when it cannot."""
    raw = (url or "").strip()
    if not raw:
        return LinkVerdict(False, "empty", "", "Paste a link first.")

    host = _hostname(raw)
    if not host:
        return LinkVerdict(False, "invalid", "", "That does not look like a URL.")

    if host in TELEGRAM_HOSTS:
        from telegram_uploader import TelegramCloudError, parse_tg_link

        try:
            parse_tg_link(raw)
        except (TelegramCloudError, ValueError) as exc:
            return LinkVerdict(False, "telegram", "Our Telegram archive", str(exc), raw)
        return LinkVerdict(
            True,
            "telegram",
            "Our Telegram archive",
            "Resolved from your Telegram session. Chunked archives rebuild automatically.",
            raw,
        )

    for rule in SITE_RULES:
        if host in rule.hosts:
            warnings = []
            if rule.needs_cookies:
                warnings.append(
                    f"{rule.label} sometimes needs a cookies file if the download is refused."
                )
            return _guarded(
                raw, "web", rule.label, rule.note,
                needs_cookies=rule.needs_cookies, warnings=warnings,
            )

    if _looks_like_direct_media(raw):
        return _guarded(
            raw,
            "direct",
            "Direct media file",
            "A direct link to a media file. Downloaded verbatim without re-encoding.",
        )

    return LinkVerdict(
        False,
        "unsupported",
        "",
        (
            f"'{host}' is not in the supported list. Supported: our own Telegram "
            "archive links, YouTube, Instagram, X/Twitter, Facebook, Reddit, "
            "TikTok, Vimeo, Dailymotion, Twitch, Bilibili, SoundCloud, Rumble, "
            "Odysee, VK, OK.ru, Pinterest, Internet Archive, or a direct link "
            "ending in .mp4/.mkv/.webm/.mp3. If the site has a yt-dlp extractor "
            "but is missing here, add it to SITE_RULES in link_resolver.py."
        ),
        raw,
    )


def describe_support() -> list:
    """Return the supported-site table, for rendering help inside the UI."""
    rows = [
        {
            "label": rule.label,
            "example": sorted(rule.hosts)[0],
            "note": rule.note,
            "needs_cookies": rule.needs_cookies,
        }
        for rule in SITE_RULES
    ]
    rows.append(
        {
            "label": "Direct media file",
            "example": "https://host/path/movie.mp4",
            "note": "Any https link ending in a known media extension.",
            "needs_cookies": False,
        }
    )
    return rows


def support_matrix_markdown() -> str:
    lines = [
        "| Source | Example | Notes |",
        "| --- | --- | --- |",
    ]
    for row in describe_support():
        note = row["note"] + (" (cookies may be needed)" if row["needs_cookies"] else "")
        lines.append(f"| {row['label']} | `{row['example']}` | {note} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------


def _require_yt_dlp():
    try:
        import yt_dlp
    except ImportError as exc:
        raise LinkNotSupported(
            "yt-dlp is not installed, so web links cannot be resolved. Run "
            "`pip install \"yt-dlp>=2026.3.17\"`. Telegram links still work."
        ) from exc
    return yt_dlp


def _safe_stem(text: str, fallback: str = "source") -> str:
    cleaned = _SAFE_SLUG.sub("_", (text or "").strip()).strip("._")
    return (cleaned[:80] or fallback)


def _build_ytdlp_opts(outtmpl_stem: str, max_height: int, cookies_file: str = None) -> dict:
    """Format selection mirrors mazinger's own rule.

    ``res:N`` orders renditions by the *smaller* frame dimension, so it caps
    height correctly for both landscape video and vertical Shorts -- unlike a
    ``[height<=N]`` filter, which silently rejects tall videos.
    """
    return {
        "format": "bestvideo*+bestaudio/best",
        "format_sort": [f"res:{max_height}"],
        "merge_output_format": "mp4",
        "outtmpl": f"{outtmpl_stem}.%(ext)s",
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": False,
        "no_warnings": False,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "continuedl": True,
        "concurrent_fragment_downloads": 4,
        "overwrites": True,
        "windowsfilenames": True,
        "trim_file_name": 120,
        # The cover art is worth keeping: it is what makes an archive row in the
        # Telegram channel recognisable at a glance instead of a bare filename.
        "writethumbnail": True,
        "writesubtitles": False,
        # YouTube exposes many thumbnail sizes and most of the high-numbered
        # ones 404. Without this yt-dlp walks down from the top, issuing a failed
        # request per candidate before landing on a real one.
        "throttledratelimit": 2,
        "http_headers": {"User-Agent": DEFAULT_USER_AGENT},
        "cookiefile": cookies_file or None,
        "js_runtimes": dict(JS_RUNTIMES) if JS_RUNTIMES else None,
    }


def _pick_downloaded_file(stem: str, workdir: str):
    """Return the finished media file, preferring video containers by size."""
    if not os.path.isdir(workdir):
        return None
    prefix = os.path.basename(stem)
    candidates = []
    for name in os.listdir(workdir):
        if not name.startswith(prefix):
            continue
        lowered = name.lower()
        if lowered.endswith((".part", ".ytdl", ".temp", ".json")):
            continue
        if not lowered.endswith(DIRECT_EXTENSIONS):
            continue
        full = os.path.join(workdir, name)
        if os.path.isfile(full) and os.path.getsize(full) > 0:
            candidates.append(full)
    if not candidates:
        # yt-dlp's windows filenames can differ from the probe stem by a
        # special character or trailing punctuation, so fall back to any real
        # media file written into this fresh workdir.
        for name in os.listdir(workdir):
            lowered = name.lower()
            if lowered.endswith((".part", ".ytdl", ".temp", ".json", ".webp", ".jpg", ".jpeg", ".png")):
                continue
            if not lowered.endswith(DIRECT_EXTENSIONS):
                continue
            full = os.path.join(workdir, name)
            if os.path.isfile(full) and os.path.getsize(full) > 0:
                candidates.append(full)
    if not candidates:
        return None
    videos = [c for c in candidates if c.lower().endswith(VIDEO_EXTENSIONS)]
    pool = videos or candidates
    return max(pool, key=lambda p: (len(os.path.splitext(p)[1]), os.path.getsize(p)))


def _translate_download_error(exc: Exception, url: str, verdict: LinkVerdict) -> str:
    message = str(exc)
    lowered = message.lower()
    hints = []
    if "login required" in lowered or "sign in" in lowered or "cookies" in lowered:
        hints.append(
            "This site wants a logged-in session. Export cookies.txt for the "
            "browser profile that can see the post and add it in Connection "
            "Settings."
        )
    if "private" in lowered or "not available" in lowered:
        hints.append("The post is private or was deleted.")
    if "unsupported url" in lowered:
        hints.append(
            "yt-dlp has no extractor for this URL. Open the video in a browser "
            "and grab the direct .mp4 URL instead."
        )
    if "429" in lowered or "too many requests" in lowered:
        hints.append("The site rate-limited us. Wait a minute and retry.")
    if "js runtime" in lowered or "ejs" in lowered or "challenge" in lowered:
        hints.append(
            "This site needs a JavaScript runtime for its player challenge. "
            "Install Node.js (already found at "
            f"{shutil.which('node') or 'not on PATH'}) or Deno, then retry."
        )
    if "ffmpeg" in lowered or "ffprobe" in lowered:
        hints.append("ffmpeg is required to merge video and audio. Install it and retry.")
    if not hints:
        hints.append(message.strip().splitlines()[-1][:400] if message.strip() else repr(exc))
    return "Download failed for " + url + "\n\n" + "\n".join(f"- {h}" for h in hints)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def probe(url: str, telegram_credentials: dict = None) -> dict:
    """Read metadata for a link without downloading the media."""
    verdict = classify(url)
    if not verdict.ok:
        return {"ok": False, "kind": verdict.kind, "reason": verdict.reason}

    if verdict.kind == "telegram":
        if not telegram_credentials:
            return {
                "ok": False,
                "kind": "telegram",
                "reason": "Save your Telegram settings first so we can read the archive.",
            }
        import telegram_uploader as tg

        try:
            described = tg.describe_link(
                url,
                telegram_credentials.get("api_id"),
                telegram_credentials.get("api_hash"),
                telegram_credentials.get("phone"),
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "kind": "telegram", "reason": str(exc)}
        return {
            "ok": True,
            "kind": "telegram",
            "title": described["filename"],
            "size_bytes": described["size"],
            "size_label": tg.human_bytes(described["size"]),
            "extractor": "telegram",
            "restored_from_manifest": described["is_manifest"],
            "warnings": [],
        }

    yt_dlp = _require_yt_dlp()
    try:
        with yt_dlp.YoutubeDL(
            {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "skip_download": True,
                "http_headers": {"User-Agent": DEFAULT_USER_AGENT},
                "js_runtimes": dict(JS_RUNTIMES) if JS_RUNTIMES else None,
            }
        ) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        return {
            "ok": False,
            "kind": verdict.kind,
            "reason": _translate_download_error(exc, url, verdict),
        }

    if not info:
        return {"ok": False, "kind": verdict.kind, "reason": "No metadata came back."}

    filesize = info.get("filesize") or info.get("filesize_approx") or 0
    return {
        "ok": True,
        "kind": verdict.kind,
        "title": info.get("title") or "Untitled",
        "size_bytes": int(filesize or 0),
        "size_label": _human(int(filesize or 0)),
        "duration": float(info.get("duration") or 0.0),
        "extractor": info.get("extractor_key") or info.get("extractor") or "generic",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "thumbnail": info.get("thumbnail") or "",
        "warnings": verdict.warnings,
    }


def resolve_to_local_file(
    url: str,
    workdir: str,
    telegram_credentials: dict = None,
    max_height: int = 720,
    progress_callback=None,
    cookies_file: str = None,
) -> ResolvedMedia:
    """Download a supported link into ``workdir`` and describe the result.

    ``progress_callback`` receives dicts with ``phase``, ``current``, ``total``
    and ``message``.
    """
    verdict = classify(url)
    if not verdict.ok:
        raise LinkNotSupported(verdict.reason)

    os.makedirs(workdir, exist_ok=True)

    if verdict.kind == "telegram":
        return _resolve_telegram(url, workdir, telegram_credentials, progress_callback)

    yt_dlp = _require_yt_dlp()
    safe_url = assert_fetchable_url(url)

    # Peek at metadata first so the on-disk name carries the real title.
    try:
        with yt_dlp.YoutubeDL(
            {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "skip_download": True,
                "http_headers": {"User-Agent": DEFAULT_USER_AGENT},
                "js_runtimes": dict(JS_RUNTIMES) if JS_RUNTIMES else None,
            }
        ) as ydl:
            probe_info = ydl.extract_info(safe_url, download=False) or {}
    except Exception:
        probe_info = {}

    title = probe_info.get("title") or "source"
    stem = _safe_stem(title, "source")
    stem_path = os.path.join(workdir, stem)
    total_hint = int(
        probe_info.get("filesize") or probe_info.get("filesize_approx") or 0
    )

    def hook(status):
        if status.get("status") == "downloading":
            _emit(
                progress_callback,
                phase="download",
                current=int(status.get("downloaded_bytes") or 0),
                total=int(status.get("total_bytes") or total_hint or 0),
                message=f"Downloading {title}",
            )
        elif status.get("status") == "finished":
            _emit(
                progress_callback,
                phase="merge",
                current=int(status.get("total_bytes") or 0),
                total=int(status.get("total_bytes") or total_hint or 0),
                message="Merging video and audio",
            )

    opts = _build_ytdlp_opts(stem_path, max_height, cookies_file)
    opts["progress_hooks"] = [hook]

    _emit(
        progress_callback,
        phase="start",
        current=0,
        total=total_hint,
        message=f"Resolving {verdict.label} link for '{title}'",
    )

    # Two-pass download: first the Studio/mazinger rule (best video + best
    # audio merged into mp4), then -- only if that finished media file never
    # appeared -- a single ready-to-play rendition. The fallback re-uses the
    # exact same outtmpl/format_sort, so Studio and Drive behave identically
    # and neither ever bails out with "success but produced no media file".
    info = {}
    final_path = None
    last_error = None
    for format_expr in ("bestvideo*+bestaudio/best", "best"):
        attempt = dict(opts)
        attempt["format"] = format_expr
        try:
            with yt_dlp.YoutubeDL(attempt) as ydl:
                info = ydl.extract_info(safe_url, download=True)
                if info and info.get("_type") == "playlist":
                    entries = [e for e in (info.get("entries") or []) if e]
                    if not entries:
                        raise LinkNotSupported("That playlist had no downloadable items.")
                    info = entries[0]
        except LinkNotSupported:
            raise
        except Exception as exc:
            last_error = exc
            final_path = None
        else:
            final_path = _pick_downloaded_file(stem_path, workdir)
        if final_path is not None:
            break
        # A failed attempt leaves partial fragments; clear them before the retry
        # so the second pass starts from a clean slate.
        try:
            for name in os.listdir(workdir):
                if name.startswith(os.path.basename(stem_path)) and not name.endswith(
                    (".json",)
                ):
                    try:
                        os.remove(os.path.join(workdir, name))
                    except OSError:
                        pass
        except OSError:
            pass

    if final_path is None:
        if last_error is not None:
            raise LinkNotSupported(
                _translate_download_error(last_error, url, verdict)
            ) from last_error
        raise LinkNotSupported(
            "The download reported success but produced no media file. This "
            "usually means the video stream needed ffmpeg to be remuxed."
        )

    size = os.path.getsize(final_path)
    _emit(
        progress_callback,
        phase="complete",
        current=size,
        total=size,
        message=f"Fetched {os.path.basename(final_path)} ({_human(size)})",
    )

    width, height = _probe_dimensions(final_path)
    if not height:
        width, height = _dimensions_from_info(info)
    if not height:
        width, height = _dimensions_from_info(probe_info)

    return ResolvedMedia(
        path=final_path,
        kind=verdict.kind,
        url=safe_url,
        page_url=probe_info.get("webpage_url") or safe_url,
        title=info.get("title") or title,
        size_bytes=size,
        duration=float(info.get("duration") or probe_info.get("duration") or 0.0),
        extractor=info.get("extractor_key") or verdict.label,
        uploader=info.get("uploader") or probe_info.get("uploader") or "",
        thumbnail_path=_find_thumbnail(stem_path, workdir),
        thumbnail_url=probe_info.get("thumbnail") or "",
        width=width,
        height=height,
        warnings=verdict.warnings,
    )


# ---------------------------------------------------------------------------
# Zero-disk resolution
# ---------------------------------------------------------------------------
#
# ``resolve_to_local_file`` above ends with a file on this machine. That is the
# right answer when the next step is dubbing, because the pipeline needs a real
# path to work on. It is the wrong answer when the only thing wanted is the
# video sitting safe in a Telegram channel: 9 GB of source then means 9 GB of
# disk on a laptop that has to be free at that exact moment.
#
# ``resolve_to_stream`` is the other half. It answers "give me a URL tgup can
# range-stream" without ever opening a file for writing, so the archive happens
# with 0 bytes on this PC. Everything it does is a HEAD, a one-byte GET, or a
# yt-dlp *simulate* -- metadata in, no payload out.


class StreamNotStreamable(LinkNotSupported):
    """The link cannot be turned into a range-streamable media URL."""


@dataclass(frozen=True)
class StreamTarget:
    """A media URL tgup can stream part by part, or the reason it cannot.

    ``direct=False`` is a normal answer, not an error: a page URL with no
    extractable media URL, or an origin that will not serve byte ranges, both
    come back this way with ``reason`` filled in. The caller is expected to fall
    back to :func:`resolve_to_local_file` -- loudly, never silently.
    """

    kind: str
    url: str
    size: int
    filename: str
    page_url: str = ""
    direct: bool = False
    reason: str = ""
    title: str = ""
    extractor: str = ""


def _env_flag(name: str, default: bool = True) -> bool:
    """Read a boolean kill-switch from the environment.

    Same contract as ``pipeline._env_flag``: only an explicit falsey value turns
    a feature off, so a typo in the environment can never silently disable it.
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in ("0", "off", "false", "no")


def _content_range_total(value: str) -> int:
    """Read the total size out of a ``Content-Range: bytes 0-0/12345`` header.

    Returns 0 when the header is absent or the ``/size`` part is ``*``, which is
    how an origin says "I know how much I have, actually no I do not".
    """
    match = re.search(r"/\s*(\d+)\s*$", (value or "").strip())
    return int(match.group(1)) if match else 0


def probe_remote_size(url: str, timeout: float = 15.0) -> dict:
    """Ask the origin how big it is, without downloading it.

    HEAD first because it costs nothing, then a single-byte range GET for the
    servers that answer HEAD with a 405 or an empty ``Content-Length`` (common
    on CDN-backed signed URLs). The GET is deliberately one byte: proving the
    origin serves ranges is worth exactly one byte.

    Returns ``{"size": int, "ranges": bool, "method": str, "reason": str}``.
    ``ranges`` False means tgup will refuse this URL, and the caller should say
    so rather than attempt an upload that is guaranteed to fail.
    """
    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
    size = 0
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, method="HEAD", headers=headers),
            timeout=timeout,
        ) as response:
            length = response.headers.get("Content-Length")
            if length and str(length).strip().isdigit():
                size = int(length.strip())
            if size == 0:
                return {
                    "size": 0, "ranges": False, "method": "HEAD",
                    "reason": "the origin's HEAD reply carried no usable Content-Length",
                }
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 405, 501):
            return {
                "size": 0, "ranges": False, "method": "HEAD",
                "reason": f"the origin refused HEAD with HTTP {exc.code}",
            }
        # 403/405/501 on HEAD is common on signed CDN URLs. It says nothing
        # about range support, so fall through to the byte probe.
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"size": 0, "ranges": False, "method": "HEAD",
                "reason": f"HEAD failed: {exc}"}

    # A Content-Length is a size, not a promise. Only ``Accept-Ranges: bytes``
    # plus an actual 206 says the origin will stream, and plenty of origins
    # advertise one while ignoring the other -- so range support is confirmed by
    # asking for one byte. Trusting the header instead would hand tgup a URL it
    # is guaranteed to fail on, and the failure looks like a network blip.
    ranged_headers = dict(headers)
    ranged_headers["Range"] = "bytes=0-0"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=ranged_headers), timeout=timeout
        ) as response:
            total = _content_range_total(response.headers.get("Content-Range", ""))
            return {
                "size": total or size,
                "ranges": total > 0,
                "method": "range-GET",
                "reason": "" if total > 0 else (
                    "the origin ignored the Range header, so tgup cannot "
                    "stream it and would have to download the whole file"
                ),
            }
    except urllib.error.HTTPError as exc:
        return {
            "size": 0, "ranges": False, "method": "range-GET",
            "reason": (
                f"the origin answered a range request with HTTP {exc.code}, so "
                "it cannot serve byte ranges"
            ),
        }
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"size": 0, "ranges": False, "method": "range-GET",
                "reason": f"the range probe failed: {exc}"}


def _stream_from_ytdlp(url: str, verdict: LinkVerdict, timeout: float) -> StreamTarget:
    """Ask yt-dlp for the direct media URL, in simulate-only mode.

    ``download=False`` is the whole point: yt-dlp negotiates the player, gets the
    signed media URL and reports its size and title, and then stops. Nothing is
    written to disk because nothing is ever opened for writing.

    The format selection mirrors ``_build_ytdlp_opts`` so this picks the same
    rendition the download path would have -- otherwise the archived copy would
    silently be a different quality from the dubbed one.
    """
    try:
        yt_dlp = _require_yt_dlp()
    except LinkNotSupported as exc:
        return StreamTarget(
            kind=verdict.kind, url="", size=0, filename="",
            page_url=url, direct=False,
            reason=(
                f"{exc} Zero-disk streaming needs it because a page URL has no "
                "media URL to stream until an extractor finds one."
            ),
        )

    opts = {
        "format": "bestvideo*+bestaudio/best",
        "format_sort": ["res:720"],
        "merge_output_format": "mp4",
        "noplaylist": True,
        "playlist_items": "1",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "simulate": True,
        "socket_timeout": max(1.0, float(timeout)),
        "http_headers": {"User-Agent": DEFAULT_USER_AGENT},
        "js_runtimes": dict(JS_RUNTIMES) if JS_RUNTIMES else None,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception as exc:  # noqa: BLE001
        return StreamTarget(
            kind=verdict.kind, url="", size=0, filename="",
            page_url=url, direct=False,
            reason=_translate_download_error(exc, url, verdict),
        )

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return StreamTarget(
                kind=verdict.kind, url="", size=0, filename="", page_url=url,
                direct=False, reason="That playlist had no downloadable items.",
            )
        info = entries[0]

    fmt = info.get("requested_formats") or [info]
    if len(fmt) > 1:
        # A video+audio pair has to be merged to become one file. Merging is
        # ffmpeg work on a local file, i.e. exactly the disk use this path
        # exists to avoid, so the caller is told rather than handed a URL tgup
        # would fail on.
        return StreamTarget(
            kind=verdict.kind, url="", size=0, filename="", page_url=url,
            direct=False,
            reason=(
                "that link's best rendition is separate video and audio "
                "streams, which have to be merged into one file first. That is "
                "a disk operation -- use the normal download path for it."
            ),
        )

    chosen = fmt[0] if fmt else {}
    media_url = chosen.get("url") or ""
    if not media_url:
        return StreamTarget(
            kind=verdict.kind, url="", size=0, filename="", page_url=url,
            direct=False,
            reason="yt-dlp found no media URL for that link.",
        )

    title = info.get("title") or "source"
    filename = chosen.get("filename") or f"{_safe_stem(title, 'source')}.mp4"
    size = int(
        chosen.get("filesize")
        or chosen.get("filesize_approx")
        or info.get("filesize")
        or info.get("filesize_approx")
        or 0
    )
    return StreamTarget(
        kind=verdict.kind,
        url=media_url,
        size=size,
        filename=os.path.basename(filename),
        page_url=info.get("webpage_url") or url,
        direct=True,
        reason="",
        title=title,
        extractor=info.get("extractor_key") or verdict.label,
    )


def resolve_to_stream(url: str, timeout: float = 15.0) -> StreamTarget:
    """Resolve a link to something tgup can stream straight into Telegram.

    This is the zero-disk front half of ``tgup --url``: it never writes a byte
    to this machine, so a 9 GB video can be archived without 9 GB of free disk.

    Direct media URLs are size-probed and range-checked (one HEAD, or one
    single-byte GET, never the body). Page URLs go through yt-dlp in
    simulate-only mode to find the media URL. Either way the SSRF guard runs
    first -- the media URL that comes back from a third party is untrusted until
    ``assert_fetchable_url`` has cleared it, exactly like a pasted link.

    Returns a :class:`StreamTarget`. ``direct=False`` means "cannot stream this",
    with the reason filled in; it never means "downloaded it instead".
    """
    if not _env_flag("TDUBBER_DIRECT_STREAM", True):
        raise StreamNotStreamable(
            "Zero-disk streaming is switched off (TDUBBER_DIRECT_STREAM). Use "
            "the normal download path instead."
        )

    raw = (url or "").strip()
    if not raw:
        raise StreamNotStreamable("Paste a link first.")

    verdict = classify(raw)
    if not verdict.ok:
        raise StreamNotStreamable(verdict.reason)

    if verdict.kind == "telegram":
        raise StreamNotStreamable(
            "That is a link to our own Telegram archive. It has no media URL "
            "to stream from -- open the archive itself, or download the file."
        )

    safe_url = assert_fetchable_url(raw)

    if verdict.kind == "direct":
        if not _looks_like_direct_media(safe_url):
            raise StreamNotStreamable(
                f"'{safe_url}' does not end in a media extension "
                f"({', '.join(DIRECT_EXTENSIONS)}), so it cannot be recognised "
                "as a direct file."
            )
        probe = probe_remote_size(safe_url, timeout=timeout)
        if not probe["ranges"]:
            return StreamTarget(
                kind="direct", url=safe_url, size=0, filename="",
                page_url=safe_url, direct=False,
                reason=(
                    f"{probe['reason']}. Streaming it would mean downloading it, "
                    "which is exactly what this path refuses to do -- use the "
                    "normal download path instead."
                ),
            )
        return StreamTarget(
            kind="direct",
            url=safe_url,
            size=int(probe["size"]),
            filename=os.path.basename(urlparse(safe_url).path) or "source.mp4",
            page_url=safe_url,
            direct=True,
            reason="",
            extractor="direct",
        )

    return _stream_from_ytdlp(safe_url, verdict, timeout)


def _human(count: int) -> str:
    """Byte formatting, shared with the uploader without importing it at module
    scope (telegram_uploader imports this module for its helpers)."""
    from telegram_uploader import human_bytes

    return human_bytes(count)


def build_caption(media: ResolvedMedia, kind: str = "source") -> str:
    """Compose a Telegram caption that identifies the media at a glance.

    A channel full of rows named ``download.mp4`` is unnavigable, so the caption
    carries the real title, who published it, how long it is, and a stable set of
    tags that make the row searchable later. Telegram captions cap at 1024
    characters, so the tag list is trimmed rather than letting the text be
    silently dropped.
    """
    icons = {"source": "🎬", "output": "🎞️", "archive": "☁️"}
    lines = [f'{icons.get(kind, "📁")} {media.title or os.path.basename(media.path)}']

    facts = []
    if media.uploader:
        facts.append(f"👤 {media.uploader}")
    if media.duration:
        minutes, seconds = divmod(int(media.duration), 60)
        facts.append(f"⏱ {minutes}m {seconds}s")
    if media.height:
        facts.append(f"🎥 {media.height}p")
    facts.append(f"💾 {_human(media.size_bytes)}")
    lines.append(" · ".join(facts))

    if media.page_url or media.url:
        lines.append(f'🔗 {media.page_url or media.url}')

    tags = build_tags(media)
    if tags:
        lines.append(" ".join(f"#{tag}" for tag in tags))

    caption = "\n".join(lines)
    if len(caption) > 1024:
        # Drop the URL first: the tags are what make a row findable, and the
        # link is usually recoverable from the manifest.
        short = "\n".join(line for line in lines if not line.startswith("🔗"))
        caption = short if len(short) <= 1024 else short[:1021] + "..."
    return caption


def _tag(value: str, max_length: int = 32) -> str:
    """Make a string safe to use as a Telegram hashtag.

    Hashtag parsing stops at the first character outside letters, digits and
    underscore, so "Sci-Fi" becomes "Sci_Fi" rather than a bare "Sci".
    """
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "")).strip("_")
    return cleaned[:max_length]


def build_tags(media: ResolvedMedia) -> list:
    """Derive searchable hashtags from the media's own metadata.

    Every tag is lowercased because Telegram treats hashtags case-insensitively,
    so #Netflix and #netflix would otherwise both exist and fragment any search.
    """
    tags = ["T_Dubber"]
    if media.kind == "telegram":
        tags.append("restored")
    elif media.kind in ("web", "direct"):
        tags.append(_tag(media.extractor or "web").lower())
    if media.uploader:
        slug = _tag(media.uploader, 24).lower()
        if slug:
            tags.append(slug)
    if media.duration:
        hours, minutes = divmod(int(media.duration) // 60, 60)
        tags.append(f"{hours}h{minutes:02d}m" if hours else f"{minutes}min")
    if media.height:
        tags.append(f"{media.height}p")
    # Preserve order while dropping duplicates.
    seen = set()
    return [t for t in tags if t and not (t.lower() in seen or seen.add(t.lower()))]


def _resolve_telegram(url, workdir, telegram_credentials, progress_callback) -> ResolvedMedia:
    if not telegram_credentials:
        raise LinkNotSupported(
            "Save your Telegram settings in Connection Settings first; the "
            "archive lives in your own channel."
        )
    import telegram_uploader as tg

    try:
        path = tg.download_or_restore(
            url,
            telegram_credentials.get("api_id"),
            telegram_credentials.get("api_hash"),
            telegram_credentials.get("phone"),
            workdir,
            progress_callback=progress_callback,
            verify=True,
        )
    except tg.TelegramCloudError as exc:
        raise LinkNotSupported(str(exc)) from exc

    size = os.path.getsize(path)
    width, height = _probe_dimensions(path)
    return ResolvedMedia(
        path=path,
        kind="telegram",
        url=url,
        page_url=url,
        title=os.path.basename(path),
        size_bytes=size,
        extractor="telegram",
        width=width,
        height=height,
        restored_from_manifest="part" not in os.path.basename(path).lower(),
    )


def _emit(callback, **payload):
    if callback is None:
        return
    try:
        callback(payload)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _selftest() -> None:
    allowed = [
        ("https://youtu.be/dQw4w9WgXcQ", "web", "YouTube"),
        ("https://www.youtube.com/watch?v=abc", "web", "YouTube"),
        ("https://music.youtube.com/watch?v=abc", "web", "YouTube"),
        ("https://www.instagram.com/reel/Cxyz/", "web", "Instagram"),
        ("https://x.com/user/status/1", "web", "X / Twitter"),
        ("https://v.redd.it/abc", "web", "Reddit"),
        ("https://www.tiktok.com/@u/video/1", "web", "TikTok"),
        ("https://vimeo.com/12345", "web", "Vimeo"),
        ("https://example.com/movie.mp4", "direct", "Direct media file"),
        ("https://example.com/movie.MKV", "direct", "Direct media file"),
        ("https://t.me/tgwebcloud1/12", "telegram", "Our Telegram archive"),
    ]
    for url, kind, label in allowed:
        verdict = classify(url)
        assert verdict.ok, f"{url} -> {verdict.reason}"
        assert verdict.kind == kind, f"{url} expected {kind}, got {verdict.kind}"
        assert verdict.label == label, f"{url} expected {label}, got {verdict.label}"

    rejected = [
        "http://localhost:7860/file.mp4",
        "http://127.0.0.1/x.mp4",
        "http://192.168.1.10/movie.mp4",
        "http://10.0.0.5/x.mp4",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "file:///C:/Windows/System32/config/SAM",
        "ftp://example.com/movie.mp4",
        "",
        "not a url at all",
    ]
    for url in rejected:
        verdict = classify(url)
        assert not verdict.ok, f"{url} should have been rejected but passed"

    for url in ("http://localhost/x", "http://127.0.0.1/x", "file:///c:/x"):
        try:
            assert_fetchable_url(url)
        except LinkNotSupported:
            pass
        else:
            raise AssertionError(f"assert_fetchable_url allowed {url}")

    assert _safe_stem("Ek Chatur Naar (2025) [1080p] x264") == "Ek_Chatur_Naar_2025_1080p_x264"
    assert json.dumps(describe_support())
    assert isinstance(JS_RUNTIMES, dict)
    assert "js_runtimes" in _build_ytdlp_opts("out", 720)

    # IPv6 transition mechanisms must not trip the guard for legitimate hosts,
    # but must still be checked against the IPv4 they actually tunnel to.
    assert _non_public_reason(ipaddress.ip_address("64:ff9b::7f00:1")) == (
        "it is a loopback address"
    ), "NAT64 pointing at 127.0.0.1 must be blocked"
    assert _non_public_reason(ipaddress.ip_address("64:ff9b::ac42:e3")) is None, (
        "NAT64 pointing at a public IPv4 must be allowed"
    )
    assert _non_public_reason(ipaddress.ip_address("2002:7f00:0001::")) == (
        "it is a loopback address"
    ), "6to4 pointing at 127.0.0.1 must be blocked"
    assert _non_public_reason(ipaddress.ip_address("2002:0808:0808::")) is None, (
        "6to4 pointing at 8.8.8.8 must be allowed"
    )
    assert _non_public_reason(ipaddress.ip_address("::ffff:10.0.0.5")) == (
        "it is a private network address"
    ), "IPv4-mapped private address must be blocked"
    assert _non_public_reason(ipaddress.ip_address("::ffff:8.8.8.8")) is None
    assert _non_public_reason(ipaddress.ip_address("169.254.169.254")) is not None
    assert _non_public_reason(ipaddress.ip_address("172.16.0.1")) is not None
    assert _non_public_reason(ipaddress.ip_address("100.64.0.1")) is not None, (
        "carrier-grade NAT space must be blocked"
    )

    print(f"OK  {len(allowed)} supported links classified correctly")
    print(f"OK  {len(rejected)} unsafe/unsupported links rejected with a reason")
    print("OK  SSRF guard blocks loopback, private, link-local and file:// URLs")
    print("OK  IPv6 NAT64 / 6to4 / IPv4-mapped addresses decode and check correctly")
    print(
        "OK  yt-dlp JS runtime wired up: "
        + (", ".join(sorted(JS_RUNTIMES)) if JS_RUNTIMES else "NONE FOUND (warn on use)")
    )
    print(f"OK  support matrix renders ({len(describe_support())} rows)")


if __name__ == "__main__":
    _selftest()
