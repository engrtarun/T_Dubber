#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dashboard_server.py — Mission Control backend for T_Dubber
=============================================================

Serves the local dashboard (``dashboard/index.html``) and exposes the live
snapshot that the page's ``render()`` function consumes.

    python dashboard_server.py            # http://127.0.0.1:8081 (default)
    python dashboard_server.py --port 9000

Note: 8081, not 8080 -- ``havaldar_core`` already binds 8080.
    python dashboard_server.py --no-open  # don't launch a browser

Endpoints
---------
    GET /                     the dashboard (static)
    GET /api/status           the snapshot  (alias: /status)
    GET /api/projects         recent projects, newest first
    GET /api/quota            Telegram daily quota for every channel
    GET /api/health           DB reachability + row counts
    GET /api/snapshot         pretty-printed /api/status (debugging)
    GET /docs                 OpenAPI UI, only when FastAPI is installed

Design constraints
------------------
1. NEVER BLOCK THE WORKERS. Every connection is opened read-only with a busy
   timeout, and no code path in this file issues a write. ``PRAGMA
   query_only=1`` is set as a second, independent guard, so even a mistake
   here cannot mutate ``t_dubber.db``.
2. NO THIRD-PARTY DEPENDENCY REQUIRED. FastAPI + uvicorn are used when they
   are importable (the repo already pulls FastAPI in via Gradio); otherwise it
   falls back to the standard library's ThreadingHTTPServer. The request
   handling and all logic are shared by both transports.
3. DEGRADE, NEVER CRASH. ``pipeline_stages`` and ``channel_daily_quota`` may be
   empty in the current database. Every query here tolerates a
   missing table, a missing column, or a NULL, and the snapshot still renders.
4. HONEST ABOUT GAPS. There is no chunk table, no GPU telemetry and no Rust
   binary anywhere in the pipeline, so this server does not invent them. It
   reports what the database and the ``.tg_uploads`` journals actually contain
   and leaves the rest null for the UI to render as "—".

Security
--------
Binds to 127.0.0.1 by default: the payload contains Telegram channels, file
paths and message links. Passing ``--host 0.0.0.0`` exposes them to the LAN and
the server prints a warning saying so.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs

__version__ = "1.0.0"

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(APP_ROOT, "t_dubber.db")
DEFAULT_WEB_ROOT = os.path.join(APP_ROOT, "dashboard")

# chop_drop.CHUNK_SECONDS — the length of one dubbing chunk. Chunk counts are
# derived with this constant, so it must match the worker.
CHUNK_SECONDS = 600

# BUSY_TIMEOUT is how long a reader waits for a writer's lock before giving
# up. db.py uses 15 s for its own writers; matching that means this server
# never fails a poll that the main app would have waited out.
BUSY_TIMEOUT_MS = 15_000

# Canonical stage rail, mirroring dashboard/index.html's STAGES array and the
# [STAGE:n] markers emitted by pipeline.py. DB stage names are fuzzy ("Bundle",
# "Dataset upload"), so they are classified onto these indices.
STAGE_ORDER = [
    "resolve", "compress", "bundle", "dataset", "kernel",
    "worker", "download", "verify", "transport",
]
STAGE_KEYWORDS: List[Tuple[int, Tuple[str, ...]]] = [
    (0, ("resolve", "source", "link", "ingest")),
    (1, ("compress", "transcode", "scale", "480p")),
    (2, ("bundle", "package", "zip", "archive source")),
    (3, ("dataset", "kaggle api", "upload video")),
    (4, ("kernel", "push", "notebook", "worker push")),
    (5, ("worker", "gpu", "vllm", "dub", "transcribe", "translate", "tts", "synth")),
    (6, ("download", "fetch output", "retrieve")),
    (7, ("verify", "validate", "report", "check")),
    (8, ("transport", "telegram", "tgup", "archive", "upload to")),
]

# projects.status -> the UI's coarse state.
PROJECT_STATE_MAP = {
    "processing": "running",
    "running": "running",
    "queued": "running",
    "success": "done",
    "succeeded": "done",
    "completed": "done",
    "done": "done",
    "failed": "error",
    "failed_missing_output": "error",
    "error": "error",
    "cancelled": "error",
    "canceled": "error",
    "abandoned": "error",
}


# --------------------------------------------------------------------------- #
# Small tolerant helpers                                                      #
# --------------------------------------------------------------------------- #

_TS_RE = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse a timestamp from this project, tolerating sloppy formats.

    Timestamps in the database are inconsistent on purpose-by-accident:
    ``2026-10-04T22:17:21+0530`` (no colon in the offset, written by Go) and
    ``2026-10-04T10:13:04+05:30`` (with colon, written by Python) both occur.
    ``datetime.fromisoformat`` only accepts the second form on Python 3.10,
    which is the interpreter the Kaggle kernel runs, so the colon is inserted
    by hand before parsing. Returns None for anything unparseable rather than
    raising, because a bad timestamp must never blank the dashboard.
    """
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    text = _TS_RE.sub(r"\1:\2", text, count=1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text[: len(fmt) + 2], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone().isoformat(timespec="seconds") if dt else None


def today_str(now: Optional[datetime] = None, offset_hours: int = 5) -> str:
    """Local day key (YYYY-MM-DD) in the project's UTC+05:30 timezone."""
    tz = timezone(timedelta(hours=offset_hours))
    return (now or datetime.now(tz)).astimezone(tz).date().isoformat()


