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


def _fastest_over(deadline: float, attempt) -> float:
    """Best wall-clock time over a few attempts, or a loud failure.

    The overlap assertions below are wall-clock deadlines, which makes them the
    one kind of test in this file that can fail for a reason that has nothing to
    do with the multitasker. This one did: with Docker Desktop booting in the
    background it spent all eight cores, the run stretched from 9.2 s to 11.5 s,
    and `assert elapsed < 12.0` failed with "no overlap" -- naming a pipeline
    bug that was not there. Every other check in this file is deterministic; this
    is the one that needs the machine to be quiet.

    So the deadline is attempted a few times and the BEST run counts. That is not
    a weaker claim about overlap, it is the same claim made against a machine
    that was not competing with a container runtime: the fastest observed run is
    the closest thing to the unloaded behaviour, and the fastest run is exactly
    what got squeezed. If the overlap is real -- and it is, structurally: the
    downloader, GPU and uploader stages run on separate threads -- one attempt on
    a quiet machine already passes. If the overlap is gone, every attempt fails
    and the error still says so.

    The number of attempts is deliberately small. This is a test, not a
    benchmark, and a load-sensitive assertion is not worth turning into a
    thirty-second wait to reduce flakiness.
    """
    best = float("inf")
    last = 0.0
    for _ in range(3):
        last = attempt()
        best = min(best, last)
        if last < deadline:
            break
    return best

def test_pipeline_overlaps_and_completes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        workdir = Path(raw) / "work"
        workdir.mkdir(parents=True)
        jobs = _fake_jobs(4, Path(raw))

        def attempt() -> float:
            started = time.monotonic()
            summary = multitasker.run_multitasker(
                jobs=jobs, workdir=workdir, dry_run=True,
            )
            elapsed = time.monotonic() - started

            # The correctness assertions stay inside the attempt: they are
            # deterministic and must hold on EVERY run, not just the fast one.
            # A retry that fixed a broken summary would be hiding the bug.
            assert summary["prepared"] == 4, summary
            assert summary["dubbed"] == 4, summary
            assert summary["uploaded"] == 4, summary
            assert summary["failed"] == 0, summary
            assert (workdir / "output.mp4").is_file()
            return elapsed

        # 4 jobs x (2s gpu + 1s upload) serially = >= 12 s of fake work.
        # Overlapped, the wall clock must beat that comfortably (dub N+1 runs
        # while upload N streams).
        elapsed = _fastest_over(12.0, attempt)
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

        def attempt() -> float:
            started = time.monotonic()
            summary = mt.run_lip_sync_phase(
                jobs, provider, workdir=workdir, dry_run=True)
            elapsed = time.monotonic() - started
            assert summary["flagged"] == 3, summary
            assert summary["synced"] == 3, summary
            assert summary["uploaded"] == 3, summary
            assert summary["failed"] == 0, summary
            assert (workdir / "output.mp4").is_file()
            return elapsed

        # 3 jobs x (2s sync + 1s upload) = 9 s serial.
        # Overlapped, the wall clock must beat it. Same load tolerance as the
        # dub-phase assertion above, and for exactly the same reason: a
        # wall-clock deadline fails when the machine is busy, not when the
        # pipeline is serial.
        elapsed = _fastest_over(9.0, attempt)
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
