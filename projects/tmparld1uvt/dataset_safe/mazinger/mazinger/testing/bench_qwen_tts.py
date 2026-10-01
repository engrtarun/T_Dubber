"""Benchmark Qwen3-TTS inference speed on the current GPU.

Usage:
    python -m mazinger.testing.bench_qwen_tts [--model MODEL] [--device DEVICE]
           [--dtype DTYPE] [--warmup N] [--runs N] [--voice-theme THEME]
           [--output-dir DIR]

Measures per-segment latency, real-time factor (RTF), and throughput
for the native Qwen3-TTS model.

Generated audio files are saved to ``mazinger/testing/output/`` by default
(git-ignored) so you can listen and judge quality.
"""

from __future__ import annotations

import argparse
import gc
import sys
import time

import numpy as np


def _gpu_info() -> dict:
    """Collect basic GPU information."""
    try:
        import torch

        if not torch.cuda.is_available():
            return {"gpu": "N/A (CPU only)"}
        return {
            "gpu": torch.cuda.get_device_name(0),
            "vram_total_gb": round(torch.cuda.get_device_properties(0).total_mem / 1e9, 1),
            "cuda_version": torch.version.cuda or "N/A",
            "torch_version": torch.__version__,
        }
    except Exception:
        return {"gpu": "unknown"}


# ── Test sentences (short / medium / long) ──────────────────────────────────

BENCH_SENTENCES = [
    # ~5 words — short
    ("short", "English", "Hello, how are you today?"),
    # ~20 words — medium
    ("medium", "English",
     "The quick brown fox jumps over the lazy dog near the river bank on a sunny afternoon."),
    # ~50 words — long
    ("long", "English",
     "Artificial intelligence has transformed the way we interact with technology. "
     "From voice assistants to self-driving cars, machine learning algorithms are "
     "becoming increasingly sophisticated. Researchers continue to push the boundaries "
     "of what is possible, developing new architectures that can understand and generate "
     "human language with remarkable fluency."),
    # Chinese — short
    ("short-zh", "Chinese", "你好，今天天气怎么样？"),
    # Chinese — medium
    ("medium-zh", "Chinese",
     "人工智能正在改变我们与技术互动的方式。从语音助手到自动驾驶汽车，机器学习算法变得越来越复杂。"),
]


# ── Benchmark runner ────────────────────────────────────────────────────────

