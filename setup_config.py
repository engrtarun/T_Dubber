import sys
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

try:
    import app
    result = app.save_tg_config("35578684", "0c28170bc8f776097c5137411c1c248b", "+919286175802", "@tgwebcloud1")
    print("Config setup result:", result)
except Exception as e:
    print("Error:", e)
