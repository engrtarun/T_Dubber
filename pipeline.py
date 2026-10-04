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
from pathlib import Path

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Force Kaggle CLI to use the local folder containing kaggle.json
local_kaggle_dir = os.path.join(APP_DIR, "kaggle_paperWork")
os.environ["KAGGLE_CONFIG_DIR"] = local_kaggle_dir

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


def _wait_for_kernel(kernel_id, project_dir, timeout, stage="STAGE:5", poll_seconds=30):
    """Poll a Kaggle kernel until it finishes, yielding progress lines.

    Returns ``{"success": bool, "network_error": bool, "status": str}``. This is
    deliberately a separate generator so :func:`reattach_to_kernel` can reuse
    the exact same logic -- when a browser tab closes or the PC reboots, the
    work is still running on Kaggle and we need to pick the thread back up.
    """
    status_cmd = ["python", "-m", "kaggle", "kernels", "status", kernel_id]
    output_cmd = ["python", "-m", "kaggle", "kernels", "output", kernel_id, "-p", project_dir]

    start_time = time.time()
    last_report = 0

    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout:
            yield f"[{stage}] ❌ Timeout: Worker did not finish within {int(timeout / 3600)} hours.\n"
            return {"success": False, "network_error": False, "status": "timeout"}

        result = run_cmd(status_cmd)
        status_text = (result.stdout or "") + (result.stderr or "")
        lowered = status_text.lower()

        if "complete" in lowered:
            yield f"[{stage}] ✅ Kaggle execution completed!\n"
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
                return {"success": False, "network_error": network_error, "status": "error"}
            yield f"[{stage}] ❌ No error log or .log file found in {project_dir}\n"
            if (result.stderr or result.stdout):
                yield f"[{stage}] Kaggle CLI said:\n{(result.stderr or result.stdout).strip()[-1200:]}\n"
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
    yield "[STAGE:5] ♻️ Reattaching to the Kaggle worker...\n"
    yield f"[STAGE:5] ☁️ Your PC going offline does not stop Kaggle; the job is still running there.\n"
    yield f"[STAGE:5] 🔗 Worker URL: https://www.kaggle.com/code/{kernel_id}\n"

    outcome = yield from _wait_for_kernel(
        kernel_id, project_dir, timeout=timeout, stage="STAGE:5", poll_seconds=poll_seconds
    )
    if not outcome["success"]:
        return outcome

    yield "[STAGE:6] ⬇️ Downloading final outputs from Kaggle...\n"
    output_cmd = ["python", "-m", "kaggle", "kernels", "output", kernel_id, "-p", project_dir]
    yield f"[STAGE:6] Executing: {' '.join(output_cmd)}\n"
    run_cmd(output_cmd)

    mp4_files = [
        f
        for f in glob.glob(os.path.join(project_dir, "*.mp4"))
        if os.path.getsize(f) > 0
    ]
    if mp4_files:
        stale = os.path.join(project_dir, "error_log.txt")
        if os.path.isfile(stale):
            os.remove(stale)
        biggest = max(mp4_files, key=lambda f: os.path.getsize(f))
        yield f"[STAGE:7] 🎉 Pipeline finished successfully! Output: {os.path.basename(biggest)}"
        outcome["output_video"] = biggest
    else:
        content, _source = _collect_kernel_logs(project_dir)
        if content:
            yield f"[STAGE:7] ❌ Worker Error Log:\n{content[-2500:]}\n"
        else:
            yield "[STAGE:7] ❌ Worker reported success but produced no video.\n"
    return outcome


def run_pipeline(video_path, project_dir, target_lang, speaker_detection,
                 backup_link=None, source_url=None, source_title=None, source_size=None):
    """
    Orchestrator function (Generator) using Kaggle API to push the job to a cloud GPU.
    Yields status logs incrementally with [STAGE:X] markers for the UI stepper.

    ``backup_link`` is the Telegram archive link created before this run
    started. It travels with the job so the worker records it in its report and
    a finished dub can always be traced back to its archive.
    """
    yield "[STAGE:1] 🚀 Starting Kaggle pipeline...\n"

    if backup_link:
        yield f"[STAGE:1] ☁️ Telegram backup on file: {backup_link}\n"
    if source_url:
        yield f"[STAGE:1] 🔗 Source resolved from: {source_url}\n"


    kaggle_creds_path = os.path.join(local_kaggle_dir, "kaggle.json")
    if not os.path.exists(kaggle_creds_path):
        yield f"[STAGE:1] ❌ Error: Missing Kaggle credentials!\nCould not find kaggle.json at {kaggle_creds_path}.\n"
        return
        
    yield "[STAGE:1] 🔍 Checking Kaggle CLI installation...\n"
    if run_cmd(["python", "-m", "kaggle", "--version"]).returncode != 0:
        yield "[STAGE:1] ❌ Error: Kaggle API CLI not installed or authenticated.\n"
        return
        
    try:
        with open(kaggle_creds_path) as kf:
            kcreds = json.load(kf)
            KAGGLE_USERNAME = kcreds.get("username", "YOUR_KAGGLE_USERNAME")
    except Exception as e:
        yield f"[STAGE:1] ❌ Error reading kaggle.json: {e}\n"
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
            return
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    meta["id"] = dataset_id
    meta["title"] = dataset_title
    meta["licenses"] = [{"name": "CC0-1.0"}]
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    yield f"[STAGE:3] ☁️ Uploading video to reusable Kaggle dataset ({dataset_id})...\n"
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
        return

    yield "[STAGE:3] ✅ Upload complete.\n"
    
    # Push Kaggle Notebook (The Worker)
    kernel_dir = os.path.join(project_dir, "kernel_safe")
    os.makedirs(kernel_dir, exist_ok=True)
    
    shutil.copy(os.path.join(APP_DIR, "kaggle_worker.ipynb"), os.path.join(kernel_dir, "kaggle_worker.ipynb"))
    
    yield "[STAGE:4] ⚙️ Initializing Kaggle Worker Kernel...\n"
    k_init_cmd = ["python", "-m", "kaggle", "kernels", "init", "-p", kernel_dir]
    yield f"[STAGE:4] Executing: {' '.join(k_init_cmd)}\n"
    run_cmd(k_init_cmd)
    
    kmeta_path = os.path.join(kernel_dir, "kernel-metadata.json")
    # Keep one private notebook so its configuration persists between video runs.
    kernel_slug = "dubber-worker-homura"
    kernel_id = kernel_id_for(KAGGLE_USERNAME)
    
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
                    time.sleep(60)
                    continue
            return
            
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
