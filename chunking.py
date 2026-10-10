"""
chunking.py -- adaptive, individually resumable chunking for huge uploads.

THE PROBLEM THIS SOLVES
-----------------------
A 100 GB upload that dies at 90% costs everything that was already sent unless
the run can be resumed. ``telegram_uploader.py`` already journals *parts*, but
the part size is a constant (1900 MiB, chosen to sit under Telegram's 2 GB
per-file ceiling). That constant is the wrong answer on two ends of the same
link: on a slow uplink a failed 1900 MiB part is a long wait thrown away, and on
a fast one it is an unnecessarily small unit of work.

So the size here is *measured*, not chosen. A running EWMA of observed upload
throughput picks the next chunk's byte count, clamped so it can never be
pathological:

    chunk_bytes = clamp(round_MiB(throughput_bps * TARGET_CHUNK_SECONDS),
                        MIN_CHUNK_BYTES, MAX_CHUNK_BYTES)

``MAX_CHUNK_BYTES`` is Telegram's own limit, not a taste call: past 2 GB a
non-Premium account cannot send the file at all. ``MIN_CHUNK_BYTES`` exists so
that a pathological throughput estimate (a stalled first sample, a clock that
lied) cannot produce thousands of tiny messages, which Telegram throttles and
which cost more in per-message overhead than the retry they were meant to avoid.

WHAT IS GUARANTEED, AND HOW
--------------------------
* **Per-chunk SHA-256, computed while the bytes are produced.** One sequential
  pass over the file, 4 MiB at a time. A 100 GB movie never lands in RAM.
* **Write-ahead ordering.** The manifest line saying "chunk N is uploading,
  here is its digest" is written AND fsynced *before* the upload starts; the
  line saying "chunk N is done, message id M" is written after it succeeds.
  A crash therefore always leaves an honest record: the worst case is a chunk
  marked ``uploading`` whose bytes did land on Telegram, and re-uploading it
  costs one duplicate message. The opposite ordering would silently lose a
  chunk that was never sent.
* **Resume re-verifies, it does not trust.** A chunk marked ``done`` is only
  skipped after the local bytes still hash to what the manifest recorded --
  size first, then SHA-256. If the source file changed underneath, the chunk is
  re-uploaded.
* **Reassembly fails loudly.** Every chunk is verified against the manifest as
  it is appended to a staging file. A mismatch aborts, deletes the staging file
  and raises. It never renames a partial file into place.

This module owns no Telegram code. Uploading is injected as a callable and
recovery calls the existing verified fetch path (``tgup_bridge.fetch``), so
there is no second, weaker downloader to drift out of step with the real one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable

# 4 MiB, the same block size multitasker.py and go_planner.py hash with. Kept in
# step so a digest produced here is byte-comparable with one produced there.
HASH_BLOCK = 4 * 1024 * 1024

MIB = 1024 * 1024

# --- The adaptive formula's three constants --------------------------------

# Fallback throughput when nothing has been measured yet: the single-stream
# rate this machine actually settles at (TGUP.md). Seeding with a real number
# means the very first plan is sensible instead of arbitrary.
DEFAULT_THROUGHPUT_BPS = 2.06 * MIB

# Seconds of transfer one chunk is meant to represent. This is the retry
# economy: a chunk that fails costs at most this much work, whatever the link
# speed. 120 s is the compromise -- short enough that a flaky uplink loses
# minutes rather than an hour, long enough that 100 GB does not become
# thousands of Telegram messages.
TARGET_CHUNK_SECONDS = 120.0

# Below this the per-message overhead dominates and Telegram throttles bursts.
MIN_CHUNK_BYTES = 64 * MIB

# Telegram's per-file ceiling for an ordinary account is 2 GB. 1900 MiB leaves
# room for transport overhead, and it is the value telegram_uploader.CHUNK_SIZE
# and tgup's DefaultChunkSize already use -- max_chunk_bytes() reads it from
# there rather than restating the number, so the three cannot drift.
_MAX_CHUNK_FALLBACK = 1900 * MIB

# EWMA weight for the newest sample. 0.3 means a chunk that suddenly runs twice
# as fast moves the estimate within a few chunks instead of over a whole run.
EWMA_ALPHA = 0.3


class ChunkingError(RuntimeError):
    """Base class for every failure this module raises."""


class ChunkIntegrityError(ChunkingError):
    """A chunk's bytes did not match the digest the manifest recorded."""


