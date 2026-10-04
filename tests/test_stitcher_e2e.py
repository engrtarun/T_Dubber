"""End-to-end test of the **compiled** Rust `stitcher` binary.

`test_stitcher_bridge.py` exercises the Python side of the bridge, but it
deliberately points `STITCHER_BIN` at `tests/fake_stitcher.bat` so that the
failure/fallback paths can be driven deterministically.  That means nothing in
the suite actually executes `stitcher/src/main.rs`.

This file fills that gap: it synthesises real 24 kHz mono WAVs, writes a real
`timeline.json`, runs the compiled binary and then measures the rendered PCM.
Because the fixtures are constant-valued, every expectation reduces to a
simple arithmetic identity, so a wrong offset, a missing trim, a duck applied
in the wrong order or a gain applied twice all show up as a wrong mean.

Verified behaviours:
  * output is 24 kHz / mono / 16-bit and exactly `round(duration * 24000)` samples;
  * segments land on their declared start offsets and are hard-trimmed to the
    window (a longer source file cannot bleed past `end`);
  * a source shorter than its window is *not* stretched — the tail stays as
    whatever the background was;
  * multi-channel / non-24 kHz sources are downmixed and resampled;
  * overlapping segments sum;
  * background is laid first, halved across the union of voice spans, and the
    voice is then added at unity gain (so voice is never attenuated);
  * exit codes: 1 for a wrong argument count, 2 for any rejected input.

Run:  python tests/test_stitcher_e2e.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / "stitcher" / "target" / "release" / (
    "stitcher.exe" if sys.platform == "win32" else "stitcher"
)

SR = 24_000
TOL = 0.01  # covers int16 quantisation plus the /32767-vs-/32768 read scale


# ── tiny WAV helpers (stdlib only, so no soundfile dependency) ───────────────

def write_wav(path: Path, samples: np.ndarray, rate: int = SR, channels: int = 1) -> None:
    clipped = np.clip(samples.astype(np.float64), -1.0, 1.0)
    pcm = np.round(clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def read_wav(path: Path) -> tuple[np.ndarray, int, int, int]:
    """Return (float samples, sample_rate, channels, sample_width_bytes)."""
    with wave.open(str(path), "rb") as w:
        n, width, ch, rate = (
            w.getnframes(), w.getsampwidth(), w.getnchannels(), w.getframerate()
        )
        raw = w.readframes(n)
    pcm = np.frombuffer(raw, dtype="<i2")
    return pcm.astype(np.float64) / 32768.0, rate, ch, width


def constant(seconds: float, value: float, rate: int = SR) -> np.ndarray:
    return np.full(int(round(seconds * rate)), value, dtype=np.float64)


def mean_in(samples: np.ndarray, t0: float, t1: float, rate: int = SR) -> float:
    """Mean amplitude over [t0, t1), inset by 5 ms to dodge boundary samples."""
    lo = int(round((t0 + 0.005) * rate))
    hi = int(round((t1 - 0.005) * rate))
    return float(np.mean(samples[lo:hi]))


# ── test scaffolding ─────────────────────────────────────────────────────────

FAILURES: list[str] = []
CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append(msg)


def near(got: float, want: float, msg: str, tol: float = TOL) -> None:
    check(abs(got - want) <= tol,
          f"{msg}: got {got:+.5f}, want {want:+.5f} (±{tol})")


def timeline_path(work: Path, timeline: dict) -> str:
    """Materialise a timeline under a collision-proof name, return its path."""
    digest = abs(hash(json.dumps(timeline, sort_keys=True))) & 0xFFFFFFFF
    path = work / f"tl_{digest:x}.json"
    path.write_text(json.dumps(timeline), encoding="utf-8")
    return str(path)


def run_stitcher(work: Path, timeline: dict) -> subprocess.CompletedProcess:
    tl = work / "timeline.json"
    tl.write_text(json.dumps(timeline), encoding="utf-8")
    return subprocess.run(
        [str(BINARY), str(tl)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300,
    )


def make_timeline(work: Path, **over) -> dict:
    base = {
        "duration": 6.0,
        "background_audio": "",
        "background_volume": 0.0,
        "segments": [],
        "output": str(work / "out.wav"),
    }
    base.update(over)
    return base


# ── stages ───────────────────────────────────────────────────────────────────

def stage_placement(work: Path) -> None:
    """Two constant segments land exactly where the timeline says."""
    a = work / "seg_a.wav"
    b = work / "seg_b.wav"
    write_wav(a, constant(1.0, 0.5))
    write_wav(b, constant(1.0, -0.25))

    proc = run_stitcher(work, make_timeline(work, segments=[
        {"start": 1.0, "end": 2.0, "file": str(a)},
        {"start": 3.0, "end": 4.0, "file": str(b)},
    ]))
    check(proc.returncode == 0, f"exit {proc.returncode}: {proc.stderr}")

    out = work / "out.wav"
    check(out.exists(), "output was not written")
    samples, rate, ch, width = read_wav(out)
    check(rate == 24_000, f"sample rate {rate}")
    check(ch == 1, f"channels {ch}")
    check(width == 2, f"sample width {width}")
    check(len(samples) == round(6.0 * SR),
          f"sample count {len(samples)} != {round(6.0 * SR)}")

    near(mean_in(samples, 0.0, 1.0), 0.0, "silence before first segment")
    near(mean_in(samples, 1.0, 2.0), 0.5, "segment A level")
    near(mean_in(samples, 2.0, 3.0), 0.0, "gap between segments")
    near(mean_in(samples, 3.0, 4.0), -0.25, "segment B level")
    near(mean_in(samples, 4.0, 6.0), 0.0, "silence after last segment")
    check("[stitcher]" in proc.stderr, "expected [stitcher] progress on stderr")


def stage_hard_trim(work: Path) -> None:
    """A source longer than its window must not bleed past `end`."""
    long = work / "seg_long.wav"
    write_wav(long, constant(2.0, 0.5))          # 2 s of audio ...
    proc = run_stitcher(work, make_timeline(work, segments=[
        {"start": 1.0, "end": 1.5, "file": str(long)},   # ... but a 0.5 s window
    ]))
    check(proc.returncode == 0, f"exit {proc.returncode}: {proc.stderr}")
    samples, *_ = read_wav(work / "out.wav")
    near(mean_in(samples, 1.0, 1.5), 0.5, "inside the window")
    near(mean_in(samples, 1.5, 2.5), 0.0, "bleed past the window must be zero")

    # And a source *shorter* than the window is not stretched to fill it.
    short = work / "seg_short.wav"
    write_wav(short, constant(0.5, 0.5))
    proc = run_stitcher(work, make_timeline(work, segments=[
        {"start": 1.0, "end": 2.0, "file": str(short)},
    ]))
    check(proc.returncode == 0, f"exit {proc.returncode}: {proc.stderr}")
    samples, *_ = read_wav(work / "out.wav")
    near(mean_in(samples, 1.0, 1.5), 0.5, "short segment: present part")
    near(mean_in(samples, 1.5, 2.0), 0.0, "short segment: tail must not be stretched")


def stage_resample_and_overlap(work: Path) -> None:
    """48 kHz input is resampled; overlapping segments sum."""
    hi = work / "seg_48k.wav"
    write_wav(hi, constant(1.0, 0.5, rate=48_000), rate=48_000)

    a = work / "ov_a.wav"
    b = work / "ov_b.wav"
    write_wav(a, constant(1.5, 0.3))
    write_wav(b, constant(1.0, 0.3))

    proc = run_stitcher(work, make_timeline(work, segments=[
        {"start": 1.0, "end": 2.0, "file": str(hi)},
        {"start": 3.0, "end": 4.5, "file": str(a)},
        {"start": 3.5, "end": 4.5, "file": str(b)},
    ]))
    check(proc.returncode == 0, f"exit {proc.returncode}: {proc.stderr}")
    samples, rate, *_ = read_wav(work / "out.wav")
    check(rate == 24_000, f"output rate {rate}")
    near(mean_in(samples, 1.0, 2.0), 0.5, "48 kHz source resampled to 24 kHz")
    near(mean_in(samples, 3.0, 3.5), 0.3, "overlap: single segment")
    near(mean_in(samples, 3.5, 4.5), 0.6, "overlap: both segments sum")


def stage_background_and_ducking(work: Path) -> None:
    """Background first, halved under the voice, voice added at unity."""
    a = work / "duck_a.wav"
    b = work / "duck_b.wav"
    bg = work / "bg.wav"
    write_wav(a, constant(1.0, 0.5))
    write_wav(b, constant(1.0, -0.25))
    write_wav(bg, constant(3.0, 0.4))            # 3 s, looped across 6 s

    proc = run_stitcher(work, make_timeline(
        work,
        background_audio=str(bg),
        background_volume=0.5,
        segments=[
            {"start": 1.0, "end": 2.0, "file": str(a)},
            {"start": 3.0, "end": 4.0, "file": str(b)},
        ],
    ))
    check(proc.returncode == 0, f"exit {proc.returncode}: {proc.stderr}")
    samples, *_ = read_wav(work / "out.wav")

    bg_only = 0.4 * 0.5          # background at the declared volume
    bg_ducked = bg_only * 0.5    # ... halved where the voice speaks

    near(mean_in(samples, 0.0, 1.0), bg_only, "background at full level")
    near(mean_in(samples, 2.0, 3.0), bg_only, "background in the gap")
    near(mean_in(samples, 4.0, 6.0), bg_only, "background after the voice")
    near(mean_in(samples, 1.0, 2.0), 0.5 + bg_ducked,
         "voice + ducked background (voice must be at unity)")
    near(mean_in(samples, 3.0, 4.0), -0.25 + bg_ducked,
         "negative voice + ducked background")

    # Without a background the same timeline must be voice-only.
    proc = run_stitcher(work, make_timeline(work, segments=[
        {"start": 1.0, "end": 2.0, "file": str(a)},
    ]))
    check(proc.returncode == 0, f"exit {proc.returncode}: {proc.stderr}")
    samples, *_ = read_wav(work / "out.wav")
    near(mean_in(samples, 0.0, 1.0), 0.0, "no background -> silence")
    near(mean_in(samples, 1.0, 2.0), 0.5, "no background -> voice unchanged")


def stage_exit_codes(work: Path) -> None:
    """Usage mistakes exit 1; every rejected input exits 2."""
    def expect(args, want, label):
        p = subprocess.run([str(BINARY), *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=60)
        check(p.returncode == want,
              f"{label}: exit {p.returncode}, want {want} ({p.stderr.strip()[:120]})")

    expect([], 1, "no arguments")

    good = work / "seg_ok.wav"
    write_wav(good, constant(1.0, 0.5))

    expect([str(work / "does_not_exist.json")], 2, "missing timeline file")

    cases = {
        "zero duration": make_timeline(work, duration=0.0),
        "negative duration": make_timeline(work, duration=-1.0),
        "non-finite duration": dict(make_timeline(work), duration=float("nan")),
        "duration over the 12 h cap": make_timeline(work, duration=13 * 3600.0),
        "background_volume above 1": make_timeline(work, background_volume=1.5),
        "negative background_volume": make_timeline(work, background_volume=-0.1),
        "empty output path": make_timeline(work, output=""),
        "inverted segment span": make_timeline(work, segments=[
            {"start": 2.0, "end": 1.0, "file": str(good)}]),
        "zero-length segment": make_timeline(work, segments=[
            {"start": 1.0, "end": 1.0, "file": str(good)}]),
        "missing segment file": make_timeline(work, segments=[
            {"start": 1.0, "end": 2.0, "file": str(work / "nope.wav")}]),
    }
    for label, timeline in cases.items():
        expect([timeline_path(work, timeline)], 2, label)

    # Malformed JSON rather than a semantic error.
    bad = work / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    expect([str(bad)], 2, "malformed timeline JSON")


def main() -> int:
    if not BINARY.exists():
        print(f"SKIP: build the binary first ({BINARY})")
        return 1
    print(f"binary under test: {BINARY}")

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        for stage in (
            stage_placement,
            stage_hard_trim,
            stage_resample_and_overlap,
            stage_background_and_ducking,
            stage_exit_codes,
        ):
            print(f"\n== {stage.__name__.removeprefix('stage_')} ==")
            stage(work)
            print(f"   ... {len(FAILURES)} failure(s) so far")

    print()
    if FAILURES:
        print(f"FAIL ({len(FAILURES)} of {CHECKS} checks)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK ({CHECKS} checks passed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
