import json

# Fix gpu-memory-utilization to 0.65 for safety margin on T4 16GB
with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker.ipynb', 'r', encoding='utf-8') as f:
    notebook = json.load(f)

cell = notebook['cells'][1]  # The cell with vLLM startup
source = cell['source']

for i, line in enumerate(source):
    if '"--gpu-memory-utilization", "0.72"' in line:
        source[i] = '        "--gpu-memory-utilization", "0.65",\n'
        print(f'Changed gpu-memory-utilization at line {i}')
        break

with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker.ipynb', 'w', encoding='utf-8') as f:
    json.dump(notebook, f, ensure_ascii=False, indent=1)

print('Fixed main kaggle_worker.ipynb')

# Also fix kaggle_worker_local
with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker_local\kaggle_worker.ipynb', 'r', encoding='utf-8') as f:
    notebook = json.load(f)

cell = notebook['cells'][1]
source = cell['source']

for i, line in enumerate(source):
    if '"--gpu-memory-utilization", "0.72"' in line:
        source[i] = '        "--gpu-memory-utilization", "0.65",\n'
        print(f'Changed gpu-memory-utilization at line {i}')
        break

with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker_local\kaggle_worker.ipynb', 'w', encoding='utf-8') as f:
    json.dump(notebook, f, ensure_ascii=False, indent=1)

print('Fixed kaggle_worker_local/kaggle_worker.ipynb')