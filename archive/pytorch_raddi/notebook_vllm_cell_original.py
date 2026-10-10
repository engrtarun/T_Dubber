# Install Mazinger and start a local, OpenAI-compatible Homura server.
#
# WHERE THE TIME USED TO GO (Version 14 run, seconds from kernel start):
#     0 ->  48   pip install of the mazinger/speech deps
#    49 -> 379   pip install vllm==0.29.0  -- ~100 s downloading, then ~215 s of
#                                    "Attempting uninstall": pip was ripping the
#                                    image's torch 2.10.0+cu128, torchaudio, triton
#                                    and setuptools out of site-packages before
#                                    replacing them with torch 2.13.0 + a CUDA 13
#                                    wheel set (nvidia-*-13.x).
#   391 -> 916   Homura-2B download + server start (nothing was cached)
#   ------------------------------------------------------------- 916 s ~ 15 min
#
# This cell attacks exactly those three numbers:
#   * deps go into /kaggle/working/pylibs via `pip install --target`, which NEVER
#     touches site-packages -- so there is nothing to uninstall, and a warm run
#     skips pip completely (0 s instead of 378 s);
#   * HF_HOME moves out of /root/.cache (dies with the container) into
#     /kaggle/working/hf_cache, which Kaggle saves with the notebook output;
#   * both are also searched under /kaggle/input first, because a NEW session
#     starts with an EMPTY /kaggle/working: Kaggle saves output (20 GB quota) but
#     does not restore it automatically -- this notebook's output, or a cache
#     dataset, has to be attached as Input. Mounted cache = warm run, nothing
#     mounted = cold run, and the log always says which one happened;
#   * every phase prints its own elapsed time, so the 1-2 min target stays
#     measurable instead of guessed.
import os
import sys
import time
import json
import shutil
import zipfile
import subprocess
import urllib.request
import signal
from pathlib import Path

T0 = time.monotonic()


def _tick(label):
    print(f"[{time.monotonic() - T0:6.1f}s] {label}", flush=True)


WORK = Path("/kaggle/working")
INPUT = Path("/kaggle/input")
WORK.mkdir(parents=True, exist_ok=True)

# --- Task 1: keep the model weights out of /root/.cache ----------------------
# Has to run before anything imports huggingface_hub, which is why it is here at
# the top of the cell and not next to the vLLM launch.
HF_CACHE = WORK / "hf_cache"
os.environ.setdefault("HF_HOME", str(HF_CACHE))
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # xet uploads stall on Kaggle
os.makedirs(os.environ["HF_HOME"], exist_ok=True)
_tick(f"HF_HOME={os.environ['HF_HOME']}")

os.environ["OPENAI_API_KEY"] = "EMPTY"
os.environ["OPENAI_BASE_URL"] = "http://localhost:8000/v1"
os.environ["OPENAI_MODEL"] = "IndexTeam/Index-Homura-2B"
os.environ["MAZINGER_DISABLE_VISION"] = "1"
os.environ["MAZINGER_LLM_MAX_OUTPUT_TOKENS"] = "2048"

MODEL_ID = os.environ["OPENAI_MODEL"]
READY_MARKER = ".tdubber_ready"
PYLIBS_DIR = WORK / "pylibs"

SHALLOW_PROBE = """
import importlib.util as u, sys
names = ["vllm", "torch", "torchaudio", "torchvision", "faster_whisper", "av",
          "openai", "demucs", "omnivoice"]
missing = [n for n in names if u.find_spec(n) is None]
if missing:
    sys.exit("missing: " + ", ".join(missing))
print("deps present: " + ", ".join(names))
"""
# Only names whose import name is certainly the distribution name, because this
# probe failing means a full reinstall.
#
# The deep probe must go past `import vllm`. That import does not touch
# torchvision, so a tree with a mismatched torch/torchvision pair passes it
# and prints "deps ok" -- and then `vllm serve` dies importing
# transformers -> torchvision with "operator torchvision::nms does not
# exist". Importing torchvision and then calling the op is what proves the
# C++ extension is actually bound to this torch build.
DEEP_PROBE = """
import vllm, torch, faster_whisper, av, openai
import torchvision
import torchaudio
import vllm.entrypoints.cli.main
torch.ops.torchvision.nms
# Importing torchaudio IS the alignment check: the function that raised in the
# failed run is torchaudio's own _check_cuda_version(). Probing it here costs a
# second and names the cause, instead of surfacing it 20 minutes later inside the
# server start, by which point the HF publish cell never runs.
print("deps ok: vllm %s / torch %s (cuda %s) / torchaudio %s / torchvision %s" % (
    vllm.__version__, torch.__version__, torch.version.cuda,
    torchaudio.__version__, torchvision.__version__))
"""


