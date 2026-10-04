"""
End-to-end tests for the Telegram cloud engine, with a fake Telegram server.

The chunking, resume, checksum and reassembly logic is the part that cannot be
exercised without a real account, and it is also the part that would silently
corrupt a multi-hour archive. These tests drive the real upload/restore code
paths against an in-memory stand-in for Telethon so all of it is verified.

Run with:  python test_tg_cloud.py
"""

import hashlib
import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import telegram_uploader as tg
from telethon.tl.types import DocumentAttributeFilename


# ---------------------------------------------------------------------------
# Fake Telegram
# ---------------------------------------------------------------------------


class FakeMessage:
    def __init__(self, message_id, name, data):
        self.id = message_id
        self.media = types.SimpleNamespace(
            document=types.SimpleNamespace(
                size=len(data),
                attributes=[DocumentAttributeFilename(name)],
            )
        )


class FakeClient:
    """Just enough Telethon surface for upload_file_detailed / download_or_restore."""

    def __init__(self):
        self.store = {}
        self._next_id = 1000
        self.sent_parts = []          # part names, in order, across the whole run
        self.corrupt_part = None      # part name to silently corrupt on read

    def is_connected(self):
        return True

    def is_user_authorized(self):
        return True

    def disconnect(self):
        pass

    def _store(self, name, data):
        self._next_id += 1
        self.store[self._next_id] = FakeMessage(self._next_id, name, data)
        self.sent_parts.append(name)
        return self.store[self._next_id]

    def send_file(self, channel, file=None, file_size=None, caption=None,
                  attributes=None, progress_callback=None, **kwargs):
        name = None
        for attribute in attributes or []:
            if isinstance(attribute, DocumentAttributeFilename):
                name = attribute.file_name
        if name is None:
            name = os.path.basename(str(file))

        if hasattr(file, "read"):
            buffer = bytearray()
            while True:
                block = file.read(256 * 1024)
                if not block:
                    break
                buffer.extend(block)
                if progress_callback:
                    progress_callback(len(buffer), file_size)
            data = bytes(buffer)
        else:
            with open(file, "rb") as handle:
                data = handle.read()

        if file_size is not None:
            assert len(data) == file_size, f"{name}: sent {len(data)} but declared {file_size}"
        return self._store(name, data)

    def get_messages(self, peer, ids=None):
        return self.store.get(int(ids))

    def download_media(self, message, file=None):
        data = self.store[int(message.id)].media.document.size
        name = message.media.document.attributes[0].file_name
        raw = self._bytes_for(message)
        if self.corrupt_part and self.corrupt_part in name:
            raw = bytes([raw[0] ^ 0xFF]) + raw[1:]
        with open(file, "wb") as handle:
            handle.write(raw)
        return file

    # Raw payload is kept alongside the message so corruption can be injected.
    def _bytes_for(self, message):
        return self._payloads[int(message.id)]

    _payloads = {}


def make_client():
    client = FakeClient()
    real_store = client.store

    class Patched(FakeClient):
        pass

    # Store payloads separately so download_media can mutate them.
    client._payloads = {}
    original_store = client._store

    def _store(name, data):
        message = original_store(name, data)
        client._payloads[message.id] = data
        return message

    client._store = _store
    assert real_store is not None
    return client


def _install(monkey_state, client):
    """Point telegram_uploader at the fake client and a scratch state dir."""
    tg._get_client = lambda *a, **k: client
    tg._client_cache.client = None
    return client


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def test_split_upload_produces_verified_parts(state):
    print("\n[1] split upload: every part hashed and sized correctly")
    client = make_client()
    _install(state, client)

    payload = os.urandom(5 * 1024 * 1024 + 777)
    source = os.path.join(state, "movie.mp4")
    with open(source, "wb") as handle:
        handle.write(payload)

    result = tg.upload_file_detailed(
        source, "1", "hash", "+9100", "@testchan", chunk_size=1024 * 1024
    )

    assert result["chunked"] is True, "a 5 MB file at 1 MB chunks must be split"
    assert result["chunk_count"] == 6, result["chunk_count"]
    assert result["state"] == "complete"

    parts = result["parts"]
    assert [p["part"] for p in parts] == [1, 2, 3, 4, 5, 6]
    assert len(client.sent_parts) == 7, f"6 parts + 1 manifest, got {client.sent_parts}"

    # Concatenating the stored parts must reproduce the source byte for byte.
    rebuilt = hashlib.sha256()
    for entry in parts:
        raw = client._payloads[entry["message_id"]]
        assert len(raw) == entry["size"]
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"], (
            f"part {entry['part']} hash mismatch"
        )
        rebuilt.update(raw)
    assert rebuilt.hexdigest() == hashlib.sha256(payload).hexdigest()
    print(f"    6 parts + manifest, all SHA-256 match, reassembly identical")


