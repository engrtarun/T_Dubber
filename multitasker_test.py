"""Local proof that the multitasker actually overlaps work.

No GPU, no ffmpeg, no tgup: the dry-run mode of multitasker.py
fakes every external call with sleeps, and this test asserts
the PIPELINE property itself:

    serial time   = prepare + dub + upload, per job, summed
    pipeline time < serial time   (when there is >1 job)

plus:
* every job reaches ``done`` (none lost between queues)
* a failing job does not stop the rest of the batch
* the ledger records the transitions and a second run
  skips the jobs already done (resume)

Run:  python multitasker_test.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import multitasker


def _fake_jobs(count: int, tmp: Path) -> list[multitasker.DubJob]:
    jobs = []
    for index in range(count):
        video = tmp / f"video_{index}.mp4"
        video.write_bytes(b"x" * 1024)
        jobs.append(multitasker.DubJob(
            job_id=f"job-{index:03d}",
            video_path=str(video),
            target_language="Hindi",
            source_title=f"Test {index}",
        ))
    return jobs


def test_pipeline_overlaps_and_completes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        workdir = Path(raw) / "work"
        workdir.mkdir()
        jobs = _fake_jobs(4, Path(raw))

        started = time.monotonic()
        summary = multitasker.run_multitasker(
            jobs=jobs, workdir=workdir, dry_run=True,
        )
        elapsed = time.monotonic() - started

        assert summary["prepared"] == 4, summary
        assert summary["dubbed"] == 4, summary
        assert summary["uploaded"] == 4, summary
        assert summary["failed"] == 0, summary
        assert (workdir / "output.mp4").is_file()

        # 4 jobs x (2s gpu + 1s upload) serially = >= 12 s of
        # fake work. Overlapped, the wall clock must beat that
        # comfortably (dub N+1 runs while upload N streams).
        assert elapsed < 12.0, f"no overlap: {elapsed:.1f}s"
        print(f"    overlap ok: {elapsed:.1f}s wall for "
              f">=12s of serial fake work")

        ledger = workdir / multitasker.LEDGER_NAME
        states = [json.loads(line)["state"]
                    for line in ledger.read_text().splitlines()]
        assert "done" in states and "dubbing" in states


def test_resume_skips_done_jobs() -> None:
    with tempfile.TemporaryDirectory() as raw:
        workdir = Path(raw) / "work"
        workdir.mkdir()
        jobs = _fake_jobs(3, Path(raw))

        first = multitasker.run_multitasker(
            jobs=jobs, workdir=workdir, dry_run=True)
        assert first["uploaded"] == 3

        # Second run over the same jobs: the ledger says done,
        # so nothing is re-dubbed and the GPU worker sees no work.
        second = multitasker.run_multitasker(
            jobs=jobs, workdir=workdir, dry_run=True)
        assert second["dubbed"] == 0, second
        assert second["uploaded"] == 0, second
        print("    resume ok: 3/3 jobs skipped from the ledger")


def test_failed_job_does_not_stop_batch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        workdir = Path(raw) / "work"
        workdir.mkdir()
        jobs = _fake_jobs(2, Path(raw))

        original = multitasker.GpuWorker._process

        def flaky_process(self, job):
            # Fail the first job only; the rest dub normally.
            if not getattr(self, "_boomed", False):
                self._boomed = True
                job.state = "failed"
                job.detail = "simulated failure"
                self.ledger.transition(job.job_id, "failed", job.detail)
                return
            return original(self, job)

        multitasker.GpuWorker._process = flaky_process
        try:
            summary = multitasker.run_multitasker(
                jobs=jobs, workdir=workdir, dry_run=True)
        finally:
            multitasker.GpuWorker._process = original

        assert summary["failed"] == 1, summary
        assert summary["dubbed"] == 1, summary
        print("    isolation ok: 1 failed, 1 still dubbed")


def test_lip_sync_phase_overlaps_sync_with_upload() -> None:
    import multitasker as mt
    from lip_sync.lip_sync import FakeProvider

    with tempfile.TemporaryDirectory() as raw:
        workdir = Path(raw) / "work"
        workdir.mkdir()
        jobs = _fake_jobs(3, Path(raw))
        # Pretend the dub phase already finished.
        for job in jobs:
            job.lip_sync = True
            job.state = "done"
            job.detail = str(Path(raw) / "video_0.mp4")

        provider = FakeProvider(workdir / "lip_sync")
        started = time.monotonic()
        summary = mt.run_lip_sync_phase(
            jobs, provider, workdir=workdir, dry_run=True)
        elapsed = time.monotonic() - started

        assert summary["flagged"] == 3, summary
        assert summary["synced"] == 3, summary
        assert summary["uploaded"] == 3, summary
        assert summary["failed"] == 0, summary
        assert (workdir / "output.mp4").is_file()

        # 3 jobs x (2s sync + 1s upload) = 9 s serial.
        # Overlapped, the wall clock must beat it.
        assert elapsed < 9.0, f"no overlap: {elapsed:.1f}s"
        print(f"    lip-sync overlap ok: {elapsed:.1f}s wall "
              f"for >=9s of serial fake work")

        ledger = workdir / mt.LEDGER_NAME
        states = [json.loads(line)["state"]
                    for line in ledger.read_text().splitlines()]
        assert "syncing" in states and "synced" in states


def test_lip_sync_phase_skips_unflagged_jobs() -> None:
    import multitasker as mt
    from lip_sync.lip_sync import FakeProvider

    with tempfile.TemporaryDirectory() as raw:
        workdir = Path(raw) / "work"
        workdir.mkdir()
        jobs = _fake_jobs(2, Path(raw))
        jobs[0].lip_sync = False
        jobs[1].lip_sync = True
        for job in jobs:
            job.state = "done"
            job.detail = str(Path(raw) / "video_0.mp4")

        summary = mt.run_lip_sync_phase(
            jobs, FakeProvider(workdir / "lip_sync"),
            workdir=workdir, dry_run=True)
        assert summary["flagged"] == 1, summary
        assert summary["synced"] == 1, summary
        print("    lip-sync flag ok: unflagged job skipped")


def main() -> int:
    print("multitasker tests (dry-run, no GPU/ffmpeg/tgup)")
    os.environ["TDUBBER_MULTITASKER"] = "1"
    # The lip-sync phase imports the provider package.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    test_pipeline_overlaps_and_completes()
    test_resume_skips_done_jobs()
    test_failed_job_does_not_stop_batch()
    test_lip_sync_phase_overlaps_sync_with_upload()
    test_lip_sync_phase_skips_unflagged_jobs()
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
