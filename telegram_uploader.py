import os
import sys
# Importing telethon.sync patches TelegramClient to support synchronous calls
import telethon.sync
from tg_up.client.tg_upload_client import TelegramUploadClient
from tg_up.upload_files import File

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_PATH = os.path.join(APP_DIR, "telegram_uploader_session")

def upload_to_telegram(file_path, api_id, api_hash, phone, channel_username):
    # Initialize the client. This will use the existing 'telegram_uploader_session.session' file.
    client = TelegramUploadClient(SESSION_PATH, int(api_id), api_hash)
    
    # We call start with phone so if session doesn't exist, it will prompt OTP in terminal
    client.start(phone=phone)
    
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"The video file was not found at {file_path}")
        
    tg_file = File(client, file_path)
    messages = client.send_files(channel_username, [tg_file])
    
    if messages and len(messages) > 0:
        message = messages[0]
        # Construct link
        username = channel_username.replace("@", "")
        link = f"https://t.me/{username}/{message.id}"
        return link
    else:
        raise Exception("Upload failed or no message returned.")

if __name__ == '__main__':
    print("=== Telegram First-Time Setup ===")
    api_id_input = input("Enter your API ID: ").strip()
    if not api_id_input.isdigit():
        print("API ID must be an integer.")
        sys.exit(1)
    api_id = int(api_id_input)
    
    api_hash = input("Enter your API HASH: ").strip()
    phone = input("Enter your Phone Number (with country code, e.g. +91...): ").strip()
    channel = input("Enter your Channel Username (e.g. @tgwebcloud1): ").strip()
    
    print("\nInitializing Telegram client...")
    client = TelegramUploadClient(SESSION_PATH, api_id, api_hash)
    client.start(phone=phone)
    print("Session created successfully! You can now use the UI.")
