import json

with open('kaggle_worker.ipynb', 'r', encoding='utf-8') as f:
    nb = json.load(f)

nb['cells'][0]['source'] = [
    '# Install Mazinger from Local Kaggle Dataset\n',
    'import os\n',
    'import subprocess\n',
    'mazinger_path = None\n',
    'for root, dirs, files in os.walk("/kaggle/input"):\n',
    '    if "pyproject.toml" in files and "mazinger" in root.lower():\n',
    '        mazinger_path = root\n',
    '        break\n',
    'if mazinger_path:\n',
    '    print(f"Found local mazinger at {mazinger_path}, installing...")\n',
    '    # Use --no-deps to avoid conflicting versions of numpy/numba on kaggle which causes errors\n',
    '    subprocess.run(["pip", "install", "--no-deps", "--no-cache-dir", f"{mazinger_path}[all]"], check=True)\n',
    '    # Install required dependencies explicitly that are not preinstalled on Kaggle\n',
    '    subprocess.run(["pip", "install", "--no-cache-dir", "faster-whisper", "deepgram-sdk", "demucs", "omnivoice"], check=True)\n',
    'else:\n',
    '    print("Local mazinger not found! Failing back to git (requires internet).")\n',
    '    subprocess.run(["pip", "install", "--no-cache-dir", "git+https://github.com/bakrianoo/mazinger.git#egg=mazinger[all]"], check=True)\n'
]

with open('kaggle_worker.ipynb', 'w', encoding='utf-8') as f:
    json.dump(nb, f, indent=1)
