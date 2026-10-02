from telethon.sync import TelegramClient
import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_PATH = os.path.join(APP_DIR, "telegram_uploader_session")

import io
class FileChunkIO(io.IOBase):
    def __init__(self, filepath, offset, length, chunk_name):
        import time
        self.filepath = filepath
        self.offset = offset
        self.length = length
        self.name = chunk_name
        self.size = length
        self._size = length
        
        # Retry mechanism for Windows Defender / Antivirus locks
        max_retries = 10
        for attempt in range(max_retries):
            try:
                self.f = open(filepath, 'rb')
                break
            except PermissionError as e:
                if attempt == max_retries - 1:
                    raise e
                time.sleep(2)
                
        self.f.seek(offset)
        self.read_bytes = 0

    def read(self, size=-1):
        if self.read_bytes >= self.length:
            return b''
        if size == -1 or size > (self.length - self.read_bytes):
            size = int(self.length - self.read_bytes)
        data = self.f.read(size)
        self.read_bytes += len(data)
        return data

    def seek(self, offset, whence=0):
        if whence == 0:
            self.read_bytes = offset
        elif whence == 1:
            self.read_bytes += offset
        elif whence == 2:
            self.read_bytes = self.length + offset
        self.f.seek(self.offset + self.read_bytes)
        return self.read_bytes

    def tell(self):
        return self.read_bytes

    def __len__(self):
        return self.length
        
    def close(self):
        self.f.close()


def upload_to_telegram(file_path, api_id, api_hash, phone, channel_username, progress_callback=None):
    from telethon.tl.types import DocumentAttributeFilename
    import json
    
    # Initialize the client. This will use the existing 'telegram_uploader_session.session' file.
    client = TelegramClient(SESSION_PATH, int(api_id), api_hash)
    
    # We call start with phone so if session doesn't exist, it will prompt OTP in terminal
    client.start(phone=phone)
    
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"The video file was not found at {file_path}")
        
    file_size = os.path.getsize(file_path)
    base_name = os.path.basename(file_path)
    MAX_CHUNK_SIZE = 1900 * 1024 * 1024 # ~1.85 GB to be safe
    
    try:
        if file_size <= MAX_CHUNK_SIZE:
            # Send file to channel normally
            print(f"Uploading {base_name} to {channel_username}...")
            message = client.send_file(
                channel_username, 
                file_path,
                caption="🎥 Uploaded by T_Dubber",
                progress_callback=progress_callback
            )
            
            # Construct link
            username = channel_username.replace("@", "")
            link = f"https://t.me/{username}/{message.id}"
            print(f"Upload successful! Link: {link}")
            return link
            
        else:
            # Chunking logic for 90GB+ files
            print(f"File too large ({file_size/(1024**3):.2f} GB). Splitting into chunks...")
            num_chunks = (file_size + MAX_CHUNK_SIZE - 1) // MAX_CHUNK_SIZE
            chunk_links = []
            
            for i in range(int(num_chunks)):
                offset = i * MAX_CHUNK_SIZE
                length = min(MAX_CHUNK_SIZE, file_size - offset)
                chunk_name = f"{base_name}.part{i+1:03d}"
                
                def chunk_progress(current, total, chunk_offset=offset):
                    if progress_callback:
                        overall_current = chunk_offset + current
                        progress_callback(overall_current, file_size)

                print(f"Uploading chunk {i+1}/{int(num_chunks)}...")
                chunk_io = FileChunkIO(file_path, offset, length, chunk_name)
                try:
                    msg = client.send_file(
                        channel_username,
                        file=chunk_io,
                        caption=f"📦 Part {i+1}/{int(num_chunks)} of {base_name}",
                        attributes=[DocumentAttributeFilename(chunk_name)],
                        progress_callback=chunk_progress
                    )
                    username = channel_username.replace("@", "")
                    link = f"https://t.me/{username}/{msg.id}"
                    chunk_links.append({"part": i+1, "link": link})
                finally:
                    chunk_io.close()

            # Upload manifest
            manifest = {
                "filename": base_name,
                "total_size": file_size,
                "total_chunks": int(num_chunks),
                "chunks": chunk_links
            }
            manifest_path = os.path.join(APP_DIR, f"{base_name}_manifest.json")
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=4)
            
            manifest_msg = client.send_file(
                channel_username,
                manifest_path,
                caption=f"📄 MANIFEST for {base_name}\nTotal parts: {int(num_chunks)}\n\n(Use this manifest to download the entire {file_size/(1024**3):.2f} GB file back)"
            )
            os.remove(manifest_path)
            
            username = channel_username.replace("@", "")
            final_link = f"https://t.me/{username}/{manifest_msg.id}"
            print(f"Chunked Upload successful! Manifest Link: {final_link}")
            return final_link

    except Exception as e:
        raise Exception(f"Upload failed: {str(e)}")
    finally:
        client.disconnect()

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
    client = TelegramClient(SESSION_PATH, api_id, api_hash)
    client.start(phone=phone)
    print("Session created successfully! You can now use the UI.")
