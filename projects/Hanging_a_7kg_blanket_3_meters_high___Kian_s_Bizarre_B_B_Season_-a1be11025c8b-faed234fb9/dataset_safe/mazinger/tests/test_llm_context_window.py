"""Output-token caps must fit the served model's real context window.

The Kaggle run died on ``400 Bad Request`` from vLLM because stages asked for
more output tokens than the server had window for, and a blanket ``except``
turned that into a silently skipped fit check and 23s of audio overflow.
These tests pin the two layers that prevent it coming back: the caps come from
one configurable helper, and the client refuses to send a request that cannot
fit.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from mazinger import llm as llm_mod
from mazinger.llm import (
    MAX_OUTPUT_TOKENS_ENV,
    _StreamingOpenAIChatCompletions,
    llm_max_output_tokens,
)


def _bad_request(message: str) -> openai.BadRequestError:
    """A vLLM 400 carrying *message*, as the Kaggle log showed it."""
    req = httpx.Request("POST", "http://localhost:8000/v1/chat/completions")
    return openai.BadRequestError(
        f"Error code: 400 - {message}",
        response=httpx.Response(400, request=req),
        body={"message": message, "type": "BadRequestError", "code": 400},
    )


# The two wordings vLLM actually used, verbatim from the Kaggle log.
_VLLM_TOKENS_ONLY = (
    "max_tokens=8000 cannot be greater than max_model_len=max_total_tokens=4096."
    " Please request fewer output tokens. (parameter=max_tokens, value=8000)"
)
_VLLM_WITH_PROMPT = (
    "This model's maximum context length is 4096 tokens. However, you requested "
    "4000 output tokens and your prompt contains at least 97 input tokens, for a "
    "total of at least 4097 tokens. Please reduce the length of the input prompt "
    "or the number of requested output tokens. (parameter=input_tokens, value=97)"
)


class _WindowServer:
    """Fake server with a hard context window, like vLLM."""

    def __init__(self, window: int, *, prompt_tokens: int = 0) -> None:
        self.window = window
        self.prompt_tokens = prompt_tokens
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        requested = kwargs.get("max_tokens")
        if requested is not None:
            total = self.prompt_tokens + requested
            if total > self.window:
                raise _bad_request(
                    f"This model's maximum context length is {self.window} tokens."
                    f" However, you requested {requested} output tokens and your"
                    f" prompt contains at least {self.prompt_tokens} input tokens,"
                    f" for a total of at least {total} tokens."
                )
        return f"ok:{requested}"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv(MAX_OUTPUT_TOKENS_ENV, raising=False)
    monkeypatch.delenv(llm_mod.MAX_CONTEXT_TOKENS_ENV, raising=False)
    _StreamingOpenAIChatCompletions._rejected.clear()
    _StreamingOpenAIChatCompletions._contexts.clear()
    _StreamingOpenAIChatCompletions._probed.clear()
    _StreamingOpenAIChatCompletions._prompt_floors.clear()
    yield
    _StreamingOpenAIChatCompletions._rejected.clear()
    _StreamingOpenAIChatCompletions._contexts.clear()
    _StreamingOpenAIChatCompletions._probed.clear()
    _StreamingOpenAIChatCompletions._prompt_floors.clear()


# -- the shared cap helper -------------------------------------------------


def test_cap_defaults_to_a_value_that_fits_a_small_window():
    # 2048 leaves room inside the 4096 window the notebook configures.
    assert llm_max_output_tokens() == 2048


def test_cap_is_configurable(monkeypatch):
    monkeypatch.setenv(MAX_OUTPUT_TOKENS_ENV, "4096")
    assert llm_max_output_tokens() == 4096


def test_stage_default_wins_when_env_unset(monkeypatch):
    monkeypatch.delenv(MAX_OUTPUT_TOKENS_ENV, raising=False)
    assert llm_max_output_tokens(128) == 128


@pytest.mark.parametrize("bad", ["", "   ", "abc", "0", "-5"])
def test_unusable_env_falls_back_instead_of_crashing(monkeypatch, bad):
    """A typo in an env var must not kill a pipeline mid-run."""
    monkeypatch.setenv(MAX_OUTPUT_TOKENS_ENV, bad)
    assert llm_max_output_tokens(777) == 777


def test_no_stage_sends_a_bare_literal_cap():
    """Regression guard: the 400 came from literals nobody could configure."""
    import inspect

    for module in ("fit", "resegment", "review", "translate"):
        source = inspect.getsource(
            __import__(f"mazinger.{module}", fromlist=[module]))
        assert "num_predict=8000" not in source, module
        assert "num_predict=4000" not in source, module


# -- overflow detection ----------------------------------------------------


@pytest.mark.parametrize("message", [_VLLM_TOKENS_ONLY, _VLLM_WITH_PROMPT])
def test_both_vllm_wordings_are_recognised(message):
    parsed = llm_mod._context_overflow(_bad_request(message))
    assert parsed is not None
    limit, prompt = parsed
    assert limit == 4096
    assert prompt in (None, 97)


def test_unrelated_400_is_not_mistaken_for_overflow():
    other = _bad_request("'temperature' is not supported with this model.")
    assert llm_mod._context_overflow(other) is None


# -- the fix itself --------------------------------------------------------


def test_oversized_cap_is_reduced_and_the_call_succeeds(monkeypatch):
    """The Kaggle fit-check case: 4000 tokens against a 4096 window."""
    monkeypatch.setenv(llm_mod.MAX_CONTEXT_TOKENS_ENV, "4096")
    inner = _WindowServer(4096, prompt_tokens=97)

    out = _StreamingOpenAIChatCompletions(inner).create(
        model="Index-Homura-2B", messages=[{"role": "user", "content": "hi"}],
        num_predict=4000,
    )

    assert out == f"ok:{inner.calls[-1]['max_tokens']}"
    assert 97 + inner.calls[-1]["max_tokens"] <= 4096


def test_known_window_is_applied_before_the_first_request(monkeypatch):
    """No wasted 400 when we already know the window."""
    monkeypatch.setenv(llm_mod.MAX_CONTEXT_TOKENS_ENV, "4096")
    inner = _WindowServer(4096, prompt_tokens=97)

    _StreamingOpenAIChatCompletions(inner).create(
        model="Index-Homura-2B", messages=[{"role": "user", "content": "hi"}],
        num_predict=8000,
    )
    assert len(inner.calls) == 1


def test_unknown_window_still_recovers_from_the_400(monkeypatch):
    """With no configured limit the client's own retry has to save the run."""
    monkeypatch.setattr(llm_mod, "_probe_context_limit", lambda _url: None)
    inner = _WindowServer(4096, prompt_tokens=97)

    out = _StreamingOpenAIChatCompletions(inner).create(
        model="m", messages=[{"role": "user", "content": "hi"}], num_predict=4000,
    )

    assert out.startswith("ok:")
    assert 97 + inner.calls[-1]["max_tokens"] <= 4096


