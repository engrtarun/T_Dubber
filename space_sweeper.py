#!/usr/bin/env python3
"""
space_sweeper.py — T_Dubber "Space Debris" cleanup for Kaggle
=============================================================
Safely deletes old, keyword-matched Kaggle Datasets and Kernels created by the
T_Dubber dubbing pipeline, reclaiming storage quota without touching anything
important.

SAFETY GUARDRAILS (all enabled by default):
  * DRY-RUN by default. Real deletion requires BOTH --execute AND --yes
    (or an interactive confirmation prompt).
  * Only items owned by the authenticated user are considered.
  * Only items older than --min-age-hours (default 24) are considered.
  * Only items whose title OR slug contains one of --keywords are considered.
  * Protected refs (hardcoded + --protect + KAGGLE_PROTECTED_REFS env) are
    NEVER touched.
  * Items with missing/unparseable timestamps are skipped (never delete what
    we cannot verify).
  * Every action is appended to a JSONL audit log.
  * A hard cap (--max-deletes) can bound the blast radius of a bad keyword.

Cron (Linux/macOS) — every night at 03:00:
    0 3 * * * /usr/bin/python3 /opt/t_dubber/space_sweeper.py --execute --yes >> /var/log/space_sweeper.log 2>&1

Windows Task Scheduler — every night at 03:00:
    schtasks /Create /TN "T_Dubber_SpaceSweeper" /TR "python C:\t_dubber\space_sweeper.py --execute --yes" /SC DAILY /ST 03:00 /F

Auth: requires KAGGLE_USERNAME + KAGGLE_KEY env vars or ~/.kaggle/kaggle.json.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
DEFAULT_KEYWORDS = ["dubbing", "t-dubber", "worker-homura", "dubbing input"]
DEFAULT_MIN_AGE_HOURS = 24
DEFAULT_AUDIT_FILE = "space_sweeper_audit.jsonl"

# Hardcoded never-touch list. Add your important refs here, e.g.:
# PROTECTED_REFS = {"myuser/important-dataset", "myuser/another-one"}
PROTECTED_REFS: set[str] = set()

# --------------------------------------------------------------------------- #
# Console colors (auto-disabled when not a TTY or --no-color)
# --------------------------------------------------------------------------- #
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"


NO_COLOR = False


def paint(text: str, *codes: str) -> str:
    if NO_COLOR:
        return text
    return "".join(codes) + text + C.RESET


def hr(char: str = "─", n: int = 62) -> str:
    return paint(char * n, C.DIM)


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def human_size(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def ensure_datetime(value):
    """Normalize a Kaggle timestamp (datetime or ISO string) to an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_int(value) -> int:
    if value is None:
        return 0
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return 0


def age_hours(dt: datetime, now: datetime) -> float:
    return (now - dt).total_seconds() / 3600.0


# --------------------------------------------------------------------------- #
# Kaggle abstraction: official python module, with CLI fallback
# --------------------------------------------------------------------------- #
def get_api():
    """Return (api, mode). mode is 'module' or 'cli'."""
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi
        api = KaggleApi()
        api.authenticate()
        return api, "module"
    except ImportError:
        return None, "cli"
    except Exception as exc:  # auth failure
        print(paint(f"❌ kaggle authentication failed: {exc}", C.RED))
        sys.exit(1)


def get_username() -> str:
    """Resolve the authenticated username from env or ~/.kaggle/kaggle.json."""
    u = os.environ.get("KAGGLE_USERNAME")
    if u:
        return u
    cfg = os.path.expanduser("~/.kaggle/kaggle.json")
    if os.path.exists(cfg):
        try:
            with open(cfg, "r", encoding="utf-8") as f:
                u = json.load(f).get("username")
            if u:
                return u
        except Exception:
            pass
    print(paint("❌ Could not determine Kaggle username. Set KAGGLE_USERNAME or "
                "ensure ~/.kaggle/kaggle.json exists.", C.RED))
    sys.exit(1)


