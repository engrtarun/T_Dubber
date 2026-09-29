import os
import subprocess
import json
import time
import sys

# Force Kaggle CLI to use the local folder containing kaggle.json
os.environ["KAGGLE_CONFIG_DIR"] = os.path.abspath("kaggle_paperWork")

def run_cmd(cmd):
    print(f"Executing: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error Output:\n{result.stderr}")
    else:
        print(f"Success Output:\n{result.stdout}")
    return result

def verify_kaggle_auth():
    print("\n--- Verifying Kaggle Auth ---")
    res = run_cmd("python -m kaggle config view")
    if res.returncode == 0:
        print("Kaggle Auth is SUCCESSFUL!")
    else:
        print("Kaggle Auth FAILED!")
        sys.exit(1)

def test_kaggle_upload():
    print("\n--- Testing Kaggle Dataset Upload ---")
    test_dir = "kaggle_test_dataset"
    os.makedirs(test_dir, exist_ok=True)
    
    # Create dummy text file
    with open(os.path.join(test_dir, "dummy.txt"), "w") as f:
        f.write("This is a test file for Kaggle upload.")
        
    # Init dataset
    run_cmd(f"python -m kaggle datasets init -p {test_dir}")
    
    # Update metadata
    meta_path = os.path.join(test_dir, "dataset-metadata.json")
    if os.path.exists(meta_path):
        with open(meta_path, "r") as f:
            meta = json.load(f)
            
        # Extract username from kaggle.json
        try:
            with open(os.path.abspath("kaggle_paperWork/kaggle.json")) as kf:
                kcreds = json.load(kf)
                username = kcreds.get("username", "testuser")
        except Exception as e:
            print(f"Could not read kaggle.json: {e}")
            username = "YOUR_USERNAME"
            
        timestamp = int(time.time())
        dataset_slug = f"test-dataset-{timestamp}"
        meta["id"] = f"{username}/{dataset_slug}"
        meta["title"] = f"Test Dataset {timestamp}"
        
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=4)
            
        print(f"Updated metadata with id: {meta['id']}")
        
    # Create dataset on Kaggle
    res = run_cmd(f"python -m kaggle datasets create -p {test_dir}")
    if res.returncode == 0:
        print(f"\nDataset upload initiated successfully!")
        print(f"Please check your Kaggle profile: https://www.kaggle.com/{username}/datasets")

if __name__ == "__main__":
    verify_kaggle_auth()
    test_kaggle_upload()