def run_benchmark(
    model_name: str,
    device: str,
    dtype: str,
    warmup: int,
    runs: int,
    voice_theme: str,
    output_dir: str,
) -> None:
    import logging
    import os
    import soundfile as sf
    import torch
    from mazinger.tts import load_model, create_voice_prompt, unload_model
    from mazinger.profiles import resolve_theme

    # Suppress noisy HF "Setting pad_token_id to eos_token_id" warnings
    logging.getLogger("transformers.generation.utils").setLevel(logging.ERROR)

    # ── Prepare output directory (model-specific subfolder) ─────────────
    model_short = model_name.rsplit("/", 1)[-1]
    out_dir = os.path.join(output_dir, model_short)
    os.makedirs(out_dir, exist_ok=True)

    print("=" * 70)
    print("  Qwen3-TTS Inference Benchmark")
    print("=" * 70)

    info = _gpu_info()
    for k, v in info.items():
        print(f"  {k}: {v}")
    print(f"  model: {model_name}")
    print(f"  device: {device}")
    print(f"  dtype: {dtype}")
    print(f"  voice_theme: {voice_theme}")
    print(f"  output_dir: {out_dir}")
    print(f"  warmup: {warmup}  |  runs: {runs}")
    print("-" * 70)

    # ── Load model ──────────────────────────────────────────────────────
    print("\n⏳ Loading model...")
    t0 = time.perf_counter()
    model = load_model(model_name, device=device, dtype=dtype, engine="qwen")
    load_time = time.perf_counter() - t0
    print(f"✅ Model loaded in {load_time:.1f}s")

    # ── Resolve voice theme for ref audio ───────────────────────────────
    print(f"⏳ Resolving voice theme '{voice_theme}'...")
    t0 = time.perf_counter()
    ref_audio, ref_text = resolve_theme(
        voice_theme, "English", device=device, dtype=dtype,
    )
    theme_time = time.perf_counter() - t0
    print(f"✅ Voice theme resolved in {theme_time:.1f}s  →  {ref_audio}")

    # ── Create voice clone prompt ───────────────────────────────────────
    print("⏳ Creating voice clone prompt...")
    t0 = time.perf_counter()
    wrapper = create_voice_prompt(
        model, ref_audio, ref_text, engine="qwen",
    )
    prompt_time = time.perf_counter() - t0
    print(f"✅ Voice clone prompt created in {prompt_time:.1f}s")

    # ── Warmup ──────────────────────────────────────────────────────────
    if warmup > 0:
        print(f"\n⏳ Warming up ({warmup} iterations)...")
        for i in range(warmup):
            _ = wrapper.synthesize("Warm up sentence number one.", "English")
        torch.cuda.synchronize()
        print("✅ Warmup done")

    # ── Benchmark each sentence ─────────────────────────────────────────
    results: list[dict] = []
    total = len(BENCH_SENTENCES)

    for si, (label, language, text) in enumerate(BENCH_SENTENCES, 1):
        word_count = len(text.split())
        latencies = []
        audio_durations = []
        print(f"\n[{si}/{total}] {label} ({language}, {len(text)} chars)")

        for ri in range(runs):
            torch.cuda.synchronize()
            t0 = time.perf_counter()
            audio, sr = wrapper.synthesize(text, language)
            torch.cuda.synchronize()
            elapsed = time.perf_counter() - t0

            audio_dur = len(audio) / sr
            latencies.append(elapsed)
            audio_durations.append(audio_dur)
            print(f"  run {ri+1}/{runs}: {elapsed:.2f}s → {audio_dur:.2f}s audio (RTF {elapsed/audio_dur:.3f})")

        # Save the last run's audio for listening/judging
        wav_path = os.path.join(out_dir, f"{label}.wav")
        sf.write(wav_path, audio, sr)
        print(f"  💾 saved → {wav_path}")

        lat = np.array(latencies)
        adur = np.array(audio_durations)
        rtf = lat / adur  # < 1.0 means faster than real-time

        result = {
            "label": label,
            "words": word_count,
            "chars": len(text),
            "audio_dur_s": float(np.mean(adur)),
            "latency_mean_s": float(np.mean(lat)),
            "latency_std_s": float(np.std(lat)),
            "latency_min_s": float(np.min(lat)),
            "rtf_mean": float(np.mean(rtf)),
            "throughput_chars_per_s": len(text) / float(np.mean(lat)),
        }
        results.append(result)

    # ── Print results ───────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"  {'Label':<12} {'Words':>5} {'Audio':>7} {'Latency':>10} {'± Std':>8} "
          f"{'RTF':>6} {'Chars/s':>8}")
    print("-" * 70)

    for r in results:
        print(
            f"  {r['label']:<12} {r['words']:>5} "
            f"{r['audio_dur_s']:>6.2f}s "
            f"{r['latency_mean_s']:>9.3f}s "
            f"{r['latency_std_s']:>7.3f} "
            f"{r['rtf_mean']:>6.3f} "
            f"{r['throughput_chars_per_s']:>7.1f}"
        )

    print("-" * 70)

    # Summary
    all_rtf = [r["rtf_mean"] for r in results]
    all_lat = [r["latency_mean_s"] for r in results]
    print(f"  Average RTF: {np.mean(all_rtf):.3f}  "
          f"({'✅ faster' if np.mean(all_rtf) < 1.0 else '⚠️  slower'} than real-time)")
    print(f"  Average latency: {np.mean(all_lat):.3f}s")
    print(f"  Model load time: {load_time:.1f}s")

    # ── VRAM usage ──────────────────────────────────────────────────────
    if torch.cuda.is_available():
        allocated = torch.cuda.max_memory_allocated() / 1e9
        reserved = torch.cuda.max_memory_reserved() / 1e9
        print(f"  Peak VRAM: {allocated:.1f} GB allocated, {reserved:.1f} GB reserved")

    print("=" * 70)

    # ── Save results to JSON ────────────────────────────────────────────
    import json
    results_file = os.path.join(out_dir, "results.json")
    summary = {
        "model": model_name,
        "device": device,
        "dtype": dtype,
        "voice_theme": voice_theme,
        "gpu_info": info,
        "load_time_s": load_time,
        "avg_rtf": float(np.mean(all_rtf)),
        "avg_latency_s": float(np.mean(all_lat)),
        "sentences": results,
    }
    if torch.cuda.is_available():
        summary["peak_vram_allocated_gb"] = round(torch.cuda.max_memory_allocated() / 1e9, 1)
        summary["peak_vram_reserved_gb"] = round(torch.cuda.max_memory_reserved() / 1e9, 1)
    with open(results_file, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n💾 Results saved → {results_file}")

    # ── Cleanup ─────────────────────────────────────────────────────────
    print("\n⏳ Unloading model...")
    unload_model(model, force=True)
    gc.collect()
    torch.cuda.empty_cache()
    print("✅ Done.")


# ── CLI entry point ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark Qwen3-TTS inference speed",
    )
    parser.add_argument(
        "--model", default="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        help="HuggingFace model name (default: Qwen/Qwen3-TTS-12Hz-1.7B-Base)",
    )
    parser.add_argument(
        "--device", default="cuda:0",
        help="Device (default: cuda:0)",
    )
    parser.add_argument(
        "--dtype", default="bfloat16",
        choices=["bfloat16", "float16", "float32"],
        help="Weight dtype (default: bfloat16)",
    )
    parser.add_argument(
        "--warmup", type=int, default=2,
        help="Number of warmup iterations (default: 2)",
    )
    parser.add_argument(
        "--runs", type=int, default=3,
        help="Benchmark runs per sentence (default: 3)",
    )
    parser.add_argument(
        "--voice-theme", default="narrator-m",
        help="Mazinger voice theme for ref audio (default: narrator-m)",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Directory to save output WAVs and results JSON "
             "(default: mazinger/testing/output/)",
    )
    args = parser.parse_args()

    if args.output_dir is None:
        import os
        args.output_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "output",
        )

    run_benchmark(args.model, args.device, args.dtype, args.warmup, args.runs,
                  args.voice_theme, args.output_dir)


if __name__ == "__main__":
    main()
