"""test_pipeline_cleanup.py -- zero storage residue, pinned.

The user directive this file pins: after today, a run must not leave junk on
this machine's disk. Three mechanisms make that true, and each is easy to
silently undo, so each has its own test:

* ``run_pipeline`` (and ``reattach_to_kernel``) wrap their bodies in
  try/finally -- every exit path (success, early return, exception, Ctrl+C,
  a consumer that stops iterating) removes ``compressed_*.mp4``,
  ``dataset_safe/`` and ``kernel_safe/`` from the project dir.
* Heavy staging is ALSO deleted at the earliest provable point, so it is gone
  during the hours the worker runs, not after: the compressed temp right
  after its copy into the dataset, ``dataset_safe`` right after the dataset
  upload, ``kernel_safe`` right after the push loop.
* Whatever `kaggle kernels output` mirrors back from /kaggle/working
  (pylibs/, mazinger_output/, voice wavs, duplicate logs) is pruned the
  moment the outputs land, keeping one log for post-mortems; and
  ``sweep_stale_scratch()`` reaps what older crashes left behind.

No network and no real kaggle CLI: ``pipeline.run_cmd`` is replaced by a
fake that simulates the whole Kaggle conversation, ``APP_DIR`` points at a
throwaway tree, and telemetry (db + havaldar daemon) is switched off so the
suite can never touch the production database.

Run: python test_pipeline_cleanup.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import pytest

if TYPE_CHECKING:  # annotation-only names; `from __future__` keeps them strings
    from collections.abc import Generator, Iterator

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Path bootstrap must run before these two imports (E402 is not enabled).
import pipeline
import testkit


# ---------------------------------------------------------------------------
# The fake world: APP_DIR tree, Kaggle CLI, subtitle binary
# ---------------------------------------------------------------------------


def _build_fake_app_dir(root: Path) -> Path:
    """A minimal APP_DIR containing everything run_pipeline stages.

    Mirrors what pipeline.py reads from the real repo: the mazinger submodule
    tree (with the pyproject.toml the bundle check looks for), multitasker.py,
    the huggingface/ and lip_sync/ connectors, the worker notebook, and
    kaggle.json for the credentials check.
    """
    app_dir = root / "app"
    mazinger = app_dir / "mazinger"
    (mazinger / "mazinger").mkdir(parents=True)
    (mazinger / "pyproject.toml").write_text("[project]\nname = 'mazinger'\n", encoding="utf-8")
    (mazinger / "mazinger" / "__init__.py").write_text("", encoding="utf-8")
    (app_dir / "multitasker.py").write_text("# staged\n", encoding="utf-8")
    (app_dir / "huggingface").mkdir()
    (app_dir / "huggingface" / "hf_store.py").write_text("# connector\n", encoding="utf-8")
    (app_dir / "lip_sync").mkdir()
    (app_dir / "lip_sync" / "lip_sync.py").write_text("# provider\n", encoding="utf-8")
    (app_dir / "kaggle_worker.ipynb").write_text("{}", encoding="utf-8")
    creds = app_dir / "kaggle_paperWork"
    creds.mkdir()
    (creds / "kaggle.json").write_text(json.dumps({"username": "testuser"}), encoding="utf-8")
    return app_dir


def _fake_subtitles(
    transcript_json: str, output_stem: str, srt: bool = True, ass: bool = True
) -> dict[str, Any]:
    """generate_subtitles stand-in: writes the files the contract promises.

    The real binary is a Rust executable; whether it runs is not what these
    tests check. What they check is that the subtitle DELIVERABLES survive
    the cleanup, so the files have to exist for real.
    """
    outputs: dict[str, Any] = {"srt": None, "ass": None, "ok": True}
    if srt:
        Path(output_stem + ".srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8"
        )
        outputs["srt"] = output_stem + ".srt"
    if ass:
        Path(output_stem + ".ass").write_text("[Script Info]\n", encoding="utf-8")
        outputs["ass"] = output_stem + ".ass"
    return outputs


class _Result:
    """Stand-in for subprocess.CompletedProcess as run_cmd callers see it."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeKaggle:
    """A ``pipeline.run_cmd`` replacement that fakes the Kaggle CLI end to end.

    It records every command and answers the ones the pipeline actually sends:
    ffprobe reports a 2160p source (forcing the compress path), ffmpeg writes
    the compressed temp, ``kaggle datasets init`` writes dataset-metadata.json
    into its -p directory, ``kernels status`` reports ``kernel_status``,
    ``kernels output`` mirrors the worker's /kaggle/working back into the
    project dir the way the real CLI does -- deliverables plus the junk
    (pylibs/, mazinger_output/, voice wav, duplicate logs, error_log.txt).

    Failure injection:
      * ``fail_on`` maps a command fragment to an exception to raise;
      * ``crash_after_writing_compressed`` raises right AFTER the compressed
        temp hits disk -- the "ffmpeg died mid-run" case;
      * ``kernel_status="error"`` makes the worker fail on Kaggle.
    """

    def __init__(
        self,
        project_dir: Path,
        kernel_status: str = "complete",
        fail_on: dict[str, BaseException] | None = None,
        crash_after_writing_compressed: bool = False,
    ) -> None:
        self.project_dir = project_dir
        self.kernel_status = kernel_status
        self.fail_on = fail_on if fail_on is not None else {}
        self.crash_after_writing_compressed = crash_after_writing_compressed
        self.commands: list[list[str]] = []
        #: Path of the compressed temp the fake ffmpeg wrote, if any.
        self.compressed_written: str | None = None
        #: Observation taken AT a crash: was the temp still on disk there?
        #: None until a crash_after_writing_compressed run actually crashes.
        self.compressed_existed_at_crash: bool | None = None

    def __call__(self, cmd: list[str], **kwargs: Any) -> _Result:
        self.commands.append(list(cmd))
        text = " ".join(cmd)
        # Observe the temp's presence AT the upload command, before any
        # injected failure unwinds the generator -- the early-delete proof is
        # about state at the last consumer, not after the run exited.
        if (
            self.compressed_written
            and "datasets version" in text
            and self.compressed_existed_at_crash is None
        ):
            self.compressed_existed_at_crash = Path(self.compressed_written).exists()
        for needle, exc in self.fail_on.items():
            if needle in text:
                raise exc
        if cmd[0] == "ffprobe":
            return _Result(stdout="2160\n")  # >1080p -> the pipeline compresses
        if cmd[0] == "ffmpeg":
            output = cmd[-1]
            Path(output).write_bytes(b"fake-compressed-video")
            self.compressed_written = output
            if self.crash_after_writing_compressed:
                self.compressed_existed_at_crash = Path(output).exists()
                raise RuntimeError("simulated crash mid-run")
            return _Result()
        if "datasets" in text and "init" in text:
            meta_dir = cmd[cmd.index("-p") + 1]
            Path(meta_dir, "dataset-metadata.json").write_text(
                json.dumps({"id": "", "title": ""}), encoding="utf-8"
            )
            return _Result()
        if "kernels" in text and "status" in text:
            return _Result(stdout=f"your kernel {self.kernel_status}")
        if "kernels" in text and "output" in text:
            self._mirror_worker_working_dir(with_outputs=self.kernel_status == "complete")
            return _Result()
        # `kaggle --version`, `datasets status`, `datasets version`,
        # `kernels init`, `kernels push`: all succeed silently.
        return _Result()

    def _mirror_worker_working_dir(self, with_outputs: bool) -> None:
        """Recreate what `kaggle kernels output` downloads into project_dir."""
        root = self.project_dir
        if with_outputs:
            (root / "output.mp4").write_bytes(b"\x00" * 4096)
            testkit.write_json(root / "report.json", {"ok": True, "engine": "mazinger"})
            testkit.write_json(root / "movie_transcript.json", {"text": "hello"})
        else:
            # A failed worker has no outputs, only the error trail.
            (root / "error_log.txt").write_text("worker crashed loudly\n", encoding="utf-8")
        (root / "pylibs").mkdir(exist_ok=True)
        (root / "pylibs" / "torch.pyd").write_bytes(b"\x00" * 2048)
        (root / "mazinger_output").mkdir(exist_ok=True)
        (root / "mazinger_output" / "scratch.bin").write_bytes(b"\x00" * 1024)
        (root / "source_voice_reference.wav").write_bytes(b"\x00" * 512)
        (root / "vllm_server.log").write_text("older log\n", encoding="utf-8")
        (root / "mazinger_stderr.log").write_text("newest log\n", encoding="utf-8")
        # "Newest" must be deterministic; some filesystems store mtimes
        # coarsely enough that back-to-back writes compare equal.
        now = time.time()
        os.utime(root / "vllm_server.log", (now - 300, now - 300))
        os.utime(root / "mazinger_stderr.log", (now, now))


