import sys
import os
import json
import traceback

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)


def load_tg_config():
    """Read (api_id, api_hash, phone, channel) from config.json.

    WHY THIS DELEGATES INSTEAD OF PARSING config.json ITSELF
    --------------------------------------------------------
    This function used to return a HARD-CODED, REAL api_hash literal, with the
    comment "Hardcoded just for test since DPAPI takes time to import/setup in
    script". That comment was the whole bug: the working credential was committed
    to git in 6 commits, while DPAPI was protecting a value that was already
    public.

    ``app.load_tg_config()`` does strictly more than the hand-rolled parser it
    replaces, and costs one import:

      * decrypts ``api_hash_protected`` through DPAPI
      * migrates a legacy plaintext ``api_hash`` to DPAPI on first read
      * returns the same 4-tuple in the same order

    So the "DPAPI takes too long to import" shortcut was not buying anything.
    """
    try:
        import app

        return app.load_tg_config()
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
        print(f"Progress: {current / (1024 * 1024):.2f}MB / {total / (1024 * 1024):.2f}MB")
        sys.stdout.flush()

    print(f"Uploading file: {file_path}")
    print(f"Size: {os.path.getsize(file_path) / (1024 * 1024 * 1024):.2f} GB")
    sys.stdout.flush()

    link = upload_to_telegram(
        file_path, api_id, api_hash, phone, channel, progress_callback=my_progress
    )
    print("\nSuccess! Link:", link)

except Exception as e:
    print("\nError:")
    traceback.print_exc()
