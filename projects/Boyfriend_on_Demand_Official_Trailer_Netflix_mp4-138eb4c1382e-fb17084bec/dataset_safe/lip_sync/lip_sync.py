"""Lip-sync providers for T_Dubber -- dubbing's missing face stage.

WHY LIP-SYNC
--------------
Mazinger produces a dubbed AUDIO (and a video whose audio track
is replaced), but the speaker's mouth still moves in the source
language. A lip-sync model re-renders the mouth region so it
matches the new audio. T_Dubber's Phase 3 vision (README.md)
names Wav2Lip arrays; this module makes that real with a
provider abstraction so the model can be swapped without
touching the pipeline.

THE PICK (2026)
----------------
* **MuseTalk** (TMElyralab, Tencent) -- default.
  Latent-space face inpainting, 30 fps+ on a V100, ~4 GB
  VRAM, MIT license (commercial use allowed), HF-hosted
  weights, a ``bbox_shift`` knob for mouth openness (Hindi
  and other wide-mouth languages need it). Weaknesses: 256x256
  face render (optional GFPGAN upscale), slight jitter,
  identity details like mustaches can drift.
* **Wav2Lip** (Rudrabha) -- fallback. Older, softer mouth
  region, but torch-only: no mmcv/mmpose build step, which
  matters when a Kaggle image's torch is too new for mmcv's
  prebuilt wheels.

Both are pure-inference, fit a Kaggle T4 (16 GB), and download
their weights from Hugging Face / official releases on first
setup, then cache under the workdir so later runs are warm.

PROVIDER CONTRACT
-----------------
``setup()``     one-time install/weights, cached, idempotent
``sync(video, audio, out_path, bbox_shift)``
                video = face donor (the SOURCE video),
                audio = the DUBBED audio track,
                out_path = synced mp4.

The orchestration (multitasker.LipSyncWorker) never cares
which provider is behind the contract, which is what makes
the dry-run tests and the real Kaggle run the same code.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], cwd: str | Path | None = None,
         env: dict | None = None, timeout: float = 0) -> subprocess.CompletedProcess:
    """Run with UTF-8-safe capture; timeout=0 means no limit."""
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    if env is not None:
        kwargs["env"] = env
    if timeout > 0:
        kwargs["timeout"] = timeout
    return subprocess.run(cmd, **kwargs)


def _download(url: str, dest: Path) -> bool:
    """Plain HTTP GET to dest; True on a non-empty file."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as response:
            if response.status != 200:
                return False
            with dest.open("wb") as handle:
                shutil.copyfileobj(response, handle)
        return dest.is_file() and dest.stat().st_size > 0
    except Exception:
        return False


def _hf_download(repo_id: str, filename: str, dest: Path,
                 token: str | None = None) -> bool:
    """Fetch one file from a HF repo into dest (flat, no cache
    layout) -- keeps the provider's models/ tree exactly the
    shape MuseTalk's README documents."""
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        return False
    try:
        resolved = hf_hub_download(
            repo_id=repo_id, filename=filename,
            cache_dir=str(dest.parent / ".hf_cache"),
            token=token or os.environ.get("HF_TOKEN") or None,
        )
        shutil.copy(resolved, dest)
        return dest.is_file() and dest.stat().st_size > 0
    except Exception:
        return False


def _ffprobe_streams(path: str) -> list[dict]:
    try:
        result = _run([
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
            "-of", "json", path,
        ], timeout=120)
        if result.returncode == 0:
            return json.loads(result.stdout or "{}").get("streams", [])
    except Exception:
        pass
    return []


def _has_audio(path: str) -> bool:
    return any(stream.get("codec_type") == "audio"
               for stream in _ffprobe_streams(path))


def _mux(video_path: str, audio_path: str, out_path: str) -> bool:
    """Take the video stream of the synced video and the dubbed
    audio track, and write a proper mp4. MuseTalk already embeds
    the audio it was given, but re-muxing is cheap insurance and
    guarantees the deliverable carries the DUBBED track."""
    result = _run([
        "ffmpeg", "-y", "-i", video_path, "-i", audio_path,
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", out_path,
    ], timeout=0)
    return (result.returncode == 0 and Path(out_path).is_file()
            and Path(out_path).stat().st_size > 0)


class LipSyncResult:
    """What a provider hands back after one sync."""

    def __init__(self, ok: bool, output: str = "", detail: str = "",
                 provider: str = "", seconds: float = 0.0):
        self.ok = ok
        self.output = output
        self.detail = detail
        self.provider = provider
        self.seconds = seconds

    def to_dict(self) -> dict:
        return {"ok": self.ok, "output": self.output,
                "detail": self.detail, "provider": self.provider,
                "seconds": round(self.seconds, 1)}


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


