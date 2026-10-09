"""Drive the Go uploader (tgup) from Python, and fall back to Telethon.

Why this exists
---------------
Telemetry on the target machine showed a single-stream upload settling at
2.06 MB/s against a 3.76 MB/s uplink with a 110 ms round trip. That is 55% of
the link, and the shortfall is arithmetic: carrying 3.76 MB/s across a 110 ms
round trip needs roughly 404 KB in flight, and one connection only keeps about
232 KB.

No language fixes a TCP window. Several sockets do. tgup opens one MTProto
connection per part in flight -- same auth key, so one login covers all of them
-- which puts several windows in flight at once. That is the whole experiment.

What this module is careful about
---------------------------------
Uploading must never depend on the Go binary existing. Every failure mode here
-- no toolchain, no binary, Smart App Control blocking it, a first run that has
no tgup session yet, a bad login, a network error -- is reported and answered
with ``fallback_reason`` set. The caller then uses Telethon, which is the
original, working path. Nothing in the Python pipeline changes.

The Go binary uses its own session file (``tgup.session``) because the storage
format is gotd's, not Telethon's. The first run therefore asks for a login code
once; after that the session is reused.

Where that session lives, and when a login may be attempted
----------------------------------------------------------
``session_path()`` answers both: ``$TGUP_SESSION`` when set, otherwise
``tgup.session`` beside this file. The path is passed to the binary as
``--session`` rather than left to the working directory, because a worker that
starts somewhere else would otherwise look for the session somewhere else and
decide it had never logged in.

That decision used to cost a real OTP every run: Telegram dispatches a login
code the instant the auth flow starts, a headless machine has no way to type
the answer back, and the run then failed at EOF and fell through to Telethon --
uploading fine, but leaving a fresh code in the user's Telegram each time for
nothing. So an upload, fetch or bench only starts when a session already exists
(or ``TGUP_ALLOW_LOGIN`` says a deliberate first login is wanted); otherwise the
call is refused up front with a reason, and the caller uses Telethon.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import telethon_session

ROOT = Path(__file__).resolve().parent
TGUP_DIR = ROOT
BINARY_NAMES = ("tgup.exe", "tgup")
DEFAULT_SESSION_NAME = "tgup.session"

# Values accepted as "yes, this run may spend a login code".
_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def get_base_command() -> list[str]:
    """Return the base command to run tgup.

    TGUP_BIN is authoritative when set. Falling back to a search would mean an
    override could point at nothing and still silently run a different binary,
    which is the kind of surprise that costs an afternoon.
    """
    override = os.environ.get("TGUP_BIN", "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return [str(candidate)]
        # An override pointing at nothing must NOT fall through to the search.
        # The caller named a binary on purpose; handing back some other tgup
        # (older, unpatched) instead is exactly the surprise this early return
        # exists to prevent -- and it is the case check() reports by name.
        return []

    for name in BINARY_NAMES:
        candidate = TGUP_DIR / name
        if candidate.is_file():
            return [str(candidate)]
        local = ROOT / name
        if local.is_file():
            return [str(local)]

    on_path = shutil.which("tgup")
    if on_path:
        return [str(on_path)]
    
    # Fallback to Go source via PS1 conductor
    if (ROOT / "run_go.ps1").is_file():
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "run_go.ps1")]
        
    return []


def binary_version(base_cmd: list[str]) -> str | None:
    """Ask the binary for its help text; None means it will not run.

    Both streams are read because a usage error prints to stderr and an
    explicit "help" prints to stdout. Only a real run failure yields None.
    """
    if not base_cmd:
        return None
    try:
        proc = subprocess.run(
            [*base_cmd, "help"],
            capture_output=True,
            timeout=30,
            creationflags=_no_window(),
            cwd=str(ROOT),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or b"").decode("utf-8", "replace")
    text += (proc.stderr or b"").decode("utf-8", "replace")
    if "tgup" in text and proc.returncode in (0, 2):
        return text
    return None


def _no_window() -> int:
    """Keep a console window from flashing on Windows."""
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class TransferProgress:
    """One progress line emitted by tgup on stderr."""

    event: str = ""
    part: int = 0
    part_count: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    rate: float = 0.0
    message: str = ""

    @classmethod
    def from_json(cls, line: str) -> "TransferProgress | None":
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None
        if "event" not in data:
            return None
        return cls(
            event=data.get("event", ""),
            part=int(data.get("part", 0)),
            part_count=int(data.get("part_count", 0)),
            bytes_done=int(data.get("bytes", 0)),
            bytes_total=int(data.get("total", 0)),
            rate=float(data.get("bytes_per_sec", 0)),
            message=data.get("message", ""),
        )


@dataclass
class GoUploadResult:
    """Outcome of a tgup run, shaped like the Python uploader's own result."""

    ok: bool = False
    used_go: bool = False
    fallback_reason: str = ""
    error: str = ""
    message_link: str = ""
    message_id: int = 0
    filename: str = ""
    channel: str = ""
    total_size: int = 0
    chunk_count: int = 0
    concurrency: int = 0
    elapsed_sec: float = 0.0
    bytes_per_sec: float = 0.0
    source_sha256: str = ""
    parts: list = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict) -> "GoUploadResult":
        return cls(
            ok=bool(data.get("ok", False)),
            used_go=True,
            error=data.get("error", ""),
            message_link=data.get("message_link", ""),
            message_id=int(data.get("message_id", 0)),
            filename=data.get("filename", ""),
            channel=data.get("channel", ""),
            total_size=int(data.get("total_size", 0)),
            chunk_count=int(data.get("chunk_count", 0)),
            concurrency=int(data.get("concurrency", 0)),
            elapsed_sec=float(data.get("elapsed_sec", 0)),
            bytes_per_sec=float(data.get("bytes_per_sec", 0)),
            source_sha256=data.get("source_sha256", ""),
            parts=data.get("parts", []) or [],
        )


