"""test_chunking.py -- adaptive chunking, resume and reassembly, offline.

Every test here runs against a real file on disk and a fake ``send``. Nothing
touches Telegram, nothing needs a session, and nothing needs an OTP -- the
uploading itself is injected (``send``), so what is under test is the logic
this module actually owns: how big a chunk is, what the manifest records, what
a crash leaves behind, and whether a corrupt byte ever reaches a finished file.

The crash test is the important one. A 100 GB upload that dies at 90% is the
failure this whole module exists for, so the ordering guarantee (manifest
BEFORE the send, completion AFTER it) is asserted directly rather than
inferred: the test crashes the process between the two and checks what the
manifest says afterwards.

Run with:  python -m pytest test_chunking.py -q
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chunking  # noqa: E402

MIB = 1024 * 1024


def _write_file(path: Path, size: int, seed: int = 7) -> bytes:
    """A file whose bytes are reproducible, so digests can be asserted."""
    payload = bytearray()
    block = bytes((seed + i) % 251 for i in range(4096))
    while len(payload) < size:
        payload.extend(block)
    data = bytes(payload[:size])
    path.write_bytes(data)
    return data


def _make_sender(fail_on=(), corrupt_on=(), log=None):
    """A ``send`` that records what it was asked to send.

    ``fail_on`` / ``corrupt_on`` take chunk indexes; the log is appended to with
    each chunk index that actually reached the sender, which is how the tests
    prove a resume did NOT re-send anything.
    """
    def send(record, source_path):
        if log is not None:
            log.append(record.index)
        if record.index in fail_on:
            raise OSError(f"simulated network drop on chunk {record.index}")
        return {
            "message_id": 1000 + record.index,
            "link": f"https://t.me/testchan/{1000 + record.index}",
            "corrupt": record.index in corrupt_on,
        }

    return send


class AdaptiveSizeTests(unittest.TestCase):
    """The chunk ceiling has to react to the uplink, and react sanely."""

    def test_a_slow_uplink_gets_smaller_chunks_than_a_fast_one(self):
        slow = chunking.ThroughputEstimator(seed_bps=1.0 * MIB)
        fast = chunking.ThroughputEstimator(seed_bps=40.0 * MIB)
        slow_size = chunking.adaptive_chunk_bytes(slow.bps)
        fast_size = chunking.adaptive_chunk_bytes(fast.bps)
        self.assertLess(slow_size, fast_size)
        # The gap has to be a real one, not a rounding artefact: one chunk of
        # work should differ by more than 2x between a 1 MB/s and a 40 MB/s link.
        self.assertGreater(fast_size, slow_size * 2)

    def test_the_estimate_moves_when_transfers_are_observed(self):
        estimator = chunking.ThroughputEstimator(seed_bps=2.0 * MIB)
        before = estimator.bps
        # Ten transfers at 10 MB/s for one second each: the EWMA must climb.
        for _ in range(10):
            estimator.sample(10 * MIB, 1.0)
        self.assertGreater(estimator.bps, before * 2)
        self.assertGreater(chunking.adaptive_chunk_bytes(estimator.bps), 10 * MIB)

    def test_the_estimate_falls_when_transfers_are_slow(self):
        estimator = chunking.ThroughputEstimator(seed_bps=40.0 * MIB)
        before = estimator.bps
        for _ in range(10):
            estimator.sample(0.5 * MIB, 1.0)
        self.assertLess(estimator.bps, before)
        self.assertLess(chunking.adaptive_chunk_bytes(estimator.bps), before * 120)

    def test_a_lying_clock_cannot_poison_the_estimate(self):
        """A zero-duration transfer must not read as an infinite uplink."""
        estimator = chunking.ThroughputEstimator(seed_bps=2.0 * MIB)
        estimator.sample(1 * MIB, 0.0)
        estimator.sample(1 * MIB, -5.0)
        estimator.sample(0, 1.0)
        self.assertAlmostEqual(estimator.bps, 2.0 * MIB)
        self.assertEqual(estimator.samples, 0)

    def test_the_size_is_clamped_at_both_ends(self):
        tiny = chunking.adaptive_chunk_bytes(0.0001)
        huge = chunking.adaptive_chunk_bytes(10_000_000)
        self.assertGreaterEqual(tiny, chunking.MIN_CHUNK_BYTES)
        self.assertLessEqual(huge, chunking.max_chunk_bytes())

    def test_the_ceiling_is_telegrams_limit_not_a_taste_call(self):
        self.assertLessEqual(chunking.max_chunk_bytes(), 2 * 1024 * MIB)
        # ...and it is the SAME number telegram_uploader uses, not a copy.
        import telegram_uploader

        self.assertEqual(chunking.max_chunk_bytes(), telegram_uploader.CHUNK_SIZE)

    def test_the_size_is_a_whole_number_of_mibibytes(self):
        for bps in (0.5 * MIB, 2.06 * MIB, 7.3 * MIB, 33.0 * MIB):
            value = chunking.adaptive_chunk_bytes(bps)
            self.assertEqual(value % MIB, 0, f"{bps} B/s produced {value}")

    def test_the_same_throughput_always_gives_the_same_plan(self):
        """Byte-identical plans are what let a resume recognise its own plan."""
        first = chunking.adaptive_chunk_bytes(3.7 * MIB)
        second = chunking.adaptive_chunk_bytes(3.7 * MIB)
        self.assertEqual(first, second)

    def test_an_estimate_round_trips_through_the_manifest_header(self):
        estimator = chunking.ThroughputEstimator(seed_bps=5.0 * MIB)
        estimator.sample(9 * MIB, 1.0)
        restored = chunking.ThroughputEstimator.from_dict(estimator.to_dict())
        self.assertAlmostEqual(restored.bps, estimator.bps)
        self.assertEqual(restored.samples, estimator.samples)


class PlanningAndDigestTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="chunk_plan_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.source = self.scratch / "movie.mkv"
        self.data = _write_file(self.source, 3 * MIB + 12345)

    def test_the_plan_covers_the_file_exactly_once_with_no_gaps(self):
        manifest = chunking.ChunkManifest.create(self.source, chunk_bytes=1 * MIB)
        records = manifest.chunks()
        self.assertEqual(sum(r.length for r in records), len(self.data))
        self.assertEqual(records[0].offset, 0)
        for previous, current in zip(records, records[1:]):
            self.assertEqual(previous.offset + previous.length, current.offset)
        self.assertEqual(records[-1].offset + records[-1].length, len(self.data))

    def test_every_chunk_digest_matches_its_actual_bytes(self):
        manifest = chunking.ChunkManifest.create(self.source, chunk_bytes=1 * MIB)
        for record in manifest.chunks():
            piece = self.data[record.offset: record.offset + record.length]
            self.assertEqual(
                hashlib.sha256(piece).hexdigest(), record.sha256,
                f"chunk {record.index} digest does not match its bytes",
            )

    def test_a_single_chunk_file_still_plans_and_hashes(self):
        tiny = self.scratch / "clip.mp4"
        _write_file(tiny, 1024)
        manifest = chunking.ChunkManifest.create(tiny, chunk_bytes=1 * MIB)
        self.assertEqual(manifest.chunk_count, 1)
        self.assertEqual(manifest.chunks()[0].length, 1024)
        self.assertTrue(manifest.chunks()[0].sha256)

    def test_hashing_a_range_does_not_read_the_whole_file_into_memory(self):
        """A chunk's digest must cost a fixed buffer, not the chunk's size.

        Asserted by construction rather than by timing: the reader is given a
        handle that refuses any read larger than the block size, which is the
        only way to make "streaming" a fact instead of a hope.
        """
        big = self.scratch / "big.bin"
        size = 8 * MIB
        payload = _write_file(big, size)
        max_seen = {"read": 0}
        real_open = open

        class GuardedFile:
            def __init__(self, handle):
                self._handle = handle

            def read(self, n=-1):
                max_seen["read"] = max(max_seen["read"], n if n and n > 0 else size)
                return self._handle.read(n)

            def seek(self, *a):
                return self._handle.seek(*a)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()
                return False

        import builtins
        builtins.open = lambda path, *a, **k: GuardedFile(real_open(path, *a, **k))
        try:
            digest = chunking.sha256_range(str(big), 0, size)
        finally:
            builtins.open = real_open

        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
        self.assertLessEqual(max_seen["read"], chunking.HASH_BLOCK)

    def test_an_empty_file_is_refused_rather_than_producing_a_zero_chunk(self):
        empty = self.scratch / "empty.bin"
        empty.write_bytes(b"")
        with self.assertRaises(chunking.ChunkingError):
            chunking.ChunkManifest.create(empty, chunk_bytes=1 * MIB)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="chunk_manifest_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.source = self.scratch / "movie.mkv"
        self.data = _write_file(self.source, 2 * MIB + 999)

    def test_a_fresh_manifest_records_every_chunk_as_pending_with_a_digest(self):
        manifest = chunking.ChunkManifest.create(
            self.source, chunk_bytes=1 * MIB, channel="@chan",
        )
        self.assertEqual(manifest.header["channel"], "@chan")
        self.assertGreaterEqual(manifest.chunk_count, 3)
        for record in manifest.chunks():
            self.assertEqual(record.state, chunking.STATE_PENDING)
            self.assertTrue(record.sha256)
            self.assertEqual(record.attempts, 0)

    def test_it_is_jsonl_one_object_per_line(self):
        manifest = chunking.ChunkManifest.create(self.source, chunk_bytes=1 * MIB)
        lines = manifest.path
        with open(lines, "r", encoding="utf-8") as handle:
            parsed = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(parsed[0]["kind"], "header")
        self.assertEqual(sum(1 for p in parsed if p["kind"] == "chunk"),
                         manifest.chunk_count)

    def test_a_manifest_reloads_to_the_same_state(self):
        manifest = chunking.ChunkManifest.create(self.source, chunk_bytes=1 * MIB)
        manifest.complete(manifest.record(1), message_id=7, link="https://t.me/c/1")
        manifest.fail(manifest.record(2), "boom")
        reloaded = chunking.ChunkManifest.load(manifest.path)
        self.assertEqual(reloaded.chunk_count, manifest.chunk_count)
        self.assertEqual(reloaded.header["filename"], manifest.header["filename"])
        self.assertTrue(reloaded.record(1).is_done)
        self.assertEqual(reloaded.record(2).state, chunking.STATE_FAILED)
        self.assertEqual(reloaded.record(2).error, "boom")

    def test_a_torn_final_line_does_not_destroy_the_history(self):
        """A crash mid-append must cost one event, never the file."""
        manifest = chunking.ChunkManifest.create(self.source, chunk_bytes=1 * MIB)
        manifest.complete(manifest.record(1), message_id=7, link="https://t.me/c/1")
        with open(manifest.path, "a", encoding="utf-8") as handle:
            handle.write('{"kind": "chunk", "index": 2, "offse')  # torn
        reloaded = chunking.ChunkManifest.load(manifest.path)
        self.assertTrue(reloaded.record(1).is_done)
        self.assertEqual(reloaded.chunk_count, manifest.chunk_count)

    def test_the_last_line_for_a_chunk_wins(self):
        manifest = chunking.ChunkManifest.create(self.source, chunk_bytes=1 * MIB)
        manifest.begin(manifest.record(1))
        manifest.complete(manifest.record(1), message_id=9, link="https://t.me/c/9")
        reloaded = chunking.ChunkManifest.load(manifest.path)
        self.assertEqual(reloaded.record(1).state, chunking.STATE_DONE)
        self.assertEqual(reloaded.record(1).message_id, 9)

    def test_begin_counts_the_attempt_before_the_transfer_is_tried(self):
        """An attempt that dies mid-flight is the one worth counting."""
        manifest = chunking.ChunkManifest.create(self.source, chunk_bytes=1 * MIB)
        record = manifest.record(1)
        manifest.begin(record)
        self.assertEqual(record.attempts, 1)
        self.assertEqual(record.state, chunking.STATE_UPLOADING)
        # Already durable on disk, not just in memory.
        self.assertEqual(chunking.ChunkManifest.load(manifest.path).record(1).attempts, 1)


class UploadAndResumeTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="chunk_upload_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.source = self.scratch / "movie.mkv"
        self.data = _write_file(self.source, 2 * MIB + 4096)

    def _manifest(self, **kwargs):
        return chunking.ChunkManifest.create(self.source, chunk_bytes=1 * MIB, **kwargs)

    def test_a_clean_upload_sends_every_chunk_exactly_once(self):
        manifest = self._manifest()
        sent = []
        summary = chunking.upload_chunks(
            manifest, str(self.source), _make_sender(log=sent),
        )
        self.assertTrue(summary.ok)
        self.assertEqual(sorted(sent), list(range(1, manifest.chunk_count + 1)))
        self.assertEqual(len(sent), len(set(sent)))
        self.assertTrue(manifest.all_done())

    def test_resume_sends_only_what_is_missing(self):
        """The mission rule: a failed chunk costs that chunk, not the file."""
        manifest = self._manifest()
        first = []
        # Chunk 2 fails, so 1 is stored and 2..n are never attempted.
        with self.assertRaises(OSError):
            chunking.upload_chunks(
                manifest, str(self.source), _make_sender(fail_on={2}, log=first),
            )
        self.assertEqual(first, [1, 2])
        self.assertTrue(manifest.record(1).is_done)
        self.assertEqual(manifest.record(2).state, chunking.STATE_FAILED)
        self.assertEqual(manifest.record(2).attempts, 1)

        second = []
        summary = chunking.resume(
            manifest, _make_sender(log=second),
        )[0]
        # Chunk 1 is NOT re-sent. That is the whole point.
        self.assertNotIn(1, second)
        self.assertTrue(summary.ok)
        self.assertTrue(manifest.all_done())
        self.assertEqual(summary.skipped[0].index, 1)
        self.assertEqual(manifest.record(2).attempts, 2)

    def test_resume_reverifies_a_done_chunk_before_skipping_it(self):
        """Skipping is earned by a fresh check, not remembered from last time."""
        manifest = self._manifest()
        sent = []
        chunking.upload_chunks(
            manifest, str(self.source), _make_sender(log=sent),
        )
        first_pass = list(sent)
        self.assertEqual(len(first_pass), manifest.chunk_count)

        # The file is untouched: nothing may be sent again.
        second_pass = []
        summary = chunking.resume(
            manifest, _make_sender(log=second_pass),
        )[0]
        self.assertEqual(second_pass, [], "an unchanged file must not be re-sent")
        self.assertTrue(summary.ok)
        self.assertEqual(len(summary.skipped), manifest.chunk_count)

    def test_changed_local_bytes_force_that_chunk_to_be_re_uploaded(self):
        """A chunk of a file that no longer exists must not be kept."""
        manifest = self._manifest()
        chunking.upload_chunks(manifest, str(self.source), _make_sender())
        untouched = [r.index for r in manifest.chunks()]

        # Flip one byte inside chunk 1's range.
        data = bytearray(self.data)
        data[10] ^= 0xFF
        self.source.write_bytes(bytes(data))

        second = []
        summary = chunking.resume(
            manifest, _make_sender(log=second),
        )[0]
        self.assertEqual(second, [1], "exactly the changed chunk is re-sent")
        self.assertTrue(summary.ok)
        self.assertTrue(manifest.all_done())
        # Every other chunk kept the link it already had.
        for index in untouched:
            if index == 1:
                continue
            self.assertTrue(manifest.record(index).is_done)

    def test_a_truncated_source_is_caught_by_size_without_a_hash(self):
        """Size first, hash second: a short file must not cost a full re-read."""
        manifest = self._manifest()
        record = manifest.record(1)
        self.assertEqual(chunking.verify_local_chunk(str(self.source), record), "")
        self.source.write_bytes(self.data[:1000])
        problem = chunking.verify_local_chunk(str(self.source), record)
        self.assertIn("changed under us", problem)
        self.assertIn("is only 1000 bytes", problem)

    def test_a_cancelled_transfer_is_recorded_as_failed(self):
        """CancelledError is a BaseException since 3.8 and must still be caught.

        A cancelled upload that leaves no record would let the next resume
        believe the chunk is done, which is how a movie goes missing.
        """
        manifest = self._manifest()

        def cancelling(record, source_path):
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            chunking.upload_chunks(manifest, str(self.source), cancelling)
        self.assertEqual(manifest.record(1).state, chunking.STATE_FAILED)
        self.assertIn("CancelledError", manifest.record(1).error)

    def test_progress_callbacks_cannot_break_an_upload(self):
        manifest = self._manifest()

        def broken(_payload):
            raise RuntimeError("the UI widget is broken")

        summary = chunking.upload_chunks(
            manifest, str(self.source), _make_sender(), on_progress=broken,
        )
        self.assertTrue(summary.ok)


class CrashRecoveryTests(unittest.TestCase):
    """THE boundary case: a crash between the manifest write and the upload."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="chunk_crash_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.source = self.scratch / "movie.mkv"
        self.data = _write_file(self.source, 2 * MIB + 8192)

    def test_the_manifest_is_written_before_the_upload_starts(self):
        """Ordering is the guarantee; this asserts it at the moment it matters.

        The sender reads the manifest file off disk mid-transfer. If the
        pre-upload line had not been fsynced, that read would not show the
        chunk as `uploading`, and a crash right here would resume blind.
        """
        manifest = chunking.ChunkManifest.create(self.source, chunk_bytes=1 * MIB)
        observed = []

        def inspecting_send(record, source_path):
            on_disk = chunking.ChunkManifest.load(manifest.path)
            observed.append(
                (record.index,
                 on_disk.record(record.index).state,
                 on_disk.record(record.index).sha256,
                 record.sha256)
            )
            return {"message_id": 1 + record.index,
                    "link": f"https://t.me/t/{1 + record.index}"}

        chunking.upload_chunks(manifest, str(self.source), inspecting_send)

        self.assertEqual(len(observed), manifest.chunk_count)
        for index, state, digest, expected in observed:
            self.assertEqual(state, chunking.STATE_UPLOADING,
                             f"chunk {index} was not recorded before its upload")
            self.assertEqual(digest, expected)
            self.assertTrue(digest)

    def test_a_crash_after_the_manifest_write_leaves_an_uploading_chunk(self):
        """A power cut runs no handler, so the last honest line must stand.

        Simulated with a BaseException that is deliberately NOT caught by the
        ``except (Exception, CancelledError)`` in the upload loop -- exactly
        what SystemExit and a killed process do. The chunk must still be found,
        still carry its digest, and still be honestly marked not-done.
        """
        class Crash(BaseException):
            """Stands in for a process that stops existing."""

        manifest = chunking.ChunkManifest.create(self.source, chunk_bytes=1 * MIB)
        crashed_on = {"chunk": 0}

        def crashing_send(record, source_path):
            crashed_on["chunk"] = record.index
            raise Crash("the process died mid-transfer")

        with self.assertRaises(Crash):
            chunking.upload_chunks(manifest, str(self.source), crashing_send)

        recovered = chunking.ChunkManifest.load(manifest.path)
        record = recovered.record(crashed_on["chunk"])
        self.assertEqual(record.state, chunking.STATE_UPLOADING)
        self.assertTrue(record.sha256)
        self.assertEqual(record.attempts, 1)
        self.assertFalse(record.is_done)

    def test_the_crash_chunk_alone_is_re_uploaded_on_resume(self):
        class Crash(BaseException):
            pass

        manifest = chunking.ChunkManifest.create(self.source, chunk_bytes=1 * MIB)
        sent = []

        def crashing_send(record, source_path):
            sent.append(record.index)
            if record.index == 2:
                raise Crash("died on chunk 2")
            return {"message_id": record.index,
                    "link": f"https://t.me/t/{record.index}"}

        with self.assertRaises(Crash):
            chunking.upload_chunks(manifest, str(self.source), crashing_send)

        after = []
        summary = chunking.resume(
            manifest, _make_sender(log=after),
        )[0]
        self.assertNotIn(1, after, "chunk 1 was stored before the crash")
        self.assertIn(2, after)
        self.assertTrue(summary.ok)
        self.assertTrue(manifest.all_done())

    def test_a_crash_after_the_upload_but_before_the_completion_line(self):
        """The one boundary the ordering cannot fully prevent -- handled honestly.

        Telegram may have stored the bytes even though no completion line was
        written. The manifest says `uploading`, so the chunk is re-sent. That
        costs one duplicate message; the alternative (assuming it landed) would
        cost a chunk that was never sent, and a corrupt movie is far worse than
        a duplicate.
        """
        class Crash(BaseException):
            pass

        manifest = chunking.ChunkManifest.create(self.source, chunk_bytes=1 * MIB)
        state = {"count": 0}

        def crashing_send(record, source_path):
            state["count"] += 1
            if state["count"] == 1:
                raise Crash("died after send, before the completion line")
            return {"message_id": record.index,
                    "link": f"https://t.me/t/{record.index}"}

        with self.assertRaises(Crash):
            chunking.upload_chunks(manifest, str(self.source), crashing_send)
        reloaded = chunking.ChunkManifest.load(manifest.path)
        self.assertEqual(reloaded.record(1).state, chunking.STATE_UPLOADING)

        after = []
        chunking.resume(manifest, _make_sender(log=after))
        self.assertIn(1, after, "an unrecorded chunk must be sent again, not assumed")


class ReassemblyTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="chunk_reassemble_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.source = self.scratch / "movie.mkv"
        self.data = _write_file(self.source, 2 * MIB + 555)
        self.manifest = chunking.ChunkManifest.create(self.source, chunk_bytes=1 * MIB)
        chunking.upload_chunks(
            self.manifest, str(self.source), _make_sender(),
        )
        # A fake Telegram: one file per chunk, each holding that chunk's bytes.
        self.store = self.scratch / "telegram"
        self.store.mkdir()
        for record in self.manifest.chunks():
            piece = self.data[record.offset: record.offset + record.length]
            (self.store / f"part{record.index:04d}.bin").write_bytes(piece)

        def fetch(record):
            return str(self.store / f"part{record.index:04d}.bin")

        self.fetch = fetch

    def test_a_clean_reassembly_reproduces_the_file_byte_for_byte(self):
        out = self.scratch / "restored.mkv"
        result = chunking.reassemble(self.manifest, str(out), self.fetch)
        self.assertEqual(result, str(out))
        self.assertEqual(out.read_bytes(), self.data)

    def test_a_corrupt_chunk_stops_the_reassembly_and_writes_nothing(self):
        """The loud-failure requirement: never a plausible corrupt movie."""
        victim = self.store / f"part{self.manifest.chunk_count:04d}.bin"
        payload = bytearray(victim.read_bytes())
        payload[10] ^= 0xFF
        victim.write_bytes(bytes(payload))

        out = self.scratch / "restored.mkv"
        with self.assertRaises(chunking.ChunkIntegrityError) as ctx:
            chunking.reassemble(self.manifest, str(out), self.fetch)
        self.assertIn("SHA-256", str(ctx.exception))
        self.assertFalse(out.exists(), "nothing may be renamed into place")
        self.assertFalse(
            (self.scratch / "restored.mkv.assembling").exists(),
            "the staging file must be cleaned up",
        )

    def test_a_short_chunk_is_caught_before_the_digest_check(self):
        victim = self.store / f"part{self.manifest.chunk_count:04d}.bin"
        victim.write_bytes(b"short")
        out = self.scratch / "restored.mkv"
        with self.assertRaises(chunking.ChunkIntegrityError) as ctx:
            chunking.reassemble(self.manifest, str(out), self.fetch)
        self.assertIn("bytes but the manifest recorded", str(ctx.exception))
        self.assertFalse(out.exists())

    def test_a_missing_chunk_is_refused_rather_than_skipped(self):
        (self.store / f"part{self.manifest.chunk_count:04d}.bin").unlink()
        out = self.scratch / "restored.mkv"
        with self.assertRaises(OSError):
            chunking.reassemble(self.manifest, str(out), self.fetch)
        self.assertFalse(out.exists())

    def test_an_unstored_chunk_is_refused_before_anything_is_written(self):
        self.manifest.record(1).state = chunking.STATE_PENDING
        out = self.scratch / "restored.mkv"
        with self.assertRaises(chunking.ChunkIntegrityError) as ctx:
            chunking.reassemble(self.manifest, str(out), self.fetch)
        self.assertIn("not stored", str(ctx.exception))
        self.assertFalse(out.exists())

    def test_an_empty_manifest_is_refused(self):
        empty = chunking.ChunkManifest(str(self.scratch / "empty.jsonl"), {})
        with self.assertRaises(chunking.ChunkingError):
            chunking.reassemble(empty, str(self.scratch / "x.bin"), self.fetch)


