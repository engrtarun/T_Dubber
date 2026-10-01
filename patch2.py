import json

with open('kaggle_worker.ipynb', 'r', encoding='utf-8') as f:
    nb = json.load(f)

for cell in nb['cells']:
    if cell['cell_type'] == 'code':
        src = "".join(cell['source'])
        if '"mazinger", "dub"' in src:
            src = src.replace(
                'cmd = [\n        "mazinger", "dub", video_path, ',
                'import sys\n    cmd = [\n        sys.executable, "-m", "mazinger", "dub", video_path, '
            )
            # Re-split into array
            cell['source'] = [line + '\n' for line in src.split('\n')]
            # Remove trailing newline from last element
            if cell['source']:
                cell['source'][-1] = cell['source'][-1].rstrip('\n')

with open('kaggle_worker.ipynb', 'w', encoding='utf-8') as f:
    json.dump(nb, f, indent=1)
