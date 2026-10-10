"""A failed translation parse must say why, not just fail quietly.

Kaggle produced a video that looked dubbed but kept the source language: the
LLM's reply was unparseable, ``except Exception: pass`` discarded it, and the
batch fell back to the original text.  These tests pin the diagnostics that
make that visible.

The parser itself still returns the original blocks -- it cannot tell whether
it is the first attempt or the retry -- but it now records every index in
``missing`` so the caller's retry runs, and the caller raises when even the
retry comes back empty.  Shipping the source language is never an option.
"""

from __future__ import annotations

import logging

import pytest

from mazinger.translate import _parse_translation_response, translate_srt

BLOCKS = [("1", 0.0, 2.0, "Hello there"), ("2", 2.0, 4.0, "Second line")]


def test_unparseable_reply_is_logged_with_reason_and_content(caplog):
    """The raw reply is the only evidence of what the model actually said."""
    with caplog.at_level(logging.WARNING, logger="mazinger.translate"):
        _parse_translation_response("I cannot help with that.", BLOCKS)

    assert "JSON parse failed" in caplog.text
    # The raw payload is what a human needs to diagnose a weak model.
    assert "Raw Content" in caplog.text
    assert "I cannot help with that." in caplog.text


def test_log_truncates_a_huge_reply(caplog):
    """A runaway reply must not flood the log."""
    with caplog.at_level(logging.WARNING, logger="mazinger.translate"):
        _parse_translation_response("x" * 5000, BLOCKS)

    assert "Raw Content" in caplog.text
    logged = [ln for ln in caplog.text.splitlines() if "Raw Content" in ln]
    assert logged, "nothing logged"
    assert len(logged[0]) < 1000, "raw reply was not truncated"


def test_failure_falls_back_to_source_text_and_flags_every_index():
    """The parser returns the originals, but records all of them as untranslated."""
    missing: list[str] = []
    out = _parse_translation_response("garbage", BLOCKS, missing)
    assert [b[3] for b in out] == ["Hello there", "Second line"]
    # Without this the retry never runs and the source language ships silently.
    assert missing == ["1", "2"]





def test_srt_refuses_to_ship_untranslated_text():
    """A retry that also fails must abort the run, not dub the source language.

    This is the exact shape of the Kaggle failure: one unparseable batch, a
    retry, a second unparseable batch, and a video that reported success while
    carrying the source language end to end.
    """
    replies = iter(["I cannot help with that.", "Still no JSON here."])

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        @property
        def chat(self):
            return self

        @property
        def completions(self):
            return self

        def create(self, **kwargs):
            self.calls += 1
            content = next(replies)

            class _Choice:
                pass

            choice = _Choice()
            choice.message = type("M", (), {"content": content})()
            choice.finish_reason = "stop"
            return type("R", (), {"choices": [choice], "usage": None})()

    client = Client()
    srt = (
        "1\n00:00:00,000 --> 00:00:02,000\nHello there\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\nSecond line\n"
    )

    with pytest.raises(RuntimeError, match="refusing to dub the source language"):
        translate_srt(
            srt,
            {"keywords": [], "keypoints": []},
            [],
            client,
            llm_model="test-llm",
            source_language="English",
            target_language="Hindi",
        )

    # One attempt plus exactly one retry, then it gives up.
    assert client.calls == 2


def test_good_reply_does_not_log_a_parse_failure(caplog):
    good = '[{"index": "1", "text": "Hola"}, {"index": "2", "text": "Segunda"}]'
    with caplog.at_level(logging.WARNING, logger="mazinger.translate"):
        out = _parse_translation_response(good, BLOCKS)

    assert [b[3] for b in out] == ["Hola", "Segunda"]
    assert "JSON parse failed" not in caplog.text