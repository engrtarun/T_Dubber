import json

# Fix main kaggle_worker.ipynb - add --base-dir to CLI command
with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker.ipynb', 'r', encoding='utf-8') as f:
    notebook = json.load(f)

cell = notebook['cells'][3]  # The cell with the CLI command
source = cell['source']

# Find the cmd list and add --base-dir
for i, line in enumerate(source):
    if 'sys.executable, "-m", "mazinger", "dub", video_path,' in line:
        # Found the start of cmd, look for the end of the list
        for j in range(i, len(source)):
            if 'os.environ["OPENAI_MODEL"]' in source[j] and ']' in source[j+1]:
                # Insert --base-dir before the closing bracket
                indent = '        '
                source.insert(j+1, f'{indent}        "--base-dir", base_dir,\n')
                print(f'Added --base-dir at line {j+1}')
                break
        break

with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker.ipynb', 'w', encoding='utf-8') as f:
    json.dump(notebook, f, ensure_ascii=False, indent=1)

print('Fixed main kaggle_worker.ipynb')

# Also fix kaggle_worker_local
with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker_local\kaggle_worker.ipynb', 'r', encoding='utf-8') as f:
    notebook = json.load(f)

cell = notebook['cells'][3]
source = cell['source']

for i, line in enumerate(source):
    if 'sys.executable, "-m", "mazinger", "dub", video_path,' in line:
        for j in range(i, len(source)):
            if 'os.environ["OPENAI_MODEL"]' in source[j] and ']' in source[j+1]:
                indent = '        '
                source.insert(j+1, f'{indent}        "--base-dir", base_dir,\n')
                print(f'Added --base-dir at line {j+1}')
                break
        break

with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker_local\kaggle_worker.ipynb', 'w', encoding='utf-8') as f:
    json.dump(notebook, f, ensure_ascii=False, indent=1)

print('Fixed kaggle_worker_local/kaggle_worker.ipynb')