#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run ``llama-server`` as a subprocess and hand back an OpenAI-compatible URL.

    from llamacpp_bridge import LlamaServer
    with LlamaServer(resolve("llama-server"), "IndexTeam/Index-Homura-2B-GGUF") as srv:
        base_url, model = srv.base_url, srv.model_name
        # mazinger/llm.py::build_client(base_url=base_url, api_key="EMPTY")
        # works unchanged -- anything that speaks /v1 is a drop-in.

WHY A BRIDGE AT ALL
-------------------
mazinger already does this in ``mazinger/llm.py::build_client``::

    if _is_ollama_url(base_url): ... native ollama client ...
    from openai import OpenAI
    kwargs["base_url"] = base_url

So the *only* contract that matters is "serve something that answers
``/v1/chat/completions``". ``llama-server`` does. Replacing ``vllm serve``
therefore does not require touching mazinger, the notebook, or the openai SDK
-- and that is the entire reason this migration is cheap.

The part vLLM made expensive and llama.cpp makes trivial is *startup*: vLLM
needed a 9 GB torch/CUDA install that, on Kaggle, spent 1067 s downloading
wheels built against the wrong CUDA version and then failed to import.

Stdlib only. ``openai`` is imported by :func:`build_openai_client` lazily and
never at module level, because the whole point is that it need not be there.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # the sibling module; both are meant to travel together
    from build_runtimes import canonical_name, resolve as _resolve_runtime
except ImportError:  # pragma: no cover - only when copied out on its own
    canonical_name = None
    _resolve_runtime = None

__all__ = [
    "LLAMA_SERVER",
    "LlamaServer",
    "LlamaServerError",
    "LlamaServerTimeout",
    "available",
    "build_openai_client",
    "build_argv",
    "free_port",
    "resolve",
]

#: Canonical name of the binary this module drives.
LLAMA_SERVER = "llama-server"

#: Startup failures are reported with this much of the log, matching the
#: notebook cell that caught the vLLM failure: a bare "server did not start"
#: costs the next debugging round a full 20-minute run.
LOG_TAIL_CHARS = 30000


class LlamaServerError(RuntimeError):
    """The server died, or could not be started. Carries the log tail."""


class LlamaServerTimeout(TimeoutError):
    """The server did not answer ``/v1/models`` in time. Carries the log tail."""


# --------------------------------------------------------------------------- #
# Binary resolution
# --------------------------------------------------------------------------- #

def resolve(name: str = LLAMA_SERVER) -> str | None:
    """Absolute path to a llama.cpp binary, or ``None``.

    Search order lives in ``build_runtimes.search_dirs``: ``TDUBBER_BIN_DIR``,
    then ``runtimes/bin/``, then ``cpp_accelerator/build*/``, then ``PATH``.
    """
    if _resolve_runtime is None:
        return None
    path = _resolve_runtime(name)
    return str(path) if path else None


def available(name: str = LLAMA_SERVER) -> bool:
    """True when a usable binary exists. Cheap enough for a cell to call twice."""
    return resolve(name) is not None


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #

