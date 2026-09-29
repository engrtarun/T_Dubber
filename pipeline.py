import os
import time
import json
import subprocess
import shutil

# Force Kaggle CLI to use the local folder containing kaggle.json
local_kaggle_dir = os.path.abspath("kaggle_paperWork")
os.environ["KAGGLE_CONFIG_DIR"] = local_kaggle_dir

def run_cmd(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result

def run_pipeline(video_path, project_dir, target_lang, speaker_detection):
    """
    Orchestrator function (Generator) using Kaggle API to push the job to a cloud GPU.
    Yields status logs incrementally.
    """
    yield "🚀 Starting Kaggle pipeline...\n"
    
    kaggle_creds_path = os.path.join(local_kaggle_dir, "kaggle.json")
    if not os.path.exists(kaggle_creds_path):
        yield f"❌ Error: Missing Kaggle credentials!\nCould not find kaggle.json at {kaggle_creds_path}.\n"
        return
        
    yield "🔍 Checking Kaggle CLI installation...\n"
    if run_cmd("python -m kaggle --version").returncode != 0:
        yield "❌ Error: Kaggle API CLI not installed or authenticated.\n"
        return
        
    try:
        with open(kaggle_creds_path) as kf:
            kcreds = json.load(kf)
            KAGGLE_USERNAME = kcreds.get("username", "YOUR_KAGGLE_USERNAME")
    except Exception as e:
        yield f"❌ Error reading kaggle.json: {e}\n"
        return
    
    timestamp = int(time.time())
    
    # 1. Create a Kaggle Dataset for the video
    dataset_name = f"dubber-video-{timestamp}"
    dataset_dir = os.path.join(project_dir, "dataset")
    os.makedirs(dataset_dir, exist_ok=True)
    
    video_filename = os.path.basename(video_path)
    shutil.copy(video_path, os.path.join(dataset_dir, video_filename))
    
    yield "📦 Initializing Kaggle Dataset for video upload...\n"
    run_cmd(f"python -m kaggle datasets init -p {dataset_dir}")
    
    meta_path = os.path.join(dataset_dir, "dataset-metadata.json")
    if os.path.exists(meta_path):
        with open(meta_path, "r") as f:
            meta = json.load(f)
        meta["id"] = f"{KAGGLE_USERNAME}/{dataset_name}"
        meta["title"] = dataset_name
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=4)
            
    yield f"☁️ Uploading video to Kaggle (Dataset: {dataset_name}). This might take a while...\n"
    res = run_cmd(f"python -m kaggle datasets create -p {dataset_dir}")
    if res.returncode != 0:
        yield f"❌ Error uploading dataset:\n{res.stderr}\n"
        return
    yield "✅ Upload complete.\n"
    
    # 2. Push Kaggle Notebook (The Worker)
    kernel_dir = os.path.join(project_dir, "kernel")
    os.makedirs(kernel_dir, exist_ok=True)
    
    shutil.copy("kaggle_worker.ipynb", os.path.join(kernel_dir, "kaggle_worker.ipynb"))
    
    yield "⚙️ Initializing Kaggle Worker Kernel...\n"
    run_cmd(f"python -m kaggle kernels init -p {kernel_dir}")
    
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
            
    yield f"🔥 Pushing execution code to Kaggle GPU Worker ({kernel_id})...\n"
    res = run_cmd(f"python -m kaggle kernels push -p {kernel_dir}")
    if res.returncode != 0:
        yield f"❌ Error pushing kernel:\n{res.stderr}\n"
        return
        
    yield f"⏳ Waiting for Kaggle Worker to complete...\n"
    while True:
        res = run_cmd(f"python -m kaggle kernels status {kernel_id}")
        status = res.stdout.lower()
        if "complete" in status:
            yield "✅ Kaggle execution completed!\n"
            break
        elif "error" in status or "cancel" in status:
            yield "❌ Kaggle execution failed or was cancelled!\n"
            return
            
        yield "🔄 Polling status (waiting 30s)...\n"
        time.sleep(30)
        
    yield "⬇️ Downloading final outputs from Kaggle...\n"
    run_cmd(f"python -m kaggle kernels output {kernel_id} -p {project_dir}")
    
    yield "🎉 Pipeline finished successfully! Video and report saved locally."