def test_single_small_file_is_not_split(state):
    print("\n[2] small file: uploaded whole, no manifest")
    client = make_client()
    _install(state, client)

    source = os.path.join(state, "small.mp4")
    with open(source, "wb") as handle:
        handle.write(os.urandom(64 * 1024))

    result = tg.upload_file_detailed(source, "1", "hash", "+9100", "@testchan")
    assert result["chunked"] is False
    assert result["chunk_count"] == 1
    assert len(client.sent_parts) == 1, client.sent_parts
    assert result["message_link"] == f"https://t.me/testchan/{result['message_id']}"
    print("    1 message, link points straight at the file")


def test_resume_skips_stored_parts(state):
    print("\n[3] resume: an interrupted upload does not resend stored parts")
    client = make_client()
    _install(state, client)

    payload = os.urandom(4 * 1024 * 1024)
    source = os.path.join(state, "resume.mp4")
    with open(source, "wb") as handle:
        handle.write(payload)

    # First run: interrupt on part 4 by making that part's send fail every time.
    original_store = client._store

    def flaky_store(name, data):
        if "part0004" in name:
            raise tg.TelegramCloudError("simulated connection drop")
        return original_store(name, data)

    client._store = flaky_store
    try:
        tg.upload_file_detailed(source, "1", "hash", "+9100", "@testchan", chunk_size=1024 * 1024)
    except tg.TelegramCloudError:
        pass
    else:
        raise AssertionError("the simulated drop should have surfaced")
    assert client.sent_parts == [
        "resume.mp4.part0001of0004.mp4",
        "resume.mp4.part0002of0004.mp4",
        "resume.mp4.part0003of0004.mp4",
    ], client.sent_parts

    # Second run: the journal holds parts 1-3, so only part 4 and the manifest
    # should be sent.
    client._store = original_store
    before = len(client.sent_parts)
    result = tg.upload_file_detailed(source, "1", "hash", "+9100", "@testchan", chunk_size=1024 * 1024)
    sent_now = client.sent_parts[before:]
    assert sent_now == ["resume.mp4.part0004of0004.mp4", "resume.mp4.manifest.json"], sent_now
    assert result["state"] == "complete"
    assert result["chunk_count"] == 4
    print("    run 1 stored 3 parts then dropped; run 2 sent only part 4 + manifest")


def test_restore_rebuilds_and_verifies(state):
    print("\n[4] restore: manifest link rebuilds the original file")
    client = make_client()
    _install(state, client)

    payload = os.urandom(3 * 1024 * 1024 + 5)
    source = os.path.join(state, "restore me.mp4")
    with open(source, "wb") as handle:
        handle.write(payload)

    result = tg.upload_file_detailed(source, "1", "hash", "+9100", "@testchan", chunk_size=1024 * 1024)
    assert result["chunked"] is True

    destination = os.path.join(state, "out")
    restored = tg.download_or_restore(
        result["message_link"], "1", "hash", "+9100", destination
    )
    assert os.path.isfile(restored)
    with open(restored, "rb") as handle:
        assert handle.read() == payload, "restored bytes differ from the source"

    # Also restore from a bare part link: no manifest, so it comes back as-is.
    single = tg.download_or_restore(
        result["parts"][0]["link"], "1", "hash", "+9100", destination
    )
    assert os.path.isfile(single)
    print("    rebuilt from the manifest, and a bare part link resolves too")


def test_corrupt_part_is_rejected(state):
    print("\n[5] corruption: a damaged part aborts the rebuild instead of shipping")
    client = make_client()
    _install(state, client)

    source = os.path.join(state, "damaged.mp4")
    with open(source, "wb") as handle:
        handle.write(os.urandom(2 * 1024 * 1024))

    result = tg.upload_file_detailed(source, "1", "hash", "+9100", "@testchan", chunk_size=1024 * 1024)
    client.corrupt_part = "part0002"

    destination = os.path.join(state, "out-damaged")
    try:
        tg.download_or_restore(result["message_link"], "1", "hash", "+9100", destination)
    except tg.TelegramCloudError as exc:
        assert "checksum" in str(exc).lower() or "SHA-256" in str(exc), str(exc)
    else:
        raise AssertionError("a corrupted part must not pass verification")

    leftovers = [n for n in os.listdir(destination)] if os.path.isdir(destination) else []
    assert leftovers == [], f"a failed restore must not leave files behind: {leftovers}"
    print("    detected via SHA-256, no partial file left on disk")


