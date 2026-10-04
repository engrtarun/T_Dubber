"""
T_Dubber Auto-Boost Launcher
Single command — sab khud ho jayega
"""
import ctypes
import os
import subprocess
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def apply_boost():
    """Power plan, network, TCP — sab apply karo"""
    # High Performance power plan
    subprocess.run(["powercfg", "/setactive", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"],
                   capture_output=True)

    # Network throttling off
    subprocess.run([
        "reg", "add",
        r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
        "/v", "NetworkThrottlingIndex", "/t", "REG_DWORD", "/d", "0xffffffff", "/f"
    ], capture_output=True)

    # TCP optimization
    subprocess.run(["netsh", "int", "tcp", "set", "global", "autotuninglevel=normal"],
                   capture_output=True)
    subprocess.run(["netsh", "int", "tcp", "set", "global", "rss=enabled"],
                   capture_output=True)
    subprocess.run(["netsh", "int", "tcp", "set", "global", "chimney=enabled"],
                   capture_output=True)

    # Other apps throttle
    throttle_procs = ["chrome", "firefox", "edge", "steam", "epicgameslauncher",
                      "torrent", "qbittorrent", "onedrive", "dropbox", "teams", "zoom", "discord"]
    for name in throttle_procs:
        subprocess.run(["wmic", "process", "where", f"name='{name}.exe'",
                        "set", "priority=64"], capture_output=True)


def start_tdubber():
    """T_Dubber ko high priority se start karo"""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=APP_DIR,
        env=env
    )

    # Priority set karo
    time.sleep(3)
    try:
        subprocess.run(["wmic", "process", "where", f"processid={proc.pid}",
                        "set", "priority=256"], capture_output=True)  # 256 = High
    except:
        pass

    print(f"\n  🎬 T_Dubber started (PID {proc.pid}) — Boost ACTIVE\n")
    print(f"  🌐 http://127.0.0.1:7860\n")

    proc.wait()


if __name__ == "__main__":
    if not is_admin():
        # Admin rights nahi hai — khud admin se restart karo
        print("  ⚡ Admin rights chahiye — auto-elevate ho raha hai...")
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, " ".join(sys.argv), None, 1
        )
        sys.exit()

    print("\n  🚀 T_Dubber Auto-Boost")
    apply_boost()
    print("  ✅ Boost applied — High Performance + Network QoS + CPU Priority\n")
    start_tdubber()