class Sandbox(NamedTuple):
    """The throwaway world one pipeline test runs in."""

    project_dir: Path
    source_video: Path


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sandbox]:
    """run_pipeline wired to a fake app tree, fake Kaggle and no telemetry.

    The source video lives INSIDE the project dir, exactly where app.py
    persists it, so the stage-6 `*.mp4` glob sees the same things a real run
    sees and the deliverable assertions mean something.
    """
    workspace = testkit.ScratchWorkspace(prefix="pipeline_cleanup_")
    try:
        app_dir = _build_fake_app_dir(workspace.path)
        project_dir = workspace.path / "projects" / "demo_project"
        project_dir.mkdir(parents=True)
        source = testkit.fake_media(project_dir, name="input_video.mp4")

        monkeypatch.setattr(pipeline, "APP_DIR", str(app_dir))
        monkeypatch.setattr(pipeline, "local_kaggle_dir", str(app_dir / "kaggle_paperWork"))
        monkeypatch.setattr(pipeline, "_HAS_HAVALDAR", False)
        # Telemetry off: track_stage is strictly best-effort around a missing
        # db (that is its documented contract), so this both keeps the suite
        # away from any database and exercises that contract.
        monkeypatch.setattr(pipeline, "db", None)
        monkeypatch.setattr(pipeline, "generate_subtitles", _fake_subtitles)
        # A mis-stubbed branch must fail the test, never sleep 15-60s.
        # Patched on the stdlib module itself: pipeline calls time.sleep on
        # that same object, and `pipeline.time` is not a re-export.
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
        yield Sandbox(project_dir=project_dir, source_video=source)
    finally:
        workspace.close()