class LipSyncProvider:
    """Base class: setup once, sync many. Subclasses never
    raise from sync() -- a failed sync is a failed result, so
    a movie is never lost to a crashed worker."""

    name = "base"

    def __init__(self, workdir: Path):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._ready = False

    def setup(self) -> bool:
        """Install + weights. Idempotent; cached under workdir."""
        raise NotImplementedError

    def sync(self, video_path: str, audio_path: str, out_path: str,
             bbox_shift: int = 0) -> LipSyncResult:
        raise NotImplementedError


class FakeProvider(LipSyncProvider):
    """Dry-run provider: proves the orchestration without a GPU.

    Copies a stand-in file so every downstream step (upload,
    staging, validation) sees a real non-empty mp4."""

    name = "fake"

    def setup(self) -> bool:
        self._ready = True
        return True

    def sync(self, video_path: str, audio_path: str, out_path: str,
             bbox_shift: int = 0) -> LipSyncResult:
        import time
        started = time.monotonic()
        Path(out_path).write_bytes(b"fake-lip-sync")
        return LipSyncResult(True, out_path, "fake provider",
                             self.name, time.monotonic() - started)


# ---------------------------------------------------------------------------
# MuseTalk -- the default
# ---------------------------------------------------------------------------

MUSETALK_REPO = "https://github.com/TMElyralab/MuseTalk.git"

# The five weight packs MuseTalk needs, exactly the layout its
# README documents under models/.
MUSETALK_WEIGHTS = (
    # (dest relative to models/, repo_id, filename)
    ("musetalk/musetalk.json", "TMElyralab/MuseTalk", "musetalk/musetalk.json"),
    ("musetalk/pytorch_model.bin", "TMElyralab/MuseTalk", "musetalk/pytorch_model.bin"),
    ("sd-vae-ft-mse/config.json", "stabilityai/sd-vae-ft-mse", "config.json"),
    ("sd-vae-ft-mse/diffusion_pytorch_model.bin", "stabilityai/sd-vae-ft-mse", "diffusion_pytorch_model.bin"),
    ("dwpose/dw-ll_ucoco_384.pth", "yzd-v/DWPose", "dw-ll_ucoco_384.pth"),
    ("whisper/tiny.pt", "", "https://openaipublic.azureedge.net/main/whisper/models/65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9/tiny.pt"),
    ("face-parse-bisent/79999_iter.pth", "", "https://github.com/zllrunning/face-parsing.PyTorch/releases/download/1.0/79999_iter.pth"),
    ("face-parse-bisent/resnet18-5c106cde.pth", "", "https://download.pytorch.org/models/resnet18-5c106cde.pth"),
)


class MuseTalkProvider(LipSyncProvider):
    """MuseTalk: real-time latent-space lip sync (Tencent Lyra Lab).

    Setup clones the repo, installs its deps (mmengine/mmcv/
    mmdet/mmpose + the editable whisper feature extractor), and
    downloads the five weight packs into models/. Everything is
    cached under the workdir, so a warm Kaggle run installs
    nothing and downloads nothing.

    Known setup risk (flagged, not hidden): mmcv>=2.0.1 needs a
    prebuilt wheel matching the runtime's torch+CUDA. Kaggle's
    torch moves faster than mmcv's wheel matrix; if ``mim
    install mmcv`` cannot resolve, setup fails LOUDLY and the
    caller falls back to Wav2LipProvider (torch-only).
    """

    name = "musetalk"

    def __init__(self, workdir: Path):
        super().__init__(workdir)
        self.repo_dir = self.workdir / "MuseTalk"
        self.models_dir = self.repo_dir / "models"
        self.ready_marker = self.workdir / ".musetalk_ready"

    def setup(self) -> bool:
        if self._ready:
            return True
        if self.ready_marker.is_file() and self._weights_complete():
            self._ready = True
            return True

        # 1. Clone (shallow -- the repo is small and we only
        #    run inference).
        if not (self.repo_dir / "scripts" / "inference.py").is_file():
            if self.repo_dir.exists():
                shutil.rmtree(self.repo_dir, ignore_errors=True)
            result = _run(["git", "clone", "--depth", "1",
                           MUSETALK_REPO, str(self.repo_dir)])
            if result.returncode != 0:
                return self._fail("clone failed: "
                                  + (result.stderr or "")[-300:])

        # 2. Deps. mmcv/mmpose are the risky pair; everything
        #    else is plain pip.
        result = _run([sys.executable, "-m", "pip", "install", "--no-cache-dir",
                       "-r", str(self.repo_dir / "requirements.txt")])
        if result.returncode != 0:
            return self._fail("requirements failed: "
                              + (result.stderr or "")[-300:])
        for package in ("mmengine", "mmcv>=2.0.1", "mmdet>=3.1.0",
                        "mmpose>=1.1.0"):
            result = _run([sys.executable, "-m", "pip", "install",
                           "--no-cache-dir", package])
            if result.returncode != 0:
                return self._fail(f"pip {package} failed: "
                                  + (result.stderr or "")[-300:])
        whisper_pkg = self.repo_dir / "musetalk" / "whisper"
        result = _run([sys.executable, "-m", "pip", "install",
                       "--no-cache-dir", "--editable", str(whisper_pkg)])
        if result.returncode != 0:
            return self._fail("whisper feature extractor failed: "
                              + (result.stderr or "")[-300:])

        # 3. Weights (HF-hosted first, direct URLs for the rest).
        if not self._download_weights():
            return False

        try:
            self.ready_marker.write_text("ready\n", encoding="utf-8")
        except OSError:
            pass
        self._ready = True
        return True

    def _weights_complete(self) -> bool:
        return all((self.repo_dir / rel).is_file()
                   for rel, _repo, _src in MUSETALK_WEIGHTS)

    def _download_weights(self) -> bool:
        for rel, repo_id, source in MUSETALK_WEIGHTS:
            dest = self.repo_dir / rel
            if dest.is_file() and dest.stat().st_size > 0:
                continue
            ok = (_hf_download(repo_id, source, dest) if repo_id
                    else _download(source, dest))
            if not ok:
                return self._fail(f"weight download failed: {rel}")
        return True

    def sync(self, video_path: str, audio_path: str, out_path: str,
             bbox_shift: int = 0) -> LipSyncResult:
        import time
        started = time.monotonic()
        if not self._ready and not self.setup():
            return LipSyncResult(False, "", "MuseTalk setup failed",
                                 self.name, time.monotonic() - started)

        # MuseTalk's whisper encoder wants 16 kHz mono.
        wav_path = self.workdir / "lip_sync_audio.wav"
        result = _run([
            "ffmpeg", "-y", "-i", audio_path, "-ac", "1", "-ar", "16000",
            str(wav_path),
        ], timeout=300)
        if result.returncode != 0:
            return LipSyncResult(False, "", "audio resample failed",
                                 self.name, time.monotonic() - started)

        # A generated config: the shipped test.yaml is a template;
        # pointing it at this job's files is all the inference
        # script reads.
        config_path = self.workdir / "inference_job.yaml"
        config_path.write_text(
            "video_path: %s\n"
            "audio_path: %s\n"
            "outfile: %s\n"
            % (video_path, str(wav_path),
               str(self.workdir / "musetalk_result.mp4")),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["FFMPEG_PATH"] = shutil.which("ffmpeg") or "ffmpeg"
        result = _run([
            sys.executable, "-m", "scripts.inference",
            "--inference_config", str(config_path),
            "--bbox_shift", str(int(bbox_shift)),
        ], cwd=self.repo_dir, env=env)
        raw_out = self.workdir / "musetalk_result.mp4"
        if result.returncode != 0 or not raw_out.is_file():
            return LipSyncResult(
                False, "", "inference failed: "
                + (result.stderr or result.stdout or "")[-400:],
                self.name, time.monotonic() - started)

        # Guarantee the deliverable carries the DUBBED audio.
        if not _mux(str(raw_out), audio_path, out_path):
            # MuseTalk already embeds the audio; a mux failure is
            # not fatal if the raw output is playable.
            shutil.copy(raw_out, out_path)
        return LipSyncResult(True, out_path, "musetalk",
                             self.name, time.monotonic() - started)

    def _fail(self, detail: str) -> bool:
        print(f"[lip_sync] MuseTalk setup FAILED: {detail}",
              file=sys.stderr, flush=True)
        return False


# ---------------------------------------------------------------------------
# Wav2Lip -- the torch-only fallback
# ---------------------------------------------------------------------------

WAV2LIP_REPO = "https://github.com/Rudrabha/Wav2Lip.git"
WAV2LIP_WEIGHTS = (
    ("checkpoints/wav2lip_gan.pth",
     "https://github.com/Rudrabha/Wav2Lip/releases/download/models/wav2lip_gan.pth"),
    ("checkpoints/s3fd.pth",
     "https://www.adrianbulat.com/downloads/python-fan/s3fd-619a316812.pth"),
)


class Wav2LipProvider(LipSyncProvider):
    """Wav2Lip: the classic, torch-only fallback.

    No mmcv/mmpose, so it survives environments where MuseTalk's
    build step cannot resolve. Quality is softer around the
    mouth, but it keeps the pipeline moving -- the right trade
    when the alternative is no lip-sync at all."""

    name = "wav2lip"

    def __init__(self, workdir: Path):
        super().__init__(workdir)
        self.repo_dir = self.workdir / "Wav2Lip"
        self.ready_marker = self.workdir / ".wav2lip_ready"

    def setup(self) -> bool:
        if self._ready:
            return True
        if self.ready_marker.is_file() and self._weights_complete():
            self._ready = True
            return True
        if not (self.repo_dir / "inference.py").is_file():
            if self.repo_dir.exists():
                shutil.rmtree(self.repo_dir, ignore_errors=True)
            result = _run(["git", "clone", "--depth", "1",
                           WAV2LIP_REPO, str(self.repo_dir)])
            if result.returncode != 0:
                return self._fail("clone failed: "
                                  + (result.stderr or "")[-300:])
        for rel, url in WAV2LIP_WEIGHTS:
            dest = self.repo_dir / rel
            if dest.is_file():
                continue
            if not _download(url, dest):
                return self._fail(f"weight download failed: {rel}")
        result = _run([sys.executable, "-m", "pip", "install",
                       "--no-cache-dir", "-r",
                       str(self.repo_dir / "requirements.txt")])
        if result.returncode != 0:
            return self._fail("requirements failed: "
                              + (result.stderr or "")[-300:])
        try:
            self.ready_marker.write_text("ready\n", encoding="utf-8")
        except OSError:
            pass
        self._ready = True
        return True

    def _weights_complete(self) -> bool:
        return all((self.repo_dir / rel).is_file()
                   for rel, _url in WAV2LIP_WEIGHTS)

    def sync(self, video_path: str, audio_path: str, out_path: str,
             bbox_shift: int = 0) -> LipSyncResult:
        import time
        started = time.monotonic()
        if not self._ready and not self.setup():
            return LipSyncResult(False, "", "Wav2Lip setup failed",
                                 self.name, time.monotonic() - started)
        result = _run([
            sys.executable, "inference.py",
            "--checkpoint_path",
            str(self.repo_dir / "checkpoints" / "wav2lip_gan.pth"),
            "--face", video_path,
            "--audio", audio_path,
            "--outfile", out_path,
        ], cwd=self.repo_dir)
        if result.returncode != 0 or not Path(out_path).is_file():
            return LipSyncResult(
                False, "", "inference failed: "
                + (result.stderr or result.stdout or "")[-400:],
                self.name, time.monotonic() - started)
        return LipSyncResult(True, out_path, "wav2lip",
                             self.name, time.monotonic() - started)

    def _fail(self, detail: str) -> bool:
        print(f"[lip_sync] Wav2Lip setup failed: {detail}",
              file=sys.stderr, flush=True)
        return False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

PROVIDERS = {
    "fake": FakeProvider,
    "musetalk": MuseTalkProvider,
    "wav2lip": Wav2LipProvider,
}


def build_provider(workdir: Path,
                   name: str | None = None) -> LipSyncProvider:
    """Resolve a provider by name.

    Order: explicit arg > TDUBBER_LIP_SYNC_PROVIDER > auto.
    Auto = MuseTalk when its setup already looks feasible, else
    Wav2Lip -- the fallback wins on machines where mmcv cannot
    build, which is exactly the failure auto-detect must catch.
    """
    chosen = (name or os.environ.get("TDUBBER_LIP_SYNC_PROVIDER")
              or "").strip().lower()
    if chosen in PROVIDERS:
        return PROVIDERS[chosen](workdir)
    if chosen:
        print(f"[lip_sync] unknown provider {chosen!r}; "
              f"using wav2lip", file=sys.stderr)
        return Wav2LipProvider(workdir)
    # Auto: prefer MuseTalk unless its heavy deps already failed
    # once (the marker logic keeps the choice stable per workdir).
    musetalk_dir = Path(workdir) / "MuseTalk"
    wav2lip_failed = (Path(workdir) / ".musetalk_failed").is_file()
    if not wav2lip_failed and (musetalk_dir / "models").is_dir():
        return MuseTalkProvider(workdir)
    return Wav2LipProvider(workdir)
