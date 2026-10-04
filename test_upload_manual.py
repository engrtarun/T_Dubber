import sys
import os
import json
import traceback

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

def load_tg_config():
    config_path = os.path.join(APP_DIR, "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
                import codecs
                # Hardcoded just for test since DPAPI takes time to import/setup in script
                return data.get("api_id", ""), "0c28170bc8f776097c5137411c1c248b", data.get("phone", ""), data.get("channel", "")
        except Exception:
            pass
    return "", "", "", ""

try:
    from telegram_uploader import upload_to_telegram

    api_id, api_hash, phone, channel = load_tg_config()
    print("Config loaded:", api_id, phone, channel)
    sys.stdout.flush()

    file_path = r"C:\Users\pocot\Videos\Ek.Chatur.Naar.2025.1080p.Hindi.WEB-DL.5.1.ESub.x264-HDHub4u.Ms(1).mkv"
    
    def my_progress(current, total):
        print(f"Progress: {current/(1024*1024):.2f}MB / {total/(1024*1024):.2f}MB")
        sys.stdout.flush()

    print(f"Uploading file: {file_path}")
    print(f"Size: {os.path.getsize(file_path) / (1024*1024*1024):.2f} GB")
    sys.stdout.flush()
    
    link = upload_to_telegram(file_path, api_id, api_hash, phone, channel, progress_callback=my_progress)
    print("\nSuccess! Link:", link)

except Exception as e:
    print("\nError:")
    traceback.print_exc()
