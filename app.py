import gradio as gr
import os
import shutil
import glob
import json
import re
from pipeline import run_pipeline

# Custom CSS for modern dark theme
custom_css = """
body { background-color: #0c0c0c; color: #ededed; }
.gradio-container { max-width: 1400px !important; }
.stepper-container { background: #111; padding: 20px; border-radius: 12px; border: 1px solid #222; margin-bottom: 20px; }
.stepper-wrapper { display: flex; justify-content: space-between; align-items: center; position: relative; }
.stepper-line { position: absolute; top: 18px; left: 0; right: 0; height: 2px; background: #333; z-index: 1; }
.stepper-step { display: flex; flex-direction: column; align-items: center; gap: 10px; z-index: 2; position: relative; width: 80px; text-align: center; }
.step-circle { width: 36px; height: 36px; border-radius: 50%; background: #222; border: 2px solid #333; color: #888; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 14px; transition: all 0.3s ease; }
.step-label { font-size: 10px; font-weight: 500; color: #888; transition: all 0.3s ease; }

.step-completed .step-circle { background: #10b981; border-color: #10b981; color: white; }
.step-completed .step-label { color: #10b981; }
.step-active .step-circle { background: #3b82f6; border-color: #3b82f6; color: white; box-shadow: 0 0 15px rgba(59, 130, 246, 0.4); }
.step-active .step-label { color: #3b82f6; font-weight: bold; }
.step-active .step-circle::after { content: ''; width: 10px; height: 10px; border-radius: 50%; border: 2px solid white; border-top-color: transparent; animation: spin 1s linear infinite; display: inline-block; position: absolute; }

@keyframes spin { 100% { transform: rotate(360deg); } }

.metric-card { background: #111; border: 1px solid #222; border-radius: 12px; padding: 20px; text-align: center; display: flex; flex-direction: column; gap: 5px; }
.metric-value { font-size: 28px; font-weight: 800; background: linear-gradient(90deg, #3b82f6, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.metric-label { font-size: 13px; color: #888; text-transform: uppercase; letter-spacing: 1px; }

button.primary { background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%) !important; border: none !important; font-size: 16px !important; font-weight: bold !important; transition: transform 0.2s, box-shadow 0.2s !important; }
button.primary:hover { transform: translateY(-2px); box-shadow: 0 5px 15px rgba(139, 92, 246, 0.4) !important; }
"""

def get_stepper_html(current_stage):
    stages = ["Idle", "Auth & Setup", "Create Dataset", "Upload File", "Init Kernel", "Cloud GPU", "Download", "Done"]
    html = '<div class="stepper-container"><div class="stepper-wrapper">'
    html += '<div class="stepper-line"></div>'
    
    for i, name in enumerate(stages):
        state_class = ""
        icon = str(i)
        if i < current_stage:
            state_class = "step-completed"
            icon = "✓"
        elif i == current_stage:
            state_class = "step-active"
            icon = "" # Spinner handled by CSS ::after
            
        if current_stage == 7 and i == 7:
            state_class = "step-completed"
            icon = "✨"
            
        html += f'''
        <div class="stepper-step {state_class}">
            <div class="step-circle">{icon}</div>
            <div class="step-label">{name}</div>
        </div>
        '''
    html += '</div></div>'
    return html

def parse_report_metrics(report_path):
    if not report_path or not os.path.exists(report_path):
        return '<div style="color:#888; text-align:center; padding: 20px;">No report generated yet.</div>'
    try:
        with open(report_path, "r") as f:
            data = json.load(f)
            
        html = '<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; margin-top: 15px;">'
        for k, v in data.items():
            label = k.replace("_", " ")
            html += f'''
            <div class="metric-card">
                <div class="metric-value">{v}</div>
                <div class="metric-label">{label}</div>
            </div>
            '''
        html += '</div>'
        return html
    except:
        return '<div style="color:red; text-align:center;">Failed to parse report.json</div>'