def probe_deps(code, pylibs):
    """Run a probe in a subprocess that sees `pylibs`, without mutating this kernel.

    Probing in a child (rather than importing here) keeps the check side-effect
    free: a failed probe can still switch the whole cell over to the system
    install path, which would be impossible if sys.path had already been edited.
    """
    env = os.environ.copy()
    if pylibs:
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in [str(pylibs), env.get("PYTHONPATH", "")] if part
        )
    return subprocess.run([sys.executable, "-c", code], env=env).returncode


def find_model_copy(bases):
    """A local snapshot of Homura-2B, mounted or saved from a previous run.

    Handing `vllm serve` a directory downloads nothing and writes nothing --
    which is why this beats HF_HOME: /kaggle/input is read-only, so a cache
    living there could not be updated even if we wanted to.
    """
    key = "models--" + MODEL_ID.replace("/", "--")
    for base in bases:
        if not base:
            continue
        try:
            for snap in Path(base).glob(f"**/{key}/snapshots/*"):
                if (snap / "config.json").exists() and any(snap.glob("*.safetensors")):
                    return snap
        except OSError:
            continue
    return None


def find_pylibs(bases):
    """A previously installed dependency tree, mounted or saved from a last run.

    The ready marker is written only after the install AND its import probe both
    passed, so a half-finished install is never mistaken for a warm cache.
    """
    for base in bases:
        if not base:
            continue
        try:
            for candidate in Path(base).glob("**/pylibs"):
                if (candidate / READY_MARKER).exists() and (candidate / "vllm" / "__init__.py").exists():
                    return candidate
        except OSError:
            continue
    return None


# --- mazinger source ---------------------------------------------------------
archive_candidates = list(INPUT.rglob("mazinger_source.zip"))
mazinger_extract_path = WORK / "mazinger"
search_root = INPUT
if archive_candidates:
    if mazinger_extract_path.exists():
        shutil.rmtree(mazinger_extract_path)
    mazinger_extract_path.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_candidates[0]) as source_archive:
        extraction_root = mazinger_extract_path.resolve()
        for member in source_archive.infolist():
            destination = (extraction_root / member.filename).resolve()
            if destination != extraction_root and extraction_root not in destination.parents:
                raise RuntimeError("Mazinger source archive contains an unsafe path.")
        source_archive.extractall(extraction_root)
    search_root = mazinger_extract_path
else:
    print("Kaggle mounted the source archive as an extracted folder; locating pyproject.toml directly.", flush=True)
mazinger_candidates = [
    path.parent for path in search_root.rglob("pyproject.toml")
    if (path.parent / "mazinger").is_dir()
    or (path.parent / "src" / "mazinger").is_dir()
    or path.parent.name.lower() in {"mazinger", "mazinger_source"}
]
if not mazinger_candidates:
    mounted_files = [str(path) for path in INPUT.rglob("pyproject.toml")]
    raise FileNotFoundError(f"Mazinger pyproject.toml not found in Kaggle input. Found: {mounted_files}")
mazinger_path = str(mazinger_candidates[0])
# Import the bundled source directly (no wheel build needed).
os.environ["PYTHONPATH"] = mazinger_path + os.pathsep + os.environ.get("PYTHONPATH", "")

# --- dependencies ------------------------------------------------------------
# `pip --target` resolves against an EMPTY directory -- it happily re-downloads
# what the image already has -- but in exchange it never runs a single
# "Attempting uninstall". That one difference is where ~215 s of the 330 s
# install went, and it is also why the result is a self-contained, relocatable
# tree that can be mounted read-only as Input next time.
# Installed last and as one unit, so every wheel of the family carries the
# same CUDA tag. `pip --target` resolves against an empty directory, which
# is why this has to be forced rather than merely requested.
TORCH_TRIPLE = [
    "torch==2.11.0+cu128", "torchvision==0.26.0+cu128", "torchaudio==2.11.0+cu128",
]

