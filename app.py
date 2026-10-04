import os
import sys
import queue
import shutil
import json
import random
import re
import time
import hashlib
import html
import logging
import uuid
import base64
import ctypes
import datetime
import threading
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

from pipeline import run_pipeline, reattach_to_kernel, kernel_id_for
from telegram_uploader import (
    CHUNK_SIZE,
    SESSION_PATH,
    TelegramCloudError,
    download_or_restore,
    human_bytes,
    list_uploads,
    plan_chunks,
    upload_file_detailed,
)
import db
import link_resolver

# One-time import of historical state on first run, so the database is useful
# immediately rather than only for runs started from here on.
_DB_BOOTSTRAPPED = False


def _ensure_db_seeded():
    """Import existing projects and Telegram journals exactly once per process."""
    global _DB_BOOTSTRAPPED
    if _DB_BOOTSTRAPPED:
        return
    _DB_BOOTSTRAPPED = True
    try:
        if db.stats()["projects"] == 0:
            db.import_existing_projects(PROJECTS_DIR)
    except Exception:  # noqa: BLE001 - never block startup on the database
        logger.exception("Could not seed the database from existing projects")

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
<style>
  /* Persona chips: make the Gradio radio options look like pill buttons */
  #puter_mode_selector .wrap { display: flex; flex-wrap: wrap; gap: 8px; }
  #puter_mode_selector label {
    border: 1px solid var(--border-color-primary);
    border-radius: 999px; padding: 6px 14px; cursor: pointer;
    background: var(--background-fill-secondary);
    transition: background 0.2s, border-color 0.2s, transform 0.1s;
  }
  #puter_mode_selector label:hover { transform: translateY(-1px); border-color: var(--color-accent); }
  #puter_mode_selector input[type="radio"] { accent-color: var(--color-accent); margin-right: 6px; }
  #puter_mode_selector input[type="radio"]:checked + span { font-weight: 800; color: var(--color-accent); }

  #puter-container { font-family: 'Baloo Bhai 2', cursive; padding: 18px; border-radius: 12px; background: var(--background-fill-secondary); border: 2px dashed var(--color-accent); box-shadow: var(--shadow-drop); max-width: 100%; }
  @media (max-width: 640px) {
    #puter-container { padding: 12px; font-size: 14px; }
    #puter-history { max-height: 180px !important; }
  }
  .puter-chip-btn {
    cursor: pointer; border: 1px solid var(--color-accent); background: transparent;
    color: var(--color-accent); border-radius: 999px; padding: 4px 12px; font-size: 12px; font-weight: 700;
  }
  .puter-chip-btn:hover { background: var(--color-accent); color: var(--background-fill-secondary); }
  #puter-error { display:none; background: var(--error-background-fill, rgba(255,80,80,0.12)); color: var(--error-text-color, #d33); border: 1px solid var(--error-text-color, #d33); border-radius: 8px; padding: 8px 12px; margin-bottom: 10px; }
  #puter-history div { border-bottom: 1px dashed var(--border-color-primary); padding: 6px 0; font-size: 13px; line-height: 1.4; }
</style>
<div id="puter-container">
  <div style="display:flex; justify-content:space-between; align-items:center; border-bottom: 1px dashed var(--border-color-primary); padding-bottom: 10px; margin-bottom: 10px; flex-wrap:wrap; gap:6px;">
    <span style="font-size:18px;font-weight:800;color:var(--color-accent);">🤖 Puter.js Assistant</span>
    <span style="font-size:12px;color:var(--color-accent); font-weight:bold;">● <span id="puter-countdown">Waiting for logs...</span></span>
  </div>
  <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px;">
    <button id="puter-refresh-btn" class="puter-chip-btn">🔄 Refresh now</button>
    <button id="puter-copy-btn" class="puter-chip-btn">📋 Copy latest</button>
    <button id="puter-scrolllogs-btn" class="puter-chip-btn">📜 Go to logs</button>
  </div>
  <div id="puter-error">⚠️ AI insight fetch nahi ho paya. Internet check karo. <button id="puter-retry-btn" class="puter-chip-btn" style="margin-left:8px;">Retry</button></div>
  <div id="puter-message" style="font-size: 16px; line-height: 1.5; min-height: 60px; color: var(--body-text-color);">
    <i>💡 Video daalo, Start dabao — phir har 30s me main Hinglish me mast updates dunga. Persona bhi badal sakte ho! 😎</i>
  </div>
  <div style="margin-top:10px; border-top:1px dashed var(--border-color-primary); padding-top:8px;">
    <div style="font-size:12px; font-weight:800; color:var(--color-accent); margin-bottom:4px;">🕒 Recent updates</div>
    <div id="puter-history" style="max-height:220px; overflow-y:auto; font-size:13px;"><i style="color:var(--body-text-color-subdued);">Abhi koi update nahi.</i></div>
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
            var errEl = document.getElementById("puter-error");
            if (errEl) errEl.style.display = "none";
            var el = document.getElementById("puter-message");
            var text = (typeof response === 'object' && response.message) ? response.message.content : String(response);
            text = String(text);
            window.puter_last_text = text;
            // push to history (max 5), newest first
            window.puter_history = window.puter_history || [];
            var ts = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
            window.puter_history.unshift({persona: persona, ts: ts, text: text});
            if (window.puter_history.length > 5) window.puter_history.pop();
            var histEl = document.getElementById("puter-history");
            if (histEl) {
                histEl.replaceChildren();
                window.puter_history.forEach(function(h){
                    var d = document.createElement('div');
                    d.innerHTML = '<b>[' + h.ts + '] ' + h.persona + ':</b> ' + h.text.replace(/</g,'&lt;');
                    histEl.appendChild(d);
                });
            }
            if (el) {
                el.replaceChildren();
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
            var errEl = document.getElementById("puter-error");
            if (errEl) errEl.style.display = "block";
        } finally {
            window.puter_request_in_flight = false;
        }
    }

    // Wire up buttons
    var rb = document.getElementById("puter-refresh-btn"); if (rb) rb.onclick = function(){ countdown = 0; fetchPuterData(); };
    var rt = document.getElementById("puter-retry-btn"); if (rt) rt.onclick = function(){ var e=document.getElementById("puter-error"); if(e) e.style.display="none"; countdown = 0; fetchPuterData(); };
    var cp = document.getElementById("puter-copy-btn"); if (cp) cp.onclick = function(){ if(window.puter_last_text && navigator.clipboard) navigator.clipboard.writeText(window.puter_last_text); };
    var sc = document.getElementById("puter-scrolllogs-btn"); if (sc) sc.onclick = function(){ var b=document.getElementById("log_output_box"); if(b) b.scrollIntoView({behavior:'smooth'}); };

    // Add emoji prefixes to persona chips
    var emojiMap = {"Funny":"😄","Serious":"🧐","Roast":"🔥"};
    document.querySelectorAll('#puter_mode_selector label span').forEach(function(s){
        var t = s.textContent.trim();
        if (emojiMap[t] && s.textContent.indexOf(emojiMap[t]) === -1) s.textContent = emojiMap[t] + " " + t;
    });

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
# TELEGRAM CLOUD + SOURCE LINK HELPERS
# -------------------------------------------------------------------------


def _tg_credentials():
    """Return the saved Telegram settings, or None if incomplete."""
    api_id, api_hash, phone, channel = load_tg_config()
    if not (api_id and api_hash and phone and channel):
        return None
    return {
        "api_id": api_id,
        "api_hash": api_hash,
        "phone": phone,
        "channel": channel,
    }


CHANNELS_FILE = os.path.join(APP_DIR, "channels.json")

DEFAULT_CHANNELS = {
    "channels": [],
    "strategy": "round_robin",
    "default_index": 0,
}


def load_channels() -> dict:
    """Read the multi-channel configuration, falling back to a single channel.

    A community of several channels needs more than one configured name, but the
    file must never be the reason an upload cannot start, so any problem falls
    back to the legacy single ``channel`` value from config.json.
    """
    data = dict(DEFAULT_CHANNELS)
    try:
        with open(CHANNELS_FILE, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            data.update(loaded)
    except (OSError, json.JSONDecodeError):
        pass

    if not data.get("channels"):
        fallback = load_tg_config()[3]
        data["channels"] = [fallback] if fallback else []
    data["channels"] = [
        str(name).strip() for name in data["channels"] if str(name).strip()
    ]
    if data.get("strategy") not in ("round_robin", "least_used", "random", "fixed"):
        data["strategy"] = "round_robin"
    return data


def _save_channels(data: dict) -> None:
    temporary = CHANNELS_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, CHANNELS_FILE)


def pick_channel(strategy: str = None, filename: str = None) -> str:
    """Choose which channel the next upload should go to.

    With one community split across several channels, the choice matters for two
    reasons: spreading load, and keeping a single channel from becoming the one
    that is full or rate-limited. Round-robin is the default because it is
    predictable, which makes it obvious where a given upload went.
    """
    config = load_channels()
    channels = config["channels"]
    if not channels:
        return ""
    if len(channels) == 1:
        return channels[0]

    strategy = strategy or config.get("strategy") or "round_robin"

    if strategy == "random":
        return random.choice(channels)
    if strategy == "fixed":
        index = int(config.get("default_index") or 0)
        return channels[index % len(channels)]
    if strategy == "least_used":
        # Base the choice on what the database knows, not on the last N files.
        _ensure_db_seeded()
        try:
            counts = {row["channel"]: row["uploads"] for row in db.channel_usage()}
        except Exception:  # noqa: BLE001
            logger.exception("Could not read per-channel counts")
            counts = {}
        return min(channels, key=lambda name: (counts.get(name, 0), name))
    return _next_round_robin(channels)