class PerChunkRecoveryTests(unittest.TestCase):
    """'sirf us chunk ko Telegram se wapas manga kar retry' -- and nothing else."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="chunk_recover_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.source = self.scratch / "movie.mkv"
        self.data = _write_file(self.source, 2 * MIB + 1024)
        self.manifest = chunking.ChunkManifest.create(self.source, chunk_bytes=1 * MIB)

    def _good_copy(self, index) -> str:
        """A fetched file holding exactly the bytes chunk ``index`` should be."""
        record = self.manifest.record(index)
        piece = self.data[record.offset: record.offset + record.length]
        path = self.scratch / f"fetched{index:04d}.bin"
        path.write_bytes(piece)
        return str(path)

    def _bad_copy(self, index) -> str:
        """A fetched file whose bytes do NOT match the manifest digest."""
        record = self.manifest.record(index)
        path = self.scratch / f"bad{index:04d}.bin"
        path.write_bytes(b"\x00" * record.length)
        return str(path)

    def test_a_failed_chunk_is_fetched_back_and_retried_on_its_own(self):
        """The recovery loop: send fails -> fetch back -> send again."""
        record = self.manifest.record(2)
        self.manifest.begin(record)
        record.link = "https://t.me/t/502"
        attempts = {"send": 0, "recover": 0}

        def flaky_send(_record, _path):
            attempts["send"] += 1
            if attempts["send"] == 1:
                # First send fails, and what is in Telegram turns out to be
                # damaged, so the chunk genuinely has to be sent again.
                raise OSError("connection reset")
            return {"message_id": 502, "link": "https://t.me/t/502"}

        def recover(_chunk):
            attempts["recover"] += 1
            return self._bad_copy(2)

        result = chunking.upload_chunk_with_recovery(
            record, str(self.source), flaky_send, recover=recover, max_attempts=3,
        )
        self.assertEqual(attempts["send"], 2)
        self.assertGreaterEqual(attempts["recover"], 1)
        self.assertEqual(result["link"], "https://t.me/t/502")

    def test_a_chunk_already_safe_in_telegram_is_not_re_sent(self):
        """A send can raise AFTER Telegram stored the bytes. Fetching proves it."""
        record = self.manifest.record(3)
        self.manifest.begin(record)
        record.link = "https://t.me/t/503"
        sends = []

        def always_fails(_record, _path):
            sends.append(1)
            raise OSError("the connection died after the upload")

        result = chunking.upload_chunk_with_recovery(
            record, str(self.source), always_fails,
            recover=lambda _c: self._good_copy(3), max_attempts=3,
        )
        self.assertTrue(result.get("recovered"))
        self.assertEqual(len(sends), 1, "a verified stored chunk is not re-sent")
        self.assertEqual(result["link"], "https://t.me/t/503")

    def test_a_corrupt_copy_in_telegram_is_retried_rather_than_trusted(self):
        record = self.manifest.record(1)
        self.manifest.begin(record)
        record.link = "https://t.me/t/501"
        sends = []

        def flaky_send(_record, _path):
            sends.append(1)
            if len(sends) == 1:
                raise OSError("dropped")
            return {"message_id": 501, "link": "https://t.me/t/501"}

        result = chunking.upload_chunk_with_recovery(
            record, str(self.source), flaky_send,
            recover=lambda _c: self._bad_copy(1), max_attempts=3,
        )
        self.assertEqual(len(sends), 2, "a mismatching fetch-back must be retried")
        self.assertEqual(result["link"], "https://t.me/t/501")

    def test_exhausted_retries_raise_a_clear_error(self):
        record = self.manifest.record(1)
        self.manifest.begin(record)
        record.link = "https://t.me/t/501"
        sends = []

        def always_fails(_record, _path):
            sends.append(1)
            raise OSError("permanent failure")

        with self.assertRaises(chunking.ChunkRetryExhausted) as ctx:
            chunking.upload_chunk_with_recovery(
                record, str(self.source), always_fails,
                recover=lambda _c: self._bad_copy(1), max_attempts=2,
            )
        message = str(ctx.exception)
        self.assertIn("chunk 1", message)
        self.assertIn("after 2 attempt", message)
        # It must say what survives, or the operator cannot judge the damage.
        self.assertIn("intact", message)
        self.assertEqual(len(sends), 2)

    def test_no_attempts_is_refused_rather_than_silently_succeeding(self):
        record = self.manifest.record(1)
        with self.assertRaises(ValueError):
            chunking.upload_chunk_with_recovery(
                record, str(self.source), _make_sender(), max_attempts=0,
            )

    def test_a_chunk_with_no_link_does_not_attempt_a_pointless_fetch(self):
        record = self.manifest.record(1)
        recovers = []

        def always_fails(_record, _path):
            raise OSError("failed before any message existed")

        with self.assertRaises(chunking.ChunkRetryExhausted):
            chunking.upload_chunk_with_recovery(
                record, str(self.source), always_fails,
                recover=lambda chunk: recovers.append(chunk), max_attempts=3,
            )
        self.assertEqual(recovers, [], "nothing to fetch without a link")

    def test_a_send_returning_no_link_counts_as_a_failure(self):
        """A result with no link is not a stored chunk, whatever it returned."""
        record = self.manifest.record(1)
        self.manifest.begin(record)
        sends = []

        def linkless(_record, _path):
            sends.append(1)
            return {"message_id": 0, "link": ""}

        with self.assertRaises(chunking.ChunkRetryExhausted) as ctx:
            chunking.upload_chunk_with_recovery(
                record, str(self.source), linkless,
                recover=lambda _c: self._bad_copy(1), max_attempts=2,
            )
        self.assertEqual(len(sends), 2)
        self.assertIn("no message link", str(ctx.exception))


class IntegrationTests(unittest.TestCase):
    """The whole loop, on a file small enough to run in a test."""

    def test_a_flawed_upload_resumes_into_a_byte_identical_movie(self):
        scratch = Path(tempfile.mkdtemp(prefix="chunk_e2e_"))
        self.addCleanup(shutil.rmtree, scratch, True)
        source = scratch / "movie.mkv"
        data = _write_file(source, 3 * MIB + 777)
        manifest_path = str(scratch / "movie.chunks.jsonl")

        store = {}

        def flaky_send(record, source_path):
            if record.index == 3:
                raise OSError("simulated failure on chunk 3")
            store[record.index] = record
            return {"message_id": record.index,
                    "link": f"https://t.me/t/{record.index}"}

        # First pass dies on chunk 3 and re-raises, exactly as a real failure does.
        with self.assertRaises(OSError):
            chunking.resume(
                str(source), flaky_send, manifest_path=manifest_path,
                chunk_bytes=1 * MIB,
            )
        manifest = chunking.ChunkManifest.load(manifest_path)
        self.assertFalse(manifest.all_done())
        self.assertEqual(sorted(store), [1, 2])

        # Second pass finishes the job without re-sending 1 and 2.
        resent = []

        def good_send(record, source_path):
            resent.append(record.index)
            store[record.index] = record
            return {"message_id": record.index,
                    "link": f"https://t.me/t/{record.index}"}

        summary, manifest = chunking.resume(
            str(source), good_send, manifest_path=manifest_path,
        )
        self.assertTrue(summary.ok)
        self.assertTrue(manifest.all_done())
        self.assertNotIn(1, resent)
        self.assertNotIn(2, resent)
        self.assertIn(3, resent)

        # A fake Telegram holding exactly the stored chunks.
        telegram_dir = scratch / "telegram"
        telegram_dir.mkdir()
        for index, record in store.items():
            (telegram_dir / f"p{index:04d}.bin").write_bytes(
                data[record.offset: record.offset + record.length]
            )

        out = scratch / "restored.mkv"
        chunking.reassemble(
            manifest, str(out),
            lambda record: str(telegram_dir / f"p{record.index:04d}.bin"),
        )
        self.assertEqual(out.read_bytes(), data)
        self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(),
                         hashlib.sha256(data).hexdigest())

    def test_reopening_a_manifest_is_the_same_call_as_the_first_run(self):
        """``ChunkManifest.open`` is what makes resume a one-liner."""
        scratch = Path(tempfile.mkdtemp(prefix="chunk_open_"))
        self.addCleanup(shutil.rmtree, scratch, True)
        source = scratch / "m.bin"
        _write_file(source, 2 * MIB)
        path = str(scratch / "m.chunks.jsonl")

        first = chunking.ChunkManifest.open(str(source), path, chunk_bytes=1 * MIB)
        self.assertGreaterEqual(first.chunk_count, 2)
        second = chunking.ChunkManifest.open(str(source), path)
        self.assertEqual(second.chunk_count, first.chunk_count)
        self.assertEqual(second.header["chunk_size"], first.header["chunk_size"])
        # An existing manifest's layout is kept, not re-derived.
        third = chunking.ChunkManifest.open(str(source), path, chunk_bytes=8 * MIB)
        self.assertEqual(third.header["chunk_size"], first.header["chunk_size"])


if __name__ == "__main__":
    unittest.main(verbosity=2)