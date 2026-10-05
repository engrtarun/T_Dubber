"""Tests for MLX backends."""

import sys
import types
import inspect
import importlib
from unittest.mock import patch

import numpy as np
import pytest


# ── Device detection ────────────────────────────────────────────────────────


def reload_groups():
    import mazinger.cli._groups as g

    importlib.reload(g)
    return g


def test_detect_device_cuda_available():
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: True)
    with patch.dict(sys.modules, {"torch": fake_torch}):
        g = reload_groups()
        assert g.detect_device() == "cuda"


def test_detect_device_cpu_fallback():
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    with patch.dict(sys.modules, {"torch": fake_torch}):
        g = reload_groups()
        assert g.detect_device() == "cpu"


# ── MLX TTS language mapping ────────────────────────────────────────────────


def test_mlx_synthesize_maps_language_to_code():
    """Guard against hardcoded lang_code='auto' bug from original PR."""
    from unittest.mock import Mock
    from mazinger.tts import _MLXTTSWrapper

    model = Mock()
    model.generate.return_value = [
        type("Result", (), {"audio": np.array([0.1]), "sample_rate": 24000})()
    ]
    wrapper = _MLXTTSWrapper(model, "ref.wav", "hello")
    for lang, code in [
        ("Chinese", "zh"),
        ("English", "en"),
        ("Japanese", "ja"),
        ("Korean", "ko"),
        ("German", "de"),
        ("French", "fr"),
        ("Russian", "ru"),
        ("Portuguese", "pt"),
        ("Spanish", "es"),
        ("Italian", "it"),
    ]:
        wrapper.synthesize("test", lang)
        call_kwargs = model.generate.call_args[1]
        assert call_kwargs["lang_code"] == code, f"Expected {code} for {lang}"


def test_mlx_synthesize_defaults_auto_for_unknown_language():
    from unittest.mock import Mock
    from mazinger.tts import _MLXTTSWrapper

    model = Mock()
    model.generate.return_value = [
        type("Result", (), {"audio": np.array([0.1]), "sample_rate": 24000})()
    ]
    wrapper = _MLXTTSWrapper(model, "ref.wav", "hello")
    wrapper.synthesize("test", "Swahili")
    call_kwargs = model.generate.call_args[1]
    assert call_kwargs["lang_code"] == "auto"


# ── MLX Whisper ─────────────────────────────────────────────────────────────


def test_mlx_whisper_does_not_accept_beam_size():
    """Guard against beam_size creeping back in — mlx-whisper uses sampling."""
    from mazinger.transcribe import _transcribe_mlx_whisper

    sig = inspect.signature(_transcribe_mlx_whisper)
    assert "beam_size" not in sig.parameters