def test_identical_file_reuses_archive(state):
    print("\n[6] idempotency: re-uploading the same file reuses the archive")
    client = make_client()
    _install(state, client)

    source = os.path.join(state, "same.mp4")
    with open(source, "wb") as handle:
        handle.write(os.urandom(128 * 1024))

    first = tg.upload_file_detailed(source, "1", "hash", "+9100", "@testchan")
    sent_after_first = len(client.sent_parts)
    second = tg.upload_file_detailed(source, "1", "hash", "+9100", "@testchan")

    assert second.get("reused") is True
    assert second["message_link"] == first["message_link"]
    assert len(client.sent_parts) == sent_after_first, "nothing should have been re-sent"
    print("    second call returned the original link with zero new uploads")


def test_session_lock_blocks_a_second_process(state):
    print("\n[8] session guard: a second process is refused with a clear message")
    # A lock file inside the scratch dir, so this never touches the real session
    # lock that a live T_Dubber process may be holding.
    lock_path = os.path.join(state, "session.lock")
    original_lock_path = tg.SESSION_LOCK_PATH
    tg.SESSION_LOCK_PATH = lock_path

    holder = open(lock_path, "a+b")
    holder.seek(0)
    holder.write(b"0")
    holder.flush()
    try:
        tg._acquire_lock(holder)

        # Simulate a second process: it holds no in-process handle and must go
        # through the same file lock.
        tg.release_session_lock()
        try:
            tg.hold_session_lock(wait_seconds=0.5)
        except tg.TelegramSessionBusy as exc:
            message = str(exc)
            assert "Another T_Dubber window" in message, message
            assert "journalled and will not be sent again" in message, message
        else:
            raise AssertionError("the session lock must refuse a second holder")
    finally:
        try:
            tg._release_lock(holder)
        finally:
            holder.close()
            tg.release_session_lock()
            tg.SESSION_LOCK_PATH = original_lock_path

    # Once released, the lock can be taken again. Still pointing at the scratch
    # path, so a genuinely busy real session cannot fail this check.
    tg.SESSION_LOCK_PATH = lock_path
    try:
        assert tg.hold_session_lock(wait_seconds=0.5) is True
    finally:
        tg.release_session_lock()
        tg.SESSION_LOCK_PATH = original_lock_path
    print("    refused while held, released cleanly, re-acquirable afterwards")


def test_cancelled_transfer_is_classified(state):
    print("\n[9] cancelled transfers become an actionable message, not a traceback")
    import asyncio

    client = make_client()
    _install(state, client)

    original_store = client._store

    def cancelled_store(name, data):
        if "part0002" in name:
            # This is what Telethon raises when the SQLite session is contended.
            raise asyncio.CancelledError()
        return original_store(name, data)

    client._store = cancelled_store

    payload = os.urandom(3 * 1024 * 1024)
    source = os.path.join(state, "cancelled.mp4")
    with open(source, "wb") as handle:
        handle.write(payload)

    # Reduce retries so the test does not sit through three 10s waits.
    original_retries = tg.PART_UPLOAD_RETRIES
    original_sleep = tg.time.sleep
    tg.PART_UPLOAD_RETRIES = 1
    tg.time.sleep = lambda _s: None
    try:
        tg.upload_file_detailed(
            source, "1", "hash", "+9100", "@testchan", chunk_size=1024 * 1024
        )
    except tg.TelegramSessionBusy as exc:
        message = str(exc)
        assert "another process" in message, message
        assert "parts 1-1 are already stored" in message, message
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"expected TelegramSessionBusy, got {exc!r}")
    else:
        raise AssertionError("a cancelled transfer must not report success")
    finally:
        tg.PART_UPLOAD_RETRIES = original_retries
        tg.time.sleep = original_sleep

    # CancelledError is a BaseException, so the journal must still record why.
    # The recorded type is the classified TelegramSessionBusy, not the raw
    # CancelledError, because that is what the user actually needs to read.
    key = tg._fingerprint(os.path.abspath(source), os.path.getsize(source))
    journal = tg._read_json(tg.state_path_for(key), {}) or {}
    assert journal.get("state") == "failed", journal.get("state")
    assert "TelegramSessionBusy" in journal.get("error", ""), journal.get("error")
    assert journal.get("parts"), "part 1 must remain journalled for resume"
    print("    raised TelegramSessionBusy and the journal recorded the failure")


