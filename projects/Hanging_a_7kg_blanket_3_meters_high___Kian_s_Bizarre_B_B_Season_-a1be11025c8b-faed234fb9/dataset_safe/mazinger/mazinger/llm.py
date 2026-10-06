"""LLM client factory with native Ollama support.

When the base URL points to an Ollama server, requests are routed through
the native ``/api/chat`` endpoint so that parameters like ``think`` are
handled correctly.  For all other providers the standard OpenAI SDK is used.

Streaming
---------
Call :func:`set_stream_callback` with a ``callback(token: str)`` function
before running pipeline stages.  When set, every LLM completion will stream
tokens through the callback *and* still return the full response object as
usual — callers do not need any changes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)


class OllamaRequestError(RuntimeError):
    """An Ollama HTTP call failed, carrying the server's own explanation."""


def _urlopen(req: urllib.request.Request, timeout: float | None = None):
    """``urlopen`` that preserves Ollama's error message.

    Ollama explains every failure in the response body as ``{"error": "..."}``
    — the model is not pulled, it needs more memory than is available, the
    context is too long, the runner died.  ``urlopen`` raises ``HTTPError``
    *without* reading that body, so the caller is left with a bare
    "HTTP Error 500: Internal Server Error" and no way to tell those apart.
    A pipeline that dies after transcription has already run deserves better
    than that, so unpack the body here.
    """
    try:
        if timeout is None:
            return urllib.request.urlopen(req)
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:  # body already consumed or unreadable
            raw = ""
        if raw:
            try:
                parsed = json.loads(raw)
                detail = parsed.get("error") if isinstance(parsed, dict) else ""
                detail = detail or raw
            except json.JSONDecodeError:
                detail = raw
        message = f"Ollama returned HTTP {exc.code} for {req.full_url}"
        if detail:
            message += f": {detail[:500]}"
        else:
            message += " (no detail in the response body)"
        raise OllamaRequestError(message) from exc
    except urllib.error.URLError as exc:
        raise OllamaRequestError(
            f"Could not reach the Ollama server at {req.full_url} ({exc.reason}). "
            "Start it with `ollama serve`, or point OLLAMA_HOST at the right host."
        ) from exc


# -- Global stream callback ------------------------------------------------

_stream_lock = threading.Lock()
_stream_callback: Callable[[str], Any] | None = None


def set_stream_callback(callback: Callable[[str], Any] | None) -> None:
    """Set a global callback that receives each streamed token.

    Pass ``None`` to disable streaming.  The callback signature is
    ``callback(token: str)``.
    """
    global _stream_callback
    with _stream_lock:
        _stream_callback = callback


def get_stream_callback() -> Callable[[str], Any] | None:
    """Return the current stream callback (or ``None``)."""
    with _stream_lock:
        return _stream_callback


def clear_stream_callback() -> None:
    """Convenience alias for ``set_stream_callback(None)``."""
    set_stream_callback(None)

_OLLAMA_DEFAULT_PORT = 11434


# -- Extra instructions ----------------------------------------------------

_INSTRUCTIONS_HEADER = "ADDITIONAL INSTRUCTIONS FROM THE USER (apply to this task):"


def _clean_instructions(instructions: str | None) -> str | None:
    text = (instructions or "").strip()
    return text or None


def inject_instructions(
    messages: list[dict[str, Any]], instructions: str | None,
) -> list[dict[str, Any]]:
    """Return *messages* with the user's extra *instructions* added.

    They are appended to the first system message, so they follow the task's
    own rules; without a system message a new one is put first.  The caller's
    list is left untouched.
    """
    text = _clean_instructions(instructions)
    if not text:
        return messages
    block = f"{_INSTRUCTIONS_HEADER}\n{text}"
    out = list(messages)
    for i, msg in enumerate(out):
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            out[i] = {**msg, "content": f"{content}\n\n{block}"}
        elif isinstance(content, list):
            out[i] = {**msg, "content": [*content, {"type": "text", "text": block}]}
        else:
            continue
        return out
    return [{"role": "system", "content": block}, *out]


