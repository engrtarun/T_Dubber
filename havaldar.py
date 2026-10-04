"""Wrapper for the Rust havaldar_core telemetry daemon.

This module provides Python utilities to start, stop, and check the havaldar_core
HTTP/UDP telemetry ingestion server, which writes stage transitions and log lines
to the local t_dubber.db SQLite database.

The binary must be built with: cargo build --release (in havaldar_core/)
Expected location: <repo_root>/havaldar_core/target/release/havaldar_core.exe

Architecture:
    Kaggle worker → HTTP/UDP → havaldar_core → t_dubber.db
                          ↑
                   db.py also writes directly
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class HavaldarResult:
    """Result of havaldar_core operation."""
    success: bool
    pid: Optional[int] = None
    error: str = ""


def _repo_root() -> Path:
    """Return the workspace root (3 dirname hops from this file)."""
    return Path(__file__).resolve().parent


def _havaldar_binary() -> Optional[Path]:
    """Locate the havaldar_core binary, preferring release build."""
    root = _repo_root()
    candidates = [
        root / "havaldar_core" / "target" / "release" / "havaldar_core.exe",
        root / "havaldar_core" / "target" / "release" / "havaldar_core",
        root / "havaldar_core" / "target" / "debug" / "havaldar_core.exe",
        root / "havaldar_core" / "target" / "debug" / "havaldar_core",
    ]
    for c in candidates:
        if c.is_file():
            return c
    on_path = shutil.which("havaldar_core")
    if on_path:
        return Path(on_path)
    return None


def _havaldar_wanted() -> bool:
    """Check if the daemon is explicitly disabled via env var."""
    return os.environ.get("MAZINGER_HAVALDAR", "on").lower() not in ("0", "off", "false", "no")


class HavaldarDaemon:
    """Manage the havaldar_core daemon process."""

    def __init__(
        self,
        db_path: str = "t_dubber.db",
        host: str = "127.0.0.1",
        port: int = 8080,
        udp_port: int = 0,
        busy_timeout_ms: int = 15000,
        log_format: str = "pretty",
        log_filter: str = "info",
    ):
        self.db_path = os.path.abspath(db_path)
        self.host = host
        self.port = port
        self.udp_port = udp_port
        self.busy_timeout_ms = busy_timeout_ms
        self.log_format = log_format
        self.log_filter = log_filter
        self._process: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> HavaldarResult:
        """Start the havaldar_core daemon."""
        binary = _havaldar_binary()
        if not binary:
            return HavaldarResult(
                success=False,
                error="havaldar_core binary not found. Build with: cargo build --release (in havaldar_core/)"
            )

        if not _havaldar_wanted():
            return HavaldarResult(success=False, error="MAZINGER_HAVALDAR=off")

        if self._process and self._process.poll() is None:
            return HavaldarResult(success=True, pid=self._process.pid)

        cmd = [
            str(binary),
            "--db", self.db_path,
            "--host", self.host,
            "--port", str(self.port),
            "--busy-timeout-ms", str(self.busy_timeout_ms),
            "--log-format", self.log_format,
            "--log-filter", self.log_filter,
        ]
        if self.udp_port > 0:
            cmd += ["--udp-port", str(self.udp_port)]

        log.info("Starting havaldar_core: %s", " ".join(cmd))

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as e:
            return HavaldarResult(success=False, error=f"failed to spawn havaldar_core: {e}")

        # Give it a moment to start and bind the port
        time.sleep(0.5)

        if self._process.poll() is not None:
            stdout, stderr = self._process.communicate()
            return HavaldarResult(
                success=False,
                error=f"havaldar_core exited immediately (code {self._process.returncode}): {stderr.strip() or stdout.strip()}"
            )

        # Start log drain threads
        self._stop_event.clear()
        self._stdout_thread = threading.Thread(target=self._drain_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

        log.info("havaldar_core started on %s:%d (pid=%d)", self.host, self.port, self._process.pid)
        return HavaldarResult(success=True, pid=self._process.pid)

    def _drain_stdout(self) -> None:
        assert self._process and self._process.stdout
        for line in self._process.stdout:
            if self._stop_event.is_set():
                break
            log.debug("[havaldar] %s", line.rstrip())

    def _drain_stderr(self) -> None:
        assert self._process and self._process.stderr
        for line in self._process.stderr:
            if self._stop_event.is_set():
                break
            log.warning("[havaldar] %s", line.rstrip())

    def stop(self, timeout: float = 5.0) -> HavaldarResult:
        """Stop the havaldar_core daemon gracefully."""
        if not self._process or self._process.poll() is not None:
            return HavaldarResult(success=True, pid=None)

        self._stop_event.set()
        log.info("Stopping havaldar_core (pid=%d)...", self._process.pid)

        try:
            self._process.terminate()
            self._process.wait(timeout=timeout)
            log.info("havaldar_core stopped gracefully")
            return HavaldarResult(success=True, pid=None)
        except subprocess.TimeoutExpired:
            log.warning("havaldar_core did not stop gracefully; killing")
            try:
                self._process.kill()
                self._process.wait(timeout=2.0)
            except Exception:
                pass
            return HavaldarResult(success=False, error="had to kill havaldar_core")

    def is_running(self) -> bool:
        """Check if the daemon is currently running."""
        return self._process is not None and self._process.poll() is None

    def health_check(self) -> bool:
        """Check if the HTTP health endpoint responds."""
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://{self.host}:{self.port}/health", timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def ready_check(self) -> bool:
        """Check if the HTTP ready endpoint responds (database ready)."""
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://{self.host}:{self.port}/ready", timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False


def send_telemetry(
    project_id: str,
    stage: int,
    status: str,
    message: str = "",
    duration_sec: Optional[float] = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    wait: bool = False,
) -> bool:
    """Send a telemetry packet to havaldar_core via HTTP.

    This mirrors the packet format expected by havaldar_core's /ingest endpoint.

    Args:
        project_id: The project ID (must exist in t_dubber.db projects table).
        stage: Pipeline stage number (1-7).
        status: One of "running", "success", "failed", "cancelled".
        message: Optional log message.
        duration_sec: Optional stage duration in seconds.
        host: havaldar_core host.
        port: havaldar_core port.
        wait: If True, use ?wait=1 to block until committed.

    Returns:
        True if the request succeeded (2xx), False otherwise.
    """
    import urllib.request
    import json

    packet = {
        "project_id": project_id,
        "stage": stage,
        "status": status,
        "message": message,
    }
    if duration_sec is not None:
        packet["duration_sec"] = duration_sec

    url = f"http://{host}:{port}/ingest"
    if wait:
        url += "?wait=1"

    data = json.dumps(packet).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        log.debug("Telemetry send failed: %s", e)
        return False


def check_havaldar_core() -> dict:
    """Report whether havaldar_core is available, for doctor.py."""
    binary = _havaldar_binary()
    if not binary:
        return {
            "present": False,
            "runnable": False,
            "reason": "not built. Run: cargo build --release (in havaldar_core/)",
        }
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode in (0, 1, 2):
            version = (result.stdout or result.stderr).strip().splitlines()[0]
            return {
                "present": True,
                "runnable": True,
                "path": str(binary),
                "version": version,
            }
    except Exception:
        pass
    return {
        "present": True,
        "runnable": False,
        "path": str(binary),
        "reason": "binary exists but will not run (Smart App Control?)",
    }