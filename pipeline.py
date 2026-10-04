import os
import time
import json
import datetime
import subprocess
import shutil
import re
import uuid
import hashlib
import glob
import zipfile
import sys
from pathlib import Path

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Force Kaggle CLI to use the local folder containing kaggle.json
local_kaggle_dir = os.path.join(APP_DIR, "kaggle_paperWork")
os.environ["KAGGLE_CONFIG_DIR"] = local_kaggle_dir

# ---------------------------------------------------------------------------
# Stage telemetry -- SQLITE_ROLLOUT.md, Phase 2
# ---------------------------------------------------------------------------
# Until now the only writer of `pipeline_stages` was test_db.py, so the table
# stayed empty and the dashboard had nothing real to show. This block is the
# missing writer.
#
# Three rules govern everything below, in priority order:
#
#   1. Telemetry must never break a run. A dub can be hours of GPU time and a
#      hundred retries. Losing one because SQLite was momentarily locked, or
#      because the `projects` row was missing, would be absurd. Every database
#      call is wrapped, and any failure degrades to one line on stderr.
#   2. Telemetry must never lie. Only the stages this module actually performs
#      are written (1-7). Stage 0 (Resolve) happens in app.py before
#      run_pipeline is ever called, and stage 8 (Transport) belongs to the
#      Telegram uploader. Recording "skipped" for either would put a false
#      statement in the database, so those two are deliberately left alone.
#   3. Every transition is written twice -- "running" first, then the terminal
#      state. db.record_stage only sets started_at on the "running" insert; its
#      ON CONFLICT clause does not update that column, so a stage that skipped
#      straight to "success" would keep a NULL started_at forever.
#
# The import is guarded because app.py does `from pipeline import run_pipeline`
# at module scope: if db.py were unimportable, an unguarded import here would
# take down the whole Gradio app, not just the telemetry.
try:
    import db
    _DB_IMPORT_ERROR = None
except Exception as _exc:  # pragma: no cover - telemetry is not load-bearing
    db = None
    _DB_IMPORT_ERROR = _exc

# Canonical stage rail, matching the [STAGE:n] markers yielded below.
STAGE_NAMES = {
    0: "Resolve",
    1: "Compress",
    2: "Bundle",
    3: "Dataset",
    4: "Kernel",
    5: "GPU Worker",
    6: "Download",
    7: "Verify",
    8: "Transport",
}

# The stages run_pipeline()/reattach_to_kernel() genuinely perform.
OWNED_STAGES = (1, 2, 3, 4, 5, 6, 7)

_MAX_MESSAGE = 500
_MAX_ERROR = 2000
_TEL_WARN_LIMIT = 5

_tel_started = {}   # (project_id, stage) -> time.monotonic() at "running"
_tel_ready = set()  # project ids confirmed to exist in `projects`
_tel_warned = 0


def _tel_warn(reason):
    """Report a telemetry problem on stderr, at most a handful of times.

    stderr rather than the yielded log stream on purpose: these lines carry no
    [STAGE:n] marker, and the generator's contract is to report pipeline
    progress, not the health of the progress reporter.
    """
    global _tel_warned
    if _tel_warned >= _TEL_WARN_LIMIT:
        return
    _tel_warned += 1
    suffix = " (further telemetry warnings suppressed)" if _tel_warned == _TEL_WARN_LIMIT else ""
    sys.stderr.write(f"[pipeline] stage telemetry: {reason}{suffix}\n")


def _tel_truncate(text, limit):
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _tel_ensure_project(project_id, manifest):
    """Guarantee a `projects` row exists so the stage foreign key resolves.

    app.py writes that row before calling run_pipeline, so this is a safety net
    for the paths that do not: a first write whose database half failed, or a
    reattach to a project created before this process booted.

    db.upsert_project is destructive on conflict -- it clears title,
    output_video, report_file and completed_at -- so it is only ever called
    after confirming the row really is absent. That check is what keeps this
    from being a data-loss bug.
    """
    if not manifest or project_id in _tel_ready:
        return
    try:
        if db.get_project(project_id) is not None:
            _tel_ready.add(project_id)
            return
        payload = dict(manifest)
        payload["project_id"] = project_id
        payload.setdefault("status", "processing")
        db.upsert_project(payload)
        _tel_ready.add(project_id)
    except Exception as exc:
        _tel_warn(f"could not create the projects row for {project_id}: {exc}")