def test_recovery_is_not_repeated_for_every_later_call(monkeypatch):
    """The learned window is remembered, so the retry cost is paid once."""
    monkeypatch.setattr(llm_mod, "_probe_context_limit", lambda _url: None)
    inner = _WindowServer(4096, prompt_tokens=97)
    comp = _StreamingOpenAIChatCompletions(inner)

    comp.create(model="m", messages=[{"role": "user", "content": "hi"}],
                num_predict=4000)
    first = len(inner.calls)
    inner.calls.clear()

    comp.create(model="m", messages=[{"role": "user", "content": "hi"}],
                num_predict=4000)
    assert len(inner.calls) == 1
    assert first >= 2  # the first call did pay for a rejected attempt


def test_observed_window_beats_a_wrong_env_value(monkeypatch):
    """The server's own number is more trustworthy than a guess."""
    monkeypatch.setattr(llm_mod, "_probe_context_limit", lambda _url: None)
    monkeypatch.setenv(llm_mod.MAX_CONTEXT_TOKENS_ENV, "999999")
    inner = _WindowServer(4096, prompt_tokens=97)
    comp = _StreamingOpenAIChatCompletions(inner)

    comp.create(model="m", messages=[{"role": "user", "content": "hi"}],
                num_predict=4000)
    assert comp._contexts["m"] == 4096


def test_prompt_alone_filling_the_window_is_reported_not_retried():
    """Nothing left to give: surface it instead of looping forever."""
    inner = _WindowServer(4096, prompt_tokens=4000)

    with pytest.raises(openai.BadRequestError):
        _StreamingOpenAIChatCompletions(inner).create(
            model="m", messages=[{"role": "user", "content": "hi"}],
            num_predict=4000,
        )


def test_a_cap_that_already_fits_is_left_alone(monkeypatch):
    monkeypatch.setenv(llm_mod.MAX_CONTEXT_TOKENS_ENV, "4096")
    inner = _WindowServer(4096, prompt_tokens=97)

    _StreamingOpenAIChatCompletions(inner).create(
        model="m", messages=[{"role": "user", "content": "hi"}], num_predict=256,
    )
    assert inner.calls[-1]["max_tokens"] == 256


# -- truncation visibility -------------------------------------------------


def test_truncated_reply_is_logged(caplog):
    """A reply cut at the cap used to surface only as 'could not parse'."""

    class _Truncated:
        def __init__(self):
            self.choices = [type("C", (), {"finish_reason": "length"})()]
            self.usage = None

    with caplog.at_level("WARNING", logger="mazinger.llm"):
        llm_mod._warn_if_truncated(_Truncated(), "Index-Homura-2B")
    assert "finish_reason" in caplog.text
    assert MAX_OUTPUT_TOKENS_ENV in caplog.text


def test_complete_reply_is_quiet(caplog):
    class _Complete:
        def __init__(self):
            self.choices = [type("C", (), {"finish_reason": "stop"})()]

    with caplog.at_level("WARNING", logger="mazinger.llm"):
        llm_mod._warn_if_truncated(_Complete(), "m")
    assert caplog.text == ""


def test_non_openai_shaped_reply_does_not_raise():
    llm_mod._warn_if_truncated("ok", "m")  # must not raise


# -- prompt estimation -----------------------------------------------------


def test_wide_scripts_are_not_undercounted():
    """Hindi/Devanagari costs far more per char than Latin; don't divide by 4."""
    devanagari = "अ" * 400
    latin = "a" * 400
    assert llm_mod._estimate_prompt_tokens(
        [{"content": devanagari}]) > llm_mod._estimate_prompt_tokens(
        [{"content": latin}])


def test_non_string_content_is_skipped():
    assert llm_mod._estimate_prompt_tokens(
        [{"content": [{"type": "image_url"}]}]) == 0