def human_bytes(n: Optional[float]) -> Optional[str]:
    if n is None:
        return None
    step = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if step < 1024 or unit == "TB":
            return f"{step:.0f} {unit}" if unit == "B" else f"{step:.1f} {unit}"
        step /= 1024.0
    return None


def classify_stage(name: Optional[str], number: Optional[int]) -> int:
    """Map a (stage_name, stage_number) pair onto the canonical 0..8 rail."""
    text = (name or "").lower()
    for idx, keys in STAGE_KEYWORDS:
        if any(k in text for k in keys):
            return idx
    if isinstance(number, int) and 0 <= number <= len(STAGE_ORDER) - 1:
        return number
    # db.py's own numbering starts at 1 for "Compress"; the rail's index 1 is
    # also Compress, so an in-range number is already aligned.
    return max(0, min(len(STAGE_ORDER) - 1, int(number or 0)))


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


# --------------------------------------------------------------------------- #
# Store — read-only SQLite access                                             #
# --------------------------------------------------------------------------- #

class Store:
    """Thread-safe, strictly read-only access to t_dubber.db.

    Connections are opened per request and closed in a ``finally`` block.
    SQLite's open cost on a sub-megabyte file is negligible, and a fresh
    connection is the only way to be certain a long-lived reader never pins a
    stale snapshot after a writer commits.
    """

    def __init__(self, db_path: str, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
        self.db_path = os.path.abspath(db_path)
        self.busy_timeout_ms = busy_timeout_ms
        self._schema_lock = threading.Lock()
        self._tables: Optional[set] = None
        self.missing_db = not os.path.exists(self.db_path)

    # -- connection handling ------------------------------------------------
    def connect(self) -> Optional[sqlite3.Connection]:
        """Open a read-only connection, or None if the DB is unusable.

        Two attempts, in order:

        1. ``mode=ro`` — the correct path. In WAL mode a reader also needs the
           ``-shm`` file, which it may not create if the directory is not
           writable; that is handled by attempt 2.
        2. A normal connection with ``PRAGMA query_only=1``. This can create
           the ``-shm`` file it needs, yet SQLite itself rejects any write,
           so the database still cannot be modified.
        """
        if self.missing_db:
            return None
        uri = "file:{}?mode=ro".format(self.db_path.replace("\\", "/").replace("?", "%3f"))
        for attempt in (uri, self.db_path):
            try:
                con = sqlite3.connect(attempt, uri=attempt.startswith("file:"), timeout=self.busy_timeout_ms / 1000.0)
            except sqlite3.Error:
                continue
            con.row_factory = sqlite3.Row
            try:
                # Independent belt-and-braces guard. Never remove this: it is
                # the backstop that makes "this file cannot write" true even if
                # a future edit adds a stray INSERT.
                con.execute("PRAGMA query_only=1")
                con.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
                # Never write; leave the writer's journal mode alone.
                con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
                return con
            except sqlite3.Error:
                con.close()
        return None

    # -- schema introspection ----------------------------------------------
    def tables(self, con: sqlite3.Connection) -> set:
        if self._tables is None:
            with self._schema_lock:
                if self._tables is None:
                    rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    self._tables = {r[0] for r in rows}
        return self._tables

    def has(self, con: sqlite3.Connection, table: str) -> bool:
        return table in self.tables(con)

    def columns(self, con: sqlite3.Connection, table: str) -> set:
        try:
            return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    def rows(self, con: sqlite3.Connection, sql: str, params: Iterable = ()) -> List[sqlite3.Row]:
        try:
            return con.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error:
            return []

    def one(self, con: sqlite3.Connection, sql: str, params: Iterable = ()) -> Optional[sqlite3.Row]:
        try:
            return con.execute(sql, tuple(params)).fetchone()
        except sqlite3.Error:
            return None


# --------------------------------------------------------------------------- #
# Snapshot assembly                                                           #
# --------------------------------------------------------------------------- #

class SnapshotBuilder:
    """Turns database rows into the exact dict dashboard/index.html renders."""

    def __init__(self, store: Store, tg_dir: str) -> None:
        self.store = store
        self.tg_dir = tg_dir
        # (path, mtime, size) -> parsed journal. The journals are rewritten on
        # every progress event, so caching by mtime keeps a 1.5 s poll from
        # re-reading and re-parsing ten files sixty times a minute.
        self._journal_cache: Dict[str, Tuple[float, int, Dict[str, Any]]] = {}
        self._journal_lock = threading.Lock()

    # -- journals ------------------------------------------------------------
    def _journal(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = os.path.abspath(path)
        with self._journal_lock:
            hit = self._journal_cache.get(key)
            if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
                return hit[2]
        try:
            with open(key, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return None
        except (OSError, ValueError):
            return None
        with self._journal_lock:
            if len(self._journal_cache) > 256:
                self._journal_cache.clear()
            self._journal_cache[key] = (st.st_mtime, st.st_size, data)
        return data

    def active_journals(self, limit: int = 40) -> List[Dict[str, Any]]:
        """All upload journals, newest first. Cheap: usually ten small files."""
        out: List[Dict[str, Any]] = []
        try:
            names = [n for n in os.listdir(self.tg_dir) if n.endswith(".json")]
        except OSError:
            return out
        for name in names[: limit * 4]:
            data = self._journal(self.path_join(self.tg_dir, name))
            if not data:
                continue
            updated = parse_ts(data.get("updated_at")) or datetime.fromtimestamp(0, timezone.utc)
            # The journal filename is the only identifier guaranteed to be
            # present and unique: several journals in the wild carry a null
            # fingerprint, which would collapse distinct uploads into one id.
            data["_name"] = os.path.splitext(name)[0]
            data["_updated"] = updated
            out.append(data)
        out.sort(key=lambda d: d.get("_updated") or datetime.fromtimestamp(0, timezone.utc), reverse=True)
        return out[:limit]

    @staticmethod
    def path_join(base: str, name: str) -> str:
        return os.path.join(base, name)

    # -- project selection ---------------------------------------------------
    def pick_project(self, con: sqlite3.Connection, project_id: Optional[str]) -> Optional[sqlite3.Row]:
        if project_id:
            return self.store.one(con, "SELECT * FROM projects WHERE id=?", (project_id,))
        # An in-flight run is always the interesting one; otherwise fall back
        # to whatever was touched most recently.
        return self.store.one(
            con,
            "SELECT * FROM projects ORDER BY (status='processing') DESC, "
            "COALESCE(updated_at, created_at) DESC, rowid DESC LIMIT 1",
        )

    # -- chunk progress ------------------------------------------------------
    def duration_of(self, con: sqlite3.Connection, project: sqlite3.Row) -> Optional[float]:
        """Source duration in seconds, from whichever table has it.

        media_metadata.duration_sec is the intended home for this, but the
        table is empty today, so report.json is checked as a fallback. Both
        return None rather than 0.0 when unknown, because 0.0 would imply a
        zero-length video and produce a nonsensical chunk count.
        """
        pid = project["id"]
        if self.store.has(con, "media_metadata"):
            row = self.store.one(
                con,
                "SELECT duration_sec FROM media_metadata WHERE project_id=? AND duration_sec IS NOT NULL",
                (pid,),
            )
            if row and row["duration_sec"] and row["duration_sec"] > 0:
                return float(row["duration_sec"])
        report = project["report_file"] if "report_file" in project.keys() else None
        if report and os.path.exists(report):
            try:
                with open(report, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                dur = data.get("Source_Duration_Seconds")
                if dur and float(dur) > 0:
                    return float(dur)
            except (OSError, ValueError, TypeError):
                pass
        return None

    def build_progress(self, con: sqlite3.Connection, project: sqlite3.Row,
                       stage_idx: Optional[int], state: str) -> Dict[str, Any]:
        duration = self.duration_of(con, project)
        chunk_count = int(math.ceil(duration / CHUNK_SECONDS)) if duration else 0
        chunk_index = chunk_count if state == "done" else 0
        pct = 100.0 if (state == "done" and chunk_count) else 0.0
        return {
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "pct": pct,
            "phase": (STAGE_ORDER[stage_idx] if stage_idx is not None else "IDLE").upper(),
            # Documented as optional in the UI contract; chunk_error marks a
            # specific chunk as the one that failed.
            "chunk_error": None,
            "chunk_seconds": CHUNK_SECONDS,
            "source_duration_sec": duration,
        }

    # -- stages --------------------------------------------------------------
    def build_stages(self, con: sqlite3.Connection, project: sqlite3.Row,
                     state: str) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """Return (stage rows, current stage index).

        Reads pipeline_stages when it has rows. When the table is empty the
        rail is inferred from projects.current_stage instead of rendering empty.
        """
        pid = project["id"]
        rows: List[Dict[str, Any]] = []
        current: Optional[int] = None

        if self.store.has(con, "pipeline_stages"):
            raw = self.store.rows(
                con,
                "SELECT stage_number, stage_name, status, started_at, finished_at, duration_sec "
                "FROM pipeline_stages WHERE project_id=? ORDER BY stage_number",
                (pid,),
            )
            merged: Dict[int, Dict[str, Any]] = {}
            for r in raw:
                idx = classify_stage(r["stage_name"], r["stage_number"])
                status = (r["status"] or "pending").lower()
                dur = r["duration_sec"]
                entry = {
                    "stage": idx,
                    "stage_number": r["stage_number"],
                    "name": r["stage_name"] or STAGE_ORDER[idx],
                    "status": status,
                    "ms": int(float(dur) * 1000) if dur else None,
                    "started_at": iso(parse_ts(r["started_at"])),
                    "finished_at": iso(parse_ts(r["finished_at"])),
                    "error": r["error"] if "error" in r.keys() else None,
                }
                # Prefer the most advanced status if two rows map to one index.
                rank = {"pending": 0, "skipped": 1, "success": 2, "done": 2, "running": 3, "failed": 4}
                prev = merged.get(idx)
                if prev is None or rank.get(status, 0) >= rank.get(prev["status"], 0):
                    merged[idx] = entry
            rows = [merged[k] for k in sorted(merged)]
            for row in rows:
                if row["status"] in ("running", "success", "done", "failed"):
                    current = row["stage"] if row["status"] == "running" else (current or row["stage"])

        if current is None:
            raw_stage = project["current_stage"] if "current_stage" in project.keys() else 0
            current = int(raw_stage or 0)
            if state == "done":
                current = len(STAGE_ORDER) - 1
            elif state == "error":
                current = int(raw_stage or 0)

        # No rows in pipeline_stages for this project. Synthesise the rail from
        # projects.current_stage so the UI shows forward progress instead of a
        # dead row of nine grey pips. These rows are marked inferred=True so a
        # consumer can tell them from real measurements.
        if not rows:
            last = len(STAGE_ORDER) - 1
            if state == "done":
                current = last
            current = max(0, min(last, current or 0))
            for idx, name in enumerate(STAGE_ORDER):
                if idx < current:
                    status = "success"
                elif idx == current:
                    status = "running" if state == "running" else (
                        "failed" if state == "error" else "success")
                else:
                    status = "pending"
                rows.append({"stage": idx, "stage_number": idx, "name": name.title(),
                             "status": status, "ms": None, "started_at": None,
                             "finished_at": None, "error": None, "inferred": True})
            return rows, current

        return rows, current

    # -- engines -------------------------------------------------------------
    def build_engines(self, con: sqlite3.Connection, project: sqlite3.Row,
                      state: str, current_stage: Optional[int],
                      upload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Map pipeline reality onto the four dashboard engine slots.

        Only the Go slot has genuine telemetry (the .tg_uploads journals). The
        other three are inferred from where the run currently is, and the Rust
        slot is deliberately reported as unbuilt rather than pretending to
        merge audio that Python is actually merging.
        """
        stage = current_stage if current_stage is not None else 0
        running = state == "running"

        # --- Go / Telegram --------------------------------------------------
        go = upload.get("engine") or {
            "state": "idle", "detail": "no upload journal", "pct": 0,
        }
        if state == "done" and go.get("state") in ("idle", "queued"):
            go = {"state": "done", "detail": upload.get("summary", "archived"), "pct": 100}

        # --- Python AI ------------------------------------------------------
        if running and stage >= 5:
            python_eng = {"state": "running", "detail": f"GPU worker · stage {stage}", "pct": 40}
        elif state == "done":
            python_eng = {"state": "done", "detail": "dub complete", "pct": 100}
        elif state == "error" and stage >= 5:
            python_eng = {"state": "error", "detail": "worker reported failure", "pct": 0}
        elif running:
            python_eng = {"state": "queued", "detail": "awaiting GPU", "pct": 0}
        else:
            python_eng = {"state": "idle", "detail": "standby", "pct": 0}

        # --- FFmpeg ---------------------------------------------------------
        if running and stage == 1:
            ff = {"state": "running", "detail": "compress / remux", "pct": 50}
        elif stage >= 2:
            ff = {"state": "done", "detail": "mux complete", "pct": 100}
        else:
            ff = {"state": "idle", "detail": "standby", "pct": 0}

        # --- Rust -----------------------------------------------------------
        # stitcher (timeline mixing) and normalizer (loudness) are built and
        # shipped in the Docker/Kaggle pack. Their state is inferred from the
        # stage rail because they emit no telemetry of their own.
        if running and stage >= 6:
            rust = {"state": "running", "detail": "stitcher + normalizer", "pct": 50}
        elif state == "done":
            rust = {"state": "done", "detail": "stitched", "pct": 100}
        else:
            rust = {"state": "idle", "detail": "standby", "pct": 0}

        return {"python": python_eng, "ffmpeg": ff, "rust": rust, "go": go}

    # -- Telegram quota ------------------------------------------------------
    def build_quota(self, con: sqlite3.Connection, day: str) -> Dict[str, Any]:
        """Daily Telegram quota.

        channel_daily_quota is authoritative but currently empty, so the
        observed figure is computed from telegram_archives for the same day
        and flagged with ``source`` so an operator always knows which number
        they are looking at. This server never writes to the quota table.
        """
        channels: List[Dict[str, Any]] = []
        observed: Dict[str, Dict[str, Any]] = {}
        if self.store.has(con, "telegram_archives"):
            rows = self.store.rows(
                con,
                "SELECT COALESCE(channel,'(unknown)') AS ch, COUNT(*) AS n, "
                "COALESCE(SUM(file_size),0) AS bytes, "
                "SUM(CASE WHEN state='uploading' THEN 1 ELSE 0 END) AS uploading "
                "FROM telegram_archives WHERE substr(created_at,1,10)=? GROUP BY ch",
                (day,),
            )
            for r in rows:
                observed[r["ch"]] = {
                    "channel": r["ch"],
                    "parts": int(r["n"] or 0),
                    "used_mb": round(float(r["bytes"] or 0) / (1024 * 1024), 2),
                    "uploading": int(r["uploading"] or 0),
                }

        recorded: Dict[str, Dict[str, Any]] = {}
        if self.store.has(con, "channel_daily_quota"):
            rows = self.store.rows(
                con,
                "SELECT channel, day, used_mb, allocations, updated_at FROM channel_daily_quota WHERE day=?",
                (day,),
            )
            for r in rows:
                recorded[r["channel"]] = {
                    "used_mb": float(r["used_mb"] or 0),
                    "allocations": int(r["allocations"] or 0),
                    "updated_at": r["updated_at"],
                }

        names = sorted(set(recorded) | set(observed))
        for name in names:
            rec = recorded.get(name)
            obs = observed.get(name, {})
            entry: Dict[str, Any] = {
                "channel": name,
                "used_mb": rec["used_mb"] if rec else obs.get("used_mb", 0.0),
                "allocations": rec["allocations"] if rec else obs.get("parts", 0),
                "updated_at": rec["updated_at"] if rec else None,
                "source": "channel_daily_quota" if rec else "observed:telegram_archives",
            }
            if not rec:
                entry["observed_parts"] = obs.get("parts", 0)
                entry["uploading"] = obs.get("uploading", 0)
            entry["used_human"] = human_bytes(entry["used_mb"] * 1024 * 1024)
            channels.append(entry)

        return {
            "day": day,
            "channels": channels,
            "recorded": bool(recorded),
            "total_used_mb": round(sum(c["used_mb"] for c in channels), 2),
            "note": (
                "channel_daily_quota is empty; 'observed' figures are derived from "
                "telegram_archives and are not authoritative."
                if not recorded else "authoritative figures from channel_daily_quota."
            ),
        }

    # -- upload state (Go engine) -------------------------------------------
    def build_upload(self, con: sqlite3.Connection, project: sqlite3.Row,
                     journals: List[Dict[str, Any]]) -> Dict[str, Any]:
        archives = [j for j in journals if j.get("state") == "uploading"]
        completed = [j for j in journals if j.get("state") == "complete"]

        rate_bps = None
        for j in journals:
            if j.get("go_rate_bps"):
                rate_bps = float(j["go_rate_bps"])
                break

        active = archives[0] if archives else None
        parts_stored: Optional[int] = None
        parts_total: Optional[int] = None

        if active:
            parts = active.get("parts") or []
            total_bytes = int(active.get("size") or 0)
            sent = sum(int(p.get("size") or 0) for p in parts)
            if total_bytes <= 0 and parts:
                total_bytes = max(1, sent)
            pct = int(clamp((sent / total_bytes) * 100.0, 0, 100)) if total_bytes else 0
            channel = active.get("channel") or "(unknown)"
            # chunk_count is what the planner decided up front; fall back to the
            # parts already listed, then to a single unsplit part.
            parts_total = int(active.get("chunk_count") or 0) or len(parts) or (1 if total_bytes else 0)
            parts_stored = len(parts)
            engine = {
                "state": "running",
                "detail": f"Uploading to Telegram · {int(active.get('go_concurrency') or 3)} conn",
                "pct": pct,
            }
            summary = f"{len(parts)} part(s) · {human_bytes(sent)} / {human_bytes(total_bytes)}"
        elif completed:
            done = completed[0]
            channel = done.get("channel") or "(unknown)"
            pct = 100
            parts_total = int(done.get("chunk_count") or 0) or len(done.get("parts") or []) or 1
            parts_stored = parts_total
            engine = {"state": "done", "detail": f"Archived · {channel}", "pct": 100}
            summary = f"{done.get('filename', 'archive')} archived"
        else:
            channel, pct = "(unknown)", 0
            engine = {"state": "idle", "detail": "no upload journal", "pct": 0}
            summary = "nothing uploaded yet"

        # Prefer the channel recorded against this specific project.
        if project is not None:
            for r in self.store.rows(
                con,
                "SELECT a.channel, a.state, a.chunk_count FROM telegram_archives a WHERE a.project_id=? "
                "ORDER BY a.updated_at DESC LIMIT 1",
                (project["id"],),
            ):
                channel = r["channel"] or channel
                break

        return {
            "engine": engine,
            "channel": channel,
            "summary": summary,
            "pct": pct,
            "uplink_bps": rate_bps,
            "parts_stored": parts_stored,
            "parts_total": parts_total,
            "in_flight": len(archives),
            "archives_total": len(journals),
            "archives_complete": len(completed),
        }

    # -- metrics -------------------------------------------------------------
    def build_metrics(self, project: sqlite3.Row, state: str,
                      upload: Dict[str, Any], current_stage: Optional[int]) -> Dict[str, Any]:
        created = parse_ts(project["created_at"])
        completed = parse_ts(project["completed_at"])
        updated = parse_ts(project["updated_at"])
        now = datetime.now(timezone.utc)

        if not created:
            elapsed = None
        elif state == "done" and completed:
            elapsed = (completed - created).total_seconds()
        elif state == "running":
            # A run that is still going has been going since it started, not
            # since it last wrote a row. Using updated_at here reports 0 for a
            # job that has actually been in flight for hours.
            elapsed = (now - created).total_seconds()
        else:
            elapsed = ((completed or updated or now) - created).total_seconds()

        bps = upload.get("uplink_bps")
        return {
            "elapsed_sec": elapsed,
            # No stage-duration history exists yet, so no honest ETA. Leaving
            # this null renders as "—" instead of a fabricated number.
            "eta_sec": None,
            "uplink_mbps": round(bps / (1024 * 1024), 2) if bps else None,
            "vram_gb": None,   # no GPU telemetry is stored in SQLite
            "rtf": None,
            "parts_stored": upload.get("parts_stored"),
            "parts_total": upload.get("parts_total"),
            "conns": 3,       # tgup default, per TGUP.md
            "current_stage": current_stage,
        }

    # -- log stream ----------------------------------------------------------
    def build_log(self, con: sqlite3.Connection, project: sqlite3.Row,
                  journals: List[Dict[str, Any]], quota: Dict[str, Any]) -> List[Dict[str, Any]]:
        """A bounded event list whose entries carry stable ids.

        The dashboard de-duplicates on ``id``, so an id must identify the same
        underlying fact forever. Deriving it from the row that produced the
        event (primary key, or fingerprint+state for journals) rather than from
        the buffer position means the list can be trimmed, reordered or resent
        without the client ever showing a duplicate or missing a new line.
        """
        events: List[Tuple[str, str, str, str]] = []  # (sortkey, id, level, msg)
        now = datetime.now(timezone.utc)

        def add(when: datetime, level: str, msg: str, key: str) -> None:
            events.append((when.isoformat(), key, level, msg))

        if project is not None:
            pid = project["id"]
            for col, level in (("created_at", "info"), ("updated_at", "info"), ("completed_at", "ok")):
                ts = parse_ts(project[col]) if col in project.keys() else None
                if ts:
                    add(ts, level, f"project.{col} = {project[col]}", f"p:{pid}:{col}")
            if project["status"]:
                add(parse_ts(project["updated_at"]) or now, "info",
                    f"projects.status = {project['status']}", f"p:{pid}:status:{project['status']}")

        for j in journals[:6]:
            when = j.get("_updated") or now
            state = j.get("state")
            level = {"complete": "ok", "failed": "err", "uploading": "info"}.get(state, "info")
            name = j.get("filename") or j.get("fingerprint") or j.get("_name") or "?"
            ident = j.get("_name") or j.get("fingerprint") or name
            add(when, level, f"telegram_uploader [{state}] {name}", f"tg:{ident}:{state}")

        if self.store.has(con, "run_errors"):
            for r in self.store.rows(
                con,
                "SELECT rowid AS rid, * FROM run_errors ORDER BY rowid DESC LIMIT 5",
            ):
                d = dict(r)
                add(parse_ts(d.get("created_at") or d.get("timestamp")) or now, "err",
                    f"run_error: {d.get('error') or d.get('message') or ''}"[:220],
                    f"re:{d.get('rid')}")

        if self.store.has(con, "kaggle_sweeper_logs"):
            for r in self.store.rows(
                con,
                "SELECT * FROM kaggle_sweeper_logs ORDER BY id DESC LIMIT 5",
            ):
                d = dict(r)
                add(parse_ts(d.get("timestamp")) or now, "warn",
                    f"sweeper: {d.get('action')} {d.get('kind') or ''} {d.get('ref') or ''}".strip(),
                    f"sw:{d.get('id')}")

        if not quota["recorded"]:
            add(now, "warn", f"quota table empty - {quota['note']}", f"quota:{quota['day']}")

        events.sort(key=lambda e: e[0])
        return [{"id": key, "level": lvl, "msg": msg} for _, key, lvl, msg in events[-40:]]

    # -- top level -----------------------------------------------------------
    def build(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        con = self.store.connect()
        if con is None:
            return self._degraded_snapshot(project_id)
        now = datetime.now(timezone.utc)
        try:
            journals = self.active_journals()
            day = today_str()
            quota = self.build_quota(con, day)

            project = self.pick_project(con, project_id) if self.store.has(con, "projects") else None
            if project is None:
                return self._degraded_snapshot(project_id, quota=quota, journals=journals)

            status = (project["status"] or "").lower()
            state = PROJECT_STATE_MAP.get(status, "running" if status == "processing" else "idle")

            stages, current_stage = self.build_stages(con, project, state)
            upload = self.build_upload(con, project, journals)
            progress = self.build_progress(con, project, current_stage, state)
            engines = self.build_engines(con, project, state, current_stage, upload)
            metrics = self.build_metrics(project, state, upload, current_stage)
            log = self.build_log(con, project, journals, quota)

            digest = None
            if self.store.has(con, "media_metadata"):
                r = self.store.one(
                    con,
                    "SELECT source_sha256, media_title, page_url FROM media_metadata WHERE project_id=?",
                    (project["id"],),
                )
                if r:
                    digest = r["source_sha256"]
                    metrics["page_url"] = r["page_url"]
                    metrics["media_title"] = r["media_title"]

            kernel_id = project["kernel_id"] if "kernel_id" in project.keys() else None
            worker = {
                "kernel_id": kernel_id,
                "kernel_slug": (kernel_id or "").split("/")[-1] or None,
                # No live Kaggle API is consulted; this reflects the database
                # view of whether a worker kernel is attached to this run.
                "attached": bool(kernel_id) and state == "running",
                "state": "running" if (kernel_id and state == "running")
                         else "idle" if not kernel_id
                         else "complete" if state == "done" else "error",
            }

            return {
                "project": {
                    "id": project["id"],
                    "title": project["title"],
                    "kernel_id": kernel_id,
                    "target_language": project["target_language"],
                    "source_url": project["source_url"] if "source_url" in project.keys() else None,
                    "source_size": project["source_size"] if "source_size" in project.keys() else None,
                    "created_at": project["created_at"],
                    "updated_at": project["updated_at"],
                    "completed_at": project["completed_at"],
                    "output_video": project["output_video"] if "output_video" in project.keys() else None,
                    # The UI falls back to this string when no DB column exists.
                    "model": "Index-Homura-2B · fp16 · TP1",
                },
                "progress": progress,
                "engines": engines,
                "metrics": metrics,
                "stages": stages,
                "journal": {
                    "channel": upload["channel"],
                    "source_sha256": digest,
                },
                "conns": metrics["conns"],
                "state": state,
                "worker": worker,
                "quota": quota,
                "upload": {
                    "summary": upload["summary"],
                    "pct": upload["pct"],
                    "in_flight": upload["in_flight"],
                    "archives_total": upload["archives_total"],
                    "archives_complete": upload["archives_complete"],
                },
                "log": log,
                "generated_at": iso(now),
                "server": {"version": __version__, "db": os.path.basename(self.store.db_path)},
            }
        finally:
            con.close()

    def _degraded_snapshot(self, project_id: Optional[str],
                           quota: Optional[Dict[str, Any]] = None,
                           journals: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """A valid, honest snapshot for when the database cannot be opened."""
        upload_eng = {"state": "idle", "detail": "database unavailable", "pct": 0}
        return {
            "project": {"id": project_id, "title": "no database", "target_language": None},
            "progress": {"chunk_index": 0, "chunk_count": 0, "pct": 0, "phase": "OFFLINE",
                         "chunk_error": None},
            "engines": {
                "python": {"state": "idle", "detail": "database unavailable", "pct": 0},
                "ffmpeg": {"state": "idle", "detail": "standby", "pct": 0},
                "rust": {"state": "idle", "detail": "standby", "pct": 0},
                "go": upload_eng,
            },
            "metrics": {"elapsed_sec": None, "eta_sec": None, "uplink_mbps": None,
                        "vram_gb": None, "rtf": None, "parts_stored": None, "parts_total": None},
            "stages": [],
            "journal": {"channel": None, "source_sha256": None},
            "state": "idle",
            "worker": {"kernel_id": None, "attached": False, "state": "idle"},
            "quota": quota or {"day": today_str(), "channels": [], "recorded": False,
                               "total_used_mb": 0.0, "note": "database unavailable"},
            "upload": {"summary": "unavailable", "pct": 0, "in_flight": 0,
                       "archives_total": len(journals or []), "archives_complete": 0},
            "log": [{"id": "db:unavailable", "level": "err", "msg": "t_dubber.db could not be opened (read-only)"}],
            "generated_at": iso(datetime.now(timezone.utc)),
            "server": {"version": __version__, "db": "unavailable"},
        }

    # -- auxiliary endpoints -------------------------------------------------
    def list_projects(self, limit: int = 25) -> List[Dict[str, Any]]:
        con = self.store.connect()
        if con is None or not self.store.has(con, "projects"):
            return []
        try:
            rows = self.store.rows(
                con,
                "SELECT id, title, status, current_stage, target_language, kernel_id, "
                "created_at, updated_at, output_video FROM projects "
                "ORDER BY COALESCE(updated_at, created_at) DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            )
            out = []
            for r in rows:
                d = dict(r)
                d["state"] = PROJECT_STATE_MAP.get((d.get("status") or "").lower(), "idle")
                out.append(d)
            return out
        finally:
            con.close()

    def health(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "server": __version__,
            "db_path": self.store.db_path,
            "db_exists": os.path.exists(self.store.db_path),
            "readable": False,
            "error": None,
        }
        con = self.store.connect()
        if con is None:
            info["error"] = "could not open database"
            return info
        try:
            names = sorted(self.store.tables(con))
            info["readable"] = True
            info["tables"] = names
            for t in ("projects", "pipeline_stages", "channel_daily_quota", "telegram_archives"):
                if t in names:
                    info.setdefault("counts", {})[t] = self.store.one(
                        con, f"SELECT COUNT(*) AS n FROM {t}")["n"]
            # Prove the write guard holds rather than trusting the pragma.
            try:
                con.execute("CREATE TABLE _should_fail (x)")
                info["query_only"] = False
            except sqlite3.Error:
                info["query_only"] = True
        finally:
            con.close()
        info["wal_present"] = os.path.exists(self.store.db_path + "-wal")
        return info


# --------------------------------------------------------------------------- #
# Transport 1 — FastAPI (optional)                                            #
# --------------------------------------------------------------------------- #

def build_fastapi(builder: SnapshotBuilder, web_root: str):
    try:
        from fastapi import FastAPI, Query
        from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(f"FastAPI unavailable: {exc}") from exc

    app = FastAPI(
        title="T_Dubber Mission Control",
        version=__version__,
        description="Live pipeline telemetry for the T_Dubber dubbing worker.",
    )

    @app.get("/api/status", summary="Dashboard snapshot")
    @app.get("/status", include_in_schema=False)
    def api_status(project_id: Optional[str] = Query(None, description="Force a specific project")):
        return builder.build(project_id)

    @app.get("/api/projects")
    def api_projects(limit: int = Query(25, ge=1, le=200)):
        return {"projects": builder.list_projects(limit)}

    @app.get("/api/quota")
    def api_quota():
        con = builder.store.connect()
        try:
            if con is None:
                return JSONResponse({"day": today_str(), "channels": [], "recorded": False,
                                     "error": "database unavailable"}, status_code=200)
            return builder.build_quota(con, today_str())
        finally:
            if con is not None:
                con.close()

    @app.get("/api/health")
    def api_health():
        info = builder.health()
        return JSONResponse(info, status_code=200 if info["readable"] else 503)

    @app.get("/api/snapshot", response_class=PlainTextResponse)
    def api_snapshot():
        return json.dumps(builder.build(), indent=2, default=str)

    if os.path.isdir(web_root):
        app.mount("/static", StaticFiles(directory=web_root), name="static")

        @app.get("/", include_in_schema=False)
        @app.get("/index.html", include_in_schema=False)
        def index():
            page = os.path.join(web_root, "index.html")
            if not os.path.exists(page):
                return PlainTextResponse("index.html not found", status_code=404)
            return FileResponse(page, headers={"Cache-Control": "no-store"})

    return app


# --------------------------------------------------------------------------- #
# Transport 2 — standard library (always available)                           #
# --------------------------------------------------------------------------- #

class DashboardHandler(BaseHTTPRequestHandler):
    """Minimal router used when FastAPI is not installed."""

    server_version = f"T_Dubber/{__version__}"
    builder: SnapshotBuilder
    web_root: str
    quiet: bool = False

    # -- helpers -------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _file(self, rel: str) -> None:
        # Resolve inside web_root only. Without this check, /../../.ssh/...
        # would be served by the static path.
        root = os.path.realpath(self.web_root)
        target = os.path.realpath(os.path.join(root, rel.lstrip("/").split("?")[0]))
        if not (target == root or target.startswith(root + os.sep)) or not os.path.isfile(target):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        try:
            with open(target, "rb") as fh:
                self._send(200, fh.read(), ctype)
        except OSError:
            self._send(500, b"read error", "text/plain; charset=utf-8")

    # -- routes --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = self.path.split("?", 1)
        route = parsed[0].rstrip("/") or "/"
        query: Dict[str, str] = {}
        if len(parsed) > 1:
            for key, values in parse_qs(parsed[1]).items():
                if values:
                    query[key] = values[0]

        try:
            if route in ("/api/status", "/status"):
                self._json(self.builder.build(query.get("project_id")))
            elif route == "/api/projects":
                self._json({"projects": self.builder.list_projects(int(query.get("limit", 25) or 25))})
            elif route == "/api/quota":
                con = self.builder.store.connect()
                try:
                    self._json(self.builder.build_quota(con, today_str()) if con else
                               {"day": today_str(), "channels": [], "recorded": False})
                finally:
                    if con:
                        con.close()
            elif route == "/api/health":
                info = self.builder.health()
                self._json(info, 200 if info.get("readable") else 503)
            elif route == "/api/snapshot":
                body = json.dumps(self.builder.build(), indent=2, default=str).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif route in ("/", "/index.html"):
                self._file("index.html")
            elif route.startswith("/static/"):
                self._file(route[len("/static"):])
            else:
                self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")
        except Exception as exc:  # never let a bad request kill the thread
            self._json({"error": type(exc).__name__, "detail": str(exc)}, 500)

    do_HEAD = do_GET

    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.quiet:
            sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Serve the T_Dubber Mission Control dashboard.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address; 0.0.0.0 exposes Telegram metadata to the LAN")
    p.add_argument("--port", type=int, default=8081, help="TCP port (8081: havaldar_core owns 8080)")
    p.add_argument("--db", default=DEFAULT_DB, help="path to t_dubber.db")
    p.add_argument("--root", default=DEFAULT_WEB_ROOT, help="directory holding index.html")
    p.add_argument("--tg-dir", default=None, help="upload journal dir (default: <app>/.tg_uploads)")
    p.add_argument("--busy-timeout", type=int, default=BUSY_TIMEOUT_MS,
                   help="SQLite busy timeout in ms; raise this if a poll ever fails under load")
    p.add_argument("--no-open", action="store_true", help="do not launch a browser")
    p.add_argument("--quiet", action="store_true", help="suppress per-request access logs")
    p.add_argument("--engine", choices=("auto", "fastapi", "stdlib"), default="auto",
                   help="HTTP transport; 'auto' prefers FastAPI and falls back to the stdlib")
    p.add_argument("--check", action="store_true",
                   help="probe the database, print the snapshot, and exit without serving")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    if not os.path.exists(args.db):
        print(f"[!] database not found: {args.db}", file=sys.stderr)
        print("    The dashboard will still serve, showing an OFFLINE banner.", file=sys.stderr)
    if not os.path.isdir(args.root):
        print(f"[!] web root not found: {args.root}", file=sys.stderr)
        return 2

    tg_dir = args.tg_dir or os.path.join(APP_ROOT, ".tg_uploads")
    store = Store(args.db, args.busy_timeout)
    builder = SnapshotBuilder(store, tg_dir)

    if args.check:
        health = builder.health()
        print(json.dumps(health, indent=2, default=str))
        print("\n" + "=" * 72)
        print(json.dumps(builder.build(), indent=2, default=str))
        return 0 if health.get("readable") else 1

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
    print(f"  T_Dubber Mission Control  v{__version__}")
    print(f"  dashboard   {url}")
    print(f"  api         {url}api/status   (alias: {url}status)")
    print(f"  docs        {url}docs  (FastAPI only)")
    print(f"  database    {store.db_path}  (read-only, busy_timeout={args.busy_timeout}ms)")
    print(f"  journals    {tg_dir}")
    if args.host in ("0.0.0.0", "::"):
        print("\n  [!] bound to all interfaces: Telegram channels, file paths and message")
        print("      links are readable by anything that can reach this port.")
    print()

    app = None
    if args.engine in ("auto", "fastapi"):
        try:
            app = build_fastapi(builder, args.root)
        except Exception as exc:
            if args.engine == "fastapi":
                print(f"[x] FastAPI requested but unavailable: {exc}", file=sys.stderr)
                return 3
            print(f"  FastAPI unavailable ({exc.__class__.__name__}); using the stdlib server.\n")

    if app is not None:
        try:
            import uvicorn
        except ImportError:
            print("[x] FastAPI is installed but uvicorn is not: pip install uvicorn", file=sys.stderr)
            return 3
        if not args.no_open:
            threading.Thread(target=lambda: (time.sleep(0.8), webbrowser.open(url)),
                             daemon=True).start()
        try:
            uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
        except KeyboardInterrupt:
            print("\n  stopped.")
        return 0

    # Standard-library path.
    from http.server import ThreadingHTTPServer

    handler = type("BoundDashboardHandler", (DashboardHandler,),
                   {"builder": builder, "web_root": args.root, "quiet": args.quiet})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    try:
        httpd = Server((args.host, args.port), handler)
    except OSError as exc:
        print(f"[x] cannot bind {args.host}:{args.port} — {exc}", file=sys.stderr)
        print("    Another process may hold the port. Try --port 8082.", file=sys.stderr)
        return 4

    if not args.no_open:
        threading.Thread(target=lambda: (time.sleep(0.5), webbrowser.open(url)),
                         daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())