#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
doctor.py — Pre-flight system audit for T_Dubber
=================================================

Scans the local machine and reports whether the dubbing pipeline is ready
to launch.  Run this before a Kaggle deployment to catch missing binaries,
unmet Python dependencies, and configuration gaps in one pass.

    python doctor.py            # full audit
    python doctor.py --quiet    # machine-readable (exit code only)

Checks performed
----------------
1. Native binaries — the five .exe tools the pipeline calls:
   stitcher.exe, subtitle_forge.exe, havaldar_core.exe, normalizer.exe,
   tgup.exe  (the Go uploader — the slow path exists only as a fallback)
2. Python dependencies — gradio, telethon, yt-dlp, kaggle, requests
3. System tools — ffmpeg, ffprobe on PATH
4. Configuration — kaggle.json, t_dubber.db
5. havaldar_core runnable (not just present) — see havaldar.check_havaldar_core

Exit code is 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Check definitions
# ---------------------------------------------------------------------------

# The native binaries the pipeline calls.  Each entry is (label, path).
# Paths are relative to the repo root and use the Windows build layout.
#
# tgup.exe lives at the repo root (build.ps1 emits it there) and is the
# fast Telegram upload path; without it every upload silently falls back
# to the slower Telethon path.  It used to be the one binary never checked,
# so a missing build only showed up as a mystery speed drop.
BINARY_CHECKS = [
    ("stitcher",        APP_DIR / "stitcher" / "target" / "release" / "stitcher.exe"),
    ("subtitle_forge",  APP_DIR / "subtitle_forge" / "target" / "release" / "subtitle_forge.exe"),
    ("havaldar_core",   APP_DIR / "havaldar_core" / "target" / "release" / "havaldar_core.exe"),
    ("normalizer",      APP_DIR / "cpp_accelerator" / "build" / "normalizer.exe"),
    ("tgup",            APP_DIR / "tgup.exe"),
]

# Python packages the pipeline imports.  Each entry is (module_name, pip_name).
PYTHON_DEPS = [
    ("gradio",   "gradio"),
    ("telethon", "telethon"),
    ("yt_dlp",   "yt-dlp"),
    ("kaggle",   "kaggle"),
    ("requests", "requests"),
]

# System tools that must be on PATH.
SYSTEM_TOOLS = ["ffmpeg", "ffprobe"]

# Configuration files that should exist.
CONFIG_FILES = [
    ("kaggle.json",  APP_DIR / "kaggle_paperWork" / "kaggle.json"),
    ("t_dubber.db",  APP_DIR / "t_dubber.db"),
]


def check_binaries() -> list[tuple[str, bool, str]]:
    """Check that each native binary exists and is non-empty."""
    results = []
    for label, path in BINARY_CHECKS:
        ok = path.is_file() and path.stat().st_size > 0
        size = f"{path.stat().st_size:,} B" if ok else "missing"
        results.append((label, ok, str(path.relative_to(APP_DIR)) + f"  ({size})"))
    return results


def check_python_deps() -> list[tuple[str, bool, str]]:
    """Check that each Python dependency is importable."""
    results = []
    for module, pip_name in PYTHON_DEPS:
        spec = importlib.util.find_spec(module)
        ok = spec is not None
        version = ""
        if ok:
            try:
                mod = __import__(module)
                version = f"  v{mod.__version__}" if hasattr(mod, "__version__") else ""
            except Exception:
                version = "  (import failed)"
        results.append((pip_name, ok, "installed" + version if ok else "NOT INSTALLED"))
    return results


def check_system_tools() -> list[tuple[str, bool, str]]:
    """Check that each system tool is on PATH."""
    results = []
    for tool in SYSTEM_TOOLS:
        path = shutil.which(tool)
        ok = path is not None
        results.append((tool, ok, path or "not on PATH"))
    return results


def check_config() -> list[tuple[str, bool, str]]:
    """Check that each configuration file exists."""
    results = []
    for label, path in CONFIG_FILES:
        ok = path.is_file()
        results.append((label, ok, str(path.relative_to(APP_DIR)) if ok else str(path.relative_to(APP_DIR)) + "  (missing)"))
    return results


def check_havaldar_runtime() -> list[tuple[bool, str, str]]:
    """Probe havaldar_core --version, so 'file exists' is not mistaken for 'runs'.

    Delegates to havaldar.check_havaldar_core(); that helper existed but was
    never called from anywhere, so this is its first caller.  Import is lazy so
    doctor.py still runs when havaldar.py itself cannot be imported.
    """
    try:
        from havaldar import check_havaldar_core
    except Exception as exc:  # noqa: BLE001 -- a broken helper must not kill the audit
        return [(False, "havaldar_core", f"could not import havaldar.py  ({exc})")]

    info = check_havaldar_core()
    if not info.get("present"):
        return [(False, "havaldar_core", info.get("reason", "not built"))]
    if info.get("runnable"):
        return [(True, "havaldar_core", info.get("version") or info.get("path", ""))]
    return [(False, "havaldar_core", info.get("reason", "will not run"))]


def main() -> int:
    parser = argparse.ArgumentParser(description="T_Dubber pre-flight system audit")
    parser.add_argument("--quiet", action="store_true", help="suppress output; exit code only")
    args = parser.parse_args()

    if args.quiet:
        # Silence all output; just compute the exit code.
        import io
        sys.stdout = io.StringIO()

    print("=" * 72)
    print("  T_Dubber Pre-Flight System Audit")
    print("=" * 72)
    print()

    all_ok = True

    # -- 1. Native binaries ---------------------------------------------------
    print("[1/5] Native binaries")
    for label, ok, detail in check_binaries():
        mark = "OK  " if ok else "FAIL"
        print(f"  {mark}  {label:<16} {detail}")
        all_ok = all_ok and ok
    print()

    # -- 2. Python dependencies -----------------------------------------------
    print("[2/5] Python dependencies")
    for name, ok, detail in check_python_deps():
        mark = "OK  " if ok else "FAIL"
        print(f"  {mark}  {name:<16} {detail}")
        all_ok = all_ok and ok
    print()

    # -- 3. System tools ------------------------------------------------------
    print("[3/5] System tools")
    for tool, ok, detail in check_system_tools():
        mark = "OK  " if ok else "FAIL"
        print(f"  {mark}  {tool:<16} {detail}")
        all_ok = all_ok and ok
    print()

    # -- 4. Configuration -----------------------------------------------------
    print("[4/5] Configuration")
    for label, ok, detail in check_config():
        mark = "OK  " if ok else "FAIL"
        print(f"  {mark}  {label:<16} {detail}")
        all_ok = all_ok and ok
    print()

    # -- 5. havaldar_core runnable ---------------------------------------------
    # "present" is not the same as "runs": a non-executable or Smart-App-Control
    # blocked .exe passes check_binaries() but dies on spawn.  This wires up
    # havaldar.check_havaldar_core(), which its docstring has been promising to
    # doctor.py for a while and which nothing else ever called.
    print("[5/5] havaldar_core runtime")
    for ok, label, detail in check_havaldar_runtime():
        mark = "OK  " if ok else "FAIL"
        print(f"  {mark}  {label:<16} {detail}")
        all_ok = all_ok and ok
    print()

    # -- Summary --------------------------------------------------------------
    print("=" * 72)
    if all_ok:
        print("  System Launch Readiness: PASS")
        print("  All checks passed. The pipeline is ready to launch.")
    else:
        print("  System Launch Readiness: FAIL")
        print("  One or more checks failed. See above for details.")
    print("=" * 72)

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