_ROUND_ROBIN_LOCK = threading.Lock()
_ROUND_ROBIN_STATE = {"index": 0}


def _next_round_robin(channels: list) -> str:
    with _ROUND_ROBIN_LOCK:
        index = _ROUND_ROBIN_STATE["index"] % len(channels)
        _ROUND_ROBIN_STATE["index"] = (index + 1) % len(channels)
    return channels[index]


def save_channels(text: str, strategy: str, default_index: int) -> str:
    """Persist the channel list entered in the UI."""
    names = []
    seen = set()
    for line in (text or "").splitlines():
        cleaned = line.strip().lstrip("@").strip()
        # Telegram usernames are case-insensitive, so "TGWebCloud1" and
        # "@tgwebcloud1" are the same channel and must not become two entries.
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            names.append("@" + cleaned)
    if not names:
        return "❌ Enter at least one channel, like @tgwebcloud1"
    try:
        _save_channels({
            "channels": names,
            "strategy": strategy if strategy in
            ("round_robin", "least_used", "random", "fixed") else "round_robin",
            "default_index": int(default_index or 0),
        })
        return (
            f"✅ {len(names)} channel(s) saved, strategy = {strategy}. "
            "New uploads will spread across them."
        )
    except OSError as exc:
        logger.exception("Could not save the channel list")
        return f"❌ Could not save channels: {exc}"


def tg_backup_ready() -> bool:
    return _tg_credentials() is not None


def _coerce_video_path(video_file):
    """Gradio 5/6 hands gr.Video results back as a dict, tuple or plain path."""
    for _attempt in range(3):
        if isinstance(video_file, dict):
            video_file = (
                video_file.get("video")
                or video_file.get("path")
                or video_file.get("name")
            )
        elif isinstance(video_file, (tuple, list)):
            video_file = video_file[0] if video_file else None
        else:
            break
    return video_file if isinstance(video_file, str) and video_file else None


class _BackgroundJob:
    """Run a blocking upload/download on a worker thread and stream its events.

    The archive engine reports progress through a plain callback, but Gradio
    needs to ``yield`` it to redraw the progress bar. Bridging the two needs a
    thread plus a queue; wrapping it once here keeps every caller free of that
    boilerplate and, unlike collecting events before returning, the UI still
    updates live during a multi-hour transfer.

    Events are throttled on the way through. Telethon fires its progress
    callback per network chunk, so a 9 GB upload produces tens of thousands of
    callbacks; forwarding every one would make Gradio redraw the whole log
    panel tens of thousands of times. Discrete messages (a part finishing, a
    flood wait) always pass; byte-level updates pass only when they either move
    the percentage enough or arrive far enough apart in time.
    """

    MIN_INTERVAL_SECONDS = 1.0
    MIN_PERCENT_DELTA = 1.0

    def __init__(self, worker, min_interval=None, min_percent_delta=None):
        self._worker = worker
        self._min_interval = (
            self.MIN_INTERVAL_SECONDS if min_interval is None else min_interval
        )
        self._min_delta = (
            self.MIN_PERCENT_DELTA if min_percent_delta is None else min_percent_delta
        )
        self.result = None
        self.error = None
        self._events = queue.Queue()
        self._last_emit = 0.0
        self._last_percent = None
        self._last_note = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _should_emit(self, payload):
        """Decide whether this event is worth handing to the UI.

        Message-carrying events are discrete milestones, so they always pass
        (identical repeats are collapsed). Byte-level events carry no message
        at all, so they are gated twice: the percentage must actually move, and
        enough time must have elapsed. Both gates have to be satisfied -- an
        ``and`` here lets a fast transfer through on every single callback,
        which is what floods the log with hundreds of empty lines.
        """
        note = payload.get("message")
        now = time.monotonic()

        if note:
            if note == self._last_note:
                return False
            self._last_note = note
            self._last_emit = now
            return True

        total = payload.get("total") or 0
        if not total:
            # Without a total there is no percentage to gate on, so fall back
            # to the time gate alone.
            if now - self._last_emit < self._min_interval:
                return False
            self._last_emit = now
            return True

        percent = round((payload.get("current") or 0) / total * 100.0, 2)
        moved = self._last_percent is None or abs(percent - self._last_percent) >= self._min_delta
        waited = (now - self._last_emit) >= self._min_interval
        if not (moved and waited):
            return False

        self._last_percent = percent
        self._last_emit = now
        return True

    def _report(self, payload):
        if self._should_emit(payload):
            self._events.put(payload)

    def _run(self):
        try:
            self.result = self._worker(self._report)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            self.error = exc
        self._events.put(None)  # sentinel closes the stream

    def events(self):
        """Yield every progress payload until the worker finishes."""
        while True:
            payload = self._events.get()
            if payload is None:
                return
            yield payload

    def check(self):
        """Re-raise whatever the worker failed with, then return its result."""
        if self.error is not None:
            raise self.error
        return self.result


def _progress_bar_html(percent, current, total, label=""):
    percent = max(0.0, min(100.0, float(percent)))
    safe_label = html.escape(str(label))
    return f"""
    <div style="width: 100%; background-color: var(--background-fill-secondary);
                border: 1px solid var(--border-color-primary); border-radius: 12px;
                margin-top: 12px; overflow: hidden;
                box-shadow: inset 0 2px 4px rgba(0,0,0,0.1);">
      <div style="width: {percent:.2f}%;
                  background: linear-gradient(90deg, #1e3c72 0%, #2a5298 100%);
                  height: 40px; transition: width 0.15s linear;
                  display: flex; align-items: center; justify-content: center;
                  color: white; font-weight: bold; font-size: 16px;">
        {percent:.1f}%
      </div>
    </div>
    <p style="text-align: center; margin-top: 8px; font-size: 14px;
              color: var(--body-text-color);">
      {safe_label}
      <b>{human_bytes(current)}</b> / <b>{human_bytes(total)}</b>
    </p>
    """


def _link_card(link, title, subtitle, accent="#28a745"):
    if not link:
        return ""
    safe_link = html.escape(str(link), quote=True)
    safe_title = html.escape(str(title))
    safe_subtitle = html.escape(str(subtitle))
    return f"""
    <div style="padding: 14px 16px; background: rgba(40,167,69,0.08);
                border-left: 5px solid {accent}; border-radius: 8px; margin-top: 12px;">
      <div style="font-weight: 700; color: {accent};">{safe_title}</div>
      <div style="font-size: 13px; color: var(--body-text-color-subdued);
                  margin: 4px 0 10px;">{safe_subtitle}</div>
      <a href="{safe_link}" target="_blank" rel="noopener"
         style="display: inline-block; padding: 8px 16px; background: {accent};
                color: white; text-decoration: none; border-radius: 5px;
                font-weight: 600; font-size: 14px;">
        Open in Telegram
      </a>
    </div>
    """


# -------------------------------------------------------------------------
# SOURCE RESOLUTION  (paste a link instead of uploading a file)
# -------------------------------------------------------------------------


def check_link_status(url):
    """Validate a pasted link and preview what it is, without downloading.

    Returns HTML only. A ``(html, ok)`` tuple would be read by Gradio as
    ``(value, props)``, and the stray boolean surfaces in the panel as a stray
    ".True" fragment.
    """
    url = (url or "").strip()
    if not url:
        return '<div style="color:var(--body-text-color-subdued);">Paste a link to check it.</div>'

    verdict = link_resolver.classify(url)
    if not verdict.ok:
        badge = "unsafe" if verdict.kind == "unsafe" else "unsupported"
        colour = "#dc2626" if badge == "unsafe" else "#d97706"
        return (
            f'<div style="padding:12px 14px; border-left:4px solid {colour};'
            f' background:rgba(220,38,38,0.06); border-radius:6px; margin-top:8px;">'
            f'<b style="color:{colour};">Not usable ({badge})</b>'
            f'<div style="font-size:13px; margin-top:6px; color:var(--body-text-color);">'
            f'{html.escape(verdict.reason)}</div></div>'
        )

    if verdict.kind == "telegram":
        return (
            f'<div style="padding:12px 14px; border-left:4px solid #28a745;'
            f' background:rgba(40,167,69,0.08); border-radius:6px; margin-top:8px;">'
            f'<b style="color:#28a745;">Telegram archive link</b>'
            f'<div style="font-size:13px; margin-top:6px;">'
            f'{html.escape(verdict.reason)}</div></div>'
        )

    info = link_resolver.probe(url, _tg_credentials())
    if not info.get("ok"):
        return (
            f'<div style="padding:12px 14px; border-left:4px solid #dc2626;'
            f' background:rgba(220,38,38,0.06); border-radius:6px; margin-top:8px;">'
            f'<b style="color:#dc2626;">{html.escape(verdict.label)} link found, '
            f'but it could not be read</b>'
            f'<pre style="white-space:pre-wrap; font-size:12px; margin-top:6px;">'
            f'{html.escape(str(info.get("reason", "unknown error")))}</pre></div>'
        )

    duration = info.get("duration") or 0
    duration_text = f" &middot; {int(duration // 60)}m {int(duration % 60)}s" if duration else ""
    warning_html = "".join(
        f'<div style="font-size:12px; color:#d97706; margin-top:4px;">⚠ {html.escape(w)}</div>'
        for w in info.get("warnings") or []
    )
    return (
        f'<div style="padding:12px 14px; border-left:4px solid #28a745;'
        f' background:rgba(40,167,69,0.08); border-radius:6px; margin-top:8px;">'
        f'<b style="color:#28a745;">{html.escape(verdict.label)}</b>'
        f'<div style="font-size:14px; font-weight:600; margin-top:4px;">'
        f'{html.escape(str(info.get("title", "Untitled")))}</div>'
        f'<div style="font-size:12px; color:var(--body-text-color-subdued);">'
        f'{html.escape(str(info.get("size_label", "unknown size")))}'
        f'{duration_text} &middot; via {html.escape(str(info.get("extractor", "?")))}</div>'
        f'{warning_html}</div>'
    )