def _run_cli(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"CLI failed: {' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc.stdout


def _cli_rows(base_args: list[str]) -> list[dict]:
    """Paginate a `kaggle ... list --csv` command and return lowercased rows."""
    rows: list[dict] = []
    page = 1
    while True:
        cmd = base_args + ["--page", str(page), "--page-size", "100", "--csv"]
        out = _run_cli(cmd)
        reader = csv.DictReader(io.StringIO(out))
        batch = []
        for raw in reader:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if row:
                batch.append(row)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return rows


def _pick(row: dict, *keys: str):
    for k in keys:
        if k in row and row[k] != "":
            return row[k]
    return None


def _norm(ref: str, title: str, updated, bytes_) -> dict:
    return {
        "ref": ref,
        "title": title or "",
        "slug": ref.split("/", 1)[-1] if ref else "",
        "updated": ensure_datetime(updated),
        "bytes": parse_int(bytes_),
    }


def list_datasets(api, mode: str) -> list[dict]:
    if mode == "module":
        out: list[dict] = []
        page = 1
        while True:
            batch = api.datasets_list(mine=True, page=page, page_size=100) or []
            for d in batch:
                ref = getattr(d, "ref", None)
                if not ref:
                    continue
                out.append(_norm(ref, getattr(d, "title", ""),
                                 getattr(d, "last_updated", None),
                                 getattr(d, "total_bytes", 0)))
            if len(batch) < 100:
                break
            page += 1
        return out
    # CLI fallback
    out = []
    for row in _cli_rows(["kaggle", "datasets", "list", "--mine"]):
        ref = _pick(row, "ref")
        if not ref:
            continue
        out.append(_norm(ref, _pick(row, "title"),
                         _pick(row, "lastupdated", "last_updated"),
                         _pick(row, "totalbytes", "total_bytes")))
    return out


def list_kernels(api, mode: str) -> list[dict]:
    if mode == "module":
        out: list[dict] = []
        page = 1
        while True:
            batch = api.kernels_list(mine=True, page=page, page_size=100) or []
            for k in batch:
                ref = getattr(k, "ref", None)
                if not ref:
                    continue
                out.append(_norm(ref, getattr(k, "title", ""),
                                 getattr(k, "last_run_time", None),
                                 getattr(k, "total_bytes", 0)))
            if len(batch) < 100:
                break
            page += 1
        return out
    # CLI fallback
    out = []
    for row in _cli_rows(["kaggle", "kernels", "list", "--mine"]):
        ref = _pick(row, "ref")
        if not ref:
            continue
        out.append(_norm(ref, _pick(row, "title"),
                         _pick(row, "lastruntime", "last_run_time"),
                         _pick(row, "totalbytes", "total_bytes")))
    return out


def delete_dataset(api, mode: str, ref: str) -> None:
    if mode == "module":
        api.dataset_delete(ref)
    else:
        _run_cli(["kaggle", "datasets", "delete", ref, "-y"])


def delete_kernel(api, mode: str, ref: str) -> None:
    if mode == "module":
        api.kernels_delete(ref)
    else:
        _run_cli(["kaggle", "kernels", "delete", ref, "-y"])


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #
def is_candidate(item: dict, keywords: list[str], min_age_hours: float,
                 username: str, protected: set[str], now: datetime):
    """Return (True, reason) if the item should be deleted, else (False, reason)."""
    ref = item["ref"]

    # 1) Ownership — belt and suspenders on top of mine=True.
    if not ref.lower().startswith(username.lower() + "/"):
        return False, "not owned by you"

    # 2) Protected list.
    if ref in protected:
        return False, "protected ref"

    # 3) Age — skip if we cannot verify the timestamp.
    updated = item["updated"]
    if updated is None:
        return False, "no timestamp (skipped)"
    age = age_hours(updated, now)
    if age < min_age_hours:
        return False, f"too recent ({age:.1f}h)"

    # 4) Keyword match against title OR slug.
    text = f"{item['title']} {item['slug']}".lower()
    if not any(kw.lower() in text for kw in keywords):
        return False, "no keyword match"

    return True, f"age {age:.1f}h"


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
def audit(audit_file: str, entry: dict) -> None:
    d = os.path.dirname(os.path.abspath(audit_file))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(audit_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def print_scan(username: str, n_ds: int, n_k: int, args, n_protected: int) -> None:
    print()
    print(paint("🧹 T_Dubber Space Sweeper", C.BOLD, C.CYAN))
    print(hr())
    print(f"  👤 Account:      {paint('@' + username, C.BOLD)}")
    print(f"  🔎 Scanned:      {n_ds} datasets · {n_k} kernels")
    print(f"  🛡️  Filters:      age ≥ {args.min_age_hours}h · "
          f"keywords: {paint(', '.join(args.keywords), C.YELLOW)}")
    print(f"  🚫 Protected:    {n_protected} ref(s) in never-touch list")
    if not args.execute:
        print(paint("  🧪 DRY-RUN mode — nothing will be deleted. "
                    "Re-run with --execute --yes to delete.", C.MAGENTA))
    print(hr())


def print_candidate(kind: str, item: dict, reason: str) -> None:
    icon = "📦" if kind == "dataset" else "📓"
    size = human_size(item["bytes"])
    age = age_hours(item["updated"], datetime.now(timezone.utc)) if item["updated"] else 0
    print(f"    {icon} {paint(item['ref'], C.BOLD)}")
    print(f"       title: {item['title'] or '(untitled)'}")
    print(f"       {reason} · {size} · updated {age:.1f}h ago")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sweep old T_Dubber datasets/kernels from Kaggle.")
    p.add_argument("--execute", action="store_true",
                   help="Actually delete (default is dry-run).")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip the interactive confirmation prompt.")
    p.add_argument("--keywords", nargs="+", default=DEFAULT_KEYWORDS,
                   help="Keywords to match in title/slug (default: %(default)s).")
    p.add_argument("--min-age-hours", type=float, default=DEFAULT_MIN_AGE_HOURS,
                   help="Only delete items older than this (default: %(default)s).")
    p.add_argument("--protect", action="append", default=[],
                   help="Ref to never delete (repeatable).")
    p.add_argument("--max-deletes", type=int, default=0,
                   help="Hard cap on deletions per run; 0 = unlimited (default: 0).")
    p.add_argument("--audit-file", default=DEFAULT_AUDIT_FILE,
                   help="JSONL audit log path (default: %(default)s).")
    p.add_argument("--no-color", action="store_true", help="Disable ANSI colors.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    global NO_COLOR
    NO_COLOR = args.no_color or not sys.stdout.isatty()

    api, mode = get_api()
    username = get_username()

    # Merge protected refs: hardcoded + CLI + env.
    protected = set(PROTECTED_REFS)
    protected.update(args.protect)
    env_protected = os.environ.get("KAGGLE_PROTECTED_REFS", "")
    protected.update(r.strip() for r in env_protected.split(",") if r.strip())

    now = datetime.now(timezone.utc)

    # --- Scan -------------------------------------------------------------
    print(paint("🔍 Scanning your Kaggle account...", C.DIM))
    datasets = list_datasets(api, mode)
    kernels = list_kernels(api, mode)

    ds_candidates, ds_skipped = [], []
    for d in datasets:
        ok, reason = is_candidate(d, args.keywords, args.min_age_hours, username, protected, now)
        (ds_candidates if ok else ds_skipped).append((d, reason))

    k_candidates, k_skipped = [], []
    for k in kernels:
        ok, reason = is_candidate(k, args.keywords, args.min_age_hours, username, protected, now)
        (k_candidates if ok else k_skipped).append((k, reason))

    print_scan(username, len(datasets), len(kernels), args, len(protected))

    if not ds_candidates and not k_candidates:
        print(paint("✨ Nothing to do — no matching debris found. Your account is clean!", C.GREEN))
        print(hr())
        return 0

    # --- Show candidates ---------------------------------------------------
    print(paint(f"🗑️  Candidates: {len(ds_candidates)} datasets · {len(k_candidates)} kernels",
                C.BOLD))
    for item, reason in ds_candidates:
        print_candidate("dataset", item, reason)
    for item, reason in k_candidates:
        print_candidate("kernel", item, reason)
    if ds_skipped or k_skipped:
        print(paint(f"  ⏭️  Skipped {len(ds_skipped) + len(k_skipped)} item(s) "
                    "(too recent / no match / protected).", C.DIM))
    print(hr())

    # --- Safety cap --------------------------------------------------------
    total_candidates = len(ds_candidates) + len(k_candidates)
    if args.max_deletes and total_candidates > args.max_deletes:
        print(paint(f"⚠️  Candidate count ({total_candidates}) exceeds --max-deletes "
                    f"({args.max_deletes}). Aborting — check your keywords!", C.RED))
        return 1

    # --- Delete (only with --execute) --------------------------------------
    if not args.execute:
        would_free = sum(i["bytes"] for i, _ in ds_candidates) + sum(i["bytes"] for i, _ in k_candidates)
        print(paint(f"💾 Space that WOULD be freed: {human_size(would_free)}", C.CYAN))
        print(paint("🧪 Dry-run complete. Re-run with --execute --yes to delete.", C.MAGENTA))
        print(hr())
        return 0

    # Confirmation prompt unless --yes.
    if not args.yes:
        ans = input(paint(f"⚠️  Permanently delete {len(ds_candidates)} dataset(s) and "
                          f"{len(k_candidates)} kernel(s)? [y/N] ", C.YELLOW))
        if ans.strip().lower() not in ("y", "yes"):
            print(paint("🛑 Aborted. Nothing was deleted.", C.RED))
            return 0

    # --- Execute deletions --------------------------------------------------
    freed = 0
    deleted = 0
    failed = 0

    for kind, candidates in (("dataset", ds_candidates), ("kernel", k_candidates)):
        for item, reason in candidates:
            ref = item["ref"]
            try:
                if kind == "dataset":
                    delete_dataset(api, mode, ref)
                else:
                    delete_kernel(api, mode, ref)
                freed += item["bytes"]
                deleted += 1
                print(paint(f"  ✅ Deleted {kind}: {ref} ({human_size(item['bytes'])})", C.GREEN))
                audit(args.audit_file, {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "action": "delete", "kind": kind, "ref": ref,
                    "title": item["title"], "bytes": item["bytes"],
                })
            except Exception as exc:  # noqa: BLE001 — one failure must not abort the sweep
                failed += 1
                print(paint(f"  ❌ Failed to delete {kind} {ref}: {exc}", C.RED))
                audit(args.audit_file, {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "action": "error", "kind": kind, "ref": ref,
                    "title": item["title"], "error": str(exc),
                })
            time.sleep(1)  # be polite to the Kaggle API

    # --- Summary -----------------------------------------------------------
    print(hr())
    print(paint("🎉 Sweep complete!", C.BOLD, C.GREEN))
    print(f"  🗑️  Deleted:  {deleted} item(s)")
    if failed:
        print(paint(f"  ⚠️  Failed:   {failed} item(s) — see audit log", C.YELLOW))
    print(f"  💾 Freed:     {paint(human_size(freed), C.BOLD, C.CYAN)}")
    print(f"  🧾 Audit log: {args.audit_file}")
    print(hr())
    return 0 if failed == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
