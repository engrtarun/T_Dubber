from tg_up.client.tg_upload_client import TelegramUploadClient
import sys

api_id = 35578684
api_hash = '0c28170bc8f776097c5137411c1c248b'
phone = '+919286175802'
code = '46879'

try:
    client = TelegramUploadClient(r"C:\Users\pocot\Music\T_Dubber\telegram_uploader_session", api_id, api_hash)
    client.start(phone=phone, code_callback=lambda: code)
    print("Session created successfully!")
except Exception as e:
    print("Error:", e)