def _is_ollama_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return (
        host in ("localhost", "127.0.0.1", "0.0.0.0")
        and (port == _OLLAMA_DEFAULT_PORT or path == "")
        and "ollama" in url.lower()
    ) or port == _OLLAMA_DEFAULT_PORT


def _ollama_base(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "localhost"
    port = parsed.port or _OLLAMA_DEFAULT_PORT
    return f"{scheme}://{host}:{port}"


# -- Lightweight response objects that match the OpenAI SDK shape ----------

class _Usage:
    __slots__ = ("prompt_tokens", "completion_tokens", "total_tokens")

    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion


class _Message:
    __slots__ = ("role", "content")

    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


class _Choice:
    __slots__ = ("message", "finish_reason")

    def __init__(self, message: _Message, finish_reason: str | None = None) -> None:
        self.message = message
        self.finish_reason = finish_reason


class _ChatCompletion:
    __slots__ = ("choices", "usage")

    def __init__(self, choices: list[_Choice], usage: _Usage) -> None:
        self.choices = choices
        self.usage = usage


# -- Ollama native chat ----------------------------------------------------

class _OllamaChatCompletions:
    def __init__(
        self, base_url: str, think: bool | None,
        instructions: str | None = None, timeout: float | None = None,
    ) -> None:
        self._url = f"{base_url}/api/chat"
        self._think = think
        self._instructions = _clean_instructions(instructions)
        self._timeout = timeout

    @staticmethod
    def _convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-style multimodal messages to Ollama format.

        Ollama expects ``images`` as a list of raw base64 strings on the
        message dict, not nested ``image_url`` content blocks.
        """
        converted = []
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                converted.append(msg)
                continue
            text_parts: list[str] = []
            images: list[str] = []
            for part in content:
                if part.get("type") == "text":
                    text_parts.append(part["text"])
                elif part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    # Strip the data URI prefix to get raw base64
                    if url.startswith("data:"):
                        url = url.split(",", 1)[-1]
                    images.append(url)
            out: dict[str, Any] = {"role": msg["role"], "content": "\n".join(text_parts)}
            if images:
                out["images"] = images
            converted.append(out)
        return converted

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 1.0,
        **_kwargs: Any,
    ) -> _ChatCompletion:
        callback = get_stream_callback()

        options: dict[str, Any] = {"temperature": temperature}
        # Forward Ollama-specific sampling options when provided.
        for opt_key in (
            "repeat_penalty", "top_p", "top_k", "num_predict",
            "frequency_penalty", "presence_penalty", "seed",
        ):
            if opt_key in _kwargs:
                options[opt_key] = _kwargs[opt_key]

        body: dict[str, Any] = {
            "model": model,
            "messages": self._convert_messages(
                inject_instructions(messages, self._instructions)),
            "stream": bool(callback),
            "options": options,
        }
        # Per-call ``think`` overrides the client-level default.
        # Default to *disabled* so thinking models don't burn tokens
        # unless explicitly opted-in via ``build_client(think=True)``
        # or a per-call ``think=True``.
        think = _kwargs.get("think", self._think)
        body["think"] = bool(think)

        data = json.dumps(body).encode()
        req = urllib.request.Request(
            self._url, data=data,
            headers={"Content-Type": "application/json"},
        )

        if not callback:
            # Non-streaming path (original behaviour)
            with _urlopen(req, timeout=self._timeout) as resp:
                result = json.loads(resp.read())

            content = result.get("message", {}).get("content", "")
            prompt_tokens = result.get("prompt_eval_count", 0) or 0
            eval_tokens = result.get("eval_count", 0) or 0
        else:
            # Streaming path — accumulate tokens, forward to callback
            content_parts: list[str] = []
            prompt_tokens = 0
            eval_tokens = 0
            with _urlopen(req, timeout=self._timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    chunk = json.loads(line)
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        content_parts.append(token)
                        try:
                            callback(token)
                        except Exception:
                            pass
                    # Last chunk carries the usage counters
                    if chunk.get("done"):
                        prompt_tokens = chunk.get("prompt_eval_count", 0) or 0
                        eval_tokens = chunk.get("eval_count", 0) or 0
            content = "".join(content_parts)

        return _ChatCompletion(
            choices=[_Choice(_Message("assistant", content))],
            usage=_Usage(prompt_tokens, eval_tokens),
        )


class _OllamaChat:
    __slots__ = ("completions",)

    def __init__(self, completions: _OllamaChatCompletions) -> None:
        self.completions = completions


class _OllamaClient:
    """Drop-in replacement for ``openai.OpenAI`` that talks native Ollama."""

    def __init__(
        self, base_url: str, think: bool | None,
        instructions: str | None = None, timeout: float | None = None,
    ) -> None:
        self._base_url = base_url
        self.chat = _OllamaChat(
            _OllamaChatCompletions(base_url, think, instructions, timeout))

    def unload_model(self, model: str) -> None:
        """Tell Ollama to unload *model* from GPU memory."""
        body = json.dumps({
            "model": model, "keep_alive": 0,
        }).encode()
        req = urllib.request.Request(
            f"{self._base_url}/api/generate", body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with _urlopen(req, timeout=10) as resp:
                resp.read()
            log.info("Ollama model %s unloaded from GPU", model)
        except Exception:
            log.debug("Ollama unload request failed (non-critical)", exc_info=True)


# -- Output-token budget ---------------------------------------------------

#: Caps the ``max_tokens`` any single completion may ask for.  Every LLM
#: stage should route its token cap through :func:`llm_max_output_tokens`
#: instead of writing a literal, because a literal is a latent 400: vLLM
#: counts the *whole* window, so ``max_tokens`` has to fit in the context
#: length minus whatever the prompt already used.
MAX_OUTPUT_TOKENS_ENV = "MAZINGER_LLM_MAX_OUTPUT_TOKENS"

#: Overrides the context window we *assume* a served model has.  Only needed
#: for servers whose ``/v1/models`` does not advertise ``max_model_len``; the
#: real value is always preferred once the server itself has told us.
MAX_CONTEXT_TOKENS_ENV = "MAZINGER_LLM_MAX_CONTEXT_TOKENS"

#: Default per-call cap.  Conservative on purpose — a cap that is too high is
#: not a slower run but a hard 400 that silently disables a whole stage.
_DEFAULT_MAX_OUTPUT_TOKENS = 2048

#: Head-room kept free for what the server counts but we cannot see: chat
#: template tokens, the assistant turn prefix, image placeholder expansion.
_CONTEXT_MARGIN = 64

#: Extra slack on the *first* call for a model whose window we have not yet
#: measured.  Without a tokenizer our prompt estimate is a guess, and a guess
#: that comes in low turns into one rejected request per model.  Once the
#: server has told us what a prompt really cost, this is not used again.
_ESTIMATE_SLACK = 192

#: Never clamp down to less than this; below it the reply is worthless anyway
#: and the real fix is a shorter prompt, which only the caller can do.
_MIN_OUTPUT_TOKENS = 128


def llm_max_output_tokens(default: int = _DEFAULT_MAX_OUTPUT_TOKENS) -> int:
    """Return the per-call output-token budget for an LLM stage.

    Reads :data:`MAX_OUTPUT_TOKENS_ENV`, falling back to *default* when the
    variable is unset, blank, non-numeric or not positive.  Guarding the parse
    matters because this runs inside pipeline stages that must not die over a
    typo in an environment variable.
    """
    raw = os.environ.get(MAX_OUTPUT_TOKENS_ENV)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        log.warning(
            "%s=%r is not an integer — falling back to %d",
            MAX_OUTPUT_TOKENS_ENV, raw, default,
        )
        return default
    if value <= 0:
        log.warning(
            "%s=%d is not positive — falling back to %d",
            MAX_OUTPUT_TOKENS_ENV, value, default,
        )
        return default
    return value


# vLLM reports one overflow two ways depending on where it gave up: on
# ``max_tokens`` alone ("max_tokens=8000 cannot be greater than
# max_model_len=max_total_tokens=4096"), or on the prompt+completion total
# ("maximum context length is 4096 tokens ... your prompt contains at least
# 97 input tokens").  One pattern reads the window out of either.
_CONTEXT_LIMIT_RE = re.compile(
    r"max(?:imum)?[_ ](?:model[_ ]len|context length)[^0-9]{0,24}(\d+)",
    re.IGNORECASE,
)
_PROMPT_TOKENS_RE = re.compile(
    r"prompt contains at least (\d+) input tokens", re.IGNORECASE,
)

# Characters that cost roughly one token each rather than one per ~4, so a
# single divisor would badly under-count CJK, Hangul, Arabic or Devanagari
# prompts — exactly the languages this pipeline is asked to translate.
_WIDE_CHAR_RE = re.compile(
    r"[\u0600-\u06ff\u0900-\u097f\u1100-\u11ff\u3040-\u30ff"
    r"\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]"
)


def _estimate_prompt_tokens(messages: Any) -> int:
    """Rough token count for *messages*, used only to pre-clamp ``max_tokens``.

    Deliberately pessimistic: over-estimating trims the reply budget slightly,
    whereas under-estimating reintroduces the very 400 this is meant to avoid.
    Image payloads are skipped — their cost is the server's business, and the
    retry path below corrects the guess from the server's own count.
    """
    wide = narrow = 0
    for msg in messages or ():
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, str):
            continue
        hits = len(_WIDE_CHAR_RE.findall(content))
        wide += hits
        narrow += len(content) - hits
    return wide + (narrow + 3) // 4


def _context_overflow(exc: Exception) -> tuple[int, int | None] | None:
    """Return ``(window, prompt_tokens)`` if *exc* is a context-length 400.

    ``prompt_tokens`` is ``None`` when the server rejected ``max_tokens``
    without counting the prompt, and an int when it told us exactly how much
    the prompt used — which makes the retry arithmetic exact instead of
    pessimistic.  ``None`` return means this is some other 400.
    """
    if getattr(exc, "status_code", None) != 400:
        return None
    # The explanation lives in the message, but depending on the SDK version
    # and how the server framed it, only ``exc.body`` may carry it — so read
    # both rather than assume one.
    for text in (str(exc), str(getattr(exc, "body", ""))):
        if "token" not in text.lower():
            continue
        limit = _CONTEXT_LIMIT_RE.search(text)
        if limit is None:
            continue
        prompt = _PROMPT_TOKENS_RE.search(text)
        return int(limit.group(1)), int(prompt.group(1)) if prompt else None
    return None


def _probe_context_limit(base_url: str) -> int | None:
    """Ask an OpenAI-compatible server for its context window.

    vLLM advertises ``max_model_len`` in ``/v1/models``.  Knowing the window up
    front means the first request is already valid, instead of spending a
    round-trip on a 400.  Any failure just means "unknown" — the adaptive
    retry still covers us — so this never raises.
    """
    if not base_url:
        return None
    try:
        req = urllib.request.Request(f"{base_url.rstrip('/')}/models")
        with _urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    entries = data.get("data") if isinstance(data, dict) else None
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        for key in ("max_model_len", "context_length", "max_context_length"):
            value = entry.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return None


def _warn_if_truncated(resp: Any, model: str) -> None:
    """Log loudly when a reply stopped because it hit the token cap.

    A reply cut off at ``max_tokens`` is almost never valid JSON, so it used to
    resurface one stage later as a silent "could not parse, keep the source
    text" fallback.  Naming the cause here is the difference between a
    diagnosable run and a mystery, and points straight at the fix.
    """
    try:
        if resp.choices[0].finish_reason == "length":
            log.warning(
                "Model %s hit the output-token cap (finish_reason='length') — "
                "the reply is truncated and will not parse. Raise %s.",
                model, MAX_OUTPUT_TOKENS_ENV,
            )
    except Exception:  # noqa: BLE001 — non-OpenAI-shaped replies are fine
        pass


# -- Factory ---------------------------------------------------------------


class _StreamingOpenAIChatCompletions:
    """Proxy that intercepts ``create()`` to stream tokens via callback."""

    # Keys that are Ollama-specific and not understood by the OpenAI SDK.
    _OLLAMA_ONLY_KEYS = frozenset({
        "repeat_penalty", "top_k", "num_predict", "think",
    })

    # Map portable kwarg names to their OpenAI equivalents.
    _KWARG_MAP = {"num_predict": "max_tokens"}

    # Error codes OpenAI returns when a model rejects a sampling parameter.
    # Reasoning models (o-series, GPT-5) refuse ``max_tokens`` outright and
    # only accept the default ``temperature`` / ``top_p`` / penalties.
    _REJECTED_PARAM_CODES = frozenset({"unsupported_parameter", "unsupported_value"})

    # model -> kwargs the provider has rejected for it, shared across clients
    # so each pipeline stage does not have to rediscover them.
    _rejected: dict[str, set[str]] = {}
    _rejected_lock = threading.Lock()

    # model -> context window, learned from the server's own overflow error.
    # Observed beats configured: a machine that told us its limit is more
    # trustworthy than an env var typed on the assumption.
    _contexts: dict[str, int] = {}

    # model -> largest prompt we have been told actually cost.  Our char-based
    # estimate under-counts real prompts (a 2-character prompt here cost 97
    # tokens there), so the first cap for an unmeasured model is deliberately
    # padded; after that this high-water mark makes later calls single-shot.
    _prompt_floors: dict[str, int] = {}

    # base_url -> advertised context window.  ``None`` is cached too, so a
    # server that does not advertise one is asked only once per process.
    _probed: dict[str, int | None] = {}

    def __init__(self, inner, instructions: str | None = None) -> None:
        self._inner = inner
        self._instructions = _clean_instructions(instructions)

    @classmethod
    def _remember_context(
        cls, model: str, limit: int, prompt_tokens: int | None = None,
    ) -> None:
        with cls._rejected_lock:
            cls._contexts[model] = limit
            if prompt_tokens:
                seen = cls._prompt_floors.get(model, 0)
                cls._prompt_floors[model] = max(seen, prompt_tokens)

    def _context_limit(self, model: str) -> int | None:
        """Best known context window for *model*, or ``None`` if unknown.

        Order matters: a window the server has actually reported beats the
        configured guess, which beats a live probe.  Returning ``None`` is a
        normal outcome, not a failure — the adaptive retry below covers it.
        """
        with self._rejected_lock:
            learned = self._contexts.get(model)
        if learned:
            return learned

        configured = self._configured_context()
        if configured:
            return configured

        base_url = str(getattr(getattr(self._inner, "_client", None), "base_url", "") or "")
        with self._rejected_lock:
            if base_url in self._probed:
                return self._probed[base_url]
        limit = _probe_context_limit(base_url)
        with self._rejected_lock:
            self._probed[base_url] = limit
        return limit

    @classmethod
    def _configured_context(cls) -> int | None:
        """Context window from :data:`MAX_CONTEXT_TOKENS_ENV`, if set."""
        raw = os.environ.get(MAX_CONTEXT_TOKENS_ENV)
        try:
            value = int(raw) if raw and raw.strip() else 0
        except ValueError:
            log.warning("%s=%r is not an integer — ignoring", MAX_CONTEXT_TOKENS_ENV, raw)
            return None
        return value if value > 0 else None

    @classmethod
    def _rejected_param(cls, exc: Exception, kwargs: dict) -> str | None:
        """Return the kwarg a 400 error says the model does not support."""
        if getattr(exc, "status_code", None) != 400:
            return None
        body = getattr(exc, "body", None)
        if not isinstance(body, dict):
            return None
        if body.get("code") not in cls._REJECTED_PARAM_CODES:
            return None
        param = body.get("param")
        return param if param in kwargs else None

    def _create_adaptive(self, **kwargs):
        """Call ``create()``, dropping sampling kwargs the model rejects.

        These kwargs are tuning hints, and a model that refuses them should
        still run with its defaults.  ``max_tokens`` is dropped rather than
        renamed to ``max_completion_tokens``: for reasoning models that limit
        also counts hidden reasoning tokens, so the small caps the pipeline
        uses would often leave no room for the visible answer.
        """
        model = kwargs.get("model", "")
        with self._rejected_lock:
            for key in self._rejected.get(model, ()):
                kwargs.pop(key, None)

        # Pre-empt the 400 when we already know the window: a request that
        # cannot possibly fit is worse than a slightly tighter cap, and the
        # saving is only ever the round-trip we would spend being corrected.
        limit = self._context_limit(model)
        if limit:
            with self._rejected_lock:
                measured = self._prompt_floors.get(model)
            self._clamp_max_tokens(kwargs, limit, measured)

        while True:
            try:
                return self._inner.create(**kwargs)
            except Exception as exc:
                param = self._rejected_param(exc, kwargs)
                if param is not None and param not in ("model", "messages"):
                    log.warning(
                        "Model %s does not support '%s' — retrying without it",
                        model, param,
                    )
                    kwargs.pop(param)
                    with self._rejected_lock:
                        self._rejected.setdefault(model, set()).add(param)
                    continue

                overflow = _context_overflow(exc)
                if overflow is None or "max_tokens" not in kwargs:
                    raise
                self._shrink_to_fit(kwargs, model, exc, *overflow)
                continue

    def _clamp_max_tokens(
        self, kwargs: dict, limit: int, measured_prompt: int | None = None,
    ) -> None:
        """Trim ``max_tokens`` so prompt + reply fits in *limit*, in place.

        *measured_prompt* is the largest prompt this model was actually
        charged for.  When we have one it beats the estimate outright; when we
        do not, the estimate is padded, because an under-count shows up as a
        rejected request and an over-count only trims the reply slightly.
        """
        cap = kwargs.get("max_tokens")
        if not isinstance(cap, int) or cap <= 0:
            return
        estimated = _estimate_prompt_tokens(kwargs.get("messages"))
        used = max(estimated, measured_prompt or 0)
        slack = _CONTEXT_MARGIN if measured_prompt else _ESTIMATE_SLACK
        budget = limit - used - slack
        if budget < _MIN_OUTPUT_TOKENS or budget >= cap:
            return  # too tight to be useful, or already fine
        log.warning(
            "Capping max_tokens %d -> %d: the window is %d tokens and this "
            "prompt needs about %d of them.",
            cap, budget, limit, used,
        )
        kwargs["max_tokens"] = budget

    def _shrink_to_fit(
        self, kwargs: dict, model: str, exc: Exception,
        limit: int, prompt_tokens: int | None,
    ) -> None:
        """React to a context-overflow 400 by retrying with a cap that fits.

        The server has just handed us the two numbers that matter — its window
        and, when it counted them, what the prompt cost — so the corrected cap
        is arithmetic rather than a guess.  That matters because a stage which
        used to be skipped outright on this error (the fit check, silently, via
        a blanket ``except``) is the difference between dubbing that fits the
        timeline and dubbing that does not.
        """
        self._remember_context(model, limit, prompt_tokens)
        used = prompt_tokens if prompt_tokens is not None else _estimate_prompt_tokens(
            kwargs.get("messages"))
        budget = limit - used - _CONTEXT_MARGIN
        current = kwargs.get("max_tokens")
        if budget < _MIN_OUTPUT_TOKENS or (isinstance(current, int) and budget >= current):
            # Either the prompt alone fills the window, or we have nothing
            # smaller left to try.  Only the caller can fix that, so report it.
            raise exc
        log.warning(
            "Model %s refused max_tokens=%s — its window is %d tokens and the "
            "prompt uses %d. Retrying with max_tokens=%d.",
            model, current, limit, used, budget,
        )
        kwargs["max_tokens"] = budget

    def _normalise_kwargs(self, kwargs: dict) -> dict:
        """Translate portable kwargs to OpenAI names, drop unsupported ones."""
        # Apply mappings first (e.g. num_predict → max_tokens)
        for src, dst in self._KWARG_MAP.items():
            if src in kwargs and dst not in kwargs:
                kwargs[dst] = kwargs.pop(src)

        # Strip remaining Ollama-only keys
        for key in self._OLLAMA_ONLY_KEYS:
            kwargs.pop(key, None)
        return kwargs

    def create(self, **kwargs):
        kwargs = self._normalise_kwargs(kwargs)
        if "messages" in kwargs:
            kwargs["messages"] = inject_instructions(kwargs["messages"], self._instructions)

        model = kwargs.get("model", "")

        callback = get_stream_callback()
        if not callback:
            resp = self._create_adaptive(**kwargs)
            _warn_if_truncated(resp, model)
            return resp

        # Force streaming on, collect full response for caller
        kwargs["stream"] = True
        stream_resp = self._create_adaptive(**kwargs)

        content_parts: list[str] = []
        role = "assistant"
        finish_reason = None
        prompt_tokens = 0
        completion_tokens = 0

        for chunk in stream_resp:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                content_parts.append(delta.content)
                try:
                    callback(delta.content)
                except Exception:
                    pass
            if delta and delta.role:
                role = delta.role
            if chunk.choices and chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason
            if chunk.usage:
                prompt_tokens = chunk.usage.prompt_tokens or 0
                completion_tokens = chunk.usage.completion_tokens or 0

        content = "".join(content_parts)
        resp = _ChatCompletion(
            choices=[_Choice(_Message(role, content), finish_reason)],
            usage=_Usage(prompt_tokens, completion_tokens),
        )
        _warn_if_truncated(resp, model)
        return resp


class _StreamingOpenAIChat:
    """Proxy for ``client.chat`` that wraps ``completions``."""

    def __init__(self, inner_chat, instructions: str | None = None) -> None:
        self.completions = _StreamingOpenAIChatCompletions(
            inner_chat.completions, instructions)


class _StreamingOpenAIClient:
    """Thin wrapper around ``openai.OpenAI`` that adds stream-callback support."""

    def __init__(self, inner, instructions: str | None = None) -> None:
        self._inner = inner
        self.chat = _StreamingOpenAIChat(inner.chat, instructions)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def build_client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    think: bool | None = None,
    instructions: str | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> Any:
    """Return an LLM client appropriate for the given backend.

    For Ollama endpoints, returns a lightweight native client that honours
    the ``think`` parameter.  For everything else, returns a standard
    ``openai.OpenAI`` instance (wrapped for stream-callback support).

    *instructions* are extra guidelines from the user, added to the system
    prompt of every completion made through the client — see
    :func:`inject_instructions`.  *timeout* (seconds) and *max_retries*
    default to the backend's own settings.
    """
    if _is_ollama_url(base_url):
        ollama_base = _ollama_base(base_url)
        log.debug("Using native Ollama client → %s", ollama_base)
        return _OllamaClient(ollama_base, think, instructions, timeout)

    from openai import OpenAI

    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    if timeout is not None:
        kwargs["timeout"] = timeout
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    return _StreamingOpenAIClient(OpenAI(**kwargs), instructions)