def free_port() -> int:
    """An OS-assigned free TCP port.

    Binding port 0 and reading the port back is the only race-free way to do
    this. A hardcoded 8080 collides with whatever the previous failed attempt
    left behind, and the failure then looks like a server bug."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_busy(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, int(port))) == 0


# --------------------------------------------------------------------------- #
# argv
# --------------------------------------------------------------------------- #

def _looks_like_repo(model: str) -> bool:
    """True when *model* is a Hugging Face repo id rather than a local file.

    Order matters and the naive "contains a slash" test is wrong twice over:
    a Windows path (``C:/models/homura.gguf``) has a slash, and the resolved
    file is a real model, not a Hub id -- sending that to ``-hf`` turns a typo
    into a confusing download. A ``.gguf`` extension is likewise never a repo."""
    try:
        if Path(model).expanduser().exists():
            return False
    except OSError:
        pass
    if Path(model).suffix.lower() in (".gguf", ".ggml", ".bin"):
        return False
    # A drive letter or a leading separator is a path, not "org/name".
    text = model.replace("\\", "/")
    if re.match(r"^[A-Za-z]:[\\/]", model) or text.startswith(("/", "./", "../")):
        return False
    return "/" in text


def build_argv(
    binary: str | Sequence[str],
    model: str,
    *,
    port: int,
    host: str = "127.0.0.1",
    ctx: int = 16384,
    threads: int | None = None,
    parallel: int = 1,
    model_name: str | None = None,
    add_alias: bool = True,
    extra_args: Iterable[str] = (),
) -> list[str]:
    """The exact ``llama-server`` command line.

    ``-m`` for a local .gguf path, ``-hf repo[:quant]`` for a Hub id. Those are
    different flags with different behaviour: ``-m`` with a repo id fails, and
    ``-hf`` with a path fails. Deciding here means the caller does not have to.

    ``--jinja`` is deliberately NOT passed: the chat template in the GGUF is
    used by default, and passing it against an older llama.cpp that does not
    know the flag aborts the server before it ever binds a port.
    """
    argv = [binary] if isinstance(binary, str) else list(binary)

    is_repo = _looks_like_repo(model)
    argv += ["-hf", model] if is_repo else ["-m", str(Path(model).expanduser())]

    argv += ["--host", str(host), "--port", str(int(port))]
    # 16384, not 4096: the pipeline asks for 8000 output tokens on resegment
    # merge and 4000+prompt on the fit check, and both were rejected with
    # "max_tokens cannot be greater than max_model_len".
    argv += ["-c", str(int(ctx))]
    argv += ["-np", str(int(parallel))]
    if threads:
        argv += ["-t", str(int(threads))]
    if add_alias:
        # The OpenAI client sends whatever model name mazinger was given; if the
        # server does not advertise that name the request 404s with
        # "model not found" even though the model is loaded.
        alias = model_name or (model if is_repo else Path(model).stem)
        if alias:
            argv += ["--alias", alias]
    argv += [str(arg) for arg in extra_args]
    return argv


def default_alias(model: str) -> str:
    """The name ``llama-server`` will advertise for *model* by default.

    Kept next to :func:`build_argv` so the alias the server registers and the
    model name a client sends cannot drift apart."""
    return model if _looks_like_repo(model) else Path(model).stem


# --------------------------------------------------------------------------- #
# The endpoint description
# --------------------------------------------------------------------------- #

class Endpoint:
    """``base_url`` + ``api_key`` + ``model`` -- enough to build any client.

    Returned instead of a live ``openai.OpenAI`` so this module stays stdlib
    only and so callers who already hold an SDK instance can just point it
    here instead of constructing a new one."""

    __slots__ = ("api_key", "base_url", "model")

    def __init__(self, base_url: str, api_key: str = "EMPTY", model: str = "") -> None:
        self.base_url = base_url
        # llama-server ignores the key but the openai SDK refuses to construct
        # without one, so "EMPTY" is the conventional filler.
        self.api_key = api_key
        self.model = model

    def openai_client(self, **kwargs: Any):
        """Builds ``openai.OpenAI(base_url=..., api_key=...)``.

        Imported here, not at module scope: `openai` may be absent (it is one
        of the things this migration stops needing) and importing it eagerly
        would make this module unimportable on a bare Kaggle image."""
        from openai import OpenAI  # noqa: PLC0415 - deliberately lazy

        kwargs.setdefault("api_key", self.api_key)
        return OpenAI(base_url=self.base_url, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Endpoint(base_url={self.base_url!r}, model={self.model!r})"


def build_openai_client(
    base_url: str | None = None,
    *,
    server: "LlamaServer | None" = None,
    api_key: str = "EMPTY",
    model: str | None = None,
) -> Endpoint:
    """Describe an OpenAI-compatible endpoint for ``mazinger.build_client``."""
    if base_url is None:
        if server is None:
            raise ValueError("pass base_url= or server=")
        base_url = server.base_url
    if model is None:
        model = server.served_model_name if server is not None else ""
    return Endpoint(base_url, api_key=api_key, model=model or "")


# --------------------------------------------------------------------------- #
# Process control
# --------------------------------------------------------------------------- #

def _spawn_kwargs() -> dict[str, Any]:
    """Detach the server into its own process group.

    Needed on both platforms: llama-server spawns worker children, and killing
    only the parent leaves them holding the port so the next attempt in the
    same notebook session fails with a bind error."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_tree(proc: subprocess.Popen, timeout: float = 15.0) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=timeout, check=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
        pass