def track_stage(project_id, stage_number, status, message=None, error=None,
                manifest=None, duration_sec=None):
    """Record one stage transition. Returns True on success, never raises.

    ``status`` is "running", "success" or "failed". ``duration_sec`` is measured
    from the matching "running" call when it is not supplied, which is what
    feeds db.timing_estimates() and therefore the dashboard's ETA.
    """
    if db is None:
        _tel_warn(f"db unavailable ({_DB_IMPORT_ERROR}); stage {stage_number} not recorded")
        return False

    stage_number = int(stage_number)
    name = STAGE_NAMES.get(stage_number, f"Stage {stage_number}")
    key = (project_id, stage_number)
    now = time.monotonic()

    if status == "running":
        _tel_started[key] = now
    elif duration_sec is None and key in _tel_started:
        duration_sec = round(now - _tel_started.pop(key), 3)

    try:
        _tel_ensure_project(project_id, manifest)
        db.record_stage(
            project_id,
            stage_number,
            name,
            status,
            message=_tel_truncate(message, _MAX_MESSAGE),
            error=_tel_truncate(error, _MAX_ERROR),
            duration_sec=duration_sec,
        )
        return True
    except Exception as exc:
        _tel_warn(f"{project_id} stage {stage_number} {status} not recorded: {exc}")
        # Forget the cached state so a later stage retries from scratch rather
        # than inheriting a half-finished attempt.
        _tel_started.pop(key, None)
        _tel_ready.discard(project_id)
        return False


def project_id_for(project_dir):
    """The projects.id for a project directory (its basename)."""
    return os.path.basename(os.path.abspath(project_dir))

def run_cmd(cmd):
    """Run a subprocess with UTF-8-safe output capture on Windows and Linux."""
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    if isinstance(cmd, list):
        print(f"Executing command list: {cmd}")
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=child_env
        )
    print(f"Executing command string: {cmd}")
    return subprocess.run(
        cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace", env=child_env
    )

def kernel_id_for(username):
    """The single reusable private worker notebook for this Kaggle account."""
    return f"{username}/dubber-worker-homura"


def _collect_kernel_logs(project_dir):
    """Read whatever the Kaggle output download left behind, newest wins."""
    for path in [os.path.join(project_dir, "error_log.txt")] + sorted(
        glob.glob(os.path.join(project_dir, "*.log"))
    ):
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    return handle.read(), path
            except OSError:
                continue
    return "", None