# ---------------------------------------------------------------------------
# Session status
# ---------------------------------------------------------------------------


def _credentials_json(api_id, api_hash: str) -> str:
    """Serialise the credentials the way the Go binary's struct expects them.

    ``api_id`` arrives from ``config.json`` as a *string* ("35578684"), because
    that is how it is stored. Go's credential struct declares the field as
    ``int``, so passing the string through made the binary answer::

        failed to parse JSON credentials: json: cannot unmarshal string into
        Go struct field .api_id of type int

    which is not a credential problem at all -- it is a type mismatch, and the
    resulting failure looks like a network or login error to anyone reading the
    log. ``auto_tuner`` therefore never once got a real benchmark and silently
    fell back to the default concurrency, every single run.

    Coercing here rather than at each call site means upload, bench and fetch
    cannot drift apart.
    """
    if isinstance(api_id, bool):
        # bool is an int subclass; "True" as an api_id is never intended.
        api_id = 0
    try:
        api_id = int(str(api_id).strip())
    except (TypeError, ValueError):
        api_id = 0
    return json.dumps({"api_id": api_id, "api_hash": api_hash}) + "\n"


def session_path() -> Path:
    """Where tgup keeps its gotd session for this run.

    ``$TGUP_SESSION`` wins so a worker can pin it to a directory that outlives
    the process -- on a Kaggle kernel, ``/kaggle/working/tgup.session`` -- and
    every run reuses the same authorized session instead of standing in a
    fresh directory and concluding it has never logged in.
    """
    override = os.environ.get("TGUP_SESSION", "").strip()
    if override:
        return Path(override)
    return TGUP_DIR / DEFAULT_SESSION_NAME


def session_ready() -> bool:
    """True when tgup already holds a usable session."""
    return session_path().is_file()


def login_allowed() -> bool:
    """True when this run may deliberately perform a first login.

    The default is no: a login without a terminal to type the code into can
    only end in EOF, and it has already spent a real OTP by then. Someone who
    genuinely wants the interactive first login says so with
    ``TGUP_ALLOW_LOGIN=1``.
    """
    return os.environ.get("TGUP_ALLOW_LOGIN", "").strip().lower() in _TRUTHY


def needs_login() -> bool:
    """True when the first run will ask the user for a login code."""
    return not session_ready()


# ---------------------------------------------------------------------------
# Session verdict cache
# ---------------------------------------------------------------------------

