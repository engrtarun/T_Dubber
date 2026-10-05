import json

with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker.ipynb', 'r', encoding='utf-8') as f:
    notebook = json.load(f)

cell = notebook['cells'][1]
source = cell['source']

# The install_deps function is at lines 188-197 (inclusive)
start_idx = 188
end_idx = 197

print(f'Replacing lines {start_idx} to {end_idx}')

new_function = [
    'def install_deps():\n',
    '    """Fill pylibs; True when the tree landed.\n',
    '\n',
    '    Follows the Dockerfile\'s proven order:\n',
    '      1. vLLM FIRST (pins exact torch + nvidia-* wheels)\n',
    '      2. torch (no version = no-op, vLLM already brought the right one)\n',
    '      3. faster-whisper\n',
    '      4. Speech deps (yt-dlp, openai, etc.)\n',
    '    This avoids the torch/torchvision mismatch that crashes vLLM at startup.\n',
    '    """\n',
    '    PYLIBS_DIR.mkdir(parents=True, exist_ok=True)\n',
    '\n',
    '    # Step 1: vLLM pins the correct torch/torchvision/torchaudio for cu128\n',
    '    if run_pip(["vllm==0.29.0", "--extra-index-url", "https://download.pytorch.org/whl/cu128"], "vllm+cu128") != 0:\n',
    '        _tick("pip: vllm cu128 index failed; retrying from plain PyPI")\n',
    '        if run_pip(["vllm==0.29.0"], "vllm plain") != 0:\n',
    '            return False\n',
    '\n',
    '    # Step 2: torch (no version) - no-op, vLLM already pulled the certified build\n',
    '    if run_pip(["torch", "--extra-index-url", "https://download.pytorch.org/whl/cu128"], "torch+cu128") != 0:\n',
    '        _tick("pip: torch cu128 index failed; retrying from plain PyPI")\n',
    '        if run_pip(["torch"], "torch plain") != 0:\n',
    '            return False\n',
    '\n',
    '    # Step 3: faster-whisper\n',
    '    if run_pip(["faster-whisper"], "faster-whisper") != 0:\n',
    '        return False\n',
    '\n',
    '    # Step 4: Speech deps (yt-dlp, openai, json-repair, Pillow, soundfile, numpy, tqdm, python-slugify, av, demucs, omnivoice, kaggle)\n',
    '    if run_pip(SPEECH_DEPS + ["kaggle"], "speech_deps") != 0:\n',
    '        return False\n',
    '\n',
    '    return True\n',
]

# Replace the function
new_source = source[:start_idx] + new_function + source[end_idx+1:]
cell['source'] = new_source

with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker.ipynb', 'w', encoding='utf-8') as f:
    json.dump(notebook, f, ensure_ascii=False, indent=1)

print('Done!')