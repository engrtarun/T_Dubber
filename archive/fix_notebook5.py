import json

# Fix indentation in main kaggle_worker.ipynb
with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker.ipynb', 'r', encoding='utf-8') as f:
    notebook = json.load(f)

cell = notebook['cells'][4]
source = cell['source']

# Fix the indentation of --base-dir line
for i, line in enumerate(source):
    if '"--base-dir", base_dir,' in line and line.startswith('                '):
        source[i] = '        "--base-dir", base_dir,\n'
        print(f'Fixed indentation at line {i}')
        break

with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker.ipynb', 'w', encoding='utf-8') as f:
    json.dump(notebook, f, ensure_ascii=False, indent=1)

print('Fixed main kaggle_worker.ipynb')

# Also fix kaggle_worker_local
with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker_local\kaggle_worker.ipynb', 'r', encoding='utf-8') as f:
    notebook = json.load(f)

cell = notebook['cells'][4]
source = cell['source']

for i, line in enumerate(source):
    if '"--base-dir", base_dir,' in line and line.startswith('                '):
        source[i] = '        "--base-dir", base_dir,\n'
        print(f'Fixed indentation at line {i}')
        break

with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker_local\kaggle_worker.ipynb', 'w', encoding='utf-8') as f:
    json.dump(notebook, f, ensure_ascii=False, indent=1)

print('Fixed kaggle_worker_local/kaggle_worker.ipynb')