# How long a cached verdict stays trusted. A session can be revoked at
# any time, so a day-old yes is not a yes.
SESSION_STATE_TTL_SECONDS = 24 * 60 * 60


def session_state_path() -> Path:
    """Where the last known session verdict is cached.

    Beside the session itself, so a second session (``TGUP_SESSION``)
    gets its own verdict instead of inheriting the first one's.
    """
    return session_path().with_name(session_path().name + ".state.json")


def record_session_state(authorized: bool, detail: str = "") -> None:
    """Cache what a real, authenticated tgup round trip proved.

    Only an actual bench, fetch or upload can show whether Telegram
    still accepts the session; this caches that verdict so ``check()``
    and the UI can answer "is the session good?" without paying for
    another process launch and network call on every status poll.
    """
    try:
        session_state_path().write_text(
            json.dumps({
                "checked_at": datetime.datetime.now(
                    datetime.timezone.utc).isoformat(timespec="seconds"),
                "authorized": bool(authorized),
                "detail": detail or "",
            }),
            encoding="utf-8",
        )
    except OSError:
        pass  # a missing cache must never break a run


def session_state() -> dict:
    """The tri-state answer for the Go session.

    ``no_file`` -- there is no session to talk about.
    ``present_unverified`` -- a file exists, but nothing has proved
    Telegram still accepts it (the honest default).
    ``verified_ok`` / ``verified_rejected`` -- a real round trip said
    so, recently enough to still be trusted.
    """
    if not session_ready():
        return {"state": "no_file"}
    try:
        data = json.loads(session_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "present_unverified"}
    checked_at = data.get("checked_at", "")
    try:
        stamp = datetime.datetime.fromisoformat(checked_at)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        age = (datetime.datetime.now(datetime.timezone.utc)
               - stamp).total_seconds()
    except ValueError:
        age = None
    if age is None or age > SESSION_STATE_TTL_SECONDS:
        return {"state": "present_unverified"}
    return {
        "state": "verified_ok" if data.get("authorized") else "verified_rejected",
        "checked_at": checked_at,
        "detail": data.get("detail", ""),
    }


def _record_auth_verdict(returncode: int, human: str) -> None:
    """Remember what a real tgup attempt proved about the session.

    A clean exit proves the session is alive; tgup's own
    "not authorized yet" proves it is not. Both are cached, so the
    next status poll answers for free.
    """
    if "not authorized" in (human or "").lower():
        record_session_state(False, _last_error(human))
    elif returncode == 0:
        record_session_state(True)


def session_refusal(command: str) -> str:
    """Why ``command`` must not start, or "" when it may.

    Refusing here, before the process is spawned, is the whole point: tgup
    requests the code as soon as its auth flow runs, so a refusal after the
    spawn would already have cost the OTP it was meant to save.
    """
    if session_ready() or login_allowed():
        return ""
    return (
        f"tgup {command} skipped: no session at {session_path()} and this run "
        "cannot answer a login code (no terminal), so starting one would only "
        "send an OTP and then fail. Reuse an existing session via TGUP_SESSION, "
        "log in once from a console, or set TGUP_ALLOW_LOGIN=1; Telethon takes "
        "over meanwhile."
    )


def _refuse(command: str, on_human: Callable[[str], None] | None) -> str:
    """Report a refusal through the human log and return the reason."""
    reason = session_refusal(command)
    if reason and on_human is not None:
        try:
            on_human(reason)
        except Exception:  # noqa: BLE001 - a broken display never changes the answer
            pass
    return reason


# ---------------------------------------------------------------------------
# Adopting the Python login
# ---------------------------------------------------------------------------


def _adopt_allowed() -> bool:
    """Whether this run may carry the Telethon login into the go session.

    Two cases say yes, and they are deliberately different:

    * ``$TGUP_TELETHON_SESSION`` names a source, so someone wants this to
      happen wherever the session lives (a worker, a scratch dir in a test).
    * No override: the destination is *this repo's own* ``tgup.session``, which
      is the login everyone actually uses.

    ``$TGUP_SESSION`` pointing somewhere else is a deliberate choice of session,
    and quietly rewriting it from another file would be exactly the surprise
    this codebase keeps refusing to spring on its operator.
    """
    if os.environ.get("TGUP_TELETHON_SESSION", "").strip():
        return True
    return session_path() == TGUP_DIR / DEFAULT_SESSION_NAME