def test_go_crosscheck_agrees_on_an_unchanged_file(state):
    print("\n[10] Go pre-hash cross-check agrees when the file is untouched")
    client = make_client()
    _install(state, client)

    payload = os.urandom(3 * 1024 * 1024 + 91)
    source = os.path.join(state, "crosscheck.bin")
    with open(source, "wb") as handle:
        handle.write(payload)

    # Ask the helper for the same plan it would produce mid-upload.
    digests = tg._plan_digests_from_go(source, 1024 * 1024, 4)
    if not digests:
        print("    skipped: the Go helper is unavailable here")
        return

    for part_number, offset, length in tg.plan_chunks(len(payload), 1024 * 1024):
        prefix = digests.get(part_number)
        view = tg.HashingFileSlice(source, offset, length, f"part{part_number}")
        try:
            while True:
                block = view.read(256 * 1024)
                if not block:
                    break
        finally:
            streamed = view.hexdigest()
            view.close()
        assert prefix, f"part {part_number} had no pre-hash"
        assert streamed.startswith(prefix), (
            f"part {part_number}: streamed {streamed[:16]} does not start with "
            f"pre-hash {prefix}"
        )
    print(f"    {len(digests)} prefixes matched their full streamed digests")


def test_go_crosscheck_catches_a_mutated_file(state):
    print("\n[11] Go pre-hash cross-check catches a file that changes mid-flight")
    client = make_client()
    _install(state, client)

    source = os.path.join(state, "mutating.bin")
    with open(source, "wb") as handle:
        handle.write(os.urandom(2 * 1024 * 1024))

    digests = tg._plan_digests_from_go(source, 1024 * 1024, 2)
    if not digests:
        print("    skipped: the Go helper is unavailable here")
        return

    # Simulate the file being edited after the pre-hash was taken.
    with open(source, "r+b") as handle:
        handle.seek(4096)
        handle.write(b"\xff\xff\xff\xff")

    mutated = False
    for part_number, offset, length in tg.plan_chunks(os.path.getsize(source), 1024 * 1024):
        view = tg.HashingFileSlice(source, offset, length, f"part{part_number}")
        try:
            while True:
                block = view.read(256 * 1024)
                if not block:
                    break
        finally:
            streamed = view.hexdigest()
            view.close()
        if not tg._digest_prefix_agrees(digests.get(part_number, ""), streamed):
            mutated = True
            break

    assert mutated, "a mutated file must not match its pre-hash"
    print("    the changed part was detected by prefix mismatch")


def test_empty_file_refused(state):
    print("\n[7] guard rails: empty files and missing files are refused")
    client = make_client()
    _install(state, client)

    empty = os.path.join(state, "empty.mp4")
    open(empty, "wb").close()
    try:
        tg.upload_file_detailed(empty, "1", "hash", "+9100", "@testchan")
    except tg.TelegramCloudError as exc:
        assert "0-byte" in str(exc)
    else:
        raise AssertionError("a 0-byte upload must be refused")

    try:
        tg.upload_file_detailed(
            os.path.join(state, "nope.mp4"), "1", "hash", "+9100", "@testchan"
        )
    except tg.TelegramCloudError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("a missing file must be refused")

    for bad in ("", "https://t.me/", "https://t.me/onlychannel",
                "https://example.com/a/b/c"):
        try:
            tg.parse_tg_link(bad)
        except (tg.TelegramCloudError, ValueError):
            pass
        else:
            raise AssertionError(f"parse_tg_link accepted {bad!r}")
    print("    empty, missing and malformed links all refused with a reason")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main():
    tests = [
        test_split_upload_produces_verified_parts,
        test_single_small_file_is_not_split,
        test_resume_skips_stored_parts,
        test_restore_rebuilds_and_verifies,
        test_corrupt_part_is_rejected,
        test_identical_file_reuses_archive,
        test_empty_file_refused,
        test_session_lock_blocks_a_second_process,
        test_cancelled_transfer_is_classified,
        test_go_crosscheck_agrees_on_an_unchanged_file,
        test_go_crosscheck_catches_a_mutated_file,
    ]

    original_state_dir = tg.STATE_DIR
    failures = []

    for test in tests:
        scratch = tempfile.mkdtemp(prefix="tgcloud_")
        tg.STATE_DIR = os.path.join(scratch, ".tg_uploads")
        os.makedirs(tg.STATE_DIR, exist_ok=True)
        try:
            test(scratch)
        except AssertionError as exc:
            failures.append((test.__name__, str(exc)))
            print(f"    FAIL: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, repr(exc)))
            print(f"    ERROR: {exc!r}")
        finally:
            tg.STATE_DIR = original_state_dir
            shutil.rmtree(scratch, ignore_errors=True)

    print("\n" + "=" * 62)
    if failures:
        for name, message in failures:
            print(f"FAILED  {name}: {message}")
        print(f"{len(failures)}/{len(tests)} tests failed")
        return 1
    print(f"All {len(tests)} Telegram cloud tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