def start_dubbing(video_file, target_lang, speaker_detection):
    if not video_file:
        yield get_stepper_html(0), "Error: Please upload a video first.", None, parse_report_metrics(None)
        return
        
    movie_name = os.path.splitext(os.path.basename(video_file))[0]
    movie_name = re.sub(r'[^A-Za-z0-9_-]', '_', movie_name.replace(' ', '_'))
    project_dir = os.path.join("projects", movie_name)
    os.makedirs(project_dir, exist_ok=True)
    
    persisted_video = os.path.join(project_dir, os.path.basename(video_file))
    if os.path.abspath(video_file) != os.path.abspath(persisted_video):
        shutil.copy(video_file, persisted_video)
    
    logs = ""
    output_video_path = None
    output_report_path = None
    current_stage = 1
    
    for status_update in run_pipeline(persisted_video, project_dir, target_lang, speaker_detection):
        stage_match = re.search(r"\[STAGE:(\d)\]", status_update)
        if stage_match:
            current_stage = int(stage_match.group(1))
            status_update = status_update.replace(stage_match.group(0), "")
            
        logs += status_update
        
        if "Pipeline finished successfully" in status_update:
            current_stage = 7
            mp4_files = glob.glob(os.path.join(project_dir, "*.mp4"))
            for f in mp4_files:
                if os.path.basename(f) != os.path.basename(video_file):
                    output_video_path = f
                    break
            
            json_files = glob.glob(os.path.join(project_dir, "report.json"))
            if json_files:
                output_report_path = json_files[0]
                
        yield get_stepper_html(current_stage), logs, output_video_path, parse_report_metrics(output_report_path)

def load_project_history():
    projects_dir = "projects"
    if not os.path.exists(projects_dir):
        return []
    return [d for d in os.listdir(projects_dir) if os.path.isdir(os.path.join(projects_dir, d))]

def load_project_details(project_name):
    if not project_name:
        return None, parse_report_metrics(None)
        
    project_dir = os.path.join("projects", project_name)
    output_video = None
    mp4_files = glob.glob(os.path.join(project_dir, "*.mp4"))
    if mp4_files:
        output_video = mp4_files[-1] 
        
    report_path = os.path.join(project_dir, "report.json")
    if not os.path.exists(report_path):
        report_path = None
        
    return output_video, parse_report_metrics(report_path)

theme = gr.themes.Monochrome(
    primary_hue="blue", 
    secondary_hue="indigo", 
    neutral_hue="slate"
)

with gr.Blocks(title="Tarun Dubber AI", css=custom_css) as demo:
    gr.Markdown("<h1 style='text-align: center; margin-bottom: 5px; background: linear-gradient(90deg, #3b82f6, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent;'>🎬 Tarun Dubber AI - Control Panel</h1>")
    gr.Markdown("<p style='text-align: center; color: #888; margin-bottom: 30px;'>Professional Cloud-GPU Accelerated Movie Dubbing Pipeline</p>")
    
    with gr.Tabs():
        with gr.Tab("Studio Workspace"):
            with gr.Row():
                with gr.Column(scale=1):
                    gr.Markdown("### 📥 Input Settings")
                    video_input = gr.Video(label="Upload Source Video", sources=["upload"])
                    
                    with gr.Row():
                        target_language = gr.Dropdown(choices=["Hindi", "English", "Spanish", "French", "German"], value="Hindi", label="Target Language")
                        speaker_toggle = gr.Checkbox(label="Enable Multi-Speaker Detection", value=True)
                        
                    start_button = gr.Button("🚀 START DUBBING", elem_classes="primary", size="lg")
                    
                with gr.Column(scale=2):
                    gr.Markdown("### ⚙️ Pipeline Status")
                    stepper_ui = gr.HTML(get_stepper_html(0))
                    status_output = gr.Textbox(label="Live Engine Logs", lines=10, interactive=False, max_lines=15)
                    
                    with gr.Row():
                        output_video = gr.File(label="📥 Download Output Video", interactive=False)
                        
                    gr.Markdown("### 📊 Quality Dashboard")
                    metrics_ui = gr.HTML(parse_report_metrics(None))
                    
        with gr.Tab("Project History"):
            gr.Markdown("### 📂 Past Projects")
            with gr.Row():
                with gr.Column(scale=1):
                    project_dropdown = gr.Dropdown(choices=load_project_history(), label="Select Project")
                    refresh_btn = gr.Button("🔄 Refresh List")
                with gr.Column(scale=2):
                    history_video = gr.File(label="Project Output Video", interactive=False)
                    history_metrics = gr.HTML(parse_report_metrics(None))
            
            project_dropdown.change(
                fn=load_project_details,
                inputs=[project_dropdown],
                outputs=[history_video, history_metrics]
            )
            refresh_btn.click(
                fn=lambda: gr.update(choices=load_project_history()),
                inputs=[],
                outputs=[project_dropdown]
            )

    start_button.click(
        fn=start_dubbing,
        inputs=[video_input, target_language, speaker_toggle],
        outputs=[stepper_ui, status_output, output_video, metrics_ui]
    )

if __name__ == "__main__":
    demo.launch(inbrowser=True, theme=theme)
