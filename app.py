import gradio as gr
import os
import shutil
import glob
from pipeline import run_pipeline

def start_dubbing(video_file, target_lang, speaker_detection):
    if not video_file:
        yield "Error: Please upload a video first.", None, None
        return
        
    movie_name = os.path.splitext(os.path.basename(video_file))[0]
    project_dir = os.path.join("projects", movie_name)
    os.makedirs(project_dir, exist_ok=True)
    
    # Copy uploaded file to project directory to persist it
    persisted_video = os.path.join(project_dir, os.path.basename(video_file))
    if os.path.abspath(video_file) != os.path.abspath(persisted_video):
        shutil.copy(video_file, persisted_video)
    
    logs = ""
    output_video_path = None
    output_report_path = None
    
    # Run the orchestrator pipeline which yields logs in real-time
    for status_update in run_pipeline(persisted_video, project_dir, target_lang, speaker_detection):
        logs += status_update
        
        # Once it finishes successfully, locate the downloaded files
        if "Pipeline finished successfully" in status_update:
            # We look for output files inside project_dir
            mp4_files = glob.glob(os.path.join(project_dir, "*.mp4"))
            for f in mp4_files:
                # Exclude the original video if possible
                if os.path.basename(f) != os.path.basename(video_file):
                    output_video_path = f
                    break
            
            json_files = glob.glob(os.path.join(project_dir, "report.json"))
            if json_files:
                output_report_path = json_files[0]
                
        yield logs, output_video_path, output_report_path

with gr.Blocks(title="AI Movie Dubbing Orchestrator", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 🎬 AI Movie Dubbing Control Panel")
    gr.Markdown("Upload a video, choose your language, and let the cloud GPU do the heavy lifting!")
    
    with gr.Row():
        with gr.Column():
            video_input = gr.Video(label="Upload Video", sources=["upload"])
            target_language = gr.Dropdown(choices=["Hindi", "English", "Spanish", "French", "German"], value="Hindi", label="Target Language")
            speaker_toggle = gr.Checkbox(label="Enable Speaker Detection", value=True)
            start_button = gr.Button("🚀 Start Dubbing", variant="primary")
            
        with gr.Column():
            status_output = gr.Textbox(label="Status / Logs", lines=15, interactive=False, max_lines=25)
            
    with gr.Row():
        output_video = gr.File(label="Download Final Dubbed Video", interactive=False)
        output_report = gr.File(label="Download Quality Report (JSON)", interactive=False)
            
    start_button.click(
        fn=start_dubbing,
        inputs=[video_input, target_language, speaker_toggle],
        outputs=[status_output, output_video, output_report]
    )

if __name__ == "__main__":
    demo.launch(inbrowser=True)
