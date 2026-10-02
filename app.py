import os
import sys
import shutil
import glob
import json
import re
import hashlib
import html
import logging
import uuid
import base64
import ctypes
import datetime
from ctypes import wintypes
import gradio as gr

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(APP_DIR, "projects")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# UI THEME: KOTAEMON
# -------------------------------------------------------------------------
kotaemon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kotaemon-gradio-theme')
if kotaemon_path not in sys.path:
    sys.path.insert(0, kotaemon_path)

try:
    from theme import Kotaemon
    custom_theme = Kotaemon()
except ImportError as e:
    print(f"Failed to import Kotaemon theme: {e}")
    custom_theme = gr.themes.Default()

from pipeline import run_pipeline
from telegram_uploader import upload_to_telegram, SESSION_PATH

# -------------------------------------------------------------------------
# AI ASSISTANT & UI HELPERS
# -------------------------------------------------------------------------

# The HTML container for the Puter.js AI Assistant.
PUTER_AI_HTML = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Baloo+Bhai+2:wght@400;600;800&display=swap');
#puter-container {
  font-family: 'Baloo Bhai 2', cursive;
  padding: 18px; 
  border-radius: 12px; 
  background: var(--background-fill-secondary); 
  border: 2px dashed var(--color-accent); 
  box-shadow: var(--shadow-drop);
}
.load3 .loader {
  font-size: 5px;
  margin: 0px 10px;
  text-indent: -9999em;
  width: 4em;
  height: 4em;
  border-radius: 50%;
  background: var(--color-accent);
  background: -moz-linear-gradient(left, var(--color-accent) 10%, rgba(255, 255, 255, 0) 42%);
  background: -webkit-linear-gradient(left, var(--color-accent) 10%, rgba(255, 255, 255, 0) 42%);
  background: -o-linear-gradient(left, var(--color-accent) 10%, rgba(255, 255, 255, 0) 42%);
  background: -ms-linear-gradient(left, var(--color-accent) 10%, rgba(255, 255, 255, 0) 42%);
  background: linear-gradient(to right, var(--color-accent) 10%, rgba(255, 255, 255, 0) 42%);
  position: relative;
  -webkit-animation: load3 1.4s infinite linear;
  animation: load3 1.4s infinite linear;
  -webkit-transform: translateZ(0);
  -ms-transform: translateZ(0);
  transform: translateZ(0);
  display: inline-block;
  vertical-align: middle;
}
.load3 .loader:before {
  width: 50%;
  height: 50%;
  background: var(--color-accent);
  border-radius: 100% 0 0 0;
  position: absolute;
  top: 0;
  left: 0;
  content: '';
}
.load3 .loader:after {
  background: var(--background-fill-secondary);
  width: 75%;
  height: 75%;
  border-radius: 50%;
  content: '';
  margin: auto;
  position: absolute;
  top: 0;
  left: 0;
  bottom: 0;
  right: 0;
}
@-webkit-keyframes load3 {
  0% { -webkit-transform: rotate(0deg); transform: rotate(0deg); }
  100% { -webkit-transform: rotate(360deg); transform: rotate(360deg); }
}
@keyframes load3 {
  0% { -webkit-transform: rotate(0deg); transform: rotate(0deg); }
  100% { -webkit-transform: rotate(360deg); transform: rotate(360deg); }
}
</style>
<div id="puter-container">
  <div style="display:flex; justify-content:space-between; align-items:center; border-bottom: 1px dashed var(--border-color-primary); padding-bottom: 10px; margin-bottom: 10px;">
    <span style="font-size:18px;font-weight:800;color:var(--color-accent);">🤖 Puter.js Assistant</span>
    <span style="font-size:12px;color:var(--color-accent); font-weight:bold;">● <span id="puter-countdown">Waiting for logs...</span></span>
  </div>
  <div id="puter-message" style="font-size: 16px; line-height: 1.5; min-height: 60px; color: var(--body-text-color);">
    <i>Bhai video daal aur Start daba, phir dekh main har 30 second mein kya mast updates deta hu... 😎</i>
  </div>