SPEECH_DEPS = [
    "yt-dlp>=2026.3.17", "openai>=1.0", "json-repair>=0.28", "Pillow>=10.0",
    "soundfile>=0.12", "numpy>=1.24", "tqdm>=4.60", "python-slugify>=8.0",
    "faster-whisper", "av>=14.0.0", "demucs", "omnivoice",
]


def run_pip(packages, label):
    cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir",
           "--target", str(PYLIBS_DIR)] + packages
    _tick(f"pip [{label}]: {' '.join(packages[:2])}{' ...' if len(packages) > 2 else ''}")
    return subprocess.run(cmd).returncode


def install_deps():
    """Fill pylibs; True when the tree landed.

    Two passes, and the split point is the whole point:

      1. vLLM, which hard-pins the torch family -- ONE resolver pass, so the
         three agree. vLLM 0.26.0 pins torch==2.11.0 / torchvision==0.26.0 /
         torchaudio==2.11.0, all three of which the cu128 index actually carries.
         vLLM 0.27.0+ pins torch==2.13.0, and the cu128 index has no 2.13.0 at
         all, so that pair of requests cannot both be satisfied.
      2. speech/mazinger deps.

    Asking for that torch family across separate passes is what broke this.
    `pip --target` resolves against an EMPTY directory, so a later pass picks
    its own newest torch, then refuses to overwrite the files the earlier pass
    already wrote ("Target directory ... already exists. Specify --upgrade").
    450 such warnings, and the tree ends up holding one pass's torch next to
    the other pass's torchvision. vLLM then dies on import with
    "operator torchvision::nms does not exist" -- a torch whose C++ ops the
    installed torchvision was not built against.

    Pass 2 is safe exactly because pass 1 landed first: every speech dep that
    needs torch accepts the version vLLM pinned, so pip resolves nothing new
    for torch and leaves the installed tree alone.
    """
    PYLIBS_DIR.mkdir(parents=True, exist_ok=True)

    # One index, one CUDA build, one atomic install of the torch family.
    #
    # `--extra-index-url` does not prefer an index -- pip takes the best version
    # match across both. That is how the failed run ended up with torch 2.13.0
    # (PyPI = CUDA 13.0) beside torchaudio 2.11.0+cu128 (cu128 index): two CUDA
    # stacks from a single resolver pass, and a crash 20 minutes later inside
    # `import torchaudio`. vLLM 0.26.0 is the newest release whose three pins
    # all exist on the cu128 index; 0.27.0+ moved to torch 2.13.0, which the
    # cu128 index does not carry at all.
    cu128 = ["--extra-index-url", "https://download.pytorch.org/whl/cu128"]
    triple = ["--index-url", "https://download.pytorch.org/whl/cu128", "--force-reinstall", "--no-deps"]
    if run_pip(["vllm==0.26.0"] + cu128, "vllm+cu128") != 0:
        _tick("pip: vllm cu128 index failed; retrying from plain PyPI")
        if run_pip(["vllm==0.26.0"], "vllm plain") != 0:
            return False
    # Force the exact family in afterwards. `--no-deps` stops pip from pulling a
    # fourth opinion about torch; `--index-url` (not --extra) means there is no
    # other place a cu13 wheel could come from.
    if run_pip(TORCH_TRIPLE + triple, "torch triple (cu128)") != 0:
        print("WARNING: could not force the cu128 torch family; the probe decides.", flush=True)

    # Pass 2 gets the SAME extra-index AND an explicit torch pin.
    #
    # `pip --target` resolves against an EMPTY directory: it cannot see the tree
    # pass 1 just wrote. So the unpinned form of this line resolved torch to
    # 2.14.1 (verified with a dry run against live PyPI), downloaded that wheel
    # -- ~554 MB -- and then refused to overwrite the 2.13.0 files vLLM had
    # pinned, emitting the "Target directory ... already exists" churn and
    # leaving the right version in place anyway. Correct result, wasted download,
    # every single cold run.
    #
    # Pinning to the version installed above makes the second resolve agree with
    # the first, so the wheel is fetched once instead of twice. The extra-index has
    # to match too: without it pip looks at PyPI, where the torch CUDA wheels
    # are named `nvidia-*-cu13`, while the cu128 index serves `nvidia-*-cu12`.
    # That naming difference is what makes the unpinned pass drag in a second,
    # different CUDA stack.
    torch_pin = [TORCH_TRIPLE[0]]
    if run_pip(SPEECH_DEPS + ["kaggle"] + torch_pin + cu128, "speech_deps") != 0:
        print("WARNING: speech deps failed with the torch pin; retrying unpinned.", flush=True)
        if run_pip(SPEECH_DEPS + ["kaggle"], "speech_deps-unpinned") != 0:
            return False

    return True