def _wait_for_kernel(kernel_id, project_dir, timeout, stage="STAGE:5", poll_seconds=30,
                     stage_number=5, manifest=None):
    """Poll a Kaggle kernel until it finishes, yielding progress lines.

    Returns ``{"success": bool, "network_error": bool, "status": str}``. This is
    deliberately a separate generator so :func:`reattach_to_kernel` can reuse
    the exact same logic -- when a browser tab closes or the PC reboots, the
    work is still running on Kaggle and we need to pick the thread back up.

    ``stage_number`` and ``manifest`` exist purely for the telemetry writer and
    both have defaults, so existing call sites keep working unchanged.
    """
    status_cmd = ["python", "-m", "kaggle", "kernels", "status", kernel_id]
    output_cmd = ["python", "-m", "kaggle", "kernels", "output", kernel_id, "-p", project_dir]

    project_id = project_id_for(project_dir)
    track_stage(project_id, stage_number, "running",
                message=f"Waiting on Kaggle worker {kernel_id} (timeout {int(timeout / 3600)}h)",
                manifest=manifest)

    start_time = time.time()
    last_report = 0

    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout:
            yield f"[{stage}] ❌ Timeout: Worker did not finish within {int(timeout / 3600)} hours.\n"
            track_stage(project_id, stage_number, "failed",
                        message=f"Timed out after {int(elapsed)}s",
                        error=f"Kaggle worker did not finish within {int(timeout / 3600)} hours")
            return {"success": False, "network_error": False, "status": "timeout"}

        result = run_cmd(status_cmd)
        status_text = (result.stdout or "") + (result.stderr or "")
        lowered = status_text.lower()

        if "complete" in lowered:
            yield f"[{stage}] ✅ Kaggle execution completed!\n"
            track_stage(project_id, stage_number, "success",
                        message=f"Kaggle execution completed in {int(elapsed)}s",
                        manifest=manifest)
            return {"success": True, "network_error": False, "status": "complete"}

        if "error" in lowered or "cancel" in lowered:
            yield f"[{stage}] ❌ Kaggle execution failed or was cancelled.\n"
            yield f"[{stage}] ⬇️ Downloading Kaggle logs for debugging...\n"
            for attempt in range(3):
                if attempt:
                    yield f"[{stage}] ⏳ Kaggle outputs are not ready; retrying log download ({attempt + 1}/3).\n"
                    time.sleep(8)
                run_cmd(output_cmd)
                if _collect_kernel_logs(project_dir)[0]:
                    break

            content, source = _collect_kernel_logs(project_dir)
            if content:
                yield f"[{stage}] ❌ Error Details (from {os.path.basename(source)}):\n{content[-2000:]}\n"
                network_error = (
                    "could not resolve host" in content.lower()
                    or "network is unreachable" in content.lower()
                )
                track_stage(project_id, stage_number, "failed",
                            message="Kaggle execution failed or was cancelled",
                            error=content[-_MAX_ERROR:], manifest=manifest)
                return {"success": False, "network_error": network_error, "status": "error"}
            yield f"[{stage}] ❌ No error log or .log file found in {project_dir}\n"
            if (result.stderr or result.stdout):
                yield f"[{stage}] Kaggle CLI said:\n{(result.stderr or result.stdout).strip()[-1200:]}\n"
            cli_said = (result.stderr or result.stdout or "").strip()
            track_stage(project_id, stage_number, "failed",
                        message="Kaggle execution failed; no worker log was downloaded",
                        error=cli_said or "Kaggle reported an error but produced no log",
                        manifest=manifest)
            return {"success": False, "network_error": False, "status": "error"}

        # Report at most once a minute so the log stays readable on long jobs.
        if elapsed - last_report >= 60:
            last_report = elapsed
            yield (
                f"[{stage}] 🔄 Worker still processing on Kaggle... "
                f"(elapsed {int(elapsed // 60)}m {int(elapsed % 60)}s)\n"
            )
        time.sleep(poll_seconds)


def reattach_to_kernel(project_dir, kernel_id, timeout=10 * 60 * 60, poll_seconds=30):
    """Resume monitoring a Kaggle worker that was already submitted.

    This is the PC-died case. The kernel keeps running on Kaggle regardless of
    the user's machine, so all that is needed is to point this at the same
    kernel id and wait again. Yields the same ``[STAGE:X]`` log lines as a live
    run, then hands off to the output download.
    """
    project_id = project_id_for(project_dir)
    manifest = {"title": project_id, "status": "processing", "kernel_id": kernel_id}
    tel_manifest = manifest

    yield "[STAGE:5] ♻️ Reattaching to the Kaggle worker...\n"
    yield f"[STAGE:5] ☁️ Your PC going offline does not stop Kaggle; the job is still running there.\n"
    yield f"[STAGE:5] 🔗 Worker URL: https://www.kaggle.com/code/{kernel_id}\n"

    outcome = yield from _wait_for_kernel(
        kernel_id, project_dir, timeout=timeout, stage="STAGE:5", poll_seconds=poll_seconds,
        stage_number=5, manifest=tel_manifest,
    )
    if not outcome["success"]:
        return outcome

    yield "[STAGE:6] ⬇️ Downloading final outputs from Kaggle...\n"
    track_stage(project_id, 6, "running", message="Downloading Kaggle outputs",
                manifest=tel_manifest)
    output_cmd = ["python", "-m", "kaggle", "kernels", "output", kernel_id, "-p", project_dir]
    yield f"[STAGE:6] Executing: {' '.join(output_cmd)}\n"
    run_cmd(output_cmd)

    mp4_files = [
        f
        for f in glob.glob(os.path.join(project_dir, "*.mp4"))
        if os.path.getsize(f) > 0
    ]
    if mp4_files:
        track_stage(project_id, 6, "success",
                    message=f"Downloaded {len(mp4_files)} file(s) from Kaggle",
                    manifest=tel_manifest)
        stale = os.path.join(project_dir, "error_log.txt")
        if os.path.isfile(stale):
            os.remove(stale)
        biggest = max(mp4_files, key=lambda f: os.path.getsize(f))
        yield f"[STAGE:7] 🎉 Pipeline finished successfully! Output: {os.path.basename(biggest)}"
        track_stage(project_id, 7, "success",
                    message=f"Verified output {os.path.basename(biggest)}",
                    manifest=tel_manifest)
        outcome["output_video"] = biggest
    else:
        track_stage(project_id, 6, "success",
                    message="Downloaded Kaggle outputs (no video present)",
                    manifest=tel_manifest)
        content, _source = _collect_kernel_logs(project_dir)
        if content:
            yield f"[STAGE:7] ❌ Worker Error Log:\n{content[-2500:]}\n"
        else:
            yield "[STAGE:7] ❌ Worker reported success but produced no video.\n"
        track_stage(project_id, 7, "failed",
                    message="Worker reported success but produced no video",
                    error=content[-_MAX_ERROR:] if content else "No output video and no worker log",
                    manifest=tel_manifest)
    return outcome