</div>
"""

# The JavaScript to interact with Puter.js in the browser context.
# Gradio 6.0 does not allow <script> tags inside gr.HTML, so we inject this via demo.load().
PUTER_JS = """
async () => {
    if (window.puter_fetch_interval) clearInterval(window.puter_fetch_interval);
    if (window.puter_tick_interval) clearInterval(window.puter_tick_interval);
    
    if (typeof puter === 'undefined') {
        await new Promise(resolve => {
            let s = document.createElement('script');
            s.src = "https://js.puter.com/v2/";
            s.onload = resolve;
            s.onerror = resolve;
            document.head.appendChild(s);
        });
    }

    if (typeof puter === 'undefined') {
        var messageEl = document.getElementById("puter-message");
        var statusEl = document.getElementById("puter-countdown");
        if (messageEl) messageEl.textContent = "Puter assistant unavailable; check browser internet access.";
        if (statusEl) statusEl.textContent = "Offline";
        return;
    }

    var countdown = 30;
    window.puter_request_in_flight = window.puter_request_in_flight || false;
    window.puter_last_successful_key = window.puter_last_successful_key || "";

    function updateCountdown() {
        var logBox = document.querySelector("#log_output_box textarea");
        var hasLogs = logBox && logBox.value && logBox.value.trim() !== "";
        
        var countdownEl = document.getElementById("puter-countdown");
        if (countdownEl) {
            if (!hasLogs) {
                countdownEl.innerText = "Waiting for logs...";
                return;
            }
            if (countdown > 0) {
                countdownEl.innerText = "Next update in " + countdown + "s...";
                countdown--;
            }
        }
    }

    async function fetchPuterData() {
        var logBox = document.querySelector("#log_output_box textarea");
        if (!logBox) return;
        var logs = logBox.value;
        if (!logs || logs.trim() === "") return;
        
        // Read persona from the UI
        var persona = "Funny";
        var radioChecked = document.querySelector('#puter_mode_selector input[type="radio"]:checked');
        if (radioChecked) {
            persona = radioChecked.nextElementSibling ? radioChecked.nextElementSibling.innerText : radioChecked.value;
        }

        var recentLogs = logs.split('\\n').slice(-10).join('\\n');
        
        var promptMap = {
            "Funny": "You are a highly entertaining and funny Indian AI assistant helping a user with a video dubbing tool. Read the following logs and explain what is happening in 1 or 2 lines. STRICTLY RESPOND IN HINGLISH (Hindi words written in English alphabet). DO NOT USE DEVNAGARI SCRIPT. Make it casual and funny.\\nLogs:\\n",
            "Serious": "You are a professional DevOps AI assistant. Read the following logs and provide a 1-line status update. STRICTLY RESPOND IN HINGLISH (Hindi words written in English alphabet). DO NOT USE DEVNAGARI SCRIPT.\\nLogs:\\n",
            "Roast": "You are a savage Indian AI assistant who loves roasting the user. Read the logs and explain the status while casually roasting the user in 1-2 lines. STRICTLY RESPOND IN HINGLISH (Hindi words written in English alphabet). DO NOT USE DEVNAGARI SCRIPT.\\nLogs:\\n"
        };
        var promptStr = promptMap[persona] || promptMap["Funny"];
        var prompt = promptStr + recentLogs;
        var requestKey = persona + "\\n" + recentLogs;
        if (window.puter_request_in_flight || requestKey === window.puter_last_successful_key) return;
        window.puter_request_in_flight = true;
        
        try {
            var el = document.getElementById("puter-message");
            var countdownEl = document.getElementById("puter-countdown");
            
            if (el) {
                el.replaceChildren();
                var spinner = document.createElement('span');
                spinner.className = 'load3';
                spinner.textContent = '⏳';
                var loading = document.createElement('span');
                loading.style.color = 'var(--color-accent)';
                loading.style.fontWeight = 'bold';
                loading.textContent = ' Fetching AI insight...';
                el.append(spinner, loading);
            }
            if (countdownEl) {
                countdownEl.innerText = "Analyzing...";
            }
            
            var response = await puter.ai.chat(prompt);
            
            countdown = 30;
            window.puter_last_successful_key = requestKey;
            
            if (el) {
                el.replaceChildren();
                var text = (typeof response === 'object' && response.message) ? response.message.content : String(response);
                text = String(text);
                var i = 0;
                function typeWriter() {
                    if (i >= text.length) return;
                    var ch = text[i++];
                    if (ch === '\\n') { el.appendChild(document.createElement('br')); }
                    else { el.appendChild(document.createTextNode(ch)); }
                    setTimeout(typeWriter, ch === '.' || ch === '!' || ch === '?' ? 30 : 10);
                }
                typeWriter();
            }
        } catch (e) {
            console.error("Puter Error:", e);
            countdown = 10;
        } finally {
            window.puter_request_in_flight = false;
        }
    }
    
    window.puter_tick_interval = setInterval(updateCountdown, 1000);
    window.puter_fetch_interval = setInterval(() => {
        var logBox = document.querySelector("#log_output_box textarea");
        if (logBox && logBox.value && logBox.value.trim() !== "" && countdown <= 0) {
            fetchPuterData();
        }
    }, 1000);
}
"""

def parse_report_metrics(report_path):
    if not report_path or not os.path.exists(report_path):
        return '<div style="color:var(--body-text-color-subdued); text-align:center; padding: 20px;">No report generated yet.</div>'
    try:
        with open(report_path, "r") as f:
            data = json.load(f)
            
        cards_html = '<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; margin-top: 15px;">'
        for k, v in data.items():
            label = html.escape(str(k).replace("_", " "))
            value = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
            value = html.escape(value)
            cards_html += f'''
            <div style="background: var(--background-fill-secondary); border: 1px solid var(--border-color-primary); border-radius: 8px; text-align:center; padding:1.2rem; box-shadow: var(--shadow-drop);">
                <div style="font-size: 26px; font-weight: 800; color: var(--color-accent);">{value}</div>
                <div style="font-size: 13px; color: var(--body-text-color-subdued); text-transform: uppercase; letter-spacing: 1px; margin-top: 5px;">{label}</div>
            </div>
            '''
        cards_html += '</div>'
        return cards_html
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        logger.exception("Could not render report metrics from %s", report_path)
        return '<div style="color:var(--error-text-color); text-align:center;">Failed to parse report.json</div>'


def build_beautiful_link_card(url):
    return f"""
    <style>
    .btn-101, .btn-101 *, .btn-101 :after, .btn-101 :before, .btn-101:after, .btn-101:before {{
      border: 0 solid; box-sizing: border-box;
    }}
    .btn-101 {{
      -webkit-tap-highlight-color: transparent; -webkit-appearance: button;
      background-color: transparent; background-image: none;
      color: var(--color-accent); font-family: inherit; font-size: 100%;
      font-weight: 900; line-height: 1.5; margin: 0;
      -webkit-mask-image: -webkit-radial-gradient(#000, #fff); padding: 0; text-transform: uppercase;
      --thickness: 0.3rem; --roundness: 1.2rem; --color: var(--color-accent); --opacity: 0.6;
      -webkit-backdrop-filter: blur(100px); backdrop-filter: blur(100px);
      background: hsla(0, 0%, 100%, 0.05); border: none; border-radius: var(--roundness);
      cursor: pointer; display: block; font-family: Poppins, "sans-serif";
      font-size: 1rem; font-weight: 500; padding: 0.8rem 3rem; position: relative;
      text-decoration: none; text-align: center;
    }}
    .btn-101:hover {{
      background: hsla(0, 0%, 100%, 0.1); filter: brightness(1.2);
    }}
    .btn-101:active {{
      --opacity: 0; background: hsla(0, 0%, 100%, 0.1);
    }}
    .btn-101 svg {{
      border-radius: var(--roundness); display: block; filter: url(#glow);
      height: 100%; left: 0; position: absolute; top: 0; width: 100%;
    }}
    .btn-101 rect {{
      fill: none; stroke: var(--color); stroke-width: var(--thickness);
      rx: var(--roundness); stroke-linejoin: round; stroke-dasharray: 185%;
      stroke-dashoffset: 80; animation: snake 2s linear infinite; animation-play-state: paused;
      height: 100%; opacity: 0; transition: opacity 0.2s; width: 100%;
    }}
    .btn-101:hover rect {{
      animation-play-state: running; opacity: var(--opacity);
    }}
    @keyframes snake {{ to {{ stroke-dashoffset: 370%; }} }}
    </style>
    
    <div style="margin-top: 15px; padding: 20px; background: var(--background-fill-secondary); border-radius: 12px; border: 1px solid var(--border-color-primary); box-shadow: var(--shadow-drop);">
      <h3 style="margin-top:0; color:var(--body-text-color);">🔥 Active Kaggle Worker</h3>
      <p style="color:var(--body-text-color-subdued); font-size:14px; margin-bottom: 20px;">Kaggle worker ka current status <b>Live Engine Logs</b> mein dekhein. Run fail ho toh yahin par error details aur agla step dikhega.</p>
      <a href="{url}" target="_blank" class="btn-101" style="display:inline-block; text-decoration: none;">
        🚀 Open Kaggle Notebook
        <svg>
          <defs>
            <filter id="glow">
              <fegaussianblur result="coloredBlur" stddeviation="5"></fegaussianblur>
              <femerge>
                <femergenode in="coloredBlur"></femergenode>
                <femergenode in="coloredBlur"></femergenode>
                <femergenode in="coloredBlur"></femergenode>
                <femergenode in="SourceGraphic"></femergenode>
              </femerge>
            </filter>
          </defs>
          <rect />
        </svg>
      </a>
    </div>
    """

# -------------------------------------------------------------------------
# PIPELINE EXECUTION
# -------------------------------------------------------------------------

def _fingerprint_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_project_manifest(project_dir, manifest):
    path = os.path.join(project_dir, "project.json")
    temporary_path = path + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as target:
        json.dump(manifest, target, ensure_ascii=False, indent=2)
    os.replace(temporary_path, path)


def _read_project_manifest(project_dir):
    path = os.path.join(project_dir, "project.json")
    try:
        with open(path, "r", encoding="utf-8") as source:
            return json.load(source)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read project manifest: %s", path)
        return {}


def _manifest_file(project_dir, relative_path):
    if not isinstance(relative_path, str) or not relative_path:
        return None
    path = os.path.abspath(os.path.join(project_dir, relative_path))
    try:
        if os.path.commonpath([os.path.abspath(project_dir), path]) != os.path.abspath(project_dir):
            return None
    except ValueError:
        return None
    return path if os.path.isfile(path) else None


def _project_output_video(project_dir):
    manifest = _read_project_manifest(project_dir)
    output_video = _manifest_file(project_dir, manifest.get("output_video"))
    if output_video:
        return output_video

    # The worker currently writes this fixed filename. Do not treat the
    # uploaded source movie as a completed output when no manifest exists.
    for filename in ("output.mp4", "dubbed.mp4", "final.mp4"):
        candidate = os.path.join(project_dir, filename)
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    return None


def _project_report_path(project_dir):
    manifest = _read_project_manifest(project_dir)
    return _manifest_file(project_dir, manifest.get("report_file")) or os.path.join(project_dir, "report.json")

def start_dubbing(video_file, target_lang, speaker_detection):
    if not video_file:
        yield (
            gr.update(value="❌ Error: Please upload a video first."), 
            gr.update(value=None), 
            gr.update(value=parse_report_metrics(None)), 
            gr.update(value="")
        )
        return
        
    # Handle Gradio 5/6 gr.Video returning dict or tuple instead of string
    if isinstance(video_file, dict):
        video_file = video_file.get("video") or video_file.get("path") or video_file.get("name")
        # If it is STILL a dict (nested), extract path again
        if isinstance(video_file, dict):
            video_file = video_file.get("path") or video_file.get("name") or video_file.get("video")
            
    if isinstance(video_file, tuple) or isinstance(video_file, list):
        video_file = video_file[0]
        
    if not video_file or not isinstance(video_file, str):
        yield (
            gr.update(value="❌ Error: Invalid video format received from UI."), 
            gr.update(value=None), 
            gr.update(value=parse_report_metrics(None)), 
            gr.update(value="")
        )
        return
        
    movie_name = os.path.splitext(os.path.basename(video_file))[0]
    movie_name = re.sub(r'[^A-Za-z0-9_-]', '_', movie_name.replace(' ', '_')).strip('_') or "movie"
    movie_name = movie_name[:64]
    source_fingerprint = _fingerprint_file(video_file)[:12]
    run_id = uuid.uuid4().hex[:10]
    project_id = f"{movie_name}-{source_fingerprint}-{run_id}"
    project_dir = os.path.join(PROJECTS_DIR, project_id)
    os.makedirs(project_dir, exist_ok=True)
    
    persisted_video = os.path.join(project_dir, os.path.basename(video_file))
    if os.path.abspath(video_file) != os.path.abspath(persisted_video):
        shutil.copy(video_file, persisted_video)

    manifest = {
        "project_id": project_id,
        "title": movie_name,
        "run_id": run_id,
        "state": "processing",
        "source_video": os.path.basename(persisted_video),
        "target_language": target_lang,
        "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "output_video": None,
        "report_file": None,
    }
    _write_project_manifest(project_dir, manifest)
    
    logs = ""
    output_video_path = None
    output_report_path = None
    active_url_html = ""
    
    for status_update in run_pipeline(persisted_video, project_dir, target_lang, speaker_detection):
        stage_match = re.search(r"\[STAGE:(\d)\]", status_update)
        if stage_match:
            status_update = status_update.replace(stage_match.group(0), "")
            
        match = re.search(r"Kaggle GPU Worker \(([^)]+)\)", status_update)
        if match:
            kernel_id = match.group(1)
            url = f"https://www.kaggle.com/code/{kernel_id}"
            active_url_html = build_beautiful_link_card(url)
            
        if "Open https://www.kaggle.com/code/" in status_update:
            # We already showed the active URL card, so we can ignore the plain text link in the logs if we want, or let it be.
            pass
        
        logs += status_update
        
        if "Pipeline finished successfully" in status_update:
            output_video_path = _project_output_video(project_dir)
            report_candidate = _project_report_path(project_dir)
            output_report_path = report_candidate if os.path.isfile(report_candidate) else None
            manifest["state"] = "success" if output_video_path else "failed_missing_output"
            manifest["output_video"] = os.path.relpath(output_video_path, project_dir) if output_video_path else None
            manifest["report_file"] = os.path.relpath(output_report_path, project_dir) if output_report_path else None
            _write_project_manifest(project_dir, manifest)
                
        yield (
            gr.update(value=logs), 
            gr.update(value=output_video_path), 
            gr.update(value=parse_report_metrics(output_report_path)), 
            gr.update(value=active_url_html)
        )

    if manifest["state"] == "processing":
        manifest["state"] = "failed"
        _write_project_manifest(project_dir, manifest)

def load_project_history():
    if not os.path.exists(PROJECTS_DIR):
        return []
    choices = []
    for project_id in os.listdir(PROJECTS_DIR):
        project_dir = os.path.join(PROJECTS_DIR, project_id)
        if not os.path.isdir(project_dir):
            continue
        manifest = _read_project_manifest(project_dir)
        title = manifest.get("title") or project_id
        created = manifest.get("created_at", "")
        label = f"{title} · {created or project_id} · {manifest.get('state', 'legacy')}"
        choices.append((label, project_id))
    return sorted(choices, key=lambda item: item[1], reverse=True)

def load_project_details(project_name):
    if not project_name:
        return None, parse_report_metrics(None)
        
    project_dir = os.path.join(PROJECTS_DIR, os.path.basename(project_name))
    output_video = _project_output_video(project_dir)

    report_path = _project_report_path(project_dir)
    if not os.path.exists(report_path):
        report_path = None
        
    return output_video, parse_report_metrics(report_path)

class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi_transform(data, protect):
    if os.name != "nt":
        raise RuntimeError("Secure API-hash storage currently requires Windows DPAPI.")
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    if protect:
        transform = crypt32.CryptProtectData
        transform.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
                              ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                              ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(_DataBlob)]
        transform.restype = wintypes.BOOL
        succeeded = transform(ctypes.byref(source), "T_Dubber Telegram API hash",
                              None, None, None, 0x1, ctypes.byref(destination))
        description = None
    else:
        transform = crypt32.CryptUnprotectData
        description = wintypes.LPWSTR()
        transform.argtypes = [ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR),
                              ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                              ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(_DataBlob)]
        transform.restype = wintypes.BOOL
        succeeded = transform(ctypes.byref(source), ctypes.byref(description),
                              None, None, None, 0x1, ctypes.byref(destination))
    if not succeeded:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        local_free(ctypes.cast(destination.pbData, ctypes.c_void_p))
        if description:
            local_free(ctypes.cast(description, ctypes.c_void_p))


def _protect_api_hash(api_hash):
    if not api_hash:
        return ""
    encrypted = _dpapi_transform(api_hash, protect=True)
    return "dpapi:v1:" + base64.b64encode(encrypted).decode("ascii")


def _unprotect_api_hash(value):
    if not value:
        return ""
    if not value.startswith("dpapi:v1:"):
        raise ValueError("Telegram API hash is not stored in the protected format.")
    encrypted = base64.b64decode(value.removeprefix("dpapi:v1:"), validate=True)
    return _dpapi_transform(encrypted, protect=False).decode("utf-8")


def _write_tg_config(data):
    temporary_path = CONFIG_PATH + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as target:
        json.dump(data, target, ensure_ascii=False, indent=2)
    os.replace(temporary_path, CONFIG_PATH)


def load_tg_config():
    if not os.path.exists(CONFIG_PATH):
        return "", "", "", ""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as source:
            data = json.load(source)
        protected_hash = data.get("api_hash_protected", "")
        if protected_hash:
            api_hash = _unprotect_api_hash(protected_hash)
        else:
            # Migrate an old plaintext config as soon as it is read.
            api_hash = data.get("api_hash", "")
            if api_hash:
                data["api_hash_protected"] = _protect_api_hash(api_hash)
                data.pop("api_hash", None)
                _write_tg_config(data)
        return data.get("api_id", ""), api_hash, data.get("phone", ""), data.get("channel", "")
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        logger.exception("Could not load Telegram settings from %s", CONFIG_PATH)
        return "", "", "", ""


def save_tg_config(api_id, api_hash, phone, channel):
    try:
        _write_tg_config({
            "api_id": api_id,
            "api_hash_protected": _protect_api_hash(api_hash),
            "phone": phone,
            "channel": channel,
        })
        return "✅ Settings saved; API hash is encrypted for this Windows account."
    except (OSError, RuntimeError, ValueError) as exc:
        logger.exception("Could not save Telegram settings")
        return f"❌ Settings could not be saved securely: {exc}"

def do_telegram_upload(video_file):
    if not video_file:
        return "❌ Please upload or dub a video first."
        
    api_id, api_hash, phone, channel = load_tg_config()
    if not api_id or not api_hash or not phone or not channel:
        return "❌ Settings missing! Save configuration in the Telegram Setup tab first."
        
    try:
        if not os.path.exists(SESSION_PATH + ".session"):
            return "❌ Session file not found! Pehle terminal mein `python telegram_uploader.py` chalao."
            
        link = upload_to_telegram(video_file, api_id, api_hash, phone, channel)
        return f"✅ **Upload Success!** Link: {link}"
    except Exception as e:
        return f"❌ Upload Failed: {str(e)}"


# -------------------------------------------------------------------------
# GRADIO INTERFACE
# -------------------------------------------------------------------------

# Moved theme out of Blocks constructor for Gradio 6.0+ compatibility
with gr.Blocks(title="Tarun Dubber AI") as demo:

    gr.Markdown(
        """
        # 🎬 Tarun Dubber AI
        ### Movie dubbing on Kaggle GPU with a local Homura translation model
        """
    )
    
    with gr.Tabs():
        # --- TAB 1: Studio Workspace ---
        with gr.Tab("🎬 Studio Workspace"):
            gr.Markdown(
                "**No external API key needed.** Translation runs on the Kaggle GPU with Index-Homura; "
                "the first run may take longer while the model downloads. Your Kaggle worker link appears in the status panel."
            )
            with gr.Row():
                with gr.Column(scale=1):
                    with gr.Group():
                        video_input = gr.Video(label="Upload Source Video", sources=["upload"])
                        target_language = gr.Dropdown(
                            choices=["Hindi", "English", "Spanish", "French", "German"], 
                            value="Hindi", 
                            label="Target Language"
                        )
                        speaker_toggle = gr.Checkbox(label="Enable Multi-Speaker Detection", value=True)
                        start_button = gr.Button("🚀 Start Dubbing", variant="primary", size="lg")
                
                with gr.Column(scale=2):
                    status_output = gr.Textbox(
                        label="Live Progress & Kaggle Worker Link", 
                        lines=15, 
                        interactive=False, 
                        max_lines=20,
                        elem_id="log_output_box"
                    )
                    
                    action_links_output = gr.HTML("")
                    
                    puter_mode = gr.Radio(
                        choices=["Funny", "Serious", "Roast"], 
                        value="Funny", 
                        label="🤖 Puter.js Persona Mode", 
                        elem_id="puter_mode_selector",
                        interactive=True
                    )
                    
                    gr.HTML(PUTER_AI_HTML)
                    
                    with gr.Group():
                        output_video = gr.File(label="Download Output Video", interactive=False)
                        with gr.Row():
                            tg_upload_btn = gr.Button("📤 Upload to Telegram", variant="secondary")
                            tg_link_output = gr.Markdown("No upload yet.")
                        
                        tg_upload_btn.click(
                            fn=do_telegram_upload,
                            inputs=[output_video],
                            outputs=[tg_link_output]
                        )
                        
                    gr.Markdown("### 📊 Quality Dashboard")
                    metrics_ui = gr.HTML(parse_report_metrics(None))

        # --- TAB 2: Telegram Setup ---
        with gr.Tab("📡 Telegram Setup"):
            gr.Markdown("### ⚙️ Telegram Configuration")
            gr.Markdown("Pehli baar terminal mein `python telegram_uploader.py` chalakar OTP se login karna zaroori hai. This saves to `config.json`.")
            
            with gr.Group():
                init_api_id, init_api_hash, init_phone, init_channel = load_tg_config()
                with gr.Row():
                    tg_api_id = gr.Textbox(label="API ID", value=init_api_id)
                    tg_api_hash = gr.Textbox(label="API HASH", type="password", value=init_api_hash)
                with gr.Row():
                    tg_phone = gr.Textbox(label="Phone Number (with country code)", value=init_phone)
                    tg_channel = gr.Textbox(label="Channel Username (e.g. @tgwebcloud1)", value=init_channel)
                
                save_config_btn = gr.Button("💾 Save Settings", variant="primary")
                config_status = gr.Markdown("")
                
                save_config_btn.click(
                    fn=save_tg_config,
                    inputs=[tg_api_id, tg_api_hash, tg_phone, tg_channel],
                    outputs=[config_status]
                )

        # --- TAB 3: Project History ---
        with gr.Tab("📂 Project History"):
            gr.Markdown("### 📂 Past Projects")
            with gr.Group():
                with gr.Row():
                    with gr.Column(scale=1):
                        project_dropdown = gr.Dropdown(choices=load_project_history(), label="Select Project")
                        refresh_btn = gr.Button("🔄 Refresh List", variant="secondary")
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
        outputs=[status_output, output_video, metrics_ui, action_links_output]
    )

    # Attach the JavaScript for Puter AI here (fixes HTML script warning and guarantees execution)
    demo.load(js=PUTER_JS)

if __name__ == "__main__":
    # In Gradio 6.0, theme goes in launch()
    demo.launch(inbrowser=True, theme=custom_theme)