def _start_run(sandbox: Sandbox) -> list[str]:
    """Drain one full run_pipeline against the sandbox's fake world."""
    return list(
        pipeline.run_pipeline(str(sandbox.source_video), str(sandbox.project_dir), "Hindi", True)
    )


def _assert_no_staging(project_dir: Path) -> None:
    """The three scratch artifacts, in one place: none may survive a run."""
    assert not (project_dir / "dataset_safe").exists()
    assert not (project_dir / "kernel_safe").exists()
    assert not list(project_dir.glob("compressed_*.mp4"))


# ---------------------------------------------------------------------------
# (b) + (c) the successful run
# ---------------------------------------------------------------------------


def test_success_run_leaves_only_deliverables(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished run leaves the product and nothing else."""
    fake = FakeKaggle(sandbox.project_dir)
    monkeypatch.setattr(pipeline, "run_cmd", fake)

    lines = _start_run(sandbox)

    joined = "".join(lines)
    assert "Pipeline finished successfully" in joined
    # (b) staging is gone -- dataset_safe is deleted right after the upload,
    # kernel_safe right after the push, so this holds even before the finally.
    _assert_no_staging(sandbox.project_dir)
    # the compressed temp really was created (the 2160p probe forces it),
    # and the copy into the dataset is all that remains of it
    assert fake.compressed_written is not None
    assert not Path(fake.compressed_written).exists()
    # (c) deliverables survive
    assert (sandbox.project_dir / "output.mp4").stat().st_size > 0
    assert (sandbox.project_dir / "report.json").is_file()
    assert (sandbox.project_dir / "output.srt").is_file()
    assert (sandbox.project_dir / "output.ass").is_file()
    # the transcript the subtitles were built from is product, not scratch
    assert (sandbox.project_dir / "movie_transcript.json").is_file()
    # the app.py-owned input video is untouched
    assert sandbox.source_video.is_file()
    # the /kaggle/working junk mirror is pruned, newest log kept
    assert not (sandbox.project_dir / "pylibs").exists()
    assert not (sandbox.project_dir / "mazinger_output").exists()
    assert not (sandbox.project_dir / "source_voice_reference.wav").exists()
    remaining_logs = sorted(path.name for path in sandbox.project_dir.glob("*.log"))
    assert remaining_logs == ["mazinger_stderr.log"]
    # the success path drops the worker's error log
    assert not (sandbox.project_dir / "error_log.txt").exists()


# ---------------------------------------------------------------------------
# (a) abnormal exits
# ---------------------------------------------------------------------------


def test_crash_mid_compression_leaves_zero_scratch(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception right after the compressed temp hits disk cleans it.

    The fake ffmpeg writes the temp and then raises -- the "ffmpeg process
    died mid-run" case. Without the generator's finally, that temp (a full
    480p re-encode in production) used to sit on disk forever.
    """
    fake = FakeKaggle(sandbox.project_dir, crash_after_writing_compressed=True)
    monkeypatch.setattr(pipeline, "run_cmd", fake)

    with pytest.raises(RuntimeError, match="simulated crash mid-run"):
        _start_run(sandbox)

    assert fake.compressed_written is not None
    _assert_no_staging(sandbox.project_dir)


def test_crash_at_dataset_upload_proves_the_early_delete(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the upload crash point the temp is ALREADY gone -- not just at exit.

    The crash fires at `kaggle datasets version`, hours before a real worker
    would finish. The stub records whether the temp was still on disk at the
    moment of the crash; the old behaviour kept it until the very end of the
    generator (or leaked it outright).
    """
    fake = FakeKaggle(
        sandbox.project_dir,
        fail_on={"datasets version": RuntimeError("simulated upload crash")},
    )
    monkeypatch.setattr(pipeline, "run_cmd", fake)

    with pytest.raises(RuntimeError, match="simulated upload crash"):
        _start_run(sandbox)

    assert fake.compressed_existed_at_crash is False
    _assert_no_staging(sandbox.project_dir)


def test_abandoned_consumer_leaves_zero_scratch(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl+C / closed browser tab: GeneratorExit mid-run still cleans.

    Closing the generator surface exactly what those look like from inside:
    the next ``yield`` raises GeneratorExit. The compressed temp exists at
    that point (the copy into the dataset happens later), so only the
    wrapper's finally can remove it.
    """
    fake = FakeKaggle(sandbox.project_dir)
    monkeypatch.setattr(pipeline, "run_cmd", fake)

    # run_pipeline is declared Iterator, but is a generator; close() is how a
    # consumer abandoning the stream is simulated (GeneratorExit inside).
    stream = cast(
        "Generator[str, None, None]",
        pipeline.run_pipeline(str(sandbox.source_video), str(sandbox.project_dir), "Hindi", True),
    )
    for line in stream:
        if "Compression successful" in line:
            break
    stream.close()

    assert fake.compressed_written is not None
    _assert_no_staging(sandbox.project_dir)


def test_failed_worker_keeps_error_log_and_prunes_junk(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed run keeps its post-mortem and nothing heavy.

    error_log.txt plus the single newest worker log survive -- they are the
    only diagnostic the user has. The multi-MB junk mirror and all staging
    are gone even though the run failed.
    """
    fake = FakeKaggle(sandbox.project_dir, kernel_status="error")
    monkeypatch.setattr(pipeline, "run_cmd", fake)

    lines = _start_run(sandbox)

    assert "worker crashed loudly" in "".join(lines)
    assert "Pipeline finished successfully" not in "".join(lines)
    # the post-mortem survives...
    assert (sandbox.project_dir / "error_log.txt").is_file()
    remaining_logs = sorted(path.name for path in sandbox.project_dir.glob("*.log"))
    assert remaining_logs == ["mazinger_stderr.log"]
    # ...the heavy junk does not...
    assert not (sandbox.project_dir / "pylibs").exists()
    assert not (sandbox.project_dir / "mazinger_output").exists()
    assert not (sandbox.project_dir / "source_voice_reference.wav").exists()
    # ...and a failed worker produced no output video to mistake for one
    assert not (sandbox.project_dir / "output.mp4").exists()
    # failed runs clean their staging too
    _assert_no_staging(sandbox.project_dir)


# ---------------------------------------------------------------------------
# (e) the empty mazinger submodule
# ---------------------------------------------------------------------------


def test_empty_mazinger_fails_fast_before_any_staging(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty submodule must refuse before a single staging byte exists.

    This is the exact on-disk state of this machine today (the submodule dir
    exists with zero files). It used to surface minutes deep into stage 2,
    after the source video, the whole mazinger tree and the source zip had
    been staged -- and every one of those doomed runs left all of it behind.
    """
    fake = FakeKaggle(sandbox.project_dir)
    monkeypatch.setattr(pipeline, "run_cmd", fake)
    # Present-but-empty: the directory exists, the submodule contents do not.
    app_mazinger = Path(pipeline.APP_DIR) / "mazinger"
    shutil.rmtree(app_mazinger)
    app_mazinger.mkdir()

    with pytest.raises(RuntimeError) as excinfo:
        _start_run(sandbox)

    message = str(excinfo.value)
    assert "mazinger" in message
    assert "git submodule update --init --recursive mazinger" in message
    # fail-FAST: not one staging byte, not one dataset command
    _assert_no_staging(sandbox.project_dir)
    dataset_commands = [c for c in fake.commands if "datasets" in " ".join(c)]
    assert dataset_commands == []


# ---------------------------------------------------------------------------
# (d) the startup sweep
# ---------------------------------------------------------------------------


def test_sweep_stale_scratch_reaps_only_junk() -> None:
    """The sweep removes stale junk, never deliverables, never live runs."""
    with testkit.ScratchWorkspace(prefix="sweep_stale_") as ws:
        projects = ws.path / "projects"
        now = time.time()
        stale = now - (30 * 3600)  # older than the 24h default cap
        ancient = now - (8 * 24 * 3600)  # older than the 7-day state fallback

        finished = projects / "finished_run"
        (finished / "dataset_safe" / "mazinger").mkdir(parents=True)
        (finished / "kernel_safe").mkdir()
        testkit.write_json(finished / "project.json", {"state": "success"})
        (finished / "output.mp4").write_bytes(b"\x00" * 4096)
        testkit.write_json(finished / "report.json", {"ok": True})
        (finished / "output.srt").write_text("1\n", encoding="utf-8")
        (finished / "compressed_old.mp4").write_bytes(b"\x00" * 16)
        os.utime(finished / "compressed_old.mp4", (stale, stale))

        live = projects / "live_run"
        (live / "dataset_safe").mkdir(parents=True)
        testkit.write_json(live / "project.json", {"state": "processing"})
        (live / "compressed_fresh.mp4").write_bytes(b"\x00" * 16)  # below the cap
        (live / "video.mp4.part").write_bytes(b"\x00" * 16)  # below the cap

        stuck = projects / "stuck_run"
        (stuck / "dataset_safe").mkdir(parents=True)
        testkit.write_json(stuck / "project.json", {"state": "processing"})
        (stuck / "compressed_dead.mp4").write_bytes(b"\x00" * 16)  # over the cap
        os.utime(stuck / "compressed_dead.mp4", (stale, stale))

        legacy = projects / "legacy_run_no_manifest"
        (legacy / "dataset_safe").mkdir(parents=True)
        os.utime(legacy / "dataset_safe", (ancient, ancient))

        removed = pipeline.sweep_stale_scratch(str(projects))

        # finished run: staging reaped, deliverables untouched
        assert not (finished / "dataset_safe").exists()
        assert not (finished / "kernel_safe").exists()
        assert not (finished / "compressed_old.mp4").exists()
        assert (finished / "output.mp4").is_file()
        assert (finished / "report.json").is_file()
        assert (finished / "output.srt").is_file()
        assert (finished / "project.json").is_file()
        # live run: untouched, it may still be running
        assert (live / "dataset_safe").exists()
        assert (live / "compressed_fresh.mp4").exists()
        assert (live / "video.mp4.part").exists()
        # stuck "processing" run: the 24h cap reaps the temp even without a
        # finished state; its staging waits for the 7-day fallback
        assert not (stuck / "compressed_dead.mp4").exists()
        assert (stuck / "dataset_safe").exists()
        # legacy project with no manifest at all: 7-day fallback reaps staging
        assert not (legacy / "dataset_safe").exists()

        assert removed == {"compressed": 2, "staging": 3, "partial": 0}


def test_sweep_stale_scratch_is_idempotent() -> None:
    """A second sweep finds nothing; a missing projects dir is not an error."""
    with testkit.ScratchWorkspace(prefix="sweep_idem_") as ws:
        projects = ws.path / "projects"
        one = projects / "p1"
        (one / "dataset_safe").mkdir(parents=True)
        testkit.write_json(one / "project.json", {"state": "failed"})

        first = pipeline.sweep_stale_scratch(str(projects))
        second = pipeline.sweep_stale_scratch(str(projects))

        assert first == {"compressed": 0, "staging": 1, "partial": 0}
        assert second == {"compressed": 0, "staging": 0, "partial": 0}
        assert pipeline.sweep_stale_scratch(str(ws.path / "does-not-exist")) == {
            "compressed": 0,
            "staging": 0,
            "partial": 0,
        }


if __name__ == "__main__":
    raise SystemExit(pytest.main(["-q", __file__]))
