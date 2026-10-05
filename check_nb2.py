import json

path = r'C:\Users\pocot\Music\T_Dubber\projects\Boyfriend_on_Demand___Official_Trailer___Netflix-138eb4c1382e-c54322d70d\kernel_safe\kaggle_worker.ipynb'
with open(path, 'r', encoding='utf-8') as f:
    notebook = json.load(f)

for i, cell in enumerate(notebook['cells']):
    if cell['cell_type'] == 'code':
        source = ''.join(cell['source'])
        if 'install_deps' in source:
            print(f'Found install_deps in cell {i}')
            for j, line in enumerate(cell['source']):
                if 'install_deps' in line or 'torch==' in line or 'vllm==' in line:
                    print(f'  {j}: {repr(line)[:120]}')
            break
        if 'def install_deps' in source:
            print(f'Found def install_deps in cell {i}')