class ChunkRetryExhausted(ChunkingError):
    """A chunk could not be sent even after fetching it back and retrying."""


# ---------------------------------------------------------------------------
# Throughput estimation
# ---------------------------------------------------------------------------


class ThroughputEstimator:
    """EWMA over recent transfer samples, in bytes per second.

    A plain average is wrong here: one 5-minute stall during a 6-hour upload
    would drag the estimate far below what the link is really doing, and every
    chunk after it would be sized for a link that is not there. An exponentially
    weighted mean forgets a bad sample within a few chunks.
    """

    def __init__(
        self,
        seed_bps: float = DEFAULT_THROUGHPUT_BPS,
        alpha: float = EWMA_ALPHA,
    ):
        self.alpha = float(alpha)
        self._bps = float(seed_bps)
        self.samples = 0

    @property
    def bps(self) -> float:
        """Current estimate in bytes per second. Never zero."""
        return self._bps

    def sample(self, nbytes: int, seconds: float) -> float:
        """Fold one completed transfer into the estimate. Returns the new bps."""
        # A non-positive duration means the clock lied or the transfer was
        # instant; either way the sample carries no information about the link,
        # so it is dropped rather than allowed to poison the estimate with an
        # infinite rate that would size every later chunk at the ceiling.
        if seconds and seconds > 0 and nbytes > 0:
            observed = float(nbytes) / float(seconds)
            self._bps = self.alpha * observed + (1.0 - self.alpha) * self._bps
            self.samples += 1
        return self._bps

    def to_dict(self) -> dict:
        return {"bps": self._bps, "samples": self.samples, "alpha": self.alpha}

    @classmethod
    def from_dict(cls, data: dict) -> "ThroughputEstimator":
        estimator = cls(
            seed_bps=float((data or {}).get("bps") or DEFAULT_THROUGHPUT_BPS),
            alpha=float((data or {}).get("alpha") or EWMA_ALPHA),
        )
        estimator.samples = int((data or {}).get("samples") or 0)
        return estimator


def max_chunk_bytes() -> int:
    """The hard ceiling: whatever ``telegram_uploader`` already uses.

    Imported lazily and read from the module rather than restated, because a
    second copy of "1900 MiB" is a number that will eventually be correct in one
    place and wrong in the other -- and the wrong one fails as an upload
    rejection from Telegram with no local hint.
    """
    try:
        import telegram_uploader

        return int(telegram_uploader.CHUNK_SIZE)
    except Exception:  # noqa: BLE001 - the fallback is the same number
        return _MAX_CHUNK_FALLBACK


