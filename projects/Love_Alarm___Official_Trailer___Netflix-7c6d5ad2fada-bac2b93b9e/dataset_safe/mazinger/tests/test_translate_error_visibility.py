"""A failed translation parse must say why, not just fail quietly.

Kaggle produced a video that looked dubbed but kept the source language: the
LLM's reply was unparseable, ``except Exception: pass`` discarded it, and the
batch fell back to the original text.  These tests pin the diagnostics that
make that visible.
"""

from __future__ import annotations

import logging

from mazinger.translate import _parse_translation_response

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


def test_failure_still_falls_back_to_source_text():
    """Fallback behaviour must not change — only its visibility."""
    out = _parse_translation_response("garbage", BLOCKS)
    assert [b[3] for b in out] == ["Hello there", "Second line"]


def test_good_reply_does_not_log_a_parse_failure(caplog):
    good = '[{"index": "1", "text": "Hola"}, {"index": "2", "text": "Segunda"}]'
    with caplog.at_level(logging.WARNING, logger="mazinger.translate"):
        out = _parse_translation_response(good, BLOCKS)

    assert [b[3] for b in out] == ["Hola", "Segunda"]
    assert "JSON parse failed" not in caplog.text