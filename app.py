import gradio as gr
import os
import shutil
import glob
import json
import re
from pipeline import run_pipeline
from telegram_uploader import upload_to_telegram

# Custom CSS for modern dark theme
custom_css = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
body { background-color: #0a0a0f; color: #ededed; font-family: 'Inter', sans-serif; }
.gradio-container { max-width: 1500px !important; }
.stepper-container { background: #0f0f1a; padding: 22px; border-radius: 14px; border: 1px solid #1e1e3a; margin-bottom: 16px; }
.stepper-wrapper { display: flex; justify-content: space-between; align-items: center; position: relative; }
.stepper-line { position: absolute; top: 18px; left: 0; right: 0; height: 2px; background: #1e1e3a; z-index: 1; }
.stepper-step { display: flex; flex-direction: column; align-items: center; gap: 10px; z-index: 2; position: relative; width: 80px; text-align: center; }
.step-circle { width: 36px; height: 36px; border-radius: 50%; background: #1a1a2e; border: 2px solid #2a2a4a; color: #555; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 14px; transition: all 0.4s ease; }
.step-label { font-size: 10px; font-weight: 500; color: #555; transition: all 0.4s ease; }
.step-completed .step-circle { background: #10b981; border-color: #10b981; color: white; box-shadow: 0 0 12px rgba(16,185,129,0.4); }
.step-completed .step-label { color: #10b981; }
.step-active .step-circle { background: #3b82f6; border-color: #3b82f6; color: white; box-shadow: 0 0 18px rgba(59,130,246,0.5); animation: pulse-ring 1.5s ease infinite; }
.step-active .step-label { color: #3b82f6; font-weight: bold; }
.step-error .step-circle { background: #ef4444; border-color: #ef4444; color: white; box-shadow: 0 0 12px rgba(239,68,68,0.4); }
.step-error .step-label { color: #ef4444; }
@keyframes pulse-ring { 0%,100% { box-shadow: 0 0 12px rgba(59,130,246,0.4); } 50% { box-shadow: 0 0 24px rgba(59,130,246,0.8); } }
@keyframes spin { 100% { transform: rotate(360deg); } }
.metric-card { background: #0f0f1a; border: 1px solid #1e1e3a; border-radius: 12px; padding: 20px; text-align: center; display: flex; flex-direction: column; gap: 5px; }
.metric-value { font-size: 28px; font-weight: 800; background: linear-gradient(90deg, #3b82f6, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.metric-label { font-size: 13px; color: #888; text-transform: uppercase; letter-spacing: 1px; }
button.primary { background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%) !important; border: none !important; font-size: 16px !important; font-weight: bold !important; transition: transform 0.2s, box-shadow 0.2s !important; }
button.primary:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(139,92,246,0.5) !important; }
.hb-box { background: linear-gradient(135deg, #0d0d1f 0%, #0a1628 100%); border: 1px solid #1e3a5f; border-radius: 16px; overflow: hidden; margin-top: 8px; box-shadow: 0 4px 24px rgba(59,130,246,0.08); }
.hb-header { background: linear-gradient(90deg, #1a1a3e, #0d2040); padding: 12px 18px; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid #1e3a5f; }
.hb-dot { width:10px; height:10px; border-radius:50%; }
.hb-body { padding: 16px 18px; min-height: 110px; font-size: 14px; line-height: 1.75; color: #c9d9f0; }
.hb-chip { display:inline-block; padding:3px 10px; border-radius:20px; font-size:11px; font-weight:600; margin-bottom:8px; }
.chip-working { background:rgba(59,130,246,0.15); color:#60a5fa; border:1px solid #3b82f6; }
.chip-error { background:rgba(239,68,68,0.15); color:#f87171; border:1px solid #ef4444; }
.chip-success { background:rgba(16,185,129,0.15); color:#34d399; border:1px solid #10b981; }
.chip-idle { background:rgba(107,114,128,0.15); color:#9ca3af; border:1px solid #6b7280; }
.hb-cursor { display:inline-block; width:2px; height:1em; background:#3b82f6; margin-left:2px; animation:blink 1s step-end infinite; vertical-align:text-bottom; }
@keyframes blink { 0%,100%{opacity:1;} 50%{opacity:0;} }
"""

def get_puter_hinglish_html(current_stage: int, has_error: bool, logs: str, is_idle: bool = False, idle_msg: str = "") -> str:
    chip_type = "idle"
    if not is_idle:
        chip_type = "error" if has_error else "working"
        if current_stage == 7 and not has_error:
            chip_type = "success"
            
    labels = {"working": "⚡ Kaam Chal Raha Hai", "error": "❌ Problem Aayi!", "success": "✅ Sab Theek!", "idle": "💤 Idle"}
    dots   = {"working": "#3b82f6", "error": "#ef4444", "success": "#10b981", "idle": "#6b7280"}
    chip_label = labels.get(chip_type, "⚙️ Status")
    chip_cls   = f"chip-{chip_type}"
    dot_col    = dots.get(chip_type, "#6b7280")
    
    import random
    unique_id = f"hinglish-msg-{current_stage}-{int(has_error)}-{random.randint(1000,9999)}"
    
    if is_idle:
        script_js = f"document.getElementById('{unique_id}').innerHTML = '{idle_msg}';"
        safe_logs = ""
    else:
        recent_logs = "\\n".join(logs.strip().split("\\n")[-6:])
        safe_logs = recent_logs.replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;').replace('\\n', ' ')
        script_js = """
(async function() {
  var el = document.getElementById('UNIQUE_ID');
  if (!el) return;
  
  if (typeof puter === 'undefined') {
      await new Promise(r => {
          let s = document.createElement('script');
          s.src = "https://js.puter.com/v2/";
          s.onload = r;
          document.head.appendChild(s);
      });
  }
  
  el.innerHTML = "<span style='color:#888; font-style:italic;'>Puter AI soch raha hai... 💭</span>";
  
  try {
      var prompt = "You are a friendly Indian AI assistant helping a user with a video dubbing tool. Explain the following pipeline log in short, friendly Hinglish (1-2 lines maximum). Tell the user what is currently happening or if there is an error.\\nLogs:\\n" + el.getAttribute('data-logs');
      var response = await puter.ai.chat(prompt);
      
      el.innerHTML = '';
      var text = response.toString();
      var i = 0;
      function tick() {
        if (i >= text.length) return;
        var ch = text[i++];
        if (ch === '\\n') { el.innerHTML += '<br>'; }
        else { el.innerHTML += ch; }
        setTimeout(tick, ch === '.' || ch === '!' || ch === '?' ? 40 : 15);
      }
      tick();
  } catch (e) {
      el.innerHTML = "<span style='color:red;'>Bhai, Puter AI load nahi ho paya. (" + e.toString() + ")</span>";
  }
})();
""".replace("UNIQUE_ID", unique_id)

    return f'''
<div class="hb-box">
  <div class="hb-header">
    <div class="hb-dot" style="background:{dot_col}; box-shadow:0 0 8px {dot_col};"></div>
    <span style="font-size:13px;font-weight:600;color:#8ab4f8;letter-spacing:.5px;">🤖 AI Assistant (Puter.js)</span>
  </div>
  <div class="hb-body">
    <span class="hb-chip {chip_cls}">{chip_label}</span><br>
    <span id="{unique_id}" data-logs="{safe_logs}" style="margin-top:6px;display:inline-block;"></span><span class="hb-cursor"></span>
  </div>
</div>
<script>
{script_js}
</script>
'''


def get_stepper_html(current_stage, has_error=False):
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
            if has_error:
                state_class = "step-error"
                icon = "✕"
            else:
                state_class = "step-active"
                icon = ""  # Spinner handled by CSS ::after
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
        yield get_stepper_html(0), "Error: Please upload a video first.", None, parse_report_metrics(None), get_puter_hinglish_html(0, True, "", False, "❌ Bhai, pehle video upload karo! Koi video select nahi ki gayi.")
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
    has_error = False
    
    last_yielded_stage = -1
    last_yielded_error = False
    
    for status_update in run_pipeline(persisted_video, project_dir, target_lang, speaker_detection):
        stage_match = re.search(r"\[STAGE:(\d)\]", status_update)
        if stage_match:
            current_stage = int(stage_match.group(1))
            status_update = status_update.replace(stage_match.group(0), "")
        
        has_error = "❌" in status_update or ("Error" in status_update and "✅" not in status_update)
        
        logs += status_update
        
        if "Pipeline finished successfully" in status_update:
            current_stage = 7
            has_error = False
            mp4_files = glob.glob(os.path.join(project_dir, "*.mp4"))
            for f in mp4_files:
                if os.path.basename(f) != os.path.basename(video_file):
                    output_video_path = f
                    break
            json_files = glob.glob(os.path.join(project_dir, "report.json"))
            if json_files:
                output_report_path = json_files[0]
                
        if current_stage != last_yielded_stage or has_error != last_yielded_error:
            hinglish_html = get_puter_hinglish_html(current_stage, has_error, logs)
            last_yielded_stage = current_stage
            last_yielded_error = has_error
        else:
            hinglish_html = gr.update()
                
        yield get_stepper_html(current_stage, has_error), logs, output_video_path, parse_report_metrics(output_report_path), hinglish_html

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

def load_tg_config():
    if os.path.exists("config.json"):
        with open("config.json", "r") as f:
            try:
                data = json.load(f)
                return data.get("api_id", ""), data.get("api_hash", ""), data.get("phone", ""), data.get("channel", "")
            except:
                pass
    return "", "", "", ""

def save_tg_config(api_id, api_hash, phone, channel):
    with open("config.json", "w") as f:
        json.dump({
            "api_id": api_id,
            "api_hash": api_hash,
            "phone": phone,
            "channel": channel
        }, f)
    return "✅ Settings saved successfully!"

def do_telegram_upload(video_file):
    if not video_file:
        return "❌ Please upload or dub a video first."
        
    api_id, api_hash, phone, channel = load_tg_config()
    if not api_id or not api_hash or not phone or not channel:
        return "❌ Settings missing! Save configuration in the Telegram Setup tab first."
        
    try:
        # Check if session exists roughly
        if not os.path.exists("telegram_uploader_session.session"):
            return "❌ Session file not found! Pehle terminal mein `python telegram_uploader.py` chalao."
            
        link = upload_to_telegram(video_file, api_id, api_hash, phone, channel)
        return f"✅ **Upload Success!** Link: {link}"
    except Exception as e:
        return f"❌ Upload Failed: {str(e)}"

theme = gr.themes.Monochrome(
    primary_hue="blue", 
    secondary_hue="indigo", 
    neutral_hue="slate"
)

IDLE_HINGLISH = get_puter_hinglish_html(
    0, False, "", True,
    "Video upload karo aur START DUBBING dabao!<br><br>Puter.js AI har step ko automatically read karke tumhe Hinglish mein samjhayega!"
)

with gr.Blocks(title="Tarun Dubber AI", css=custom_css) as demo:
    gr.Markdown("<h1 style='text-align:center; margin-bottom:5px; background:linear-gradient(90deg,#3b82f6,#8b5cf6); -webkit-background-clip:text; -webkit-text-fill-color:transparent;'>🎬 Tarun Dubber AI — Control Panel</h1>")
    gr.Markdown("<p style='text-align:center; color:#555; margin-bottom:24px;'>Professional Cloud-GPU Accelerated Movie Dubbing Pipeline</p>")
    
    with gr.Tabs():
        with gr.Tab("📡 Telegram Setup"):
            gr.Markdown("### ⚙️ Telegram Config (Saves to config.json)")
            gr.Markdown("**Note:** Pehli baar terminal mein `python telegram_uploader.py` chalakar OTP se login karna zaroori hai.")
            
            init_api_id, init_api_hash, init_phone, init_channel = load_tg_config()
            
            with gr.Row():
                tg_api_id = gr.Textbox(label="API ID", value=init_api_id)
                tg_api_hash = gr.Textbox(label="API HASH", type="password", value=init_api_hash)
            with gr.Row():
                tg_phone = gr.Textbox(label="Phone Number (with country code)", value=init_phone)
                tg_channel = gr.Textbox(label="Channel Username (e.g. @tgwebcloud1)", value=init_channel)
            
            save_config_btn = gr.Button("💾 Save Settings", elem_classes="primary")
            config_status = gr.Markdown("")
            
            save_config_btn.click(
                fn=save_tg_config,
                inputs=[tg_api_id, tg_api_hash, tg_phone, tg_channel],
                outputs=[config_status]
            )

        with gr.Tab("🎬 Studio Workspace"):
            with gr.Row():
                with gr.Column(scale=1):
                    gr.Markdown("### 📥 Input Settings")
                    video_input = gr.Video(label="Upload Source Video", sources=["upload"])
                    
                    with gr.Row():
                        target_language = gr.Dropdown(choices=["Hindi", "English", "Spanish", "French", "German"], value="Hindi", label="Target Language")
                        speaker_toggle = gr.Checkbox(label="Enable Multi-Speaker Detection", value=True)
                        
                    start_button = gr.Button("🚀 START DUBBING", elem_classes="primary", size="lg")
                    
                    gr.Markdown("### 🤖 AI Assistant (Hinglish)")
                    hinglish_ui = gr.HTML(IDLE_HINGLISH)
                    
                with gr.Column(scale=2):
                    gr.Markdown("### ⚙️ Pipeline Status")
                    stepper_ui = gr.HTML(get_stepper_html(0))
                    status_output = gr.Textbox(label="Live Engine Logs", lines=12, interactive=False, max_lines=18)
                    
                    with gr.Row():
                        output_video = gr.File(label="📥 Download Output Video", interactive=False)
                        
                    with gr.Row():
                        tg_upload_btn = gr.Button("📤 Upload to Telegram", elem_classes="primary")
                    tg_link_output = gr.Markdown("No upload yet.")
                    
                    tg_upload_btn.click(
                        fn=do_telegram_upload,
                        inputs=[output_video],
                        outputs=[tg_link_output]
                    )
                        
                    gr.Markdown("### 📊 Quality Dashboard")
                    metrics_ui = gr.HTML(parse_report_metrics(None))
                    
        with gr.Tab("📂 Project History"):
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
        outputs=[stepper_ui, status_output, output_video, metrics_ui, hinglish_ui]
    )

if __name__ == "__main__":
    demo.launch(inbrowser=True, theme=theme)