def _transfer_log_line(payload, stage="STAGE:0"):
    """Render one transfer event as a log line, or None if it says nothing new.

    Byte-level progress events carry no ``message`` at all. Yielding them
    verbatim produced a bare ``[STAGE:0]`` per callback, which flooded the log
    with hundreds of empty lines during a transfer. They are rendered as a
    compact percentage line instead, and _BackgroundJob has already throttled
    how often they arrive.
    """
    message = payload.get("message")
    if message:
        return f"[{stage}] {message}\n"

    total = int(payload.get("total") or 0)
    if not total:
        return None

    current = int(payload.get("current") or 0)
    percent = min(100.0, current / total * 100.0)
    chunk_index = payload.get("chunk_index")
    chunk_count = payload.get("chunk_count")
    prefix = (
        f"part {chunk_index}/{chunk_count} · " if chunk_index and chunk_count else ""
    )
    verb = "restored" if payload.get("phase") == "restore" else "transferred"
    return (
        f"[{stage}] ⬆️ {prefix}{percent:.1f}% {verb} "
        f"({human_bytes(current)} / {human_bytes(total)})\n"
    )


def _resolve_link_step(source_url, workdir):
    """Download a pasted link into ``workdir``, yielding live log lines.

    Returns the ResolvedMedia as the generator's return value.
    """
    creds = _tg_credentials()

    def work(report):
        return link_resolver.resolve_to_local_file(
            source_url,
            workdir,
            telegram_credentials=creds,
            max_height=720,
            progress_callback=report,
        )

    job = _BackgroundJob(work)
    for payload in job.events():
        line = _transfer_log_line(payload)
        if line:
            yield line
    return job.check()


def _archive_to_db(journal: dict, project_id: str = None):
    """Mirror a completed or in-flight Telegram upload into the database.

    The archive id is returned so the run can be linked to it, which is what
    makes "which movies are protected on Telegram?" a query rather than a folder
    listing. Failures are logged, never raised: losing the index is recoverable,
    losing the upload is not.
    """
    try:
        if not journal.get("fingerprint"):
            file_path = journal.get("file_path")
            if file_path and os.path.isfile(file_path):
                journal["fingerprint"] = _fingerprint_file(file_path)[:32]
            else:
                channel = journal.get("channel") or db.UNKNOWN_CHANNEL
                journal["fingerprint"] = (
                    f"{journal.get('filename')}@{journal.get('size')}@{channel}"
                )
        archive_id = db.upsert_archive(journal)
        if project_id:
            db.attach_archive_to_project(project_id, archive_id)
        return archive_id
    except Exception:  # noqa: BLE001
        logger.exception("Could not record archive %s in the database",
                         journal.get("filename"))
        return None


def _telegram_backup_step(
    source_path, creds, source_url, project_id=None, media=None, channel=None
):
    """Archive the source on Telegram and return the manifest link.

    Runs before the Kaggle handoff on purpose. Once the dataset is on Kaggle the
    job survives a dead PC, but until that point the only copy lives here.
    Putting the archive first means there is always a durable, checksummed copy
    with a link the user can hand to anyone, and the pipeline can be restarted
    from it later without the original file.
    """
    if source_url and link_resolver.classify(source_url).kind == "telegram":
        yield "[STAGE:0] ☁️ Source already lives in Telegram; using that archive as the backup instead of uploading it twice.\n"
        return source_url

    caption = None
    thumbnail = None
    if media is not None:
        try:
            caption = link_resolver.build_caption(media, kind="source")
            thumbnail = getattr(media, "thumbnail_path", None)
        except Exception:  # noqa: BLE001 - a caption is never worth failing over
            logger.exception("Could not build the archive caption")

    def work(report):
        # SPEED POLICY (AI/dev note): opt into the Go parallel uploader.
        # Multi-part public-channel files go via `tgdub.exe upload x4`
        # (overlapping streams close the TCP BDP gap); single-part /
        # private-channel / thumbnail files auto-fall-back to Telethon
        # inside upload_file_detailed(). Machine-level speed (CPU/TCP/QoS)
        # comes from boost.ps1 / start-boosted.ps1 on Windows.
        return upload_file_detailed(
            source_path,
            creds["api_id"],
            creds["api_hash"],
            creds["phone"],
            channel or creds["channel"],
            caption=caption,
            thumbnail_path=thumbnail,
            progress_callback=report,
            resume=True,
            reuse_completed=True,
            use_go=True,
            go_concurrency=4,
            # Mirror progress into the database as parts land, so a crash still
            # leaves a queryable record of what exists on Telegram.
            on_journal=lambda snapshot: _archive_to_db(snapshot, project_id),
        )

    job = _BackgroundJob(work)
    for payload in job.events():
        line = _transfer_log_line(payload)
        if line:
            yield line
        if payload.get("phase") in ("part", "resume", "complete"):
            # Index progress as it lands, not only at the end, so a crash still
            # leaves a queryable record of which parts exist.
            snapshot = dict(payload.get("journal") or {})
            if snapshot:
                _archive_to_db(snapshot)

    result = job.check()
    _archive_to_db(result)
    if result.get("reused"):
        yield "[STAGE:0] ♻️ This exact file was archived before; reusing that archive.\n"
    return result.get("message_link") or result.get("link")


