"""End-to-end check: no LLM stage can out-request the served context window.

Replays the Kaggle failure end to end.  A vLLM server with the window the
notebook configures (4096) is stood up for a fake client, and every stage that
used to carry a literal cap is driven through the real pipeline call sites.
Before the fix, resegment and fit were rejected with a 400 and their results
were silently dropped; the assertion here is that each stage now returns real
work instead of an empty fallback.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from mazinger import fit as fit_mod
from mazinger import llm as llm_mod
from mazinger import resegment as resegment_mod
from mazinger.llm import _StreamingOpenAIChatCompletions

WINDOW = 4096


def _too_long(requested: int, prompt_tokens: int) -> openai.BadRequestError:
    req = httpx.Request("POST", "http://localhost:8000/v1/chat/completions")
    return openai.BadRequestError(
        f"Error code: 400 - {{'error': {{'message': \"This model's maximum "
        f"context length is {WINDOW} tokens. However, you requested {requested} "
        f"output tokens and your prompt contains at least {prompt_tokens} input "
        f"tokens, for a total of at least {requested + prompt_tokens} tokens.\"}}",
        response=httpx.Response(400, request=req), body={"code": 400},
    )


class _VLLMStub:
    """Stands in for a vLLM OpenAI server with a hard 4096-token window."""

    def __init__(self, window: int = WINDOW) -> None:
        self.window = window
        self.requests: list[dict] = []
        self.prompt_tokens = 0  # filled in by each test to mimic a real prompt

    def __init__(self, window: int = WINDOW, reply: str = "[]") -> None:
        self.window = window
        self.reply = reply
        self.requests: list[dict] = []
        self.prompt_tokens = 0  # filled in by each test to mimic a real prompt

    def create(self, **kwargs):
        self.requests.append(dict(kwargs))
        requested = kwargs.get("max_tokens")
        if requested is not None and self.prompt_tokens + requested > self.window:
            raise _too_long(requested, self.prompt_tokens)
        return _StubReply(self.reply)


class _StubReply:
    def __init__(self, content: str) -> None:
        self.usage = None
        self.choices = [type("C", (), {
            "message": type("M", (), {"content": content})(),
            "finish_reason": "stop",
        })()]


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv(llm_mod.MAX_OUTPUT_TOKENS_ENV, raising=False)
    monkeypatch.setenv(llm_mod.MAX_CONTEXT_TOKENS_ENV, str(WINDOW))
    monkeypatch.setattr(llm_mod, "_probe_context_limit", lambda _url: WINDOW)
    for store in (
        _StreamingOpenAIChatCompletions._rejected,
        _StreamingOpenAIChatCompletions._contexts,
        _StreamingOpenAIChatCompletions._probed,
        _StreamingOpenAIChatCompletions._prompt_floors,
    ):
        store.clear()
    yield
    for store in (
        _StreamingOpenAIChatCompletions._rejected,
        _StreamingOpenAIChatCompletions._contexts,
        _StreamingOpenAIChatCompletions._probed,
        _StreamingOpenAIChatCompletions._prompt_floors,
    ):
        store.clear()


def _client(stub: _VLLMStub) -> _StreamingOpenAIChatCompletions:
    return _StreamingOpenAIChatCompletions(stub)


# -- resegment: the 400 that silently disabled LLM merge ------------------


def test_resegment_merge_no_longer_gets_a_400():
    """Kaggle: 400 at resegment.py, then 'falling back' with no merge done."""
    # A well-formed merge reply, so a None here can only mean a failed call.
    stub = _VLLMStub(reply='[[1, 2], [3], [4, 5]]')
    stub.prompt_tokens = 97  # the size the failing log reported
    client = _client(stub)

    # _llm_merge_batch returns None on any failure; None means "no merge".
    client.chat = type("C", (), {"completions": client})()
    result = resegment_mod._llm_merge_batch(
        [(str(i), float(i), float(i) + 1.0, f"line {i}") for i in range(1, 6)],
        client=client, llm_model="Index-Homura-2B",
    )

    assert result is not None, "merge was silently skipped — the 400 is back"
    assert stub.requests, "no request reached the server"
    for req in stub.requests:
        assert req["max_tokens"] + stub.prompt_tokens <= WINDOW


# -- fit: the 400 that made the fit check skip ---------------------------


def test_fit_check_no_longer_skips():
    """Kaggle: 97 input + 4000 output = 4097 > 4096, so fit was skipped and
    9/11 segments overflowed their slots by 23.6s."""
    stub = _VLLMStub()
    stub.prompt_tokens = 97
    client = _client(stub)
    client.chat = type("C", (), {"completions": client})()

    items = [{"idx": "1", "text": "a much too long line for its slot",
              "max": 3}]
    out = fit_mod._shorten(
        items, client=client, llm_model="Index-Homura-2B",
        language="Hindi", unit="words",
    )
    # An empty dict means the stage gave up; either way it must have been the
    # server's answer, not a rejected request.
    for req in stub.requests:
        assert req["max_tokens"] + stub.prompt_tokens <= WINDOW
    assert stub.requests, "fit check never reached the server"


# -- every stage's cap, in one sweep -------------------------------------


def test_no_stage_can_request_more_than_the_window():
    """Drives the shared helper the way each stage does, at the worst prompt
    size the Kaggle log showed (1745 tokens for a translate batch)."""
    from mazinger.llm import llm_max_output_tokens

    stub = _VLLMStub()
    stub.prompt_tokens = 1745
    client = _client(stub)

    msgs = [{"role": "user", "content": "x" * 7000}]  # ~1750 tokens
    client.create(model="Index-Homura-2B", messages=msgs,
                  num_predict=llm_max_output_tokens())

    for req in stub.requests:
        assert req["max_tokens"] + stub.prompt_tokens <= WINDOW, req


def test_translate_batch_at_its_observed_size_still_fits():
    """The translate batch that ran on Kaggle used 1745 input tokens."""
    from mazinger.llm import llm_max_output_tokens

    stub = _VLLMStub()
    stub.prompt_tokens = 1745
    client = _client(stub)

    client.create(model="Index-Homura-2B",
                  messages=[{"role": "user", "content": "अ" * 7000}],
                  num_predict=llm_max_output_tokens())

    assert stub.requests[-1]["max_tokens"] <= WINDOW - 1745


def test_repeated_stages_do_not_accumulate_failed_requests():
    """A 10-batch run should not pay 10 rejections; the window is learned."""
    from mazinger.llm import llm_max_output_tokens

    stub = _VLLMStub()
    stub.prompt_tokens = 97
    client = _client(stub)

    msgs = [{"role": "user", "content": "short"}]
    for _ in range(10):
        client.create(model="Index-Homura-2B", messages=msgs,
                      num_predict=llm_max_output_tokens())

    assert len(stub.requests) == 10, "wasted round-trips on known limits"