# --------------------------------------------------------------------------- #
# LlamaServer
# --------------------------------------------------------------------------- #

class LlamaServer:
    """A running ``llama-server``, usable as a context manager.

    ``binary`` is a path, or an argv prefix such as ``[sys.executable,
    "server.py"]`` -- the tests use the second form, and a wrapper script is
    the usual way to point at a sandboxed build.
    """

    def __init__(
        self,
        binary: str | Sequence[str],
        model_path_or_repo: str,
        port: int = 8080,
        ctx: int = 16384,
        threads: int | None = None,
        extra_args: Iterable[str] = (),
        log_path: str | Path | None = None,
        *,
        host: str = "127.0.0.1",
        parallel: int = 1,
        model_name: str | None = None,
        add_alias: bool = True,
        startup_timeout: float = 900.0,
        poll_interval: float = 0.5,
    ) -> None:
        self.binary = binary
        self.model = model_path_or_repo
        self.host = host
        self.port = int(port)
        self.ctx = int(ctx)
        self.threads = threads
        self.parallel = int(parallel)
        self.extra_args = list(extra_args)
        self.model_name = model_name
        self.add_alias = add_alias
        self.startup_timeout = float(startup_timeout)
        self.poll_interval = float(poll_interval)
        self.process: subprocess.Popen | None = None
        self.log_path = Path(log_path) if log_path else None
        self._log_handle = None

    @property
    def served_model_name(self) -> str:
        """The name the server will advertise, i.e. what a client must send.

        Callers almost always want mazinger's ``--llm-model`` to equal this,
        which is why ``chat()`` and ``--alias`` read the same value."""
        return self.model_name or default_alias(self.model)

    # -- logging ---------------------------------------------------------- #

    def _open_log(self):
        """Always a real file. The log tail is the only diagnostic that survives
        a notebook crash, so it cannot live in a PIPE someone has to be alive
        to drain."""
        if self.log_path is None:
            env = os.environ.get("TDUBBER_LLM_LOG")
            if env:
                self.log_path = Path(env)
            else:
                self.log_path = Path(tempfile.gettempdir()) / f"llama-server-{self.port}.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = open(self.log_path, "w", encoding="utf-8", errors="replace")
        return self._log_handle

    def log_tail(self, limit: int = LOG_TAIL_CHARS) -> str:
        """Last *limit* characters of the log. The server log is far larger
        than any reasonable exception message and the useful line ("error
        loading model", "unknown option") is at the end, not the start.

        The handle is flushed first: the child's output is still sitting in
        Python's buffer, and a tail read from an unflushed handle is exactly
        the empty string that made the vLLM failure undiagnosable."""
        if self._log_handle is not None:
            try:
                self._log_handle.flush()
            except OSError:
                pass
        if self.log_path is None:
            return "(no log file)"
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()[-limit:]
        except OSError as exc:
            return f"(log unreadable: {exc})"

    def _fail(self, message: str, exc_type: type[BaseException]) -> BaseException:
        return exc_type(f"{message}\n--- llama-server log ({self.log_path}) ---\n"
                        f"{self.log_tail()}")

    # -- lifecycle -------------------------------------------------------- #

    @property
    def base_url(self) -> str:
        """OpenAI base URL. Feed it straight to ``build_client(base_url=...)``."""
        return f"http://{self.host}:{self.port}/v1"

    def build_command(self) -> list[str]:
        return build_argv(
            self.binary, self.model,
            port=self.port, host=self.host, ctx=self.ctx, threads=self.threads,
            parallel=self.parallel, model_name=self.model_name,
            add_alias=self.add_alias, extra_args=self.extra_args,
        )

    def start(self, timeout: float | None = None) -> "LlamaServer":
        """Launch the server and block until ``/v1/models`` answers.

        A port that is already in use is swapped for a free one rather than
        retried: the previous cell in the same notebook may have left a server
        alive, and a confusing bind error costs a whole run."""
        if self.process is not None and self.process.poll() is None:
            return self
        # Port 0 is "pick any free port" to the OS, but llama-server reads it as
        # "pick any port" and binds one it never tells us -- then every /v1/models
        # probe 404s and the server looks dead while it is happily serving. The
        # port has to be ours before the launch, not discovered after it.
        if not self.port:
            self.port = free_port()
        elif _port_busy(self.port):
            self.port = free_port()

        command = self.build_command()
        handle = self._open_log()
        print(f"[llama-server] {' '.join(str(part) for part in command)}", flush=True)
        env = os.environ.copy()
        # llama.cpp reads GGML_* tuning knobs; keeping the caller's environment
        # intact matters because Kaggle cells set OMP_NUM_THREADS for us.
        self.process = subprocess.Popen(  # noqa: S603
            [str(part) for part in command],
            stdout=handle, stderr=subprocess.STDOUT, env=env, **_spawn_kwargs(),
        )
        try:
            self.wait_ready(timeout=timeout)
        except BaseException:
            self.stop()
            raise
        return self

    def wait_ready(self, timeout: float | None = None) -> None:
        deadline = time.monotonic() + (self.startup_timeout if timeout is None else timeout)
        while True:
            if self.process is not None and self.process.poll() is not None:
                raise self._fail(
                    f"llama-server exited with code {self.process.returncode} "
                    f"before it became ready",
                    LlamaServerError,
                )
            if self.poll_once():
                return
            if time.monotonic() >= deadline:
                raise self._fail(
                    f"llama-server did not answer /v1/models within "
                    f"{self.startup_timeout if timeout is None else timeout:.0f}s",
                    LlamaServerTimeout,
                )
            time.sleep(self.poll_interval)

    def poll_once(self) -> bool:
        """One readiness probe. A 200 on /v1/models is the signal that the
        weights are loaded and the HTTP layer is up -- same probe the notebook
        used against vLLM, so the failure modes stay comparable."""
        url = f"{self.base_url}/models"
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:  # noqa: S310
                return response.status == 200
        except Exception:  # noqa: BLE001 - a not-yet-bound socket is normal here
            return False

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        """Terminate the server and its children. Idempotent, and safe to call
        from ``finally`` when start() failed halfway."""
        proc, self.process = self.process, None
        if proc is not None:
            _kill_tree(proc)
        if self._log_handle is not None:
            try:
                self._log_handle.flush()
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None

    # -- context manager -------------------------------------------------- #

    def __enter__(self) -> "LlamaServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- smoke test ------------------------------------------------------- #

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int = 64,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Minimal POST to /v1/chat/completions.

        Exists because the cheapest possible proof that the endpoint speaks
        OpenAI is worth having: a cell that gets this 200 back knows
        ``build_client(base_url=srv.base_url)`` will work, for the cost of one
        request instead of one failed pipeline run."""
        payload = json.dumps({
            "model": model or self.served_model_name,
            "messages": list(messages),
            "max_tokens": int(max_tokens),
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))


def _main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - manual use
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        path = resolve()
        print(f"{path}" if path else "llama-server not found")
        return 0 if path else 1
    server = LlamaServer(
        resolve() or LLAMA_SERVER, args[0],
        port=int(args[1]) if len(args) > 1 else 8080,
    )
    print(" ".join(server.build_command()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())