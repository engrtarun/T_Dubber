"""
Tests for the run ordering contract in start_dubbing.

The whole point of the redesign is that the Telegram archive happens *before*
the Kaggle handoff, so there is always an off-machine copy and a link to restart
from. That ordering is easy to break with an innocent-looking edit, and it is
invisible in the UI once a run is going, so it is pinned down here.

Run with:  python test_flow_order.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app


def _drain(generator):
    """Collect every tuple a Gradio event handler yields."""
    outputs = []
    for item in generator:
        outputs.append(item)
    return outputs


def _stage_text(outputs):
    """Pull the log text out of the first (and every) yielded tuple."""
    text = ""
    for item in outputs:
        update = item[0]
        value = getattr(update, "get", lambda *_: None)("value") if hasattr(update, "get") else None
        if isinstance(value, str):
            text = value
    return text


def _manifest_for(projects_dir):
    for name in sorted(os.listdir(projects_dir)):
        if name.startswith("_"):
            continue
        path = os.path.join(projects_dir, name, "project.json")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as handle:
                return name, json.load(handle)
    raise AssertionError("no project manifest was written")


# ---------------------------------------------------------------------------


def test_telegram_archive_runs_before_kaggle(scratch):
    print("\n[1] ordering: the Telegram archive happens before the Kaggle push")
    events = []

    def fake_backup(path, creds, source_url, project_id=None, media=None, channel=None):
        events.append("TG_ARCHIVE_START")
        yield "[STAGE:0] archiving now\n"
        events.append("TG_ARCHIVE_DONE")
        return "https://t.me/testchan/42"

    def fake_pipeline(*args, **kwargs):
        events.append("KAGGLE_START")
        assert kwargs.get("backup_link") == "https://t.me/testchan/42", (
            "the Kaggle stage must receive the archive link, got "
            f"{kwargs.get('backup_link')!r}"
        )
        yield "[STAGE:1] compressing\n"
        yield "[STAGE:7] Pipeline finished successfully!\n"

    app._telegram_backup_step = fake_backup
    app.run_pipeline = fake_pipeline
    app.PROJECTS_DIR = os.path.join(scratch, "projects")
    os.makedirs(app.PROJECTS_DIR, exist_ok=True)

    source = os.path.join(scratch, "clip.mp4")
    with open(source, "wb") as handle:
        handle.write(b"\x00" * 4096)

    outputs = _drain(app.start_dubbing(source, "Hindi", True, "", True))

    assert events == ["TG_ARCHIVE_START", "TG_ARCHIVE_DONE", "KAGGLE_START"], events
    logs = _stage_text(outputs)
    assert "archiving now" in logs
    assert "compressing" in logs

    project_id, manifest = _manifest_for(app.PROJECTS_DIR)
    assert manifest["telegram_backup"] == "https://t.me/testchan/42"
    assert manifest["target_language"] == "Hindi"
    assert manifest["source_kind"] == "upload"
    print(f"    events = {events}")
    print(f"    manifest records telegram_backup = {manifest['telegram_backup']}")


def test_kaggle_still_runs_when_the_backup_fails(scratch):
    print("\n[2] resilience: a failed backup is loud but does not block the dub")
    events = []

    def failing_backup(path, creds, source_url, project_id=None, media=None, channel=None):
        events.append("TG_ARCHIVE_START")
        yield "[STAGE:0] starting\n"
        raise app.TelegramCloudError("channel is not writable")

    def fake_pipeline(*args, **kwargs):
        events.append("KAGGLE_START")
        assert kwargs.get("backup_link") is None
        yield "[STAGE:1] compressing\n"
        yield "[STAGE:7] Pipeline finished successfully!\n"

    app._telegram_backup_step = failing_backup
    app.run_pipeline = fake_pipeline
    app.PROJECTS_DIR = os.path.join(scratch, "projects2")
    os.makedirs(app.PROJECTS_DIR, exist_ok=True)

    source = os.path.join(scratch, "clip2.mp4")
    with open(source, "wb") as handle:
        handle.write(b"\x00" * 4096)

    outputs = _drain(app.start_dubbing(source, "Hindi", True, "", True))

    assert events == ["TG_ARCHIVE_START", "KAGGLE_START"], events
    logs = _stage_text(outputs)
    assert "Telegram backup failed" in logs, "the failure must be visible in the log"
    assert "channel is not writable" in logs

    _project_id, manifest = _manifest_for(app.PROJECTS_DIR)
    assert manifest["telegram_backup"] is None
    assert "channel is not writable" in manifest["telegram_backup_error"]
    print("    Kaggle still ran, and the failure is recorded in the manifest")


def test_link_source_is_recorded_and_moved(scratch):
    print("\n[3] link sources: the resolved file is moved in, and the URL recorded")
    events = []
    payload = os.path.join(scratch, "downloaded_clip.mp4")
    with open(payload, "wb") as handle:
        handle.write(b"\x00" * 8192)

    class FakeMedia:
        path = payload
        title = "Some YouTube Video"
        kind = "web"
        size_bytes = 8192

    def fake_resolve(url, workdir):
        events.append("RESOLVE")
        yield "[STAGE:0] fetching\n"
        return FakeMedia()

    def fake_backup(path, creds, source_url, project_id=None, media=None, channel=None):
        events.append("TG_ARCHIVE_START")
        yield "[STAGE:0] archiving\n"
        return "https://t.me/testchan/99"

    def fake_pipeline(*args, **kwargs):
        events.append("KAGGLE_START")
        assert kwargs.get("source_url") == "https://youtu.be/abc", kwargs.get("source_url")
        assert kwargs.get("source_title") == "Some YouTube Video"
        assert os.path.isfile(args[0]), "the pipeline must receive a real local file"
        assert os.path.basename(args[0]) == "downloaded_clip.mp4"
        yield "[STAGE:7] Pipeline finished successfully!\n"

    app._resolve_link_step = fake_resolve
    app._telegram_backup_step = fake_backup
    app.run_pipeline = fake_pipeline
    app.PROJECTS_DIR = os.path.join(scratch, "projects3")
    os.makedirs(app.PROJECTS_DIR, exist_ok=True)

    _drain(app.start_dubbing(None, "Tamil", True, "https://youtu.be/abc", True))

    assert events == ["RESOLVE", "TG_ARCHIVE_START", "KAGGLE_START"], events
    project_id, manifest = _manifest_for(app.PROJECTS_DIR)
    assert manifest["source_url"] == "https://youtu.be/abc"
    assert manifest["source_kind"] == "web"
    assert manifest["target_language"] == "Tamil"
    assert manifest["title"] == "Some_YouTube_Video", manifest["title"]
    print(f"    title sanitised to {manifest['title']!r}, source_url and kind recorded")


def test_telegram_source_skips_the_second_upload(scratch):
    print("\n[4] Telegram sources: no pointless re-upload of an existing archive")
    events = []

    def fake_resolve(url, workdir):
        payload = os.path.join(scratch, "restored.mp4")
        with open(payload, "wb") as handle:
            handle.write(b"\x00" * 4096)

        class FakeMedia:
            path = payload
            title = "restored"
            kind = "telegram"
            size_bytes = 4096

        yield "[STAGE:0] restoring archive\n"
        return FakeMedia()

    def fake_backup(path, creds, source_url, project_id=None, media=None, channel=None):
        # Must be a generator: start_dubbing drains it with next().
        events.append("TG_BACKUP_CALLED")
        yield ""
        return source_url

    def fake_pipeline(*args, **kwargs):
        events.append("KAGGLE_START")
        yield "[STAGE:7] Pipeline finished successfully!\n"

    app._resolve_link_step = fake_resolve
    app._telegram_backup_step = fake_backup
    app.run_pipeline = fake_pipeline
    app.PROJECTS_DIR = os.path.join(scratch, "projects4")
    os.makedirs(app.PROJECTS_DIR, exist_ok=True)

    _drain(app.start_dubbing(None, "Hindi", True, "https://t.me/testchan/7", True))

    assert events == ["TG_BACKUP_CALLED", "KAGGLE_START"], events
    _project_id, manifest = _manifest_for(app.PROJECTS_DIR)
    assert manifest["telegram_backup"] == "https://t.me/testchan/7", (
        "a Telegram source must reuse its own link as the backup"
    )
    print("    backup link is the archive the user already pasted")


def test_real_backup_short_circuits_for_telegram_sources(scratch):
    print("\n[5] the real backup step short-circuits instead of re-uploading")
    # Exercises the genuine _telegram_backup_step so the short-circuit and its
    # message are covered rather than stubbed away.
    def explode(*args, **kwargs):
        raise AssertionError("upload_file_detailed must not run for a Telegram source")

    original_upload = app.upload_file_detailed
    app.upload_file_detailed = explode
    try:
        generator = app._telegram_backup_step(
            os.path.join(scratch, "whatever.mp4"),
            {"api_id": "1", "api_hash": "h", "phone": "+91", "channel": "@c"},
            "https://t.me/testchan/7",
        )
        lines = []
        link = None
        while True:
            try:
                lines.append(next(generator))
            except StopIteration as stop:
                link = stop.value
                break

        text = "".join(lines)
        assert link == "https://t.me/testchan/7", link
        assert "already lives in Telegram" in text, text
        assert "uploading it twice" in text, text
    finally:
        app.upload_file_detailed = original_upload
    print("    returned the pasted link, uploaded nothing, said so in the log")


def test_progress_throttling_and_log_lines(scratch):
    print("\n[7] progress: no log flooding, and no blank lines")
    import time as _time

    from app import _BackgroundJob, _transfer_log_line

    # Simulate a fast transfer: 2000 byte-level callbacks in a burst, which is
    # roughly what a small file produces on a quick connection.
    def burst(report):
        total = 22 * 1024 * 1024
        for step in range(1, 2001):
            report({"phase": "part", "current": total * step // 2000, "total": total})
        return "done"

    job = _BackgroundJob(burst, min_interval=1.0, min_percent_delta=1.0)
    events = list(job.events())
    assert job.check() == "done"
    assert len(events) < 60, f"{len(events)} events leaked through the throttle"

    # Every yielded line must carry content, never a bare stage marker.
    lines = [_transfer_log_line(payload) for payload in events]
    rendered = [line for line in lines if line]
    assert len(rendered) == len(events), "some events produced no line at all"
    for line in rendered:
        body = line.replace("[STAGE:0]", "").strip()
        assert body, f"blank log line produced: {line!r}"
        assert "%" in body, f"byte-level line lacks progress detail: {line!r}"
    # Windows consoles are cp1252, so keep the arrow out of stdout.
    sample = rendered[0].strip().encode("ascii", "backslashreplace").decode("ascii")
    print(f"    {len(events):>3} events -> {len(rendered)} lines, e.g. {sample}")

    # A message-bearing event must always survive, even repeated ones.
    job = _BackgroundJob(
        lambda report: (
            [report({"message": "Part 1/5 stored"}), report({"message": "Part 1/5 stored"}),
             report({"message": "Part 2/5 stored"})],
            "ok",
        )[1],
        min_interval=60.0,
    )
    notes = [p.get("message") for p in job.events()]
    assert notes == ["Part 1/5 stored", "Part 2/5 stored"], notes
    assert _transfer_log_line({"message": "hi"}) == "[STAGE:0] hi\n"
    assert _transfer_log_line({"total": 0, "current": 0}) is None
    print("    discrete milestones always pass; identical repeats collapse")


def test_no_video_and_no_link_is_refused(scratch):
    print("\n[6] input validation: neither file nor link means an immediate refusal")
    app.PROJECTS_DIR = os.path.join(scratch, "projects5")
    os.makedirs(app.PROJECTS_DIR, exist_ok=True)

    outputs = _drain(app.start_dubbing(None, "Hindi", True, "", True))
    assert len(outputs) == 1
    assert "Upload a video file or paste a source link" in _stage_text(outputs)

    outputs = _drain(
        app.start_dubbing(None, "Hindi", True, "https://mysite.example/page", True)
    )
    assert len(outputs) == 1
    text = _stage_text(outputs)
    assert "cannot be used" in text
    assert "mysite.example" in text
    print("    empty input and unsupported links both stop before any work starts")


# ---------------------------------------------------------------------------


def _safe(text):
    """Print-safe for a cp1252 Windows console."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def _safe_all(text):
    """Windows consoles are cp1252; keep assertion output printable."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def main():
    tests = [
        test_telegram_archive_runs_before_kaggle,
        test_kaggle_still_runs_when_the_backup_fails,
        test_link_source_is_recorded_and_moved,
        test_telegram_source_skips_the_second_upload,
        test_real_backup_short_circuits_for_telegram_sources,
        test_no_video_and_no_link_is_refused,
        test_progress_throttling_and_log_lines,
    ]

    # Keep the real handlers so a failed run cannot leak fakes into other tests.
    originals = {
        "run_pipeline": app.run_pipeline,
        "_telegram_backup_step": app._telegram_backup_step,
        "_resolve_link_step": app._resolve_link_step,
        "upload_file_detailed": app.upload_file_detailed,
        "PROJECTS_DIR": app.PROJECTS_DIR,
    }

    failures = []
    for test in tests:
        scratch = tempfile.mkdtemp(prefix="tgdub_")
        try:
            test(scratch)
        except AssertionError as exc:
            failures.append((test.__name__, str(exc)))
            print(f"    FAIL: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, repr(exc)))
            print(f"    ERROR: {exc!r}")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            for name, value in originals.items():
                setattr(app, name, value)

    print("\n" + "=" * 62)
    if failures:
        for name, message in failures:
            print(f"FAILED  {name}: {message}")
        print(f"{len(failures)}/{len(tests)} tests failed")
        return 1
    print(f"All {len(tests)} flow-order tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())


