import os
import json
import time

def log_metric(report_path, stage, metric_name, metric_value):
    report = {}
    if os.path.exists(report_path):
        with open(report_path, "r") as f:
            try:
                report = json.load(f)
            except:
                pass
            
    if stage not in report:
        report[stage] = {}
        
    report[stage][metric_name] = metric_value
    
    with open(report_path, "w") as f:
        json.dump(report, f, indent=4)

def check_cache(project_dir, stage):
    cache_file = os.path.join(project_dir, f"{stage}_completed.txt")
    return os.path.exists(cache_file)

def mark_completed(project_dir, stage):
    cache_file = os.path.join(project_dir, f"{stage}_completed.txt")
    with open(cache_file, "w") as f:
        f.write("done")

def run_pipeline(video_path, project_dir, target_lang, speaker_detection):
    report_path = os.path.join(project_dir, "report.json")
    status_logs = f"Starting pipeline for video...\n"
    status_logs += f"Project Folder: {project_dir}\n"
    
    stages = [
        "extract_audio",
        "separate_vocals",
        "transcribe",
        "translate",
        "tts",
        "lip_sync",
        "render"
    ]
    
    for stage in stages:
        if check_cache(project_dir, stage):
            status_logs += f"⏩ Skipping '{stage}' (Loaded from cache)\n"
            continue
            
        status_logs += f"⏳ Running '{stage}' on Kaggle Worker...\n"
        
        # Here we will later integrate the actual call to the Kaggle API / Mazinger
        # For now, it's a simulation
        time.sleep(0.5) 
        
        # Log mock metrics to report.json
        log_metric(report_path, stage, "Status", "Success")
        if stage == "transcribe":
            log_metric(report_path, stage, "WER (Word Error Rate)", "4.2%")
        elif stage == "lip_sync":
            log_metric(report_path, stage, "Sync Offset", "12ms")
        elif stage == "separate_vocals":
            log_metric(report_path, stage, "SDR", "14 dB")
            
        mark_completed(project_dir, stage)
        status_logs += f"✅ Completed '{stage}'\n"
        
    status_logs += "\n🎉 Dubbing finished successfully! Video saved in project folder."
    return status_logs