def run_pipeline(video_path, project_dir, target_lang, speaker_detection,
                 backup_link=None, source_url=None, source_title=None, source_size=None):
    """
    Orchestrator function (Generator) using Kaggle API to push the job to a cloud GPU.
    Yields status logs incrementally with [STAGE:X] markers for the UI stepper.

    ``backup_link`` is the Telegram archive link created before this run
    started. It travels with the job so the worker records it in its report and
    a finished dub can always be traced back to its archive.

    Every stage transition is mirrored into the ``pipeline_stages`` table via
    :func:`track_stage`, so the dashboard can show real progress instead of
    guessing. Those writes are strictly best-effort: a database problem logs to
    stderr and never interrupts the run.
    """
    project_id = project_id_for(project_dir)
    manifest = {
        "title": source_title or os.path.splitext(os.path.basename(video_path or project_id))[0] or project_id,
        "source_video": os.path.basename(video_path) if video_path else None,
        "source_url": source_url,
        "source_size": source_size,
        "target_language": target_lang,
        "speaker_detection": bool(speaker_detection),
        "telegram_backup": backup_link,
        "status": "processing",
    }

    yield "[STAGE:1] 🚀 Starting Kaggle pipeline...\n"
    track_stage(project_id, 1, "running", message="Starting Kaggle pipeline", manifest=manifest)

    if backup_link:
        yield f"[STAGE:1] ☁️ Telegram backup on file: {backup_link}\n"
    if source_url:
        yield f"[STAGE:1] 🔗 Source resolved from: {source_url}\n"


    kaggle_creds_path = os.path.join(local_kaggle_dir, "kaggle.json")
    if not os.path.exists(kaggle_creds_path):
        yield f"[STAGE:1] ❌ Error: Missing Kaggle credentials!\nCould not find kaggle.json at {kaggle_creds_path}.\n"
        track_stage(project_id, 1, "failed", message="Missing Kaggle credentials",
                    error=f"kaggle.json not found at {kaggle_creds_path}", manifest=manifest)
        return

    yield "[STAGE:1] 🔍 Checking Kaggle CLI installation...\n"
    if run_cmd(["python", "-m", "kaggle", "--version"]).returncode != 0:
        yield "[STAGE:1] ❌ Error: Kaggle API CLI not installed or authenticated.\n"
        track_stage(project_id, 1, "failed", message="Kaggle API CLI missing or unauthenticated",
                    error="`kaggle --version` exited non-zero", manifest=manifest)
        return

    try:
        with open(kaggle_creds_path) as kf:
            kcreds = json.load(kf)
            KAGGLE_USERNAME = kcreds.get("username", "YOUR_KAGGLE_USERNAME")
    except Exception as e:
        yield f"[STAGE:1] ❌ Error reading kaggle.json: {e}\n"
        track_stage(project_id, 1, "failed", message="Could not read kaggle.json",
                    error=f"{type(e).__name__}: {e}", manifest=manifest)
        return
    
    unique_id = uuid.uuid4().hex[:6]
    # Reuse one private dataset so normal jobs create versions instead of
    # repeatedly calling Kaggle's less reliable CreateDataset endpoint.
    install_key = hashlib.sha256(os.path.abspath(APP_DIR).encode("utf-8")).hexdigest()[:8]
    dataset_slug = f"dubber-video-{install_key}"
    dataset_title = f"Dubber Input {install_key}"
    dataset_id = f"{KAGGLE_USERNAME}/{dataset_slug}"
    
    dataset_dir = os.path.join(project_dir, "dataset_safe")
    os.makedirs(dataset_dir, exist_ok=True)
    
    video_filename = "source_video" + (Path(video_path).suffix.lower() or ".mp4")
    # A stable filename lets dataset versions replace the previous input cleanly.
    for old_video in Path(dataset_dir).glob("source_video.*"):
        old_video.unlink()
    
    # Task 3: Auto-Compression
    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    needs_compression = file_size_mb > 30
    if not needs_compression:
        probe_cmd = f'ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=s=x:p=0 "{video_path}"'
        try:
            res_probe = subprocess.run(probe_cmd, shell=True, capture_output=True, text=True)
            if res_probe.returncode == 0 and res_probe.stdout.strip():
                height = int(res_probe.stdout.strip())
                if height > 1080:
                    needs_compression = True
        except:
            pass

    compressed_video_path = None
    if needs_compression:
        yield f"[STAGE:1] 🛠️ Video is large or >1080p ({file_size_mb:.1f}MB). Compressing to 480p...\n"
        track_stage(project_id, 1, "running",
                    message=f"Compressing {file_size_mb:.1f}MB source to 480p", manifest=manifest)
        compressed_video_path = os.path.join(project_dir, f"compressed_{unique_id}.mp4")
        ffmpeg_cmd = f'ffmpeg -y -i "{video_path}" -vf scale=854:480 -b:v 1M "{compressed_video_path}"'
        yield f"[STAGE:1] Executing: {ffmpeg_cmd}\n"
        run_cmd(ffmpeg_cmd)
        
        if os.path.exists(compressed_video_path) and os.path.getsize(compressed_video_path) > 0:
            target_upload_path = compressed_video_path
            video_filename = "source_video.mp4"
            # Keep the stable dataset filename across versions.
            yield "[STAGE:1] ✅ Compression successful.\n"
        else:
            yield "[STAGE:1] ⚠️ Compression failed. Proceeding with original video.\n"
            target_upload_path = video_path
    else:
        target_upload_path = video_path
        
    track_stage(project_id, 1, "success",
                message=("Compressed to 480p and staged" if needs_compression and compressed_video_path
                         else "Source staged without compression"),
                manifest=manifest)
    yield "[STAGE:1] ✅ Compression successful.\n" if compressed_video_path else ""

    # Stage 1 covers everything from "start" through "input staged for upload".
    track_stage(project_id, 1, "success",
                message=("Compressed to 480p and staged for upload"
                         if needs_compression and compressed_video_path
                         else "Source staged for upload without compression"),
                manifest=manifest)

    shutil.copy(target_upload_path, os.path.join(dataset_dir, video_filename))

    # Pass the UI's selected target through the versioned input dataset. The
    # worker must not silently dub every project into a hard-coded language.
    job_config_path = os.path.join(dataset_dir, "dub_job.json")
    job_config = {
        "target_language": target_lang,
        "telegram_backup": backup_link,
        "source_url": source_url,
        "source_title": source_title,
        "source_size_bytes": source_size,
        "requested_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    with open(job_config_path, "w", encoding="utf-8") as config_file:
        json.dump(job_config, config_file, ensure_ascii=False, indent=2)
    
    # Task 3: Ensure no .git directory in dataset folder
    git_dir = os.path.join(dataset_dir, ".git")
    if os.path.exists(git_dir):
        shutil.rmtree(git_dir)
    
    yield "[STAGE:2] 📦 Creating Dataset structure...\n"
    track_stage(project_id, 2, "running", message="Bundling Mazinger source and dataset metadata",
                manifest=manifest)

    # Add the local mazinger source code to the dataset for offline installation
    mazinger_src = os.path.join(APP_DIR, "mazinger")
    if os.path.exists(mazinger_src):
        shutil.copytree(
            mazinger_src, 
            os.path.join(dataset_dir, "mazinger"), 
            ignore=shutil.ignore_patterns(".git"),
            dirs_exist_ok=True
        )
    bundled_mazinger = os.path.join(dataset_dir, "mazinger", "pyproject.toml")
    if not os.path.isfile(bundled_mazinger):
        yield "[STAGE:2] ❌ Local Mazinger source was not copied into the Kaggle dataset; refusing to upload a broken worker input.\n"
        track_stage(project_id, 2, "failed", message="Mazinger source was not bundled",
                    error=f"{bundled_mazinger} is missing after copytree", manifest=manifest)
        return
    # Kaggle CLI's datasets create defaults to --dir-mode skip, so nested
    # directories are not uploaded. Keep an explicit ZIP at the dataset root.
    mazinger_archive = os.path.join(dataset_dir, "mazinger_source.zip")
    if os.path.exists(mazinger_archive):
        os.remove(mazinger_archive)
    with zipfile.ZipFile(mazinger_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source_file in Path(mazinger_src).rglob("*"):
            relative_path = source_file.relative_to(mazinger_src)
            if not source_file.is_file() or any(
                part in {".git", "__pycache__"} for part in relative_path.parts
            ):
                continue
            archive.write(source_file, relative_path.as_posix())
    yield (
        f"[STAGE:2] ✅ Bundled Mazinger source as root-level archive "
        f"({os.path.getsize(mazinger_archive):,} bytes).\n"
    )
        
    meta_path = os.path.join(dataset_dir, "dataset-metadata.json")
    if not os.path.isfile(meta_path):
        init_cmd = ["python", "-m", "kaggle", "datasets", "init", "-p", dataset_dir]
        yield f"[STAGE:2] Executing: {' '.join(init_cmd)}\n"
        init_result = run_cmd(init_cmd)
        if init_result.returncode != 0:
            yield f"[STAGE:2] ❌ Could not initialize dataset metadata:\n{init_result.stderr or init_result.stdout}\n"
            track_stage(project_id, 2, "failed", message="kaggle datasets init failed",
                        error=(init_result.stderr or init_result.stdout or "").strip(),
                        manifest=manifest)
            return
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    meta["id"] = dataset_id
    meta["title"] = dataset_title
    meta["licenses"] = [{"name": "CC0-1.0"}]
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    track_stage(project_id, 2, "success",
                message=f"Bundled Mazinger source and dataset metadata for {dataset_id}",
                manifest=manifest)

    yield f"[STAGE:3] ☁️ Uploading video to reusable Kaggle dataset ({dataset_id})...\n"
    track_stage(project_id, 3, "running", message=f"Uploading input dataset {dataset_id}",
                manifest=manifest)
    status_cmd = ["python", "-m", "kaggle", "datasets", "status", dataset_id]
    status_result = run_cmd(status_cmd)
    dataset_exists = status_result.returncode == 0
    if dataset_exists:
        yield "[STAGE:3] 📚 Existing dataset found; uploading a new version.\n"
        upload_cmd = ["python", "-m", "kaggle", "datasets", "version", "-p", dataset_dir, "-m", f"Dubbing input {unique_id}"]
    else:
        yield "[STAGE:3] 🆕 Dataset not found; creating it once.\n"
        upload_cmd = ["python", "-m", "kaggle", "datasets", "create", "-p", dataset_dir]

    upload_success = False
    max_retries = 4
    last_result = None
    for attempt in range(max_retries):
        yield f"[STAGE:3] Executing upload (Attempt {attempt + 1}/{max_retries})...\n"
        last_result = run_cmd(upload_cmd)
        if last_result.returncode == 0:
            upload_success = True
            break

        # Kaggle may return a server error after accepting the create request.
        # Check the stable ID before retrying or switching to a version upload.
        status_result = run_cmd(status_cmd)
        status_text = (status_result.stdout + "\n" + status_result.stderr).lower()
        if status_result.returncode == 0 and any(word in status_text for word in ("ready", "complete", "processing")):
            if not dataset_exists:
                dataset_exists = True
                yield "[STAGE:3] ✅ Kaggle confirms the dataset was created despite the upload response; continuing with it.\n"
            upload_success = True
            break
        if not dataset_exists and attempt < max_retries - 1:
            # A create that did not commit can be retried; a duplicate response
            # on the next pass falls through to status detection above.
            pass
        elif attempt < max_retries - 1:
            upload_cmd = ["python", "-m", "kaggle", "datasets", "version", "-p", dataset_dir, "-m", f"Dubbing input {unique_id}"]
        if attempt < max_retries - 1:
            delay = 15 * (2 ** attempt)
            yield f"[STAGE:3] ⚠️ Kaggle upload failed ({(last_result.stderr or last_result.stdout).strip()[-500:]}); checking again in {delay}s.\n"
            time.sleep(delay)

    if not upload_success:
        error_text = (last_result.stderr or last_result.stdout or "No error details returned.").strip()
        yield f"[STAGE:3] ❌ Dataset upload failed after {max_retries} attempts:\n{error_text}\n"
        yield "[STAGE:3] Kaggle did not confirm that the dataset exists. Check Kaggle service/account status, then retry; this run did not submit a worker.\n"
        track_stage(project_id, 3, "failed",
                    message=f"Dataset upload failed after {max_retries} attempts",
                    error=error_text, manifest=manifest)
        return

    track_stage(project_id, 3, "success",
                message=f"Input dataset uploaded to {dataset_id}", manifest=manifest)
    yield "[STAGE:3] ✅ Upload complete.\n"
    
    # Push Kaggle Notebook (The Worker)
    kernel_dir = os.path.join(project_dir, "kernel_safe")
    os.makedirs(kernel_dir, exist_ok=True)
    
    shutil.copy(os.path.join(APP_DIR, "kaggle_worker.ipynb"), os.path.join(kernel_dir, "kaggle_worker.ipynb"))
    
    yield "[STAGE:4] ⚙️ Initializing Kaggle Worker Kernel...\n"
    manifest["kernel_id"] = kernel_id = kernel_id_for(KAGGLE_USERNAME)
    track_stage(project_id, 4, "running", message=f"Preparing worker kernel {kernel_id}",
                manifest=manifest)
    k_init_cmd = ["python", "-m", "kaggle", "kernels", "init", "-p", kernel_dir]
    yield f"[STAGE:4] Executing: {' '.join(k_init_cmd)}\n"
    run_cmd(k_init_cmd)
    
    kmeta_path = os.path.join(kernel_dir, "kernel-metadata.json")
    # Keep one private notebook so its configuration persists between video runs.
    kernel_slug = "dubber-worker-homura"
    
    if os.path.exists(kmeta_path):
        with open(kmeta_path, "r") as f:
            kmeta = json.load(f)
        kmeta["id"] = kernel_id
        kmeta["title"] = "Dubber Worker Homura"
        kmeta["code_file"] = "kaggle_worker.ipynb"
        kmeta["language"] = "python"
        kmeta["kernel_type"] = "notebook"
        kmeta["is_private"] = "true"
        kmeta["enable_gpu"] = "true"
        kmeta["enable_internet"] = "true"
        kmeta["dataset_sources"] = [dataset_id]
        
        with open(kmeta_path, "w") as f:
            json.dump(kmeta, f, indent=4)

    yield "[STAGE:4] ⚠️ If the worker cannot download its model, open the Kaggle notebook URL and enable Internet in Settings.\n"
            
    worker_url = f"https://www.kaggle.com/code/{kernel_id}"
    yield f"[STAGE:4] 🔗 Kaggle Worker URL: {worker_url}\n"
    yield "[STAGE:4] ⬇️ First run downloads vLLM and Homura-2B; setup may take up to 20 minutes. Keep Kaggle Notebook Internet enabled.\n"
    for kernel_attempt in range(2):
        yield "[STAGE:4] 💡 If downloads fail, open the worker URL and enable Internet in Kaggle Notebook Settings.\n"
        yield f"[STAGE:4] 🔥 Pushing execution code to Kaggle GPU Worker ({kernel_id}) [Attempt {kernel_attempt+1}]...\n"
        k_push_cmd = ["python", "-m", "kaggle", "kernels", "push", "-p", kernel_dir]
        yield f"[STAGE:4] Executing: {' '.join(k_push_cmd)}\n"
        res = run_cmd(k_push_cmd)
        if res.returncode != 0:
            yield f"[STAGE:4] ❌ Error pushing kernel:\n{res.stderr}\n"
            if "could not resolve" in res.stderr.lower() or "500" in res.stderr or "network" in res.stderr.lower():
                if kernel_attempt == 0:
                    yield "[STAGE:4] ⚠️ Network error during push. Retrying in 60 seconds...\n"
                    track_stage(project_id, 4, "running",
                                message="Network error during kernel push; retrying in 60s",
                                error=(res.stderr or "").strip() or None, manifest=manifest)
                    time.sleep(60)
                    continue
            track_stage(project_id, 4, "failed", message="Kernel push failed",
                        error=(res.stderr or res.stdout or "").strip() or "kaggle kernels push exited non-zero",
                        manifest=manifest)
            return

        track_stage(project_id, 4, "success",
                    message=f"Worker kernel pushed to {kernel_id}", manifest=manifest)
        yield "[STAGE:5] ⏳ Waiting for Kaggle Worker to complete...\n"

        # Feature films can take several hours on a free shared GPU.
        # Kaggle remains the hard upper bound and reports failure sooner.
        outcome = yield from _wait_for_kernel(
            kernel_id,
            project_dir,
            timeout=10 * 60 * 60,
            stage="STAGE:5",
        )

        if outcome["success"]:
            break
        if outcome["network_error"] and kernel_attempt == 0:
            yield "[STAGE:5] ⚠️ Kernel failed with a network error. Waiting 60 seconds and re-pushing once...\n"
            time.sleep(60)
            continue
        return

    yield "[STAGE:6] ⬇️ Downloading final outputs from Kaggle...\n"
    
    output_cmd = ["python", "-m", "kaggle", "kernels", "output", kernel_id, "-p", project_dir]
    yield f"[STAGE:6] Executing: {' '.join(output_cmd)}\n"
    run_cmd(output_cmd)
    
    # Verify downloaded output video
    original_video_basename = os.path.basename(video_path)
    mp4_files = [f for f in glob.glob(os.path.join(project_dir, "*.mp4")) if os.path.basename(f) != original_video_basename]
    
    if mp4_files and os.path.getsize(mp4_files[0]) > 0:
        stale_error_log = os.path.join(project_dir, "error_log.txt")
        if os.path.isfile(stale_error_log):
            os.remove(stale_error_log)
        yield f"[STAGE:7] 🎉 Pipeline finished successfully! Video saved at {os.path.basename(mp4_files[0])} and report saved locally."
    else:
        # Worker said 'complete' but output is missing — this is NOT a success.
        # Download and display the actual error log from Kaggle.
        yield "[STAGE:7] ⚠️ Worker reported 'complete' but output video is missing or empty. Fetching error logs...\n"
        
        error_log_path = os.path.join(project_dir, "error_log.txt")
        err_content = ""
        if os.path.exists(error_log_path):
            with open(error_log_path, "r") as f:
                err_content = f.read()
        else:
            log_files = glob.glob(os.path.join(project_dir, "*.log")) + glob.glob(os.path.join(project_dir, "*.txt"))
            for lf in log_files:
                with open(lf, "r") as f:
                    err_content += f"\n--- {os.path.basename(lf)} ---\n" + f.read()
        
        if err_content:
            yield f"[STAGE:7] ❌ Worker Error Log:\n{err_content[-2500:]}\n"
        else:
            yield f"[STAGE:7] ❌ No error_log.txt found. The worker likely failed silently (e.g., pip install failed due to no internet).\n"
        
        if "resolve host" in err_content.lower() or "network" in err_content.lower() or "internet" in err_content.lower() or not err_content:
            yield f"[STAGE:7] 💡 FIX: Open https://www.kaggle.com/code/{kernel_id} → Settings → Enable Internet → Re-run the notebook manually.\n"
        else:
            yield f"[STAGE:7] 💡 FIX: An error occurred in the execution. Please check the logs above or open https://www.kaggle.com/code/{kernel_id} to debug.\n"
        
    # Clean up local temporary compressed files after upload is done
    if compressed_video_path and os.path.exists(compressed_video_path):
        try:
            os.remove(compressed_video_path)
            yield f"[STAGE:7] 🧹 Cleaned up temporary compressed video.\n"
        except:
            pass