PYLIBS = find_pylibs([INPUT, WORK])
if PYLIBS:
    _tick(f"deps: warm cache hit -> {PYLIBS}")
    if probe_deps(SHALLOW_PROBE, PYLIBS) != 0:
        print("WARNING: warm pylibs failed its presence check; wiping and reinstalling.", flush=True)
        shutil.rmtree(PYLIBS, ignore_errors=True)
        PYLIBS = None

if PYLIBS is None:
    _tick(f"deps: cold install into {PYLIBS_DIR} (once per cache; ~4-6 min)")
    if install_deps() and probe_deps(DEEP_PROBE, PYLIBS_DIR) == 0:
        PYLIBS = PYLIBS_DIR
    else:
        print("WARNING: pylibs install/probe failed; falling back to a normal system install.", flush=True)
        PYLIBS = None

if PYLIBS:
    # Order matters: pylibs first, so the kernel and every subprocess see the same
    # build of torch that vLLM was resolved against.
    sys.path.insert(0, str(PYLIBS))
    os.environ["PYTHONPATH"] = str(PYLIBS) + os.pathsep + os.environ.get("PYTHONPATH", "")
    try:
        (PYLIBS / READY_MARKER).write_text(
            "installed " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n", encoding="utf-8"
        )
    except OSError:
        pass  # mounted read-only cache: the marker came with it
    _tick(f"deps: ready from {PYLIBS}")
else:
    # Last resort: the original behaviour. Slower -- pip does uninstall the
    # image's torch here -- but a working run beats a cached one.
    _tick("deps: LAST RESORT installing into system site-packages")
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir"] + SPEECH_DEPS, check=False)
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir",
                   "--index-url", "https://download.pytorch.org/whl/cu128", "--no-deps"] + TORCH_TRIPLE,
                   check=False)
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir",
                   "vllm==0.26.0"], check=False)

subprocess.run([sys.executable, "-c", "import mazinger; print('Mazinger source import OK')"], check=True)

# Kaggle's newest PyAV release dropped/changed this optional kwarg; faster-whisper still passes it.
# Remove only that compatibility kwarg so audio decode works across PyAV releases.
audio_module = subprocess.check_output(
    [sys.executable, "-c", "import faster_whisper.audio; print(faster_whisper.audio.__file__)"],
    text=True
).strip()
audio_source = Path(audio_module).read_text(encoding="utf-8")
legacy_call = 'av.open(input_file, mode="r", metadata_errors="ignore")'
if legacy_call in audio_source:
    Path(audio_module).write_text(audio_source.replace(legacy_call, 'av.open(input_file, mode="r")'), encoding="utf-8")
    print("Applied PyAV audio decode compatibility patch.", flush=True)
else:
    print("faster-whisper audio decoder has no legacy PyAV call; no patch needed.", flush=True)

try:
    import torch
    gpu_count = torch.cuda.device_count()
except Exception:
    gpu_count = 0
if gpu_count < 1:
    raise RuntimeError("No CUDA GPU is visible. Enable a Kaggle GPU accelerator and rerun.")
tensor_parallel = 1
if gpu_count >= 2:
    print("Using Homura-2B on GPU 0 and reserving GPU 1 for ASR/TTS.", flush=True)
else:
    print("Only one GPU is visible; using Homura-2B on the available GPU.", flush=True)
print(f"Visible CUDA GPUs: {gpu_count}; vLLM tensor parallel size: {tensor_parallel}", flush=True)