def tg_overview_html():
    """Live statistics and a table of everything archived on Telegram.

    Reads from the database rather than the JSON journals, so an upload made on
    another machine, or one whose journal was cleaned away, still shows up.
    """
    _ensure_db_seeded()
    try:
        usage = db.channel_usage()
        totals = db.stats()
    except Exception:  # noqa: BLE001
        logger.exception("Could not read archive usage from the database")
        return (
            '<div style="padding:16px; border:1px solid var(--border-color-primary);'
            ' border-radius:10px; color:#dc2626; text-align:center;">'
            "Could not read the archive index (t_dubber.db).</div>"
        )

    if not usage or totals.get("archives", 0) == 0:
        return (
            '<div style="padding:16px; border:1px dashed var(--border-color-primary);'
            ' border-radius:10px; color:var(--body-text-color-subdued);'
            ' text-align:center;">No uploads recorded yet.</div>'
        )

    conn = db.connect()
    recent = conn.execute(
        """
        SELECT filename, file_size, channel, chunk_count, state, manifest_link,
               updated_at, project_id
        FROM telegram_archives
        ORDER BY updated_at DESC
        LIMIT 25
        """
    ).fetchall()

    def stat(label, value):
        return (
            '<div style="text-align:center; padding:12px; border-radius:8px;'
            ' background:var(--background-fill-secondary);'
            ' border:1px solid var(--border-color-primary);">'
            f'<div style="font-size:22px; font-weight:800; color:var(--color-accent);">{value}</div>'
            f'<div style="font-size:11px; text-transform:uppercase; letter-spacing:1px;'
            f' color:var(--body-text-color-subdued);">{label}</div></div>'
        )

    # Per-channel cards come first: with several channels in play, "where did
    # this go?" is answered by which channel holds it, not by one global total.
    channel_cards = []
    for row in usage:
        row = dict(row)
        channel = html.escape(str(row["channel"]))
        unknown = row["channel"] == db.UNKNOWN_CHANNEL
        last = (row.get("last_activity") or "")[:16]
        note = " (channel was never recorded)" if unknown else f" · last {last}"
        channel_cards.append(
            '<div style="border:1px solid var(--border-color-primary);'
            ' border-radius:8px; padding:10px 12px;">'
            f'<div style="font-weight:700; font-size:13px;">{channel}</div>'
            f'<div style="font-size:12px; color:var(--body-text-color-subdued);">'
            f'{row["uploads"]} upload{"s" if row["uploads"] != 1 else ""} · '
            f'{human_bytes(row["total_bytes"])}</div>'
            f'<div style="font-size:11px; color:var(--body-text-color-subdued);">'
            f'{row["split"]} split · {row["complete"]} complete · '
            f'{row["failed"]} failed{note}</div>'
            "</div>"
        )

    rows = []
    for record in recent:
        # sqlite3.Row has no .get(); normalise to a dict first.
        record = dict(record)
        state = record.get("state") or "?"
        colour = {"complete": "#28a745", "failed": "#dc2626"}.get(state, "#d97706")
        link = record.get("manifest_link") or ""
        when = (record.get("updated_at") or "")[:16]
        name = html.escape(os.path.basename(record.get("filename") or "?"))
        project = record.get("project_id")
        if project:
            name += f'<div style="font-size:11px; color:var(--body-text-color-subdued);">'
            name += html.escape(project[:44]) + "</div>"
        cells = [
            f'<td style="padding:6px 8px; font-size:13px;">{name}</td>',
            f'<td style="padding:6px 8px; font-size:12px; white-space:nowrap;">'
            f'{html.escape(str(record.get("channel") or "-"))}</td>',
            f'<td style="padding:6px 8px; font-size:13px; white-space:nowrap;">'
            f'{human_bytes(record.get("file_size", 0))}</td>',
            f'<td style="padding:6px 8px; font-size:13px; text-align:center;">'
            f'{record.get("chunk_count") or 1}</td>',
            f'<td style="padding:6px 8px; font-size:12px; color:{colour};">'
            f'{html.escape(state)}</td>',
            f'<td style="padding:6px 8px; font-size:12px; white-space:nowrap;">{when}</td>',
        ]
        if link:
            cells.append(
                f'<td style="padding:6px 8px;">'
                f'<a href="{html.escape(link, quote=True)}" target="_blank" rel="noopener"'
                f' style="color:var(--color-accent); font-size:12px;">open</a></td>'
            )
        else:
            cells.append('<td style="padding:6px 8px;">-</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")

    header_cells = ("File", "Channel", "Size", "Parts", "State", "When", "Link")
    header = "<tr>" + "".join(
        f'<th style="text-align:left; padding:6px 8px; font-size:11px;'
        f' text-transform:uppercase; letter-spacing:1px;">{label}</th>'
        for label in header_cells
    ) + "</tr>"

    return (
        '<div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr));'
        ' gap:10px; margin-bottom:14px;">'
        + stat("Total archived", human_bytes(totals.get("archived_bytes", 0)))
        + stat("Archives", str(totals.get("archives", 0)))
        + stat("Parts", str(totals.get("parts", 0)))
        + stat("Runs", str(totals.get("projects", 0)))
        + stat("Succeeded", str(totals.get("succeeded", 0)))
        + "</div>"
        + '<div style="font-size:12px; text-transform:uppercase;'
        ' letter-spacing:1px; color:var(--body-text-color-subdued);'
        ' margin:0 0 6px;">By channel</div>'
        + '<div style="display:grid;'
        ' grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:10px;'
        ' margin-bottom:16px;">'
        + "".join(channel_cards)
        + "</div>"
        + '<div style="font-size:12px; text-transform:uppercase;'
        ' letter-spacing:1px; color:var(--body-text-color-subdued);'
        ' margin:0 0 6px;">Recent uploads</div>'
        + '<div style="overflow-x:auto;">'
        + '<table style="width:100%; border-collapse:collapse;">'
        + header
        + "".join(rows)
        + "</table></div>"
    )


def _render_transfer(payload):
    """Turn one archive-engine progress event into Drive-tab HTML."""
    total = int(payload.get("total") or 0)
    current = int(payload.get("current") or 0)
    percent = (current / total * 100.0) if total else 0.0
    chunk_index = payload.get("chunk_index")
    chunk_count = payload.get("chunk_count")
    if chunk_count and chunk_index:
        label = f"📦 Part {chunk_index}/{chunk_count} ·"
    elif payload.get("phase") == "restore":
        label = "♻️ Rebuilding ·"
    else:
        label = "☁️ Uploading ·"

    colour = "#1e3c72"
    if payload.get("phase") == "throttled":
        colour = "#d97706"
    elif payload.get("phase") == "complete":
        colour = "#28a745"

    bar = _progress_bar_html(percent, current, total, label)
    message = payload.get("message")
    note = (
        f'<p style="text-align:center; font-size:12px; color:{colour}; margin-top:2px;">'
        f"{html.escape(str(message))}</p>"
        if message
        else ""
    )
    return bar + note


def do_telegram_upload(video_file):
    """Archive any file to Telegram from the Drive tab.

    Handles files of any size: anything past Telegram's single-upload ceiling is
    streamed as numbered parts with a manifest on the end, so there is no size
    the Drive tab refuses.
    """
    path = _coerce_video_path(video_file)
    if not path or not os.path.isfile(path):
        yield "❌ Select an existing file first."
        return

    creds = _tg_credentials()
    if creds is None:
        yield "❌ Save your Telegram settings in Connection Settings first."
        return

    size = os.path.getsize(path)
    planned = len(plan_chunks(size, CHUNK_SIZE))
    channel = pick_channel() or creds["channel"]

    # Build the same rich caption a link-based upload would get, but with the
    # filename as the title since there is no source page to draw from.
    try:
        media = link_resolver.ResolvedMedia(
            path=path,
            kind="direct",
            url="",
            page_url="",
            title=os.path.splitext(os.path.basename(path))[0],
            size_bytes=size,
            extractor="local",
        )
        caption = link_resolver.build_caption(media, kind="archive")
    except Exception:  # noqa: BLE001
        caption = f"☁️ T_Dubber Drive · {os.path.basename(path)}"

    def work(report):
        # SPEED POLICY (AI/dev note): same as _telegram_backup_step above —
        # Go parallel x4 for multi-part public files, Telethon fallback
        # otherwise. See telegram_uploader._try_go_upload() for the routing.
        return upload_file_detailed(
            path,
            creds["api_id"],
            creds["api_hash"],
            creds["phone"],
            channel,
            caption=caption,
            progress_callback=report,
            resume=True,
            reuse_completed=True,
            use_go=True,
            go_concurrency=4,
        )

    job = _BackgroundJob(work)
    try:
        for payload in job.events():
            yield _render_transfer(payload)
        result = job.check()
    except (TelegramCloudError, OSError, ValueError) as exc:
        yield f'<div style="padding:12px; border-left:4px solid #dc2626; color:#dc2626;">❌ Upload failed: {html.escape(str(exc))}</div>'
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Telegram upload failed for %s", path)
        yield f'<div style="padding:12px; border-left:4px solid #dc2626; color:#dc2626;">❌ Upload failed: {html.escape(str(exc))}</div>'
        return

    link = result.get("message_link") or result.get("link")
    parts = result.get("parts") or []
    reused = bool(result.get("reused"))

    rows = "".join(
        f'<tr><td style="padding:4px 8px; font-size:12px;">{p["part"]}</td>'
        f'<td style="padding:4px 8px; font-size:12px;">{human_bytes(p["size"])}</td>'
        f'<td style="padding:4px 8px; font-size:11px; color:var(--body-text-color-subdued);'
        f' font-family:monospace;">{p["sha256"][:20]}…</td></tr>'
        for p in parts[:100]
    )
    table = (
        f'<details style="margin-top:10px;">'
        f'<summary style="cursor:pointer; font-size:13px;">'
        f'Show all {len(parts)} part checksums</summary>'
        f'<div style="max-height:260px; overflow:auto;">'
        f'<table style="width:100%; margin-top:8px; border-collapse:collapse;">'
        f'<tr><th style="text-align:left;font-size:11px;">Part</th>'
        f'<th style="text-align:left;font-size:11px;">Size</th>'
        f'<th style="text-align:left;font-size:11px;">SHA-256</th></tr>'
        f'{rows}</table></div></details>'
        if len(parts) > 1
        else ""
    )

    yield _link_card(
        link,
        "♻️ Already archived" if reused else "✅ Archived on Telegram",
        (
            f"{html.escape(os.path.basename(path))} · {human_bytes(size)} · "
            f"{planned} part{'s' if planned != 1 else ''}"
            + (" · reused an identical earlier archive" if reused else "")
        ),
    ) + table


def do_restore_from_link(link):
    """Rebuild an archived file from a manifest or part link."""
    link = (link or "").strip()
    if not link:
        yield '<div style="color:#dc2626;">Paste an archive or manifest link first.</div>'
        return

    creds = _tg_credentials()
    if creds is None:
        yield '<div style="color:#dc2626;">Save your Telegram settings in Connection Settings first.</div>'
        return

    destination = os.path.join(PROJECTS_DIR, "_restored")

    def work(report):
        return download_or_restore(
            link, creds["api_id"], creds["api_hash"], creds["phone"],
            destination, progress_callback=report, verify=True,
        )

    job = _BackgroundJob(work)
    try:
        for payload in job.events():
            yield _render_transfer(payload)
        path = job.check()
    except (TelegramCloudError, ValueError) as exc:
        yield f'<div style="padding:12px; border-left:4px solid #dc2626; color:#dc2626;">{html.escape(str(exc))}</div>'
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Telegram restore failed")
        yield f'<div style="padding:12px; border-left:4px solid #dc2626; color:#dc2626;">Restore failed: {html.escape(str(exc))}</div>'
        return

    size = os.path.getsize(path)
    yield f"""
    <div style="padding:14px 16px; border-left:5px solid #28a745; border-radius:8px;
                background:rgba(40,167,69,0.08); margin-top:12px;">
      <div style="font-weight:700; color:#28a745;">
        ✅ Restore complete, every part checksum verified</div>
      <div style="font-size:13px; margin-top:6px;">
        {html.escape(os.path.basename(path))} &middot; {human_bytes(size)}
      </div>
      <code style="display:block; margin-top:8px; font-size:11px; word-break:break-all;
                   color:var(--body-text-color-subdued);">{html.escape(path)}</code>
      <div style="font-size:12px; color:var(--body-text-color-subdued); margin-top:8px;">
        Paste the original manifest link into the Studio's source box to dub it
        again, without needing the file on this machine.
      </div>
    </div>
    """


# -------------------------------------------------------------------------
# LINK -> TELEGRAM BACKUP (Drive tab: archive only, no dubbing)
# -------------------------------------------------------------------------

def _drive_log_panel(text):
    safe = html.escape(text)
    return (
        "<pre style='white-space:pre-wrap; font-family:monospace; font-size:13px; "
        "line-height:1.4; color:var(--body-text-color); background:var(--background-fill-secondary); "
        "border-radius:8px; padding:12px; max-height:300px; overflow:auto;'>"
        f"{safe}</pre>"
    )


def do_telegram_upload_from_link(source_url):
    """Archive a pasted link on Telegram and return its backup link.

    Same link logic as the Studio Workspace, but backup-only: we fetch the
    link, push it to Telegram, and hand back the manifest link. No dubbing.
    """
    link = (source_url or "").strip()
    if not link:
        yield '<div style="color:#dc2626;">Paste a source link first.</div>'
        return

    creds = _tg_credentials()
    if creds is None:
        yield '<div style="color:#dc2626;">Save your Telegram settings in Connection Settings first.</div>'
        return

    verdict = link_resolver.classify(link)
    if not verdict.ok:
        yield (
            '<div style="padding:12px; border-left:4px solid #dc2626; color:#dc2626;">'
            f"That link cannot be used.<br>{html.escape(verdict.reason)}</div>"
        )
        return

    # Already a Telegram archive -> nothing to re-upload, just surface it.
    if verdict.kind == "telegram":
        yield _link_card(
            link,
            "♻️ Already on Telegram",
            "This link already points to a Telegram archive, so it is reused as the backup.",
        )
        return

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    inbox_dir = os.path.join(PROJECTS_DIR, "_inbox", stamp)
    os.makedirs(inbox_dir, exist_ok=True)

    # 1) Resolve the link to a local file (live progress).
    yield '<div style="padding:8px 0;">🔗 Fetching the link…</div>'
    media = None
    log = ""
    try:
        resolver = _resolve_link_step(link, inbox_dir)
        while True:
            try:
                line = next(resolver)
            except StopIteration as stop:
                media = stop.value
                break
            log += line
            yield _drive_log_panel(log)
    except (link_resolver.LinkNotSupported, TelegramCloudError) as exc:
        yield (
            '<div style="padding:12px; border-left:4px solid #dc2626; color:#dc2626;">'
            f"Could not fetch that link.<br>{html.escape(str(exc))}</div>"
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Link resolution failed for %s", link)
        yield (
            '<div style="padding:12px; border-left:4px solid #dc2626; color:#dc2626;">'
            f"Could not fetch that link.<br>{html.escape(str(exc))}</div>"
        )
        return

    if media is None or not getattr(media, "path", None):
        yield '<div style="color:#dc2626;">Could not fetch that link.</div>'
        return

    yield (
        f'<div style="padding:8px 0;">✅ Got it: <b>{html.escape(os.path.basename(media.path))}</b> '
        f"({human_bytes(media.size_bytes)}). Archiving on Telegram…</div>"
    )

    # 2) Upload to Telegram (live progress), then return the backup link.
    channel = pick_channel() or creds["channel"]
    backup_link = None
    log = ""
    try:
        backup_resolver = _telegram_backup_step(
            media.path, creds, link, None, media=media, channel=channel
        )
        while True:
            try:
                line = next(backup_resolver)
            except StopIteration as stop:
                backup_link = stop.value
                break
            log += line
            yield _drive_log_panel(log)
    except (TelegramCloudError, OSError, ValueError) as exc:
        logger.exception("Telegram upload failed for %s", media.path)
        yield (
            '<div style="padding:12px; border-left:4px solid #dc2626; color:#dc2626;">'
            f"Telegram upload failed.<br>{html.escape(str(exc))}</div>"
        )
        return

    # Free local disk from now on: the archive already lives on Telegram, so the
    # downloaded copy can go. Scratch lives in projects/_inbox/<timestamp>, never
    # in the user's Documents/Pictures.
    try:
        import shutil as _shutil

        _shutil.rmtree(inbox_dir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        logger.exception("Could not clean up %s", inbox_dir)

    yield _link_card(
        backup_link,
        "✅ Archived on Telegram",
        "Checksummed parts plus a manifest. This link is your backup for the original URL.",
    )


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
    """Persist a project manifest as JSON, then mirror it into the database.

    JSON stays the source of truth for this phase of the rollout. Writing to
    SQLite as well means the history is queryable straight away without any
    behaviour change if the database write fails.
    """
    path = os.path.join(project_dir, "project.json")
    temporary_path = path + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as target:
        json.dump(manifest, target, ensure_ascii=False, indent=2)
    os.replace(temporary_path, path)

    try:
        db.upsert_project(manifest)
    except Exception:  # noqa: BLE001 - the JSON write already succeeded
        logger.exception("Could not mirror project %s into the database",
                         manifest.get("project_id"))


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

def start_dubbing(video_file, target_lang, speaker_detection, source_url="", backup_to_telegram=True):
    """Run one dubbing job: Telegram archive first, then the Kaggle GPU handoff.

    Order matters. The Kaggle worker keeps running no matter what happens to
    this machine, but nothing has left the PC until the dataset upload is
    accepted. Archiving to Telegram first guarantees there is always a durable,
    checksummed copy plus a link the user can pass on, and gives us a way to
    restart the whole job later from the archive alone.
    """
    source_url = (source_url or "").strip()
    creds = _tg_credentials()

    logs = ""
    output_video_path = None
    output_report_path = None
    active_url_html = ""
    backup_html = ""

    def fail(message):
        return (
            gr.update(value=f"❌ {message}\n"),
            gr.update(value=None),
            gr.update(value=parse_report_metrics(None)),
            gr.update(value=""),
            gr.update(value=""),
        )

    # ------------------------------------------------------------------
    # Stage 0a: work out what the source actually is
    # ------------------------------------------------------------------
    inbox_dir = None
    media = None
    if source_url:
        verdict = link_resolver.classify(source_url)
        if not verdict.ok:
            yield fail(f"That link cannot be used.\n{verdict.reason}")
            return

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        inbox_dir = os.path.join(PROJECTS_DIR, "_inbox", stamp)
        os.makedirs(inbox_dir, exist_ok=True)
        logs += f"[STAGE:0] 🔗 Reading {verdict.label} link...\n"
        yield (
            gr.update(value=logs),
            gr.update(value=None),
            gr.update(value=parse_report_metrics(None)),
            gr.update(value=""),
            gr.update(value=""),
        )

        try:
            resolver = _resolve_link_step(source_url, inbox_dir)
            media = None
            while True:
                try:
                    line = next(resolver)
                except StopIteration as stop:
                    media = stop.value
                    break
                logs += line
                yield (
                    gr.update(value=logs),
                    gr.update(value=None),
                    gr.update(value=parse_report_metrics(None)),
                    gr.update(value=""),
                    gr.update(value=""),
                )
        except (link_resolver.LinkNotSupported, TelegramCloudError) as exc:
            yield fail(f"Could not fetch that link.\n{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Link resolution failed for %s", source_url)
            yield fail(f"Could not fetch that link.\n{exc}")
            return

        resolved_path = media.path
        source_title = media.title
        source_kind = media.kind
        logs += f"[STAGE:0] ✅ Got it: {os.path.basename(resolved_path)} ({human_bytes(media.size_bytes)})\n"
    else:
        path = _coerce_video_path(video_file)
        if not path or not os.path.isfile(path):
            yield fail("Upload a video file or paste a source link first.")
            return
        resolved_path = path
        source_title = os.path.splitext(os.path.basename(path))[0]
        source_kind = "upload"

    # ------------------------------------------------------------------
    # Project folder + manifest
    # ------------------------------------------------------------------
    movie_name = re.sub(r'[^A-Za-z0-9_-]', '_', source_title.replace(' ', '_')).strip('_') or "movie"
    movie_name = movie_name[:64]
    source_fingerprint = _fingerprint_file(resolved_path)[:12]
    run_id = uuid.uuid4().hex[:10]
    project_id = f"{movie_name}-{source_fingerprint}-{run_id}"
    project_dir = os.path.join(PROJECTS_DIR, project_id)
    os.makedirs(project_dir, exist_ok=True)

    persisted_video = os.path.join(project_dir, os.path.basename(resolved_path))
    if os.path.abspath(resolved_path) != os.path.abspath(persisted_video):
        if inbox_dir:
            shutil.move(resolved_path, persisted_video)
        else:
            shutil.copy(resolved_path, persisted_video)

    manifest = {
        "project_id": project_id,
        "title": movie_name,
        "run_id": run_id,
        "state": "processing",
        "source_video": os.path.basename(persisted_video),
        "source_url": source_url or None,
        "source_kind": source_kind,
        "source_size": os.path.getsize(persisted_video),
        "target_language": target_lang,
        "speaker_detection": bool(speaker_detection),
        "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "telegram_backup": None,
        "kernel_id": None,
        "output_video": None,
        "report_file": None,
    }
    _write_project_manifest(project_dir, manifest)

    # Record what the source actually was: real title, uploader, runtime, where
    # it came from. Without this the archive is just a column of filenames.
    if media is not None:
        try:
            db.upsert_media_metadata(project_id, media.to_metadata())
        except Exception:  # noqa: BLE001
            logger.exception("Could not record media metadata for %s", project_id)

    # ------------------------------------------------------------------
    # Stage 0b: Telegram archive BEFORE the Kaggle handoff
    # ------------------------------------------------------------------
    backup_link = None
    if backup_to_telegram and creds is None:
        logs += (
            "[STAGE:0] ⚠️ Telegram is not configured, so no cloud backup will be made.\n"
            "[STAGE:0]    Save API details in the ☁️ Telegram Drive tab to switch this on.\n"
        )
        yield (
            gr.update(value=logs), gr.update(value=None),
            gr.update(value=parse_report_metrics(None)),
            gr.update(value=""), gr.update(value=""),
        )
    elif backup_to_telegram:
        channel = pick_channel() or creds["channel"]
        if channel != creds["channel"]:
            logs += f"[STAGE:0] 📺 Archiving to {channel}\n"
        # The generator raises while it is being drained, so the whole loop
        # (not just the post-processing) has to sit inside the try.
        backup_resolver = _telegram_backup_step(
            persisted_video, creds, source_url, project_id,
            media=media, channel=channel,
        )
        try:
            while True:
                try:
                    line = next(backup_resolver)
                except StopIteration as stop:
                    backup_link = stop.value
                    break
                logs += line
                yield (
                    gr.update(value=logs), gr.update(value=None),
                    gr.update(value=parse_report_metrics(None)),
                    gr.update(value=""), gr.update(value=backup_html),
                )
        except (TelegramCloudError, OSError, ValueError) as exc:
            # A failed backup must not block the dub, but it must be loud: the
            # user needs to know this run has no off-machine copy yet.
            logs += (
                f"[STAGE:0] ❌ Telegram backup failed: {exc}\n"
                "[STAGE:0]    Continuing to Kaggle anyway, but nothing is off "
                "this machine yet.\n"
            )
            manifest["telegram_backup_error"] = str(exc)
        else:
            manifest["telegram_backup"] = backup_link
            _write_project_manifest(project_dir, manifest)
            backup_html = _link_card(
                backup_link,
                "☁️ Archived on Telegram",
                "Checksummed parts plus a manifest. This copy survives anything "
                "that happens to this PC.",
            )
        yield (
            gr.update(value=logs), gr.update(value=None),
            gr.update(value=parse_report_metrics(None)),
            gr.update(value=""), gr.update(value=backup_html),
        )

    # ------------------------------------------------------------------
    # Stages 1-7: Kaggle
    # ------------------------------------------------------------------
    for status_update in run_pipeline(
        persisted_video, project_dir, target_lang, speaker_detection,
        backup_link=backup_link,
        source_url=source_url or None,
        source_title=source_title,
        source_size=os.path.getsize(persisted_video),
    ):
        stage_match = re.search(r"\[STAGE:(\d)\]", status_update)
        if stage_match:
            status_update = status_update.replace(stage_match.group(0), "")

        match = re.search(r"Kaggle GPU Worker \(([^)]+)\)", status_update)
        if match:
            manifest["kernel_id"] = match.group(1)
            _write_project_manifest(project_dir, manifest)
            active_url_html = build_beautiful_link_card(
                f"https://www.kaggle.com/code/{match.group(1)}"
            )

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
            gr.update(value=active_url_html),
            gr.update(value=backup_html),
        )

    if manifest["state"] == "processing":
        manifest["state"] = "failed"
        _write_project_manifest(project_dir, manifest)


def reattach_project(project_name):
    """Resume monitoring a Kaggle job whose UI session is long gone.

    The kernel runs on Kaggle's infrastructure, so closing the tab, reloading
    the page, or powering the PC off changes nothing about the job. This
    re-points the same polling logic at the same kernel and downloads the
    output once it lands.
    """
    project_id = os.path.basename(project_name or "")
    project_dir = os.path.join(PROJECTS_DIR, project_id)
    if not project_id or not os.path.isdir(project_dir):
        yield (
            gr.update(value="❌ That project no longer exists on this machine.\n"),
            gr.update(value=None),
            gr.update(value=parse_report_metrics(None)),
            gr.update(value=""),
        )
        return

    manifest = _read_project_manifest(project_dir)
    kernel_id = manifest.get("kernel_id")
    if not kernel_id:
        kaggle_json = os.path.join(APP_DIR, "kaggle_paperWork", "kaggle.json")
        try:
            with open(kaggle_json, "r", encoding="utf-8") as handle:
                kernel_id = kernel_id_for(json.load(handle).get("username", ""))
        except (OSError, json.JSONDecodeError):
            kernel_id = None
    if not kernel_id:
        yield (
            gr.update(
                value=(
                    "❌ No Kaggle worker was ever recorded for this project, so "
                    "there is nothing to reattach to. Start a fresh run instead.\n"
                )
            ),
            gr.update(value=None),
            gr.update(value=parse_report_metrics(None)),
            gr.update(value=""),
        )
        return

    active_url_html = build_beautiful_link_card(f"https://www.kaggle.com/code/{kernel_id}")
    logs = ""
    output_video_path = None
    output_report_path = None

    for status_update in reattach_to_kernel(project_dir, kernel_id):
        status_update = re.sub(r"\[STAGE:(\d)\]", "", status_update)
        logs += status_update
        if "Pipeline finished successfully" in status_update:
            output_video_path = _project_output_video(project_dir)
            report_candidate = _project_report_path(project_dir)
            output_report_path = report_candidate if os.path.isfile(report_candidate) else None
            manifest["state"] = "success" if output_video_path else "failed_missing_output"
            manifest["output_video"] = (
                os.path.relpath(output_video_path, project_dir) if output_video_path else None
            )
            manifest["report_file"] = (
                os.path.relpath(output_report_path, project_dir) if output_report_path else None
            )
            _write_project_manifest(project_dir, manifest)
        yield (
            gr.update(value=logs),
            gr.update(value=output_video_path),
            gr.update(value=parse_report_metrics(output_report_path)),
            gr.update(value=active_url_html),
        )

WORKSPACE_DIRS = {"_inbox", "_restored"}


def load_project_history():
    """Build the Project History dropdown from the database.

    Falls back to scanning the projects directory when the database is empty or
    unreadable, so the dropdown is never blank just because the index is new.
    """
    _ensure_db_seeded()
    choices = []
    try:
        rows = db.list_projects(limit=500)
    except Exception:  # noqa: BLE001
        logger.exception("Could not read projects from the database")
        rows = []

    if rows:
        for row in rows:
            title = row.get("title") or row["id"]
            created = (row.get("created_at") or row["id"])[:16]
            state = row.get("status") or "legacy"
            label = f"{title} · {created} · {state}"
            if row.get("backup_link"):
                label += " · ☁️"
            choices.append((label, row["id"]))
        return choices

    if not os.path.exists(PROJECTS_DIR):
        return []
    for project_id in os.listdir(PROJECTS_DIR):
        # _inbox and _restored are scratch space for link fetches and archive
        # rebuilds, not runs, so they must not show up as past projects.
        if project_id in WORKSPACE_DIRS or project_id.startswith("_"):
            continue
        project_dir = os.path.join(PROJECTS_DIR, project_id)
        if not os.path.isdir(project_dir):
            continue
        manifest = _read_project_manifest(project_dir)
        title = manifest.get("title") or project_id
        created = manifest.get("created_at", "")
        state = manifest.get("state", "legacy")
        label = f"{title} · {created or project_id} · {state}"
        if manifest.get("telegram_backup"):
            label += " · ☁️"
        choices.append((label, project_id))
    return sorted(choices, key=lambda item: item[1], reverse=True)


def project_backup_html(project_name):
    """Show the Telegram backup link recorded for a past project."""
    project_dir = os.path.join(PROJECTS_DIR, os.path.basename(project_name or ""))
    manifest = _read_project_manifest(project_dir) if os.path.isdir(project_dir) else {}
    link = manifest.get("telegram_backup")
    if link:
        return _link_card(
            link,
            "☁️ This run's Telegram backup",
            "The source archive taken before the Kaggle handoff.",
        )
    if manifest.get("telegram_backup_error"):
        return (
            '<div style="padding:12px; border-left:4px solid #d97706;'
            ' border-radius:6px; font-size:13px;">'
            f"⚠️ No backup was made: {html.escape(str(manifest['telegram_backup_error']))}"
            "</div>"
        )
    return ""

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

# -------------------------------------------------------------------------
# GRADIO INTERFACE
# -------------------------------------------------------------------------

BACKUP_BADGE = (
    '<div style="padding:10px 14px; border-radius:8px; font-size:13px;'
    ' background:rgba(40,167,69,0.10); border-left:4px solid #28a745;'
    ' color:var(--body-text-color);">'
    "<b>Cloud backup is on.</b> Every run is archived on Telegram before it "
    "reaches Kaggle, so a dead PC never costs you the source."
    "</div>"
    if tg_backup_ready()
    else '<div style="padding:10px 14px; border-radius:8px; font-size:13px;'
    ' background:rgba(217,119,6,0.10); border-left:4px solid #d97706;'
    ' color:var(--body-text-color);">'
    "<b>Cloud backup is off.</b> Add your Telegram API details in the "
    "☁️ Telegram Drive tab to archive every video before it goes to Kaggle."
    "</div>"
)

SUPPORT_TABLE_HTML = f"""
<details style="margin-top:12px; border:1px solid var(--border-color-primary);
                border-radius:10px; padding:10px 12px;">
  <summary style="cursor:pointer; font-weight:700; font-size:15px;">
    📋 Which links can I paste? (click to open the full list)
  </summary>

  <div style="margin-top:12px;">

    <p style="font-size:14px; margin:0 0 10px;">
      Doosri jagah link kholo, <b>Copy link address</b> karo, aur yahan paste kar do.
      Koi alag software nahi chahiye.
    </p>

    <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
                gap:14px;">

      <div style="border-left:4px solid #28a745; padding:8px 12px;">
        <b style="color:#28a745;">✅ Bina login ke chalne wale</b>
        <div style="font-size:12px; color:var(--body-text-color-subdued);
                    margin:4px 0 6px;">Inme se seedha kaam karega</div>
        <ul style="margin:0; padding-left:18px; font-size:13px;">
          <li><b>YouTube</b> — youtu.be, watch?v=...</li>
          <li><b>TikTok</b> — single video</li>
          <li><b>Vimeo</b> — public/unlisted</li>
          <li><b>Dailymotion</b></li>
          <li><b>VK</b>, <b>OK.ru</b> — public clips</li>
          <li><b>Rumble</b>, <b>Odysee</b></li>
          <li><b>Twitch</b> — VOD aur clips</li>
          <li><b>SoundCloud</b> — sirf audio</li>
        </ul>
      </div>

      <div style="border-left:4px solid #d97706; padding:8px 12px;">
        <b style="color:#d97706;">⚠️ Login shayad chahiye</b>
        <div style="font-size:12px; color:var(--body-text-color-subdued);
                    margin:4px 0 6px;">Anonymous request refuse ho to cookies file do</div>
        <ul style="margin:0; padding-left:18px; font-size:13px;">
          <li><b>Instagram</b> — Reels/posts</li>
          <li><b>Reddit</b> — v.redd.it</li>
          <li><b>X / Twitter</b> — kuch posts</li>
          <li><b>Facebook</b> — public posts</li>
          <li><b>Pinterest</b> — pin video</li>
        </ul>
      </div>

      <div style="border-left:4px solid #28a745; padding:8px 12px;">
        <b style="color:#28a745;">☁️ Apna Telegram</b>
        <div style="font-size:12px; color:var(--body-text-color-subdued);
                    margin:4px 0 6px;">Sabse reliable — humara hi archive</div>
        <ul style="margin:0; padding-left:18px; font-size:13px;">
          <li>Is tool ne jo kabhi upload kiya, uska link</li>
          <li>Parts me bat hua bhi chalega</li>
          <li>Har part ka checksum verify hota hai</li>
        </ul>
      </div>

      <div style="border-left:4px solid #6b7280; padding:8px 12px;">
        <b style="color:#6b7280;">📁 Direct file</b>
        <div style="font-size:12px; color:var(--body-text-color-subdued);
                    margin:4px 0 6px;">Koi bhi host, bas extension sahi ho</div>
        <ul style="margin:0; padding-left:18px; font-size:13px;">
          <li><code>.mp4</code> <code>.mkv</code> <code>.mov</code></li>
          <li><code>.webm</code> <code>.avi</code> <code>.flv</code></li>
          <li><code>.mp3</code> <code>.m4a</code> <code>.wav</code></li>
        </ul>
      </div>

    </div>

    <div style="margin-top:14px; padding:10px 12px; border-radius:8px;
                background:var(--background-fill-secondary); font-size:13px;">
      <b>❌ Jo nahi chalega, aur kyun:</b>
      <ul style="margin:6px 0 0 18px; padding:0;">
        <li><b>Private/incognito mode me video</b> — link do, account nahi hai</li>
        <li><b>Live stream (abhi chal raha hai)</b> — record hoke aayega</li>
        <li><b>Paid/DRM wala content</b> — download nahi hoga</li>
        <li><b>Geo-block</b> — jis desh me block hai, wahan se nahi aayega</li>
        <li><b>Playlist/channel link</b> — sirf pehla video lenge</li>
      </ul>
    </div>

    <div style="margin-top:10px; padding:10px 12px; border-radius:8px;
                border-left:4px solid #dc2626; font-size:13px;">
      <b>🔒 Safety:</b> local aur private network addresses (127.0.0.1, 192.168.x.x,
      10.x, aur cloud metadata 169.254.169.254) jaan-boojh kar block hain — taaki koi
      pasted link tumhare apne network se files kheench ke bahar na bhej sake.
      {"<br><b>JS runtime:</b> " + ", ".join(sorted(link_resolver.JS_RUNTIMES)) + " detected — YouTube ka player challenge solve ho sakta hai."
       if link_resolver.JS_RUNTIMES else
       "<br><b>⚠️ JS runtime nahi mila.</b> Deno ya Node install karo, warna kuch YouTube videos download nahi hongi."}
    </div>

    <details style="margin-top:12px;">
      <summary style="cursor:pointer; font-size:13px; font-weight:600;">
        Poori technical list ({len(link_resolver.describe_support())} rows)
      </summary>
      <div style="overflow-x:auto; margin-top:8px;">
        <table style="width:100%; border-collapse:collapse; font-size:12px;">
          <tr>
            <th style="text-align:left; padding:5px 7px;">Source</th>
            <th style="text-align:left; padding:5px 7px;">Example host</th>
            <th style="text-align:left; padding:5px 7px;">Notes</th>
          </tr>
          {"".join(
              f'<tr>'
              f'<td style="padding:5px 7px; border-top:1px solid var(--border-color-primary);">'
              f'{html.escape(row["label"])}</td>'
              f'<td style="padding:5px 7px; border-top:1px solid var(--border-color-primary);">'
              f'<code>{html.escape(row["example"])}</code></td>'
              f'<td style="padding:5px 7px; border-top:1px solid var(--border-color-primary);">'
              f'{html.escape(row["note"])}'
              f'{" (cookies)" if row["needs_cookies"] else ""}</td>'
              f'</tr>'
              for row in link_resolver.describe_support()
          )}
        </table>
      </div>
    </details>

  </div>
</details>
"""

# Moved theme out of Blocks constructor for Gradio 6.0+ compatibility
with gr.Blocks(title="Tarun Dubber AI") as demo:

    gr.Markdown(
        """
        # 🎬 Tarun Dubber AI
        ### Movie dubbing on Kaggle GPU, with a Telegram cloud backup taken first
        """
    )

    with gr.Tabs():
        # --- TAB 1: Studio Workspace ---
        with gr.Tab("🎬 Studio Workspace"):
            gr.Markdown(
                "**No external API key needed.** Translation runs on the Kaggle "
                "GPU with Index-Homura. The first run may take longer while the "
                "model downloads."
            )

            with gr.Row():
                with gr.Column(scale=1):
                    with gr.Group():
                        source_mode = gr.Radio(
                            choices=["📁 Upload a file", "🔗 Paste a source link"],
                            value="📁 Upload a file",
                            label="Source",
                        )
                        video_input = gr.Video(
                            label="Upload Source Video", sources=["upload"]
                        )
                        source_url = gr.Textbox(
                            label="Source link",
                            placeholder=(
                                "https://youtu.be/... · https://www.instagram.com/reel/... "
                                "· https://x.com/.../status/... · https://t.me/<channel>/<id>"
                            ),
                            lines=2,
                            visible=False,
                        )
                        check_link_btn = gr.Button("🔍 Check this link", variant="secondary", visible=False)
                        link_status = gr.HTML("")

                        target_language = gr.Dropdown(
                            choices=["Hindi", "English", "Spanish", "French", "German"],
                            value="Hindi",
                            label="Target Language",
                        )
                        speaker_toggle = gr.Checkbox(
                            label="Enable Multi-Speaker Detection", value=True
                        )
                        backup_toggle = gr.Checkbox(
                            label="☁️ Archive on Telegram before starting (recommended)",
                            value=True,
                        )
                        gr.HTML(BACKUP_BADGE)
                        start_button = gr.Button(
                            "🚀 Start Dubbing", variant="primary", size="lg"
                        )

                with gr.Column(scale=2):
                    status_output = gr.Textbox(
                        label="Live Progress",
                        lines=15,
                        interactive=False,
                        max_lines=20,
                        elem_id="log_output_box",
                    )

                    gr.Markdown("### ☁️ Telegram backup")
                    backup_output = gr.HTML("")

                    action_links_output = gr.HTML("")

                    puter_mode = gr.Radio(
                        choices=["Funny", "Serious", "Roast"],
                        value="Funny",
                        label="🤖 Puter.js Persona Mode",
                        elem_id="puter_mode_selector",
                        interactive=True,
                    )

                    gr.HTML(PUTER_AI_HTML)

                    with gr.Group():
                        output_video = gr.File(label="Download Output Video", interactive=False)
                        with gr.Row():
                            tg_upload_btn = gr.Button("📤 Re-archive output to Telegram", variant="secondary")
                            tg_link_output = gr.HTML("No upload yet.")

                        tg_upload_btn.click(
                            fn=do_telegram_upload,
                            inputs=[output_video],
                            outputs=[tg_link_output],
                        )

                    gr.Markdown("### 📊 Quality Dashboard")
                    metrics_ui = gr.HTML(parse_report_metrics(None))

            gr.HTML(SUPPORT_TABLE_HTML)

            def _mode_changed(mode):
                is_upload = mode == "📁 Upload a file"
                return (
                    gr.update(visible=is_upload),
                    gr.update(visible=not is_upload),
                    gr.update(visible=not is_upload),
                    gr.update(visible=not is_upload),
                )

            source_mode.change(
                fn=_mode_changed,
                inputs=[source_mode],
                outputs=[video_input, source_url, check_link_btn, link_status],
            )

            check_link_btn.click(
                fn=check_link_status,
                inputs=[source_url],
                outputs=[link_status],
            )

            # Pasting a link and hitting enter should check it straight away.
            source_url.submit(
                fn=check_link_status,
                inputs=[source_url],
                outputs=[link_status],
            )

            start_button.click(
                fn=start_dubbing,
                inputs=[video_input, target_language, speaker_toggle, source_url, backup_toggle],
                outputs=[status_output, output_video, metrics_ui, action_links_output, backup_output],
            )

        # --- TAB 2: Telegram Drive ---
        with gr.Tab("☁️ Telegram Drive"):
            gr.HTML(
                """
                <div style="padding: 25px; background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
                            border-radius: 12px; color: white;
                            box-shadow: 0 4px 15px rgba(0,0,0,0.2); margin-bottom: 20px;">
                  <h2 style="margin: 0; color: white; font-family: 'Inter', sans-serif;
                           font-weight: 800; font-size: 28px;">☁️ Telegram Drive</h2>
                  <p style="margin: 8px 0 0 0; opacity: 0.9; font-size: 16px;">
                    Unlimited archive space in your own channel. Files above ~1.85 GB
                    are split into parts with a manifest, so any size works.
                  </p>
                </div>
                """
            )

            with gr.Accordion("📺 Channels (spread a community of channels)", open=False):
                _ch = load_channels()
                ch_text = gr.Textbox(
                    label="One channel per line",
                    value="\n".join(_ch["channels"]),
                    lines=5,
                    placeholder="@tgwebcloud1\n@tgwebcloud2\n@tgwebcloud3",
                )
                with gr.Row():
                    ch_strategy = gr.Dropdown(
                        choices=["round_robin", "least_used", "random", "fixed"],
                        value=_ch.get("strategy", "round_robin"),
                        label="How to spread uploads",
                    )
                    ch_index = gr.Number(
                        value=int(_ch.get("default_index") or 0),
                        label="Index (only for 'fixed')",
                        precision=0,
                    )
                ch_save_btn = gr.Button("💾 Save Channels", variant="primary")
                ch_status = gr.Markdown(
                    f"Currently spreading across **{len(_ch['channels'])}** channel(s): "
                    + ", ".join(f"`{c}`" for c in _ch["channels"])
                    if len(_ch["channels"]) > 1
                    else "One channel configured. Add more lines above to spread uploads."
                )
                ch_save_btn.click(
                    fn=save_channels,
                    inputs=[ch_text, ch_strategy, ch_index],
                    outputs=[ch_status],
                )
                gr.Markdown(
                    "**round_robin** alternates so every channel gets an equal share. "
                    "**least_used** always picks the emptiest one. **random** is for "
                    "spreading manual traffic. **fixed** always uses the index above, "
                    "which is what you want when a specific channel is the archive of "
                    "record. The manifest always records which channel was used, so a "
                    "restore never has to guess."
                )

            with gr.Accordion("⚙️ Connection Settings (API & Channel)", open=False):
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
                    outputs=[config_status],
                )

            with gr.Row():
                with gr.Column(scale=2):
                    gr.Markdown("### 📤 Upload Any Size")
                    drive_file_input = gr.File(label="Select any file (no size limit)", type="filepath")
                    drive_upload_btn = gr.Button("🚀 Start Auto-Chunked Upload", variant="primary", size="lg")
                    drive_progress_out = gr.HTML("Status: <b>Ready for Upload</b>")

                    drive_upload_btn.click(
                        fn=do_telegram_upload,
                        inputs=[drive_file_input],
                        outputs=[drive_progress_out],
                    )

                    gr.Markdown("### ♻️ Restore an Archive")
                    gr.Markdown(
                        "Paste the manifest link that was posted after the parts, "
                        "or any single part link. Every part is checksum-verified "
                        "before the file is rebuilt."
                    )
                    restore_link_input = gr.Textbox(
                        label="Archive or manifest link",
                        placeholder="https://t.me/tgwebcloud1/12345",
                        lines=1,
                    )
                    restore_btn = gr.Button("⬇️ Rebuild the file", variant="secondary")
                    restore_out = gr.HTML("")

                    restore_btn.click(
                        fn=do_restore_from_link,
                        inputs=[restore_link_input],
                        outputs=[restore_out],
                    )

                    gr.Markdown("### 🔗 Archive from a Link (backup only)")
                    gr.Markdown(
                        "Paste a source link (YouTube, Instagram, X, t.me…). We fetch it, "
                        "push it to your Telegram channel, and give you a permanent backup "
                        "link. This is the Studio Workspace link flow, minus the dubbing."
                    )
                    drive_link_input = gr.Textbox(
                        label="Source link",
                        placeholder="https://youtu.be/... · https://t.me/<channel>/<id> · …",
                        lines=2,
                    )
                    with gr.Row():
                        drive_check_btn = gr.Button("🔍 Check this link", variant="secondary")
                        drive_link_btn = gr.Button("☁️ Archive this link to Telegram", variant="primary")
                    drive_link_status = gr.HTML("")
                    drive_link_out = gr.HTML("")

                    drive_check_btn.click(
                        fn=check_link_status,
                        inputs=[drive_link_input],
                        outputs=[drive_link_status],
                    )
                    drive_link_btn.click(
                        fn=do_telegram_upload_from_link,
                        inputs=[drive_link_input],
                        outputs=[drive_link_out],
                    )

                with gr.Column(scale=1):
                    gr.Markdown("### 📊 What is archived")
                    gr.Markdown(
                        f"Parts per file above ~{human_bytes(CHUNK_SIZE)}: "
                        "a 90 GB upload becomes 49 parts plus one manifest."
                    )
                    tg_overview_ui = gr.HTML(tg_overview_html())
                    refresh_overview_btn = gr.Button("🔄 Refresh", variant="secondary")
                    refresh_overview_btn.click(
                        fn=tg_overview_html,
                        inputs=[],
                        outputs=[tg_overview_ui],
                    )

        # --- TAB 3: Project History ---
        with gr.Tab("📂 Project History"):
            gr.Markdown(
                "### 📂 Past Projects\n\n"
                "A Kaggle worker keeps running on Kaggle's servers even if this "
                "tab was closed or the PC was shut down. Use **Reattach** to pick "
                "the same job back up and collect its output."
            )
            with gr.Group():
                with gr.Row():
                    with gr.Column(scale=1):
                        project_dropdown = gr.Dropdown(choices=load_project_history(), label="Select Project")
                        refresh_btn = gr.Button("🔄 Refresh List", variant="secondary")
                        reattach_btn = gr.Button("♻️ Reattach to Kaggle worker", variant="primary")
                    with gr.Column(scale=2):
                        history_video = gr.File(label="Project Output Video", interactive=False)
                        history_metrics = gr.HTML(parse_report_metrics(None))
                        history_backup = gr.HTML("")
                        reattach_log = gr.Textbox(
                            label="Reattach log",
                            lines=8,
                            interactive=False,
                            max_lines=12,
                        )

                project_dropdown.change(
                    fn=load_project_details,
                    inputs=[project_dropdown],
                    outputs=[history_video, history_metrics],
                )
                project_dropdown.change(
                    fn=project_backup_html,
                    inputs=[project_dropdown],
                    outputs=[history_backup],
                )
                refresh_btn.click(
                    fn=lambda: gr.update(choices=load_project_history()),
                    inputs=[],
                    outputs=[project_dropdown],
                )
                reattach_btn.click(
                    fn=reattach_project,
                    inputs=[project_dropdown],
                    outputs=[reattach_log, history_video, history_metrics, history_backup],
                )

    # Attach the JavaScript for Puter AI here (fixes HTML script warning and guarantees execution)
    demo.load(js=PUTER_JS)

if __name__ == "__main__":
    # In Gradio 6.0, theme goes in launch()
    demo.launch(inbrowser=True, theme=custom_theme)
