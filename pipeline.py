import os
import time
import json
import subprocess
import shutil
import re

# Force Kaggle CLI to use the local folder containing kaggle.json
local_kaggle_dir = os.path.abspath("kaggle_paperWork")
os.environ["KAGGLE_CONFIG_DIR"] = local_kaggle_dir

def run_cmd(cmd):
    """
    Accepts either a string or a list. 
    If list, shell=False is used for safe execution with spaces in paths.
    """
    if isinstance(cmd, list):
        print(f"Executing command list: {cmd}")
        result = subprocess.run(cmd, capture_output=True, text=True)
    else:
        print(f"Executing command string: {cmd}")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result

def sanitize_name(name):
    """Replaces spaces and invalid characters with underscores."""
    return re.sub(r'[^A-Za-z0-9_-]', '_', name.replace(' ', '_'))

def run_pipeline(video_path, project_dir, target_lang, speaker_detection):
    """
    Orchestrator function (Generator) using Kaggle API to push the job to a cloud GPU.
    Yields status logs incrementally with [STAGE:X] markers for the UI stepper.
    """
    yield "[STAGE:1] 🚀 Starting Kaggle pipeline...\n"
    
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
    
    timestamp = int(time.time())
    
    safe_video_name = sanitize_name(os.path.basename(video_path).split('.')[0])
    dataset_name = sanitize_name(f"dubber-video-{safe_video_name}")[:35] + f"-{timestamp}"
    dataset_name = dataset_name.lower().replace("_", "-") 
    
    dataset_dir = os.path.join(project_dir, "dataset_safe")
    os.makedirs(dataset_dir, exist_ok=True)
    
    video_filename = os.path.basename(video_path)
    shutil.copy(video_path, os.path.join(dataset_dir, video_filename))
    
    # Task 3: Ensure no .git directory in dataset folder
    git_dir = os.path.join(dataset_dir, ".git")
    if os.path.exists(git_dir):
        shutil.rmtree(git_dir)
    
    yield "[STAGE:2] 📦 Creating Dataset structure...\n"
    init_cmd = ["python", "-m", "kaggle", "datasets", "init", "-p", dataset_dir]
    yield f"[STAGE:2] Executing: {' '.join(init_cmd)}\n"
    run_cmd(init_cmd)
    
    meta_path = os.path.join(dataset_dir, "dataset-metadata.json")
    if os.path.exists(meta_path):
        with open(meta_path, "r") as f:
            meta = json.load(f)
        meta["id"] = f"{KAGGLE_USERNAME}/{dataset_name}"
        meta["title"] = dataset_name
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=4)
            
    # Task 2: Check for dataset existence (duplicate check)
    check_cmd = ["python", "-m", "kaggle", "datasets", "status", f"{KAGGLE_USERNAME}/{dataset_name}"]
    res_check = run_cmd(check_cmd)
    if "ready" in res_check.stdout.lower() or res_check.returncode == 0:
        dataset_name += f"-{int(time.time())}"
        yield f"[STAGE:2] ⚠️ Dataset name existed! Changed to {dataset_name}\n"
        with open(meta_path, "r") as f:
            meta = json.load(f)
        meta["id"] = f"{KAGGLE_USERNAME}/{dataset_name}"
        meta["title"] = dataset_name
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=4)
            
    yield f"[STAGE:3] ☁️ Uploading video to Kaggle (Dataset: {dataset_name}). This might take a while...\n"
    create_cmd = ["python", "-m", "kaggle", "datasets", "create", "-p", dataset_dir]
    
    # Task 1: Retry Logic
    max_retries = 3
    upload_success = False
    for attempt in range(max_retries):
        yield f"[STAGE:3] Executing Upload (Attempt {attempt+1}/{max_retries})...\n"
        res = run_cmd(create_cmd)
        if res.returncode == 0:
            upload_success = True
            break
        elif "500" in res.stderr or "500" in res.stdout or attempt < max_retries - 1:
            yield f"[STAGE:3] ⚠️ Upload failed (Possible 500 Error). Retrying in 10 seconds...\n"
            time.sleep(10)
        else:
            break
            
    if not upload_success:
        yield f"[STAGE:3] ❌ Error uploading dataset after {max_retries} attempts:\n{res.stderr}\n"
        return
        
    yield "[STAGE:3] ✅ Upload complete.\n"
    
    # Push Kaggle Notebook (The Worker)
    kernel_dir = os.path.join(project_dir, "kernel_safe")
    os.makedirs(kernel_dir, exist_ok=True)
    
    shutil.copy("kaggle_worker.ipynb", os.path.join(kernel_dir, "kaggle_worker.ipynb"))
    
    yield "[STAGE:4] ⚙️ Initializing Kaggle Worker Kernel...\n"
    k_init_cmd = ["python", "-m", "kaggle", "kernels", "init", "-p", kernel_dir]
    yield f"[STAGE:4] Executing: {' '.join(k_init_cmd)}\n"
    run_cmd(k_init_cmd)
    
    kmeta_path = os.path.join(kernel_dir, "kernel-metadata.json")
    kernel_id = f"{KAGGLE_USERNAME}/dubber-worker-{timestamp}"
    if os.path.exists(kmeta_path):
        with open(kmeta_path, "r") as f:
            kmeta = json.load(f)
        kmeta["id"] = kernel_id
        kmeta["title"] = f"Dubber Worker {timestamp}"
        kmeta["code_file"] = "kaggle_worker.ipynb"
        kmeta["language"] = "python"
        kmeta["kernel_type"] = "notebook"
        kmeta["is_private"] = "true"
        kmeta["enable_gpu"] = "true"
        kmeta["dataset_sources"] = [f"{KAGGLE_USERNAME}/{dataset_name}"]
        
        with open(kmeta_path, "w") as f:
            json.dump(kmeta, f, indent=4)
            
    yield f"[STAGE:4] 🔥 Pushing execution code to Kaggle GPU Worker ({kernel_id})...\n"
    k_push_cmd = ["python", "-m", "kaggle", "kernels", "push", "-p", kernel_dir]
    yield f"[STAGE:4] Executing: {' '.join(k_push_cmd)}\n"
    res = run_cmd(k_push_cmd)
    if res.returncode != 0:
        yield f"[STAGE:4] ❌ Error pushing kernel:\n{res.stderr}\n"
        return
        
    yield f"[STAGE:5] ⏳ Waiting for Kaggle Worker to complete...\n"
    
    # Task 3: 2-hour timeout
    timeout = 2 * 60 * 60  
    start_time = time.time()
    
    status_cmd = ["python", "-m", "kaggle", "kernels", "status", kernel_id]
    
    # Task 1 & 2: Real polling loop
    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout:
            yield "[STAGE:5] ❌ Timeout: Worker did not finish within 2 hours.\n"
            return
            
        res = run_cmd(status_cmd)
        status_text = res.stdout.lower() + res.stderr.lower()
        
        if "complete" in status_text:
            yield "[STAGE:5] ✅ Kaggle execution completed!\n"
            break
        elif "error" in status_text or "cancel" in status_text:
            yield "[STAGE:5] ❌ Kaggle execution failed or was cancelled!\n"
            yield "[STAGE:5] ⬇️ Downloading Kaggle logs for debugging...\n"
            run_cmd(["python", "-m", "kaggle", "kernels", "output", kernel_id, "-p", project_dir])
            
            error_log_path = os.path.join(project_dir, "error_log.txt")
            if os.path.exists(error_log_path):
                with open(error_log_path, "r") as f:
                    err_content = f.read()
                yield f"[STAGE:5] ❌ Error Details:\n{err_content}\n"
            else:
                import glob
                log_files = glob.glob(os.path.join(project_dir, "*.log"))
                if log_files:
                    with open(log_files[0], "r") as f:
                        err_content = f.read()
                        # Output last 2000 chars to avoid overwhelming the UI
                        yield f"[STAGE:5] ❌ Kaggle Kernel Log:\n{err_content[-2000:]}\n"
                else:
                    yield f"[STAGE:5] ❌ No error log or .log file found in {project_dir}\n"
            return
        
        yield f"[STAGE:5] 🔄 Worker is still processing... (Elapsed: {int(elapsed/60)}m {int(elapsed%60)}s)\n"
        time.sleep(30)
        
    yield "[STAGE:6] ⬇️ Downloading final outputs from Kaggle...\n"
    
    output_cmd = ["python", "-m", "kaggle", "kernels", "output", kernel_id, "-p", project_dir]
    yield f"[STAGE:6] Executing: {' '.join(output_cmd)}\n"
    run_cmd(output_cmd)
    
    # Task 4: Verify downloaded output video
    import glob
    mp4_files = [f for f in glob.glob(os.path.join(project_dir, "*.mp4")) if os.path.basename(f) != video_filename]
    
    if mp4_files and os.path.getsize(mp4_files[0]) > 0:
        yield f"[STAGE:7] 🎉 Pipeline finished successfully! Video saved at {os.path.basename(mp4_files[0])} and report saved locally."
    else:
        yield f"[STAGE:7] ❌ Error: Downloaded video is missing or empty. Please check Kaggle logs.\n"