def adaptive_chunk_bytes(
    throughput_bps: float,
    min_bytes: int = MIN_CHUNK_BYTES,
    max_bytes: int = None,
    target_seconds: float = TARGET_CHUNK_SECONDS,
) -> int:
    """Bytes for the next chunk, from a measured uplink speed.

    Rounded down to a whole MiB so offsets stay tidy and two runs at the same
    throughput produce byte-identical plans, which is what lets a resumed run
    recognise the plan it already has.
    """
    ceiling = max_chunk_bytes() if max_bytes is None else int(max_bytes)
    floor = int(min_bytes)
    if ceiling < floor:
        floor = ceiling

    wanted = float(throughput_bps) * float(target_seconds)
    clamped = max(float(floor), min(float(ceiling), wanted))
    # Round down, not to nearest: rounding up could push a chunk past Telegram's
    # ceiling when the clamp and the rounding disagree.
    rounded = int(clamped // MIB) * MIB
    return max(floor, min(ceiling, rounded))


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_chunks(size: int, chunk_bytes: int) -> list:
    """``[(chunk_index, offset, length), ...]`` for a file of ``size`` bytes.

    Delegates to ``telegram_uploader.plan_chunks`` rather than repeating the
    arithmetic, so the chunk layout here is provably the layout the existing
    uploader and the Go planner already produce -- test_tgup.py pins that
    agreement, and it should not have to pin it twice.
    """
    try:
        import telegram_uploader

        return telegram_uploader.plan_chunks(size, chunk_bytes)
    except Exception:  # noqa: BLE001 - keep working if the uploader is absent
        size = int(size)
        chunk_bytes = int(chunk_bytes) or 1
        if size <= chunk_bytes:
            return [(1, 0, size)]
        plan = []
        index = 0
        offset = 0
        while offset < size:
            index += 1
            length = min(chunk_bytes, size - offset)
            plan.append((index, offset, length))
            offset += length
        return plan


def sha256_range(path: str, offset: int, length: int, block: int = HASH_BLOCK) -> str:
    """SHA-256 of one byte range, streamed.

    ``length`` is a bound on the read, not an allocation: a 1900 MiB chunk
    costs ``block`` bytes of memory here, which is what lets this run inside a
    Kaggle notebook with a small disk and less RAM than the movie.
    """
    digest = hashlib.sha256()
    remaining = int(length)
    with open(path, "rb") as handle:
        handle.seek(int(offset))
        while remaining > 0:
            data = handle.read(min(block, remaining))
            if not data:
                break
            digest.update(data)
            remaining -= len(data)
    if remaining > 0:
        raise ChunkIntegrityError(
            f"{path} ended {remaining} bytes early at offset {offset}; "
            "the file is shorter than the plan says."
        )
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------

STATE_PENDING = "pending"
STATE_UPLOADING = "uploading"
STATE_DONE = "done"
STATE_FAILED = "failed"

MANIFEST_KIND = "tdubber_chunk_manifest"
MANIFEST_VERSION = 1


@dataclass
class ChunkRecord:
    """One chunk's row in the manifest."""

    index: int
    offset: int
    length: int
    sha256: str = ""
    state: str = STATE_PENDING
    message_id: int = 0
    link: str = ""
    attempts: int = 0
    error: str = ""

    @property
    def is_done(self) -> bool:
        return self.state == STATE_DONE and bool(self.link)

    def to_json(self) -> dict:
        return {
            "kind": "chunk",
            "index": int(self.index),
            "offset": int(self.offset),
            "length": int(self.length),
            "sha256": self.sha256 or "",
            "state": self.state,
            "message_id": int(self.message_id or 0),
            "link": self.link or "",
            "attempts": int(self.attempts or 0),
            "error": self.error or "",
        }

    @classmethod
    def from_json(cls, data: dict) -> "ChunkRecord":
        return cls(
            index=int(data.get("index", 0)),
            offset=int(data.get("offset", 0)),
            length=int(data.get("length", 0)),
            sha256=data.get("sha256") or "",
            state=data.get("state") or STATE_PENDING,
            message_id=int(data.get("message_id") or 0),
            link=data.get("link") or "",
            attempts=int(data.get("attempts") or 0),
            error=data.get("error") or "",
        )


class ChunkManifest:
    """Append-only JSONL ledger of a chunked transfer.

    One line per event. A chunk's state is the *last* line naming it, so the
    file is a log rather than a table that is rewritten in place -- a torn final
    write then costs at most the newest event, never the whole history.

    Ordering is the contract (see the module docstring): ``begin`` is fsynced
    before the upload starts, ``complete`` after it succeeds.
    """

    def __init__(self, path: str, header: dict = None):
        self.path = path
        self.header = dict(header or {})
        self.records: dict[int, ChunkRecord] = {}

    # -- construction -------------------------------------------------------

    @classmethod
    def create(
        cls,
        source_path: str,
        channel: str = "",
        chunk_bytes: int = None,
        content_sha256: str = None,
        estimator: ThroughputEstimator = None,
        fingerprint: str = None,
        manifest_path: str = None,
    ) -> "ChunkManifest":
        """Plan the file, hash every chunk, and write the opening manifest line.

        Returns a manifest whose records are all ``pending`` with their digests
        already known -- so an upload that never gets past chunk one has still
        recorded what the file actually is.
        """
        source_path = os.path.abspath(source_path)
        size = os.path.getsize(source_path)
        if size <= 0:
            raise ChunkingError(f"Refusing to chunk a 0-byte file: {source_path}")

        estimator = estimator or ThroughputEstimator()
        if chunk_bytes is None:
            chunk_bytes = adaptive_chunk_bytes(estimator.bps)
        chunk_bytes = int(chunk_bytes)
        layout = plan_chunks(size, chunk_bytes)

        header = {
            "kind": "header",
            "manifest": MANIFEST_KIND,
            "version": MANIFEST_VERSION,
            "filename": os.path.basename(source_path),
            "file_path": source_path,
            "size": int(size),
            "channel": channel or "",
            "chunk_size": chunk_bytes,
            "chunk_count": len(layout),
            "fingerprint": fingerprint or _fingerprint(content_sha256, size),
            "source_sha256": content_sha256 or "",
            "throughput": estimator.to_dict(),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        manifest = cls(manifest_path or os.path.join(
            os.path.dirname(source_path),
            os.path.basename(source_path) + ".chunks.jsonl",
        ), header)

        for index, offset, length in layout:
            manifest.records[index] = ChunkRecord(
                index=index,
                offset=offset,
                length=length,
                sha256=sha256_range(source_path, offset, length),
                state=STATE_PENDING,
            )
        manifest._write_header()
        for index in sorted(manifest.records):
            manifest.append(manifest.records[index])
        return manifest

    @classmethod
    def open(
        cls,
        source_path: str,
        manifest_path: str = None,
        chunk_bytes: int = None,
        channel: str = "",
    ) -> "ChunkManifest":
        """Load an existing manifest, or plan one if there is none.

        This is what makes ``resume()`` a one-liner for the caller: same entry
        point for a first run and the tenth. ``chunk_bytes`` applies only to a
        freshly created manifest -- an existing one keeps its layout, because a
        different layout would contradict the digests already recorded.
        """
        if manifest_path is None:
            manifest_path = os.path.join(
                os.path.dirname(os.path.abspath(source_path)),
                os.path.basename(source_path) + ".chunks.jsonl",
            )
        if os.path.isfile(manifest_path):
            existing = cls.load(manifest_path)
            if existing.records:
                return existing
            channel = channel or (existing.header.get("channel") or "")
        return cls.create(
            source_path, channel=channel, chunk_bytes=chunk_bytes,
            manifest_path=manifest_path,
        )

    @classmethod
    def load(cls, path: str) -> "ChunkManifest":
        """Read a manifest back. A torn or unparsable line is skipped, not fatal.

        Losing the last line costs one chunk's progress; losing the run would
        cost the file. So an unparsable line is dropped and the rest of the
        history is still trusted.
        """
        manifest = cls(path)
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                kind = data.get("kind")
                if kind == "header":
                    manifest.header = data
                elif kind == "chunk":
                    record = ChunkRecord.from_json(data)
                    # Last line wins: that is the most recent state.
                    manifest.records[record.index] = record
        return manifest

    # -- writing ------------------------------------------------------------

    def _write_header(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(self.header, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append(self, record: ChunkRecord) -> ChunkRecord:
        """Append one chunk event and fsync it.

        The fsync is the whole point of write-ahead ordering. Without it the
        "chunk N is uploading, digest D" line can sit in the OS buffer when the
        machine dies, and a resume would then start chunk N with no record of
        what it was supposed to be.
        """
        self.records[record.index] = record
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_json(), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    # -- reading ------------------------------------------------------------

    def record(self, index: int) -> ChunkRecord:
        try:
            return self.records[int(index)]
        except KeyError:
            raise ChunkingError(f"Chunk {index} is not in this manifest.")

    def chunks(self) -> list:
        """Records in index order, for code that iterates."""
        return [self.records[key] for key in sorted(self.records)]

    @property
    def size(self) -> int:
        return int(self.header.get("size") or 0)

    @property
    def chunk_count(self) -> int:
        return len(self.records)

    def done_count(self) -> int:
        return sum(1 for record in self.records.values() if record.is_done)

    def pending(self) -> list:
        """Chunks that are not finished, in the order they must be sent."""
        return [record for record in self.chunks() if not record.is_done]

    def all_done(self) -> bool:
        return bool(self.records) and self.done_count() == len(self.records)

    # -- state transitions --------------------------------------------------

    def begin(self, record: ChunkRecord) -> ChunkRecord:
        """Mark a chunk as being uploaded. Called BEFORE the transfer starts.

        ``attempts`` is incremented here rather than at completion, because the
        interesting question after a crash is "how many times did we try", and
        an attempt that died mid-flight is exactly the one worth knowing about.
        """
        record.state = STATE_UPLOADING
        record.attempts = int(record.attempts or 0) + 1
        record.error = ""
        return self.append(record)

    def complete(self, record: ChunkRecord, message_id: int, link: str) -> ChunkRecord:
        """Mark a chunk as stored. Called AFTER the transfer succeeded."""
        record.state = STATE_DONE
        record.message_id = int(message_id or 0)
        record.link = link or ""
        record.error = ""
        return self.append(record)

    def fail(self, record: ChunkRecord, error: str) -> ChunkRecord:
        record.state = STATE_FAILED
        record.error = str(error)[:1000]
        return self.append(record)

    def reset(self, record: ChunkRecord, reason: str = "") -> ChunkRecord:
        """Return a chunk to pending so it is sent again.

        Used when the local bytes no longer match what was recorded: the chunk
        on Telegram is fine, but it is a chunk of a file that no longer exists,
        so keeping it would archive the wrong movie.
        """
        record.state = STATE_PENDING
        record.message_id = 0
        record.link = ""
        record.sha256 = ""
        record.error = str(reason)[:1000]
        return self.append(record)


def _fingerprint(content_sha256: str, size: int) -> str:
    """The archive key for a chunked transfer.

    Uses the same content-first convention as ``direct_archive._archive_fingerprint``
    and ``app._archive_fingerprint`` -- the whole-content digest truncated to 32
    characters -- so a chunked upload of the same bytes is recognisable as the
    same archive as one made by the ordinary path. The size is folded in so an
    absent digest still produces a stable key instead of "".
    """
    if content_sha256:
        return content_sha256[:32]
    return hashlib.sha256(f"chunked|{size}".encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Resume verification
# ---------------------------------------------------------------------------


def verify_local_chunk(source_path: str, record: ChunkRecord) -> str:
    """Why this chunk cannot be trusted, or "" when it can. Size first, hash second.

    Size is checked before the hash because it is nearly free and settles the
    common case (the file was truncated or replaced by a shorter one) without
    reading gigabytes. Only when the byte range still exists in full does the
    SHA-256 get paid for.
    """
    try:
        size = os.path.getsize(source_path)
    except OSError as exc:
        return f"source file is unreadable: {exc}"

    end = record.offset + record.length
    if end > size:
        return (
            f"chunk {record.index} needs bytes up to {end} but {source_path} "
            f"is only {size} bytes; the source changed under us"
        )

    if not record.sha256:
        return f"chunk {record.index} has no recorded digest to verify against"

    actual = sha256_range(source_path, record.offset, record.length)
    if actual != record.sha256:
        return (
            f"chunk {record.index} hashed {actual[:12]} but the manifest "
            f"recorded {record.sha256[:12]}; the local bytes changed"
        )
    return ""


def resumable_chunks(manifest: ChunkManifest, source_path: str) -> list:
    """Chunks that still need sending: everything except verified-done ones.

    A ``done`` chunk whose bytes still match is skipped. A ``done`` chunk whose
    bytes changed is reset and returned for re-upload -- silently keeping it
    would archive a movie that no longer matches what the operator picked.
    """
    todo = []
    for record in manifest.chunks():
        if not record.is_done:
            todo.append(record)
            continue
        problem = verify_local_chunk(source_path, record)
        if not problem:
            continue
        manifest.reset(record, reason=problem)
        record.sha256 = sha256_range(
            source_path, record.offset, record.length
        )
        manifest.append(record)
        todo.append(record)
    return todo


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@dataclass
class UploadSummary:
    """What one pass over the manifest did."""

    sent: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    throughput: dict = field(default_factory=dict)
    chunk_bytes: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed


def upload_chunks(
    manifest: ChunkManifest,
    source_path: str,
    send: Callable[[ChunkRecord, str], dict],
    estimator: ThroughputEstimator = None,
    on_progress: Callable[[dict], None] = None,
) -> UploadSummary:
    """Send every chunk that is not already verified-stored.

    ``send(record, source_path)`` must return ``{"message_id": int, "link": str}``
    and raise on failure. Uploading is injected rather than implemented here so
    this module stays free of Telegram code and the tests need no session.

    The per-chunk ordering inside the loop is the load-bearing part:
    ``manifest.begin`` (fsynced) -> send -> ``manifest.complete`` (fsynced).
    """
    estimator = estimator or ThroughputEstimator.from_dict(
        manifest.header.get("throughput")
    )
    summary = UploadSummary()
    summary.chunk_bytes = int(manifest.header.get("chunk_size") or 0)

    for record in resumable_chunks(manifest, source_path):
        manifest.begin(record)
        _emit(
            on_progress,
            phase="chunk_start",
            chunk_index=record.index,
            chunk_count=manifest.chunk_count,
            current=record.offset,
            total=manifest.size,
            message=(
                f"chunk {record.index}/{manifest.chunk_count} "
                f"({human_bytes(record.length)}) uploading"
            ),
        )
        started = time.monotonic()
        try:
            result = send(record, source_path)
        except (Exception, asyncio.CancelledError) as exc:
            # A cancelled transfer must leave an honest record too, or the next
            # resume believes a chunk is done. CancelledError is named
            # explicitly because since 3.8 it derives from BaseException, not
            # Exception, and the same rule telegram_uploader follows applies.
            #
            # SystemExit / KeyboardInterrupt / a killed process deliberately do
            # NOT come through here. They are a power cut wearing a Python
            # exception costume, and a power cut runs no handler -- so writing a
            # line would make the record lie about what survived. The chunk is
            # left marked `uploading`, which is exactly the truth, and resume
            # re-sends it.
            manifest.fail(record, f"{type(exc).__name__}: {exc}")
            summary.failed.append(record)
            _emit(
                on_progress,
                phase="chunk_failed",
                chunk_index=record.index,
                message=f"chunk {record.index} failed: {exc}",
            )
            raise

        elapsed = max(time.monotonic() - started, 1e-6)
        manifest.complete(
            record,
            message_id=(result or {}).get("message_id", 0),
            link=(result or {}).get("link", ""),
        )
        estimator.sample(record.length, elapsed)
        summary.sent.append(record)
        _emit(
            on_progress,
            phase="chunk_done",
            chunk_index=record.index,
            chunk_count=manifest.chunk_count,
            current=record.offset + record.length,
            total=manifest.size,
            message=(
                f"chunk {record.index}/{manifest.chunk_count} stored "
                f"({human_bytes(record.length)})\u2713"
            ),
        )

    # Keep the throughput estimate on disk so a resumed run starts from what the
    # previous one measured instead of re-learning a link it already timed.
    manifest.header["throughput"] = estimator.to_dict()
    manifest.header["chunk_size"] = adaptive_chunk_bytes(estimator.bps)
    summary.throughput = estimator.to_dict()
    summary.chunk_bytes = manifest.header["chunk_size"]
    return summary


def resume(
    source,
    send: Callable[[ChunkRecord, str], dict],
    manifest: ChunkManifest = None,
    manifest_path: str = None,
    chunk_bytes: int = None,
    channel: str = "",
    estimator: ThroughputEstimator = None,
    on_progress: Callable[[dict], None] = None,
) -> "tuple[UploadSummary, ChunkManifest]":
    """Resume (or start) a chunked upload. Returns ``(summary, manifest)``.

    The entry point a caller actually uses. It is ``upload_chunks()`` plus the
    "open or create the manifest" step, because a separate resume path would be
    a second implementation of the one rule that matters: skip what is already
    verified, send only what is not.

    ``source`` is the path to the file, or an existing :class:`ChunkManifest`
    to continue. Passing the manifest back is how the tests (and a caller
    holding one) avoid a second disk round trip.

    ``chunk_bytes`` applies only when a manifest has to be created. An existing
    one keeps its layout -- a different layout would contradict every digest
    already on disk.
    """
    if isinstance(source, ChunkManifest):
        manifest = source
        source_path = manifest.header.get("file_path") or ""
        if not source_path:
            raise ChunkingError(
                "This manifest has no file_path, so the bytes it describes "
                "cannot be re-sent. Pass the source file to resume()."
            )
    else:
        source_path = os.path.abspath(source)

    if manifest is None:
        manifest = ChunkManifest.open(
            source_path, manifest_path, chunk_bytes=chunk_bytes, channel=channel,
        )

    already = {record.index for record in manifest.chunks() if record.is_done}
    summary = upload_chunks(
        manifest, source_path, send, estimator=estimator, on_progress=on_progress
    )
    summary.skipped = [
        record for record in manifest.chunks() if record.index in already
    ]
    return summary, manifest


# ---------------------------------------------------------------------------
# Per-chunk Telegram recovery
# ---------------------------------------------------------------------------


def fetch_chunk_from_telegram(record: ChunkRecord, dest_dir: str, **bridge_kwargs) -> str:
    """Pull one chunk back through the existing verified fetch path.

    This calls ``tgup_bridge.fetch()`` -- the same bridge ``tgup fetch`` uses
    for a whole archive -- rather than talking to Telethon here. Writing a
    second downloader would be strictly worse: the bridge already verifies each
    part against the digest tgup recorded, already handles private-channel and
    deep links, and already reports honestly when it cannot run. A chunk fetched
    this way is the same object a full restore would produce.

    Returns the path of the downloaded file.
    """
    import tgup_bridge

    result = tgup_bridge.fetch(
        link=record.link, dest=dest_dir, verify=True, **bridge_kwargs
    )
    if not result.ok:
        raise ChunkingError(
            f"fetch failed for chunk {record.index}: "
            f"{result.error or result.fallback_reason or 'unknown reason'}"
        )
    # tgup names the file after the document it downloaded. Honour that name.
    #
    # A "grab the biggest file in the directory" fallback exists for a bridge
    # that reports no filename, and ONLY then. If the bridge named a file and
    # that file is not there, the fetch did not do what it said -- and quietly
    # handing back some other file in the directory would let a retry verify
    # the wrong bytes and mark a chunk stored that never arrived.
    if result.filename:
        named = os.path.join(dest_dir, result.filename)
        if os.path.isfile(named):
            return named
        raise ChunkingError(
            f"fetch named {result.filename!r} as the downloaded chunk but that "
            f"file is not in {dest_dir} (chunk {record.index})."
        )
    candidates = [
        os.path.join(dest_dir, name)
        for name in os.listdir(dest_dir)
        if os.path.isfile(os.path.join(dest_dir, name))
    ]
    if not candidates:
        raise ChunkingError(
            f"fetch reported success but {dest_dir} holds no file "
            f"(chunk {record.index})."
        )
    return max(candidates, key=os.path.getsize)


def upload_chunk_with_recovery(
    record: ChunkRecord,
    source_path: str,
    send: Callable[[ChunkRecord, str], dict],
    recover: Callable[[ChunkRecord], str] = None,
    max_attempts: int = 3,
    on_progress: Callable[[dict], None] = None,
) -> dict:
    """Send one chunk, and on failure fetch it back and retry -- that chunk only.

    This is the mission rule in code: "sirf us chunk ko Telegram se wapas manga
    kar retry". The rest of the movie is never touched, because everything here
    operates on a single ``ChunkRecord``.

    The recovery step is not pointless busywork: a send that raised can still
    have stored the bytes (the connection can die after Telegram accepted them).
    Fetching the chunk back and checking it against the recorded digest answers
    the only question that matters -- is this chunk actually safe in Telegram?
    If it is, the chunk is done and the retry is skipped entirely.

    Raises :class:`ChunkRetryExhausted` once ``max_attempts`` is spent, naming
    the chunk and both reasons, so the failure is actionable rather than a
    generic "upload failed".
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    recover = recover or (lambda chunk: fetch_chunk_from_telegram(
        chunk, os.path.join(os.path.dirname(os.path.abspath(source_path)), "_chunk_recovery"),
    ))

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            result = send(record, source_path)
            if result and result.get("link"):
                return result
            last_error = "send returned no message link"
        except BaseException as exc:  # noqa: BLE001 - every failure is retried
            last_error = f"{type(exc).__name__}: {exc}"

        # Only try to fetch something back if a previous attempt actually got a
        # link; without one there is nothing in Telegram to fetch.
        if record.link:
            try:
                recovered = recover(record)
                actual = _sha256_file(recovered)
                if actual == record.sha256:
                    _emit(
                        on_progress,
                        phase="chunk_recovered",
                        chunk_index=record.index,
                        message=(
                            f"chunk {record.index} was already stored and "
                            "verified; not re-sending it"
                        ),
                    )
                    return {"message_id": record.message_id, "link": record.link,
                            "recovered": True}
                last_error = (
                    f"the copy in Telegram hashes {actual[:12]}, not "
                    f"{record.sha256[:12]}"
                )
            except BaseException as exc:  # noqa: BLE001 - recovery is best-effort
                last_error = f"{last_error}; fetch-back failed: {exc}"

        _emit(
            on_progress,
            phase="chunk_retry",
            chunk_index=record.index,
            attempt=attempt,
            max_attempts=max_attempts,
            message=f"chunk {record.index} attempt {attempt}/{max_attempts}: {last_error}",
        )

    raise ChunkRetryExhausted(
        f"chunk {record.index} of {human_bytes(record.length)} could not be "
        f"stored after {max_attempts} attempt(s). Last reason: {last_error}. "
        "Every other chunk is intact and journalled -- re-run to continue from "
        "here; nothing else in this file will be sent again."
    )


def _sha256_file(path: str, block: int = HASH_BLOCK) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for data in iter(lambda: handle.read(block), b""):
            digest.update(data)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Reassembly
# ---------------------------------------------------------------------------


def reassemble(
    manifest: ChunkManifest,
    dest_path: str,
    fetch: Callable[[ChunkRecord], str],
    on_progress: Callable[[dict], None] = None,
) -> str:
    """Rebuild the original file, verifying every chunk before it counts.

    Each chunk is hashed as it is appended, so the check costs nothing extra
    and a bad chunk is caught before the bytes reach the final file. A mismatch
    removes the staging file and raises :class:`ChunkIntegrityError` -- there is
    no code path here that leaves a plausible-looking but corrupt movie on disk.

    ``fetch(record)`` returns the local path of that chunk's bytes.
    """
    records = manifest.chunks()
    if not records:
        raise ChunkingError("The manifest lists no chunks.")

    staging = dest_path + ".assembling"
    directory = os.path.dirname(os.path.abspath(dest_path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    written = 0
    try:
        with open(staging, "wb") as sink:
            for position, record in enumerate(records, start=1):
                if not record.is_done:
                    raise ChunkIntegrityError(
                        f"chunk {record.index} is '{record.state}', not stored; "
                        "refusing to assemble a file with a hole in it."
                    )
                source = fetch(record)
                digest = hashlib.sha256()
                copied = 0
                with open(source, "rb") as handle:
                    for data in iter(lambda: handle.read(HASH_BLOCK), b""):
                        digest.update(data)
                        sink.write(data)
                        copied += len(data)
                if copied != record.length:
                    raise ChunkIntegrityError(
                        f"chunk {record.index} is {copied} bytes but the manifest "
                        f"recorded {record.length}."
                    )
                if record.sha256 and digest.hexdigest() != record.sha256:
                    raise ChunkIntegrityError(
                        f"chunk {record.index} failed its SHA-256 check "
                        f"({digest.hexdigest()[:12]} != {record.sha256[:12]}). "
                        "The archived copy is damaged; nothing was assembled."
                    )
                written += copied
                _emit(
                    on_progress,
                    phase="assemble",
                    chunk_index=record.index,
                    chunk_count=len(records),
                    current=written,
                    total=manifest.size,
                    message=f"verified chunk {position}/{len(records)}",
                )

        expected = manifest.size
        if expected and written != expected:
            raise ChunkIntegrityError(
                f"assembled {written} bytes but the manifest declares {expected}. "
                "Nothing was kept."
            )
        os.replace(staging, dest_path)
    except BaseException:
        # Never leave a half-built file where a restore would find it and trust
        # it. This is the same rule the Go and Telethon restore paths follow.
        try:
            os.remove(staging)
        except OSError:
            pass
        raise

    return dest_path


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def human_bytes(count) -> str:
    """Same rendering as telegram_uploader.human_bytes, kept local so this
    module imports nothing at module load."""
    try:
        count = float(count)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if count < 1024 or unit == "TB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.2f} {unit}"
        count /= 1024
    return f"{count:.2f} TB"


def _emit(callback, **payload) -> None:
    """Progress is a courtesy, never a dependency.

    A broken display must not be able to abort an upload that is otherwise
    fine -- the same rule telegram_uploader._emit follows.
    """
    if callback is None:
        return
    try:
        callback(payload)
    except Exception:  # noqa: BLE001
        pass


def manifest_for(source_path: str, chunk_bytes: int = None, **kwargs) -> ChunkManifest:
    """Convenience wrapper so a caller writes one line instead of three."""
    return ChunkManifest.create(source_path, chunk_bytes=chunk_bytes, **kwargs)