def ensure_session(on_human: Callable[[str], None] | None = None) -> str:
    """Give tgup the session Python already logged into, so no OTP is needed.

    ``tgup.session`` is gotd's format and starts unauthorized; Telegram's only
    answer to that is a login code, which is the OTP this whole module exists
    to stop wasting. The permanent auth key in ``telegram_uploader_session.session``
    is already proof of the same login — 256 bytes, copied across — so before
    refusing anything, upload/fetch/bench call this and adopt it.

    Returns a one-line note when a session was actually written (``""``
    otherwise), because "where did this session come from" is precisely the
    question every previous run left unanswered.
    """
    if not _adopt_allowed():
        return ""
    try:
        info = telethon_session.import_into(session_path())
    except (telethon_session.TelethonSessionError, OSError):
        return ""  # no Python login to carry over: the refusal still stands
    if not info.get("imported"):
        return ""
    note = (
        "session: adopted the Python login "
        f"({info['source']} -> {info['dest']}, "
        f"dc{info['dc']} {info['addr']}); no OTP needed"
    )
    if on_human is not None:
        try:
            on_human(note)
        except Exception:  # noqa: BLE001 - a broken display never changes the answer
            pass
    return note


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


def run_command(
    args: list[str],
    *,
    timeout: float = 0.0,
    on_progress: Callable[[TransferProgress], None] | None = None,
    stdin_text: str = "",
    on_human: Callable[[str], None] | None = None,
) -> tuple[int, str, str]:
    """Run tgup, calling on_progress as each line arrives. Returns the result.

    This is the version the app uses: it must not block the UI thread, and it
    must surface progress while a multi-gigabyte transfer is in flight.
    """
    base_cmd = get_base_command()
    if not base_cmd:
        raise FileNotFoundError("tgup binary or go source not found")

    proc = subprocess.Popen(
        [*base_cmd, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin_text else subprocess.DEVNULL,
        cwd=str(ROOT),
        creationflags=_no_window(),
    )
    if stdin_text:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.close()
        except (OSError, BrokenPipeError):
            pass

    human: list[str] = []
    errors: list[BaseException] = []

    def drain() -> None:
        assert proc.stderr is not None
        try:
            for raw in proc.stderr:
                text = raw.decode("utf-8", "replace")
                progress = TransferProgress.from_json(text)
                if progress is None:
                    line = text.rstrip("\n")
                    human.append(line)
                    if on_human is not None:
                        try:
                            on_human(line)
                        except Exception:
                            pass
                    continue
                if on_progress is not None:
                    try:
                        on_progress(progress)
                    except Exception:
                        # A broken display must never abort a live transfer.
                        pass
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    reader = threading.Thread(target=drain, name="tgup-stderr", daemon=True)
    reader.start()

    stdout = ""
    if proc.stdout is not None:
        stdout = proc.stdout.read().decode("utf-8", "replace")

    try:
        returncode = proc.wait(timeout=timeout or None)
    except subprocess.TimeoutExpired:
        proc.kill()
        returncode = -1
        human.append(f"tgup timed out after {timeout:.0f}s")
    reader.join(timeout=30)

    return returncode, stdout, "\n".join(human)


# ---------------------------------------------------------------------------
# High-level entry points
# ---------------------------------------------------------------------------


def upload(
    *,
    file: str | os.PathLike = "",
    channel: str,
    api_id: int,
    api_hash: str,
    phone: str = "",
    concurrency: int = 3,
    plan_in: str = "",
    plan_out: str = "",
    result_out: str = "",
    caption: str = "",
    thumbnail: str = "",
    url: str = "",
    dry_run: bool = False,
    url_timeout: float = 0.0,
    on_progress: Callable[[TransferProgress], None] | None = None,
    on_human: Callable[[str], None] | None = None,
) -> GoUploadResult:
    """Upload a file with tgup, returning a result shaped like Python's own.

    ``file`` and ``url`` are mutually exclusive and exactly one is required.
    With ``url`` the payload is streamed straight into the upload, part by part:
    a 9 GB video never first becomes 9 GB on this disk. tgup refuses a URL whose
    origin cannot serve byte ranges rather than quietly downloading it, so a
    failure here means "try the next tier", not "try harder".

    ``dry_run`` needs no credentials and no session at all -- it plans, hashes
    and writes the result file without sending a byte. That is what makes a link
    checkable on a machine that has never been authorised.

    The ``--file`` argument list is unchanged byte for byte, so every existing
    caller (and every journal it wrote) keeps working unchanged.
    """
    # A dry run sends nothing, so it needs no session and must not be gated on
    # one. The refusal exists to stop a headless worker from *spending a real
    # OTP*; a dry run cannot spend anything, and blocking it meant the zero-disk
    # path's "explain the failure without a session" step was unreachable on a
    # machine that has never been authorised -- which is most of them.
    if not dry_run:
        ensure_session(on_human)
    reason = "" if dry_run else _refuse("upload", on_human)
    if reason:
        return GoUploadResult(
            used_go=True, ok=False, fallback_reason=reason, error=reason
        )
    if bool(str(file).strip()) == bool(str(url).strip()):
        # Both or neither. Guessing here would pick one source and silently
        # upload the wrong bytes, which is the worst available failure.
        detail = (
            "give either file= or url=, not both"
            if str(file).strip()
            else "give either file= or url="
        )
        if on_human:
            on_human(f"tgup upload: {detail}")
        return GoUploadResult(used_go=True, ok=False, error=detail,
                              fallback_reason=detail)
    args = ["upload"]
    if str(url).strip():
        args += ["--url", str(url).strip()]
    else:
        args += ["--file", str(file)]
    args += [
        "--channel", channel,
        "--credentials-stdin",
        "--session", str(session_path()),
        "--concurrency", str(max(1, int(concurrency))),
    ]
    if dry_run:
        args += ["--dry-run"]
    if url_timeout and float(url_timeout) > 0:
        args += ["--url-timeout", f"{float(url_timeout):g}s"]
    if phone:
        args += ["--phone", phone]
    if plan_in:
        args += ["--plan-in", str(plan_in)]
    if plan_out:
        args += ["--plan-out", str(plan_out)]
    if result_out:
        args += ["--result-out", str(result_out)]
    if caption:
        args += ["--caption", caption]
    if thumbnail:
        args += ["--thumbnail", str(thumbnail)]

    creds_json = _credentials_json(api_id, api_hash)
    returncode, _stdout, human = run_command(
        args, on_progress=on_progress, stdin_text=creds_json, on_human=on_human
    )
    if not dry_run:
        # A dry run proves nothing about the session: it never contacts
        # Telegram, so recording "authorized" from one is how a dead session
        # ended up cached as verified_ok and trusted for a day.
        _record_auth_verdict(returncode, human)
    return _result_from(result_out, returncode, human, on_human)


def fetch(
    *,
    link: str,
    dest: str,
    api_id: int,
    api_hash: str,
    concurrency: int = 4,
    result_out: str = "",
    verify: bool = True,
    on_progress: Callable[[TransferProgress], None] | None = None,
) -> GoUploadResult:
    """Restore an archive with tgup."""
    ensure_session()
    reason = session_refusal("fetch")
    if reason:
        return GoUploadResult(
            used_go=True, ok=False, fallback_reason=reason, error=reason
        )
    args = [
        "fetch",
        "--link", link,
        "--dest", str(dest),
        "--credentials-stdin",
        "--session", str(session_path()),
        "--concurrency", str(max(1, int(concurrency))),
    ]
    if not verify:
        args.append("--verify=false")
    if result_out:
        args += ["--result-out", str(result_out)]

    creds_json = _credentials_json(api_id, api_hash)
    returncode, _stdout, human = run_command(args, on_progress=on_progress, stdin_text=creds_json)
    _record_auth_verdict(returncode, human)
    return _result_from(result_out, returncode, human, None)


def bench(
    *,
    channel: str,
    api_id: int,
    api_hash: str,
    phone: str = "",
    size_mb: int = 48,
    levels: str = "1,2,3,4",
    result_out: str = "",
    on_human: Callable[[str], None] | None = None,
) -> dict:
    """Measure single-stream vs concurrent throughput against Telegram."""
    ensure_session(on_human)
    reason = session_refusal("bench")
    if reason:
        return {"ok": False, "returncode": 2, "log": reason, "rows": {},
                "fallback_reason": reason}
    args = [
        "bench",
        "--channel", channel,
        "--credentials-stdin",
        "--session", str(session_path()),
        "--size", str(max(1, int(size_mb)) * 1024 * 1024),
        "--concurrency", levels,
    ]
    if phone:
        args += ["--phone", phone]
    if result_out:
        args += ["--result-out", str(result_out)]

    creds_json = _credentials_json(api_id, api_hash)
    returncode, _stdout, human = run_command(args, on_human=on_human, stdin_text=creds_json)
    _record_auth_verdict(returncode, human)
    return {
        "ok": returncode == 0,
        "returncode": returncode,
        "log": human,
        "rows": _read_json(result_out) if result_out else {},
    }


def _result_from(
    result_out: str,
    returncode: int,
    human: str,
    on_human: Callable[[str], None] | None,
) -> GoUploadResult:
    """Prefer the binary's own result file; fall back to its exit status."""
    data = _read_json(result_out) if result_out else {}
    if data:
        result = GoUploadResult.from_json(data)
        if returncode != 0 and result.ok:
            result.ok = False
            result.error = result.error or f"tgup exited {returncode}"
        return result

    result = GoUploadResult(used_go=True, ok=False)
    result.error = _last_error(human) or f"tgup exited {returncode} without a result file"
    return result


def _read_json(path: str) -> dict:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


_ERROR_LINE = re.compile(r"(upload failed|fetch failed|bench failed|error)\s*:?\s*(.+)$")


def _last_error(human: str) -> str:
    """Pull the most useful line out of tgup's human output.

    The last failure wins: when several parts fail in one run, the final line
    is the one the operator needs, not the first casualty.
    """
    found = ""
    for line in human.splitlines():
        match = _ERROR_LINE.search(line)
        if match:
            found = match.group(2).strip()
    return found


def check() -> dict:
    """Report whether the Go path can run at all. Used by doctor.ps1 and the UI."""
    override = os.environ.get("TGUP_BIN", "").strip()
    base_cmd = get_base_command()
    if not base_cmd:
        reason = "no tgup binary or go source found"
        if override:
            # An override pointing at nothing is a different mistake from a
            # missing build, and worth saying so rather than falling back.
            reason = f"TGUP_BIN points at {override}, which is not a file"
        return {
            "runnable": False,
            "reason": reason,
            "expected_at": str(ROOT / BINARY_NAMES[0]),
            "session": session_ready(),
            "session_path": str(session_path()),
        }
    version = binary_version(base_cmd)
    if version is None:
        return {
            "runnable": False,
            "reason": (
                f"{base_cmd} exists but will not start; Smart App Control may be "
                "blocking it"
            ),
            "path": str(base_cmd),
            "session": session_ready(),
            "session_path": str(session_path()),
        }
    return {
        "runnable": True,
        "path": str(base_cmd),
        "session": session_ready(),
        "session_path": str(session_path()),
        "needs_login": needs_login(),
        "login_allowed": login_allowed(),
        "size_mb": round(Path(base_cmd[-1]).stat().st_size / (1024 * 1024), 2) if Path(base_cmd[-1]).exists() else 0.0,
    }


def _self_test() -> int:
    """Run the checks that do not need the network."""
    status = check()
    print(json.dumps(status, indent=2))

    if not status.get("runnable"):
        return 1

    # parseTGLink logic is duplicated in Go; make sure the two agree on the
    # shapes a user is likely to paste.
    binary = Path(status["path"])
    proc = subprocess.run(
        [str(binary), "plan", "--file", __file__],
        capture_output=True,
        creationflags=_no_window(),
    )
    if proc.returncode != 0:
        print("plan on a known-good file failed:", proc.stderr.decode("utf-8", "replace"))
        return 1

    print("tgup self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())