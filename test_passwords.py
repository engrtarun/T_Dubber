
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PasswordHashInvalidError, PhoneCodeInvalidError, PhoneCodeExpiredError
import app
import sys
import asyncio

async def main():
    api_id, api_hash, phone, _ = app.load_tg_config()
    client = TelegramClient("telegram_uploader_session", api_id, api_hash)
    await client.connect()

    passwords = ["@tarundilwal", "@TarunDilwal8445", "8445", "844598"]

    try:
        await client.sign_in(phone=phone, code="27962")
        print("Logged in without password!")
    except SessionPasswordNeededError:
        print("Password needed. Testing passwords...")
        for p in passwords:
            try:
                await client.sign_in(password=p)
                print(f"SUCCESS with password: {p}")
                sys.exit(0)
            except PasswordHashInvalidError:
                print(f"FAILED with password: {p}")
        print("All passwords failed.")
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        print(f"OTP Error: {e}")

asyncio.run(main())

