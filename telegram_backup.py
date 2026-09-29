import os
import requests

# Set these variables later with your actual bot token and chat ID
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"
CHAT_ID = "YOUR_CHAT_ID_HERE"

def send_message(text):
    """Send a text message to the Telegram bot."""
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("Please configure your Telegram bot token.")
        return
        
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text
    }
    response = requests.post(url, json=payload)
    return response.json()

def upload_video(video_path, caption="Final Dubbed Video"):
    """Upload a video file to the Telegram bot as a backup."""
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("Please configure your Telegram bot token.")
        return
        
    if not os.path.exists(video_path):
        print(f"File not found: {video_path}")
        return
        
    print(f"Uploading {video_path} to Telegram...")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
    with open(video_path, 'rb') as video:
        files = {'video': video}
        data = {'chat_id': CHAT_ID, 'caption': caption}
        response = requests.post(url, data=data, files=files)
        
    print("Upload complete!")
    return response.json()

if __name__ == "__main__":
    print("Telegram backup script ready.")
    # Example usage:
    # send_message("Dubbing process completed successfully!")
    # upload_video("projects/my_movie/output.mp4")
