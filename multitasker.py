"""T_Dubber Kaggle multitasker -- producer/consumer pipelining for the GPU worker.

THE FUNDA (manager's brief)
---------------------------
Kaggle gives the worker 4 vCPU, ~30 GB RAM and a dedicated GPU. The GPU and
the network are separate hardware, but the old worker used them one at a
time::

    Download video -> Dub video -> Upload video -> Download next ...

so the GPU sat idle during every download and the network sat idle during
every dub. This module overlaps them::

    Network/CPU : [ prepare N+1 ] ------> [ upload N-1 ]
    GPU         :          [ dub N ] -> [ dub N+1 ]

Three threads, two bounded queues (backpressure is built in):

* :class:`DownloaderWorker`  -- CPU + ffmpeg + disk only. Stages each job
  (voice-reference extraction, job dir, ledger entry) and feeds ``gpu_queue``.
* :class:`GpuWorker`         -- the only thread that touches CUDA. Runs the
  Mazinger dub subprocess and feeds ``upload_queue``.
* :class:`UploaderWorker`    -- CPU + network only. Ships every finished dub
  to Telegram via ``tgup`` (the checkpoint copy) and stages
  ``/kaggle/working/output.mp4`` + ``report.json`` for Kaggle to capture.

Jobs
----
A job is one video. Jobs come from either:

* the single mounted source (the classic Kaggle flow: ``source_video.mp4``
  + ``dub_job.json`` under ``/kaggle/input``), or
* a batch manifest ``dub_batch.json`` listing many videos, which is how a
  whole season gets dubbed in one kernel run with the pipeline above.

Resume
------
Every state transition is appended to a JSONL ledger
(``multitasker_ledger.jsonl``). A kernel that is re-run reads the ledger and
skips jobs already marked ``done`` -- the same checkpoint discipline the
NEW_WORKFLOW.md asks for, at job granularity.

Safety
------
Every worker catches its own exceptions and records them; a failed job never
takes the kernel down. The GPU thread is deliberately the only place models
run, so CUDA context is never shared across threads.

Local testing
-------------
``python multitasker.py --dry-run`` fakes the ffmpeg/mazinger/tgup calls with
sleeps, so the overlap logic is provable on a laptop with no GPU. See
``multitasker_test.py``.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Master switch. The Kaggle notebook sets this before importing; locally the
# CLI sets it. Off by default so the legacy single-file cell stays the path
# of record until the pipelined one is proven.
MULTITASKER_ENABLED = os.environ.get("TDUBBER_MULTITASKER", "").strip().lower() in ("1", "true", "yes", "on")

WORKDIR = Path(os.environ.get("TDUBBER_WORKDIR", "/kaggle/working"))
INPUTDIR = Path(os.environ.get("TDUBBER_INPUTDIR", "/kaggle/input"))

# How many jobs may wait for the GPU / for upload. Bounded queues ARE the
# backpressure: a downloader that fills gpu_queue blocks, so a burst of jobs
# can never exhaust the 30 GB of RAM by staging everything at once.
GPU_QUEUE_DEPTH = int(os.environ.get("TDUBBER_GPU_QUEUE_DEPTH", "2"))
UPLOAD_QUEUE_DEPTH = int(os.environ.get("TDUBBER_UPLOAD_QUEUE_DEPTH", "2"))

# ffmpeg voice-reference extraction (same window the legacy cell used: the
# 6.9s-16.4s span of the source, mono 24 kHz PCM).
VOICE_REF_OFFSET = os.environ.get("TDUBBER_VOICE_OFFSET", "6.9")
VOICE_REF_DURATION = os.environ.get("TDUBBER_VOICE_DURATION", "9.5")

LIP_SYNC_DEFAULT = os.environ.get("TDUBBER_LIP_SYNC", "").strip().lower() in ("1", "true", "yes", "on")
BBOX_SHIFT = int(os.environ.get("TDUBBER_BBOX_SHIFT", "0") or 0)

LEDGER_NAME = "multitasker_ledger.jsonl"

# tgup (the Go uploader) ships inside the native pack; TGUP_BIN is pinned by
# the notebook's first cell. Upload is opt-in: no credentials, no upload,
# and the pipeline still runs (checkpointing is a bonus, not a requirement).
TGUP_CHANNEL = os.environ.get("TDUBBER_TG_CHANNEL", "").strip()
TGUP_CREDS = os.environ.get("TDUBBER_TGUP_CREDS", "").strip()
TGUP_CONCURRENCY = int(os.environ.get("TDUBBER_TGUP_CONCURRENCY", "3"))

# Media types the worker accepts as a source (mirrors the legacy scan).
VIDEO_SUFFIXES = (".mp4", ".mkv", ".avi", ".mov")


def log(line: str) -> None:
    """One timestamped line; flush=True because Kaggle captures stdout lazily."""
    print(f"[multitasker {time.strftime('%H:%M:%S')}] {line}", flush=True)


# ---------------------------------------------------------------------------
# Job model and ledger
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class DubJob:
    """One unit of work: dub one video into one target language."""

    job_id: str
    video_path: str
    target_language: str
    source_url: str = ""
    source_title: str = ""
    telegram_backup: str = ""
    voice_sample: str = ""      # optional pre-extracted reference voice
    voice_script: str = ""      # optional transcript for the reference voice
    lip_sync: bool = False      # post-dub face re-render (MuseTalk/Wav2Lip)
    bbox_shift: int = 0         # MuseTalk mouth-openness knob
    state: str = "queued"       # queued -> prepared -> dubbing -> uploaded -> done | failed
    detail: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class Ledger:
    """Append-only JSONL job ledger: the resume checkpoint for the run."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._states: dict[str, str] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a torn last line from a killed kernel is safe to skip
                    job_id = record.get("job_id")
                    state = record.get("state")
                    if job_id and state:
                        self._states[job_id] = state
        except OSError as exc:
            log(f"ledger unreadable ({exc}); starting a fresh one")

    def transition(self, job_id: str, state: str, detail: str = "") -> None:
        record = {
            "job_id": job_id,
            "state": state,
            "detail": detail,
            "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        with self._lock:
            self._states[job_id] = state
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError as exc:
                # Ledger loss must never stop a dub; the run degrades to
                # no-resume instead of failing.
                log(f"ledger write failed ({exc}); continuing without checkpoint")

    def state(self, job_id: str) -> str:
        return self._states.get(job_id, "unknown")


# ---------------------------------------------------------------------------
# Job discovery
# ---------------------------------------------------------------------------


def discover_jobs(input_dir: Path = INPUTDIR) -> list[DubJob]:
    """Build the job list from the mounted Kaggle input.

    Two shapes are supported:

    * ``dub_batch.json`` -- ``{"jobs": [{...}, ...]}`` -- an explicit batch.
    * the classic single job -- ``dub_job.json`` + the first mounted video.
    """
    batch_hits = sorted(input_dir.rglob("dub_batch.json"))
    if batch_hits:
        try:
            manifest = json.loads(batch_hits[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"dub_batch.json unreadable ({exc}); falling back to the single-job scan")
        else:
            jobs = []
            for index, entry in enumerate(manifest.get("jobs", [])):
                video = str(entry.get("video_path") or entry.get("video") or "")
                if not video:
                    continue
                jobs.append(DubJob(
                    job_id=str(entry.get("job_id") or f"job-{index:03d}"),
                    video_path=video,
                    target_language=str(entry.get("target_language") or "Hindi"),
                    source_url=str(entry.get("source_url") or ""),
                    source_title=str(entry.get("source_title") or ""),
                    telegram_backup=str(entry.get("telegram_backup") or ""),
                    voice_sample=str(entry.get("voice_sample") or ""),
                    voice_script=str(entry.get("voice_script") or ""),
                    lip_sync=bool(entry.get("lip_sync", LIP_SYNC_DEFAULT)),
                    bbox_shift=int(entry.get("bbox_shift", BBOX_SHIFT)),
                ))
            if jobs:
                log(f"batch manifest: {len(jobs)} job(s) from {batch_hits[0].name}")
                return jobs

    job_config: dict = {"target_language": "Hindi"}
    for config_path in sorted(input_dir.rglob("dub_job.json")):
        try:
            job_config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"ignoring unreadable job config {config_path}: {exc}")
            continue
        break

    video_path = ""
    for root, _dirs, files in os.walk(input_dir):
        for name in files:
            if name.lower().endswith(VIDEO_SUFFIXES):
                video_path = os.path.join(root, name)
                break
        if video_path:
            break

    if not video_path:
        raise FileNotFoundError("No video found in the Kaggle input directory.")

    return [DubJob(
        job_id="job-000",
        video_path=video_path,
        target_language=str(job_config.get("target_language") or "Hindi"),
        source_url=str(job_config.get("source_url") or ""),
        source_title=str(job_config.get("source_title") or ""),
        telegram_backup=str(job_config.get("telegram_backup") or ""),
        lip_sync=bool(job_config.get("lip_sync", LIP_SYNC_DEFAULT)),
        bbox_shift=int(job_config.get("bbox_shift", BBOX_SHIFT)),
    )]


# ---------------------------------------------------------------------------
# Worker 1: downloader / preparer (CPU + ffmpeg, never CUDA)
# ---------------------------------------------------------------------------


class DownloaderWorker(threading.Thread):
    """Stages jobs: voice reference, per-job directory, ledger entry."""

    def __init__(self, jobs: list[DubJob], gpu_queue: "queue.Queue",
                 ledger: Ledger, workdir: Path = WORKDIR,
                 dry_run: bool = False):
        super().__init__(name="downloader", daemon=True)
        self.jobs = jobs
        self.gpu_queue = gpu_queue
        self.ledger = ledger
        self.workdir = workdir
        self.dry_run = dry_run
        self.prepared = 0

    def _extract_voice_reference(self, job: DubJob, job_dir: Path) -> str:
        """ffmpeg the 20-60s-equivalent reference voice (CPU + disk only).

        This is exactly the work the legacy cell did inline before calling
        Mazinger -- here it happens while the GPU is still dubbing the
        previous job, which is the overlap the whole module exists for.
        """
        out_path = job_dir / "source_voice_reference.wav"
        if job.voice_sample:
            shutil.copy(job.voice_sample, out_path)
            return str(out_path)
        if self.dry_run:
            out_path.write_bytes(b"")
            return str(out_path)
        cmd = [
            "ffmpeg", "-y", "-ss", VOICE_REF_OFFSET, "-i", job.video_path,
            "-t", VOICE_REF_DURATION, "-vn", "-ac", "1", "-ar", "24000",
            "-c:a", "pcm_s16le", str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        if result.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
            raise RuntimeError(
                f"voice-reference extraction failed (ffmpeg exit {result.returncode}): "
                f"{(result.stderr or '')[-400:]}"
            )
        return str(out_path)

    def run(self) -> None:
        for job in self.jobs:
            try:
                if self.ledger.state(job.job_id) == "done":
                    log(f"[{job.job_id}] already done per ledger; skipping")
                    continue
                job_dir = self.workdir / "multitasker_jobs" / job.job_id
                job_dir.mkdir(parents=True, exist_ok=True)
                self.ledger.transition(job.job_id, "preparing", job.video_path)

                voice_ref = self._extract_voice_reference(job, job_dir)
                (job_dir / "job.json").write_text(
                    json.dumps(job.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                job.voice_sample = voice_ref
                job.state = "prepared"
                self.ledger.transition(job.job_id, "prepared", voice_ref)
                self.prepared += 1
                log(f"[{job.job_id}] staged ({Path(job.video_path).name} -> {job.target_language})")
                self.gpu_queue.put(job)
            except Exception as exc:  # a staging failure must not kill the queue
                job.state = "failed"
                job.detail = f"{type(exc).__name__}: {exc}"
                self.ledger.transition(job.job_id, "failed", job.detail)
                log(f"[{job.job_id}] PREPARE FAILED: {job.detail}")
        # Sentinel: one None per consumer tells the GPU worker to stop.
        self.gpu_queue.put(None)


# ---------------------------------------------------------------------------
# Worker 2: GPU duber (the only thread that touches CUDA)
# ---------------------------------------------------------------------------


class GpuWorker(threading.Thread):
    """Runs Mazinger dubs. One subprocess per job, never concurrent CUDA."""

    def __init__(self, gpu_queue: "queue.Queue", upload_queue: "queue.Queue",
                 ledger: Ledger, workdir: Path = WORKDIR,
                 base_url: str = "http://localhost:8000/v1",
                 model_name: str = "", dry_run: bool = False):
        super().__init__(name="gpu-duber", daemon=True)
        self.gpu_queue = gpu_queue
        self.upload_queue = upload_queue
        self.ledger = ledger
        self.workdir = workdir
        self.base_url = base_url
        self.model_name = model_name
        self.dry_run = dry_run
        self.dubbed = 0

    def _mazinger_cmd(self, job: DubJob, job_dir: Path) -> list[str]:
        """The exact invocation the legacy notebook cell used, per job."""
        base_dir = str(job_dir / "mazinger_output")
        os.makedirs(base_dir, exist_ok=True)
        return [
            sys.executable, "-m", "mazinger", "dub", job.video_path,
            "--tts-engine", "omnivoice", "--voice-sample", job.voice_sample,
            "--target-language", job.target_language,
            "--output-type", "video",
            "--device", "cuda",
            "--transcribe-method", "faster-whisper",
            "--openai-base-url", self.base_url,
            "--openai-api-key", "EMPTY",
            "--base-dir", base_dir,
            "--llm-model", self.model_name,
        ]

    def _find_dubbed_mp4(self, base_dir: Path) -> Path | None:
        candidates = sorted(
            (p for p in base_dir.rglob("dubbed.mp4") if p.is_file() and p.stat().st_size > 0),
            key=lambda p: p.stat().st_size,
        )
        return candidates[-1] if candidates else None

    def _process(self, job: DubJob) -> None:
        """Dub one job. Separated from run() so a failure in a
        single job is easy to simulate and easy to retry."""
        try:
            self.ledger.transition(job.job_id, "dubbing", job.video_path)
            job.state = "dubbing"
            job_dir = self.workdir / "multitasker_jobs" / job.job_id
            base_dir = job_dir / "mazinger_output"

            if self.dry_run:
                log(f"[{job.job_id}] DRY-RUN gpu work (simulated)")
                time.sleep(2)
                fake = base_dir / "lang" / job.target_language / "tts" / "dubbed.mp4"
                fake.parent.mkdir(parents=True, exist_ok=True)
                fake.write_bytes(b"dry-run")
                dubbed = fake
            else:
                cmd = self._mazinger_cmd(job, job_dir)
                log(f"[{job.job_id}] mazinger: {' '.join(cmd[:8])} ...")
                env = os.environ.copy()
                # Two-GPU boxes: vLLM (Homura) owns GPU 0, speech on GPU 1.
                if self.model_name.endswith("-2B") and _cuda_count() >= 2:
                    env["CUDA_VISIBLE_DEVICES"] = "1"
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", env=env,
                )
                (job_dir / "mazinger_stdout.log").write_text(
                    result.stdout or "", encoding="utf-8", errors="replace")
                (job_dir / "mazinger_stderr.log").write_text(
                    result.stderr or "", encoding="utf-8", errors="replace")
                if result.returncode != 0:
                    raise RuntimeError(
                        f"Mazinger exited {result.returncode}; "
                        f"stderr tail: {(result.stderr or '')[-600:]}"
                    )
                dubbed = self._find_dubbed_mp4(base_dir)
                if dubbed is None:
                    raise RuntimeError("Mazinger finished but produced no non-empty dubbed.mp4")

            job.state = "dubbed"
            job.detail = str(dubbed)
            self.ledger.transition(job.job_id, "dubbed", str(dubbed))
            self.dubbed += 1
            log(f"[{job.job_id}] DUBBED: {dubbed} ({dubbed.stat().st_size // 1024} KB)")
            self.upload_queue.put(job)
        except Exception as exc:
            job.state = "failed"
            job.detail = f"{type(exc).__name__}: {exc}"
            self.ledger.transition(job.job_id, "failed", job.detail)
            log(f"[{job.job_id}] DUB FAILED: {job.detail}")
            # Failed jobs do NOT reach the uploader, but the queue must
            # keep draining so later jobs still run.

    def run(self) -> None:
        while True:
            job = self.gpu_queue.get()
            if job is None:  # sentinel from the downloader
                self.upload_queue.put(None)
                return
            self._process(job)


def _cuda_count() -> int:
    try:
        import torch
        return int(torch.cuda.device_count())
    except Exception:
        return 0


def _ffprobe_json(path: str) -> dict:
    """ffprobe -show format+streams as a dict ({} on failure)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration:stream=codec_type",
             "-of", "json", path],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
        if result.returncode == 0:
            return json.loads(result.stdout or "{}")
    except Exception:
        pass
    return {}


def _srt_blocks(text: str) -> int:
    return sum(1 for line in text.splitlines() if "-->" in line)


def validate_dub(source_video: str, dubbed: Path, mazinger_output: Path,
                 target_language: str) -> dict:
    """The legacy notebook's quality gate, as a function.

    Three checks, exactly the ones the single-file cell ran:

    * **full timeline** -- the dub preserves the source duration
      (within 0.2% or 2 s), so quiet scenes and credits survive;
    * **streams** -- the output has both video and audio;
    * **subtitle coverage** -- translated segments cover >= 80% of
      the source segments, and a Hindi dub must actually contain
      Devanagari (catches the silent no-translation case).

    Returns ``{"passed": bool, "checks": [...]}`` and never raises:
    a probe failure becomes a failed check, not a dead worker.
    """
    checks = []
    source_info = _ffprobe_json(source_video)
    output_info = _ffprobe_json(str(dubbed))
    source_duration = float(source_info.get("format", {}).get("duration", 0))
    output_duration = float(output_info.get("format", {}).get("duration", 0))
    tolerance = max(2.0, source_duration * 0.002)
    duration_ok = (source_duration > 0 and output_duration > 0
                   and abs(source_duration - output_duration) <= tolerance)
    checks.append({"name": "full_timeline_duration", "passed": duration_ok,
                     "source_seconds": round(source_duration, 2),
                     "output_seconds": round(output_duration, 2),
                     "allowed_drift_seconds": round(tolerance, 2)})

    stream_types = {s.get("codec_type")
                      for s in output_info.get("streams", [])}
    streams_ok = "video" in stream_types and "audio" in stream_types
    checks.append({"name": "video_and_audio_streams", "passed": streams_ok,
                     "streams": sorted(t for t in stream_types if t)})

    translated_text = source_text = ""
    if mazinger_output.is_dir():
        translated = sorted(mazinger_output.rglob("translated.raw.srt")) or \
            sorted(mazinger_output.rglob("translated.srt"))
        sources = sorted(mazinger_output.rglob("source.raw.srt"))
        if translated:
            translated_text = translated[-1].read_text(
                encoding="utf-8", errors="replace")
        if sources:
            source_text = sources[-1].read_text(
                encoding="utf-8", errors="replace")
    source_blocks = _srt_blocks(source_text)
    translated_blocks = _srt_blocks(translated_text)
    coverage = translated_blocks / source_blocks if source_blocks else 0.0
    devanagari = sum(1 for ch in translated_text
                         if "\u0900" <= ch <= "\u097f")
    letters = sum(1 for ch in translated_text if ch.isalpha())
    script_ratio = devanagari / letters if letters else 0.0
    hindi_script_ok = (target_language.lower() != "hindi"
                       or script_ratio >= 0.15)
    subtitles_ok = (bool(translated_text) and source_blocks > 0
                    and coverage >= 0.80 and hindi_script_ok)
    checks.append({
        "name": "translated_subtitle_coverage", "passed": subtitles_ok,
        "source_segments": source_blocks,
        "translated_segments": translated_blocks,
        "coverage": round(coverage, 4),
        "devanagari_ratio": round(script_ratio, 4)
                            if target_language.lower() == "hindi" else None,
    })
    return {"passed": all(check["passed"] for check in checks),
              "checks": checks}


# ---------------------------------------------------------------------------
# Worker 3: uploader (CPU + network, never CUDA)
# ---------------------------------------------------------------------------


class UploaderWorker(threading.Thread):
    """Ships finished dubs to Telegram (checkpoint) and stages Kaggle output."""

    def __init__(self, upload_queue: "queue.Queue", ledger: Ledger,
                 workdir: Path = WORKDIR, dry_run: bool = False):
        super().__init__(name="uploader", daemon=True)
        self.upload_queue = upload_queue
        self.ledger = ledger
        self.workdir = workdir
        self.dry_run = dry_run
        self.uploaded = 0
        self.validations: dict[str, dict] = {}

    def _tgup_upload(self, job: DubJob, output_path: Path) -> str:
        """Upload via the packed tgup binary; returns the message link or ''."""
        tgup = os.environ.get("TGUP_BIN", "").strip() or shutil.which("tgup") or ""
        if not tgup or not TGUP_CHANNEL or not TGUP_CREDS:
            log(f"[{job.job_id}] Telegram checkpoint disabled (no tgup/credentials); skipping")
            return ""
        creds_path = Path(TGUP_CREDS)
        if not creds_path.is_file():
            log(f"[{job.job_id}] credentials file {creds_path} missing; skipping upload")
            return ""
        credentials = json.loads(creds_path.read_text(encoding="utf-8"))
        result_path = self.workdir / "multitasker_jobs" / job.job_id / "tgup_result.json"
        cmd = [
            tgup, "upload",
            "--file", str(output_path),
            "--channel", TGUP_CHANNEL,
            "--credentials-stdin",
            "--session", str(self.workdir / "multitasker_jobs" / "tgup.session"),
            "--concurrency", str(max(1, TGUP_CONCURRENCY)),
            "--result-out", str(result_path),
            "--caption", f"T_Dubber dub: {job.source_title or Path(output_path).name} [{job.target_language}]",
        ]
        creds_json = json.dumps({"api_id": credentials.get("api_id"),
                                 "api_hash": credentials.get("api_hash")}) + "\n"
        result = subprocess.run(cmd, input=creds_json, capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            log(f"[{job.job_id}] tgup exited {result.returncode}: {(result.stderr or '')[-300:]}")
            return ""
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        link = payload.get("message_link") or payload.get("link") or ""
        if link:
            log(f"[{job.job_id}] Telegram checkpoint: {link}")
        return str(link)

    def run(self) -> None:
        while True:
            job = self.upload_queue.get()
            if job is None:  # sentinel from the GPU worker
                return
            try:
                self.ledger.transition(job.job_id, "uploading", job.detail)
                job.state = "uploading"
                dubbed = Path(job.detail)
                if not (dubbed.is_file() and dubbed.stat().st_size > 0):
                    raise RuntimeError(f"dubbed output vanished: {dubbed}")

                checkpoint_link = ""
                if self.dry_run:
                    log(f"[{job.job_id}] DRY-RUN upload (simulated)")
                    time.sleep(1)
                    validation = {"passed": True, "checks": []}
                else:
                    checkpoint_link = self._tgup_upload(job, dubbed)
                    validation = validate_dub(
                        job.video_path, dubbed,
                        self.workdir / "multitasker_jobs" / job.job_id
                            / "mazinger_output",
                        job.target_language,
                    )
                    for check in validation["checks"]:
                        log(f"[{job.job_id}] CHECK {check['name']}: "
                            f"{'PASS' if check['passed'] else 'FAIL'}")

                # Stage the last finished dub where Kaggle captures outputs.
                staged = self.workdir / "output.mp4"
                shutil.copy(dubbed, staged)
                job.state = "done"
                self.ledger.transition(
                    job.job_id, "done",
                    json.dumps({"output": str(staged),
                                   "telegram": checkpoint_link,
                                   "validation": validation},
                                  ensure_ascii=False),
                )
                self.validations[job.job_id] = validation
                self.uploaded += 1
                log(f"[{job.job_id}] DONE -> {staged.name}")
            except Exception as exc:
                job.state = "failed"
                job.detail = f"{type(exc).__name__}: {exc}"
                self.ledger.transition(job.job_id, "failed", job.detail)
                log(f"[{job.job_id}] UPLOAD/STAGE FAILED: {job.detail}")


# ---------------------------------------------------------------------------
# Worker 4: lip-sync (post-dub, GPU freed from vLLM)
# ---------------------------------------------------------------------------


class LipSyncWorker(threading.Thread):
    """Re-renders the mouth region of the SOURCE video to
    match the DUBBED audio (MuseTalk / Wav2Lip).

    Runs in the post-dub phase: vLLM is stopped by then,
    so GPU 0 is free for the sync model. While the GPU
    syncs job N, the uploader ships job N-1's synced
    video to the Telegram cloud center -- the same
    overlap, one stage later.

    Face donor = the original (source) video; audio =
    the dubbed track extracted from Mazinger's output.
    """

    def __init__(self, jobs: list[DubJob], upload_queue: "queue.Queue",
                 ledger: Ledger, provider, workdir: Path = WORKDIR,
                 dry_run: bool = False):
        super().__init__(name="lip-sync", daemon=True)
        self.jobs = [job for job in jobs
                       if job.lip_sync
                       and job.state in ("done", "dubbed", "uploaded")]
        self.upload_queue = upload_queue
        self.ledger = ledger
        self.workdir = workdir
        self.provider = provider
        self.dry_run = dry_run
        self.synced = 0

    @staticmethod
    def _extract_audio(dubbed: Path, out_path: Path) -> str:
        """Pull the dubbed audio track out of Mazinger's mp4."""
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(dubbed), "-vn",
             "-c:a", "copy", str(out_path)],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
        )
        if result.returncode != 0 or not out_path.is_file():
            raise RuntimeError(
                f"audio extraction failed (ffmpeg {result.returncode}): "
                f"{(result.stderr or '')[-300:]}"
            )
        return str(out_path)

    def run(self) -> None:
        for job in self.jobs:
            try:
                job_dir = self.workdir / "multitasker_jobs" / job.job_id
                job_dir.mkdir(parents=True, exist_ok=True)
                dubbed = Path(job.detail)
                if not (dubbed.is_file() and dubbed.stat().st_size > 0):
                    raise RuntimeError(f"dubbed output missing: {dubbed}")

                self.ledger.transition(job.job_id, "syncing",
                                         str(dubbed))
                job.state = "syncing"
                synced = job_dir / "synced.mp4"

                if self.dry_run:
                    log(f"[{job.job_id}] DRY-RUN lip-sync (simulated)")
                    time.sleep(2)
                    synced.write_bytes(b"dry-run-synced")
                else:
                    audio = self._extract_audio(
                        dubbed, job_dir / "dubbed_audio.m4a")
                    result = self.provider.sync(
                        job.video_path, audio, str(synced),
                        job.bbox_shift,
                    )
                    if not result.ok:
                        raise RuntimeError(
                            f"{self.provider.name} failed: {result.detail}")
                    log(f"[{job.job_id}] LIP-SYNCED via "
                        f"{self.provider.name} in {result.seconds:.1f}s")

                # The synced video IS the deliverable from here on.
                job.state = "synced"
                job.detail = str(synced)
                self.ledger.transition(job.job_id, "synced", str(synced))
                self.synced += 1
                self.upload_queue.put(job)
            except Exception as exc:
                job.state = "failed"
                job.detail = f"{type(exc).__name__}: {exc}"
                self.ledger.transition(job.job_id, "failed", job.detail)
                log(f"[{job.job_id}] LIP-SYNC FAILED: {job.detail}")
        # Sentinel so the uploader drains and exits.
        self.upload_queue.put(None)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_multitasker(jobs: list[DubJob] | None = None, workdir: Path = WORKDIR,
                    input_dir: Path = INPUTDIR, base_url: str = "http://localhost:8000/v1",
                    model_name: str = "", dry_run: bool = False) -> dict:
    """Run the three-thread pipeline over ``jobs`` and return the run summary.

    This is the entry point the Kaggle notebook calls. It blocks until every
    job has drained through all three stages.
    """
    started = time.monotonic()
    if jobs is None:
        jobs = discover_jobs(input_dir)
    log(f"{len(jobs)} job(s) queued (dry_run={dry_run})")

    gpu_queue: "queue.Queue" = queue.Queue(maxsize=GPU_QUEUE_DEPTH)
    upload_queue: "queue.Queue" = queue.Queue(maxsize=UPLOAD_QUEUE_DEPTH)
    ledger = Ledger(workdir / LEDGER_NAME)

    downloader = DownloaderWorker(jobs, gpu_queue, ledger, workdir=workdir, dry_run=dry_run)
    gpu = GpuWorker(gpu_queue, upload_queue, ledger, workdir=workdir,
                    base_url=base_url, model_name=model_name, dry_run=dry_run)
    uploader = UploaderWorker(upload_queue, ledger, workdir=workdir, dry_run=dry_run)

    for worker in (uploader, gpu, downloader):  # uploader first: it drains last
        worker.start()
    downloader.join()
    gpu.join()
    uploader.join()

    summary = {
        "jobs": len(jobs),
        "prepared": downloader.prepared,
        "dubbed": gpu.dubbed,
        "uploaded": uploader.uploaded,
        "failed": sum(1 for job in jobs if job.state == "failed"),
        "validations": uploader.validations,
        "elapsed_sec": round(time.monotonic() - started, 1),
        "ledger": str(workdir / LEDGER_NAME),
    }
    log(f"RUN SUMMARY {json.dumps(summary)}")
    (workdir / "multitasker_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def run_lip_sync_phase(jobs: list[DubJob], provider,
                        workdir: Path = WORKDIR,
                        dry_run: bool = False) -> dict:
    """Post-dub phase: lip-sync every flagged job.

    CALLER CONTRACT: vLLM must be STOPPED before this is
    called (single-GPU boxes) -- the sync model needs the
    GPU the translation LLM was holding. On two-GPU
    Kaggle boxes the phase could run on GPU 1 while
    vLLM lives on GPU 0; the caller decides via
    CUDA_VISIBLE_DEVICES.

    The phase reuses the producer/consumer shape: the
    LipSyncWorker (GPU) and the UploaderWorker (network)
    run concurrently, so syncing job N overlaps with
    uploading job N-1 to the Telegram cloud center.
    """
    started = time.monotonic()
    eligible = [job for job in jobs if job.lip_sync]
    log(f"lip-sync phase: {len(eligible)}/{len(jobs)} job(s) "
        f"flagged (provider={provider.name}, dry_run={dry_run})")

    upload_queue: "queue.Queue" = queue.Queue(maxsize=UPLOAD_QUEUE_DEPTH)
    ledger = Ledger(workdir / LEDGER_NAME)

    sync_worker = LipSyncWorker(jobs, upload_queue, ledger,
                                  provider, workdir=workdir,
                                  dry_run=dry_run)
    uploader = UploaderWorker(upload_queue, ledger,
                                workdir=workdir, dry_run=dry_run)

    uploader.start()
    sync_worker.start()
    sync_worker.join()
    uploader.join()

    summary = {
        "phase": "lip-sync",
        "provider": provider.name,
        "flagged": len(eligible),
        "synced": sync_worker.synced,
        "uploaded": uploader.uploaded,
        "failed": sum(1 for job in eligible if job.state == "failed"),
        "elapsed_sec": round(time.monotonic() - started, 1),
    }
    log(f"LIP-SYNC SUMMARY {json.dumps(summary)}")
    (workdir / "lip_sync_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    return summary


# ---------------------------------------------------------------------------
# CLI (local testing / dry runs)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    if not MULTITASKER_ENABLED and not dry_run:
        print("Set TDUBBER_MULTITASKER=1 (or pass --dry-run) to run the multitasker.")
        return 2
    summary = run_multitasker(dry_run=dry_run)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
