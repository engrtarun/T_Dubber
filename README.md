<div align="center">
  <img src="Index-Homura-9B.png" alt="T-Dubber Logo" width="100%" style="border-radius: 12px; box-shadow: 0px 4px 15px rgba(0,0,0,0.5);"/>
  <br/><br/>
  
  <h1>🎬 T_Dubber</h1>
  <b>The Ultimate Free End-to-End Video Dubbing Pipeline</b><br/>
  <i>Zero Cost. Infinite Possibilities. Powered by Kaggle GPUs & Open-Source Magic.</i>
  
  <br/><br/>
  
  [![Phase: MVP](https://img.shields.io/badge/Phase-MVP%20%2898%25%29-ff69b4?style=for-the-badge)](https://github.com/engrtarun/T_Dubber)
  [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)
  [![Python 3.10+](https://img.shields.io/badge/Python-3.10+-3776AB.svg?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
</div>

<br/>

> **T_Dubber** is a fully automated, cloud-accelerated movie dubbing solution built for creators who want high-quality results **for free**. No expensive API keys required. We leverage Kaggle GPUs, `mazinger`, and `Index-Homura-9B` to extract, translate, and lip-sync audio—all controlled from a sleek local UI.

## 🚀 Why T_Dubber?
Most dubbing tools rely on expensive APIs. We built a system that **offloads the heavy lifting to free cloud GPUs (Kaggle/Colab)** while managing everything from your local laptop. By combining `faster-whisper` for transcription and Bilibili's `Index-Homura-9B` for syllable-controlled translation, you get professional-grade dubbing at zero cost without writing AI code from scratch (powered by awesome community reference codes).

---

## 🗺️ Project Roadmap & Status

### 🏗️ Phase 0: Setup & Foundation <kbd>100% Complete ✅</kbd>
- **Local Environment:** Configured Python, FFmpeg, and Git on local laptop.
- **Kaggle Auth:** Generated and linked Kaggle API keys (`kaggle.json`).
- **Architecture Setup:** Established core files (`app.py`, `pipeline.py`, `kaggle_worker.ipynb`, `telegram_backup.py`).
- **Connection Test:** Successfully uploaded dummy datasets and verified Kaggle API connectivity.

### 🎬 Phase 1: MVP (Cloud Dubbing Pipeline) <kbd>98% Complete ⏳</kbd>
- **UI Dashboard:** Built a Gradio Control Panel for Video Upload, Language Selection, and triggering dubs.
- **Local-to-Cloud Bridge:** Automated video upload from local `pipeline.py` to Kaggle datasets.
- **GPU Cloud Worker:** Automated execution of `kaggle_worker.ipynb` to run `Mazinger` on Kaggle GPUs.
- **Real-Time Monitoring:** Integrated polling for worker status (running/complete) and remote error log fetching.
- **Output Sync:** Automatic retrieval of final dubbed videos and `report.json` back to local storage.
- 🚧 *Pending (2%):* Swap out OpenAI API requirement with Groq API (Free) for LLM translation.

### 🎙️ Phase 2: Quality & Multi-Voice <kbd>0% Complete ❌</kbd>
- **Speaker Detection:** Assign distinct synthesized voices to different characters (Hero, Heroine, Villain).
- **Quality Dashboard:** Extract and display metrics at every stage (WER, Sync Offset, Speaker Similarity).
- **Telegram Storage:** Auto-backup final outputs and checkpoints to Telegram to save local laptop memory.
- **Auto-Compression:** Intelligent FFmpeg compression for large movie files (1-2 GB) before cloud upload.

### 👄 Phase 3: Lip Sync & Scaling <kbd>0% Complete ❌</kbd>
- **Wav2Lip Integration:** Synchronize video lip movements with the newly generated Hindi audio.
- **Smart Resume:** Pick up exactly where the pipeline left off if Kaggle disconnects or times out.
- **GPU Worker Manager:** Dynamic switching and failover between Kaggle, Google Colab, and Lightning AI.
- **Security (DPAPI):** Secure, encrypted storage for local API keys.

---

## ⚙️ Architecture Workflow
1. **Local Upload:** You select a video via the Gradio UI.
2. **Pre-processing:** Video is auto-compressed (if needed) and pushed to a private Kaggle dataset.
3. **Cloud Execution:** The Kaggle Kernel spins up, installs dependencies, downloads `Index-Homura-9B`, and processes the dubbing pipeline.
4. **Delivery:** The finished `.mp4` is securely downloaded back to your laptop.

---
<div align="center">
  <b>Built with passion to democratize AI dubbing.</b><br/>
  <i>Drop a ⭐ on the repo to show your support!</i>
</div>
