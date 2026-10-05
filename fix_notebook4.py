import json

# Fix main kaggle_worker.ipynb - add --base-dir to CLI command
with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker.ipynb', 'r', encoding='utf-8') as f:
    notebook = json.load(f)

cell = notebook['cells'][4]  # The cell with the CLI command
source = cell['source']

# Find the line with "--llm-model", os.environ["OPENAI_MODEL"]
for i, line in enumerate(source):
    if '--llm-model", os.environ["OPENAI_MODEL"]' in line:
        # Insert --base-dir before this line
        indent = '        '
        source.insert(i, f'{indent}        "--base-dir", base_dir,\n')
        print(f'Added --base-dir at line {i}')
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
    if '--llm-model", os.environ["OPENAI_MODEL"]' in line:
        indent = '        '
        source.insert(i, f'{indent}        "--base-dir", base_dir,\n')
        print(f'Added --base-dir at line {i}')
        break

with open(r'C:\Users\pocot\Music\T_Dubber\kaggle_worker_local\kaggle_worker.ipynb', 'w', encoding='utf-8') as f:
    json.dump(notebook, f, ensure_ascii=False, indent=1)

print('Fixed kaggle_worker_local/kaggle_worker.ipynb')