# Where the weights come from on THIS run. A mounted/local copy costs 0 bytes;
# otherwise they land in HF_HOME so the next run can reuse them if this output is
# attached as Input. Reported either way: "nothing mounted" is the reason a run is
# still slow, so it must never be silent.
MODEL_PATH = find_model_copy([INPUT, WORK])
if MODEL_PATH:
    _tick(f"model: mounted/local copy at {MODEL_PATH} (no download)")
else:
    _tick(f"model: nothing mounted, downloading into {os.environ['HF_HOME']} (~5 GB, first run only)")

vllm_log = open("/kaggle/working/vllm_server.log", "w", encoding="utf-8")


def vllm_cli():
    """Interpreter for `vllm serve`.

    The console script pip writes into pylibs/bin only imports when PYTHONPATH
    already points at pylibs (set above), so the cached script comes first, then
    whatever a last-resort system install left on PATH.
    """
    if PYLIBS:
        cached = PYLIBS / "bin" / "vllm"
        if cached.exists():
            return [str(cached), "serve"]
    on_path = shutil.which("vllm")
    if on_path:
        return [on_path, "serve"]
    return [sys.executable, "-c", "from vllm.entrypoints.cli.main import main; main()", "serve"]


def start_vllm(model_id):
    server_gpu_count = 1
    server_env = os.environ.copy()
    if gpu_count >= 2:
        # Reserve one T4 for speech models later in the pipeline.
        server_env["CUDA_VISIBLE_DEVICES"] = "0"
    command = vllm_cli() + [
        # Serving a local snapshot path instead of the repo id downloads nothing.
        str(MODEL_PATH) if MODEL_PATH else model_id,
        "--served-model-name", model_id,
        "--host", "127.0.0.1", "--port", "8000",
        "--tensor-parallel-size", str(server_gpu_count),
        "--gpu-memory-utilization", "0.65",
        # --- Task 3: was 4096. The pipeline asks for 8000 output tokens
        # (resegment merge) and 4000+prompt (fit check), and both were rejected
        # with "max_tokens cannot be greater than max_model_len". Homura-2B
        # declares max_position_embeddings=262144, so 16384 is far inside the
        # model and still leaves KV cache headroom on a 16 GB T4.
        "--max-model-len", "16384",
        "--max-num-seqs", "1",
        # Was 2048: raise the per-step token budget so long prompts are not
        # starved now that the context is 4x bigger. Chunked prefill still
        # applies above this, so a bigger prompt degrades instead of failing.
        "--max-num-batched-tokens", "4096",
        "--dtype", "half",
        "--trust-remote-code",
        "--language-model-only",
    ]
    print(f"Starting vLLM model {model_id}: {' '.join(command)}", flush=True)
    return subprocess.Popen(
        command, stdout=vllm_log, stderr=subprocess.STDOUT,
        env=server_env, start_new_session=True
    )


def stop_vllm(process):
    # Kill the whole process group; vLLM may leave CUDA worker children behind.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    time.sleep(2)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def wait_for_vllm(process, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/v1/models", timeout=5) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(5)
    return False


# Allow up to 20 minutes for model weight download and server startup.
_vllm_started = time.monotonic()
vllm_process = start_vllm(os.environ["OPENAI_MODEL"])
if not wait_for_vllm(vllm_process, 1200):
    stop_vllm(vllm_process)
    vllm_log.flush()
    tail = Path("/kaggle/working/vllm_server.log").read_text(encoding="utf-8", errors="replace")[-30000:]
    raise RuntimeError("Homura-2B did not start within 20 minutes. vLLM log tail:\n" + tail)
print(f"Local Homura server ready at {os.environ['OPENAI_BASE_URL']} using {os.environ['OPENAI_MODEL']}.", flush=True)
_tick(f"vLLM server up in {time.monotonic() - _vllm_started:.1f}s "
      f"({'no download' if MODEL_PATH else 'weights downloaded'}, "
      f"{'mounted cache' if PYLIBS else 'system install'})")


def du(path):
    """Size of a directory, cheap enough to print on every run."""
    try:
        return subprocess.check_output(["du", "-sh", str(path)], text=True).split()[0]
    except Exception:
        return "?"


print("=== setup timing (target: 60-120 s warm) ===", flush=True)
_tick(f"pylibs={du(PYLIBS) if PYLIBS else 'n/a'}  hf_cache={du(HF_CACHE)}")
_tick(f"TOTAL SETUP {time.monotonic() - T0:.1f}s")
