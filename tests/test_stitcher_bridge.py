"""Verify the Rust stitcher bridge in mazinger.assemble.

Run:  python tests/test_stitcher_bridge.py

Optional: STITCHER_TEST_DEPS=<dir> adds a directory to sys.path before
importing mazinger (used to supply soundfile/tqdm without installing them
into the ambient environment).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile

# Optional dependency shim, before mazinger is imported.
_deps = os.environ.get("STITCHER_TEST_DEPS", "").strip()
if _deps and os.path.isdir(_deps):
    sys.path.insert(0, _deps)
    # The stand-in binary runs as a child process and needs the same libs.
    os.environ["PYTHONPATH"] = (
        _deps + os.pathsep + os.environ.get("PYTHONPATH", "")
    ).rstrip(os.pathsep)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mazinger"
))

import numpy as np
import soundfile as sf

from mazinger import assemble

HERE = os.path.dirname(os.path.abspath(__file__))
# Windows uses a .bat launcher; POSIX shells need +x on the .sh.
FAKE_STITCHER = os.path.join(
    HERE, "fake_stitcher.bat" if sys.platform == "win32" else "fake_stitcher.sh"
)
if sys.platform != "win32":
    os.chmod(FAKE_STITCHER, 0o755)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"    ok   {label}")
    else:
        print(f"    FAIL {label} {detail}")
        failures.append(label)


def make_scene(root: str, *, with_background: bool = False):
    """Two 24 kHz mono voice segments, an optional stereo-ish background."""
    def tone(path, dur, freq, amp=0.4):
        t = np.linspace(0, dur, int(dur * assemble.TARGET_SR), endpoint=False,
                        dtype=np.float32)
        sf.write(path, (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32),
                 assemble.TARGET_SR, subtype="PCM_16")

    segs_dir = os.path.join(root, "segments")
    os.makedirs(segs_dir, exist_ok=True)
    tone(os.path.join(segs_dir, "seg_0001.wav"), 3.0, 220.0)
    tone(os.path.join(segs_dir, "seg_0002.wav"), 2.0, 330.0, amp=0.3)

    bg = ""
    if with_background:
        bg = os.path.join(root, "background.wav")
        tone(bg, 12.0, 110.0, amp=0.5)

    segment_info = [
        {"idx": "0001", "start": 1.0, "end": 4.0, "target_dur": 3.0,
         "wav_path": os.path.join(segs_dir, "seg_0001.wav"), "actual_dur": 3.0},
        {"idx": "0002", "start": 5.0, "end": 7.0, "target_dur": 2.0,
         "wav_path": os.path.join(segs_dir, "seg_0002.wav"), "actual_dur": 2.0},
    ]
    return segment_info, bg


def run_case(label: str, fn) -> bool:
    """Run one case; return True when it added no failures."""
    print(f"\n[{label}]")
    before = len(failures)
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        print(f"    FAIL unexpected {exc!r}")
        failures.append(f"{label}: {exc!r}")
    return len(failures) == before


def case_binary_absent() -> None:
    root = tempfile.mkdtemp(prefix="stitch_")
    try:
        segs, _ = make_scene(root)
        out = os.path.join(root, "out.wav")
        os.environ.pop("STITCHER_BIN", None)
        os.environ.pop("MAZINGER_STITCHER", None)
        assemble._stitcher_binary_cache.clear()
        assemble._stitcher_binary_cache["path"] = None  # force "not built"

        result = assemble.assemble_timeline(segs, 10.0, out, tempo_mode="off")
        check("falls back to the numpy path", result == out)
        check("output exists and is non-empty",
              os.path.exists(out) and os.path.getsize(out) > 0)
        check("valid 24 kHz WAV", sf.info(out).samplerate == assemble.TARGET_SR)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_rust_used() -> bool:
    root = tempfile.mkdtemp(prefix="stitch_")
    try:
        segs, _ = make_scene(root)
        out = os.path.join(root, "out.wav")
        os.environ["STITCHER_BIN"] = FAKE_STITCHER
        os.environ.pop("MAZINGER_STITCHER", None)
        assemble._stitcher_binary_cache.clear()

        result = assemble._assemble_audio_with_rust(segs, 10.0, out)
        check("fast path returns the output path", result == out, str(result))
        check("output written by the binary", os.path.exists(out))
        info = sf.info(out)
        check("duration = original + TAIL_PAD_SEC",
              abs(info.duration - (10.0 + assemble.TAIL_PAD_SEC)) < 0.01,
              f"got {info.duration}")
        check("24 kHz mono 16-bit",
              info.samplerate == assemble.TARGET_SR and info.channels == 1)

        # The scratch manifest must not survive the call.
        manifest = os.path.join(root, ".stitcher_timeline.json")
        check("timeline.json cleaned up", not os.path.exists(manifest))
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"    FAIL unexpected {exc!r}")
        failures.append(f"rust_used: {exc!r}")
        return False
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_manifest_schema() -> None:
    """Capture the manifest the bridge hands to the binary."""
    root = tempfile.mkdtemp(prefix="stitch_")
    captured = os.path.join(root, "captured.json")
    try:
        segs, bg = make_scene(root, with_background=True)
        out = os.path.join(root, "out.wav")

        real_run = subprocess.run

        def spy(cmd, *a, **kw):
            if len(cmd) == 2 and cmd[0] == FAKE_STITCHER:
                with open(captured, "w", encoding="utf-8") as fh:
                    json.dump(json.load(open(cmd[1], encoding="utf-8")), fh)
            return real_run(cmd, *a, **kw)

        os.environ["STITCHER_BIN"] = FAKE_STITCHER
        assemble._stitcher_binary_cache.clear()
        import unittest.mock as mock
        with mock.patch.object(assemble.subprocess, "run", spy):
            assemble._assemble_audio_with_rust(
                segs, 10.0, out, background_audio=bg, background_volume=0.2
            )

        with open(captured, encoding="utf-8") as fh:
            tl = json.load(fh)

        check("has every field the Rust struct requires",
              set(tl) == {"duration", "background_audio", "background_volume",
                          "segments", "output"}, str(sorted(tl)))
        check("segments carry exactly start/end/file",
              all(set(s) == {"start", "end", "file"} for s in tl["segments"]))
        check("segment files are absolute",
              all(os.path.isabs(s["file"]) and os.path.isfile(s["file"])
                  for s in tl["segments"]))
        check("output path is absolute", os.path.isabs(tl["output"]))
        check("background_volume passed through",
              abs(tl["background_volume"] - 0.2) < 1e-9)
        check("background_audio passed through", tl["background_audio"] == bg)
        check("duration includes the tail pad",
              abs(tl["duration"] - 12.0) < 1e-6, str(tl["duration"]))
        # seg1 may run up to seg2's start (numpy trims at next_start; the
        # gap only feeds the tempo budget). seg2 may fill the tail pad.
        s1, s2 = tl["segments"]
        check("first segment stops at the next segment's start",
              abs(s1["end"] - 5.0) < 1e-6, str(s1))
        check("last segment may fill the tail pad",
              abs(s2["end"] - (10.0 + assemble.TAIL_PAD_SEC)) < 1e-6, str(s2))
        check("segments ordered by start time",
              tl["segments"][0]["start"] < tl["segments"][1]["start"])
    except Exception as exc:  # noqa: BLE001
        print(f"    FAIL unexpected {exc!r}")
        failures.append(f"manifest: {exc!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_fallback_on_crash() -> None:
    """A binary that fails must not break the pipeline."""
    root = tempfile.mkdtemp(prefix="stitch_")
    try:
        segs, _ = make_scene(root)
        out = os.path.join(root, "out.wav")

        crashing = os.path.join(root, "crash.bat")
        with open(crashing, "w", encoding="utf-8") as fh:
            fh.write("@echo off\r\necho boom: something exploded 1>&2\r\nexit /b 3\r\n")

        os.environ["STITCHER_BIN"] = crashing
        assemble._stitcher_binary_cache.clear()
        check("non-zero exit returns None",
              assemble._assemble_audio_with_rust(segs, 10.0, out) is None)

        # And the public entry point still produces a file.
        result = assemble.assemble_timeline(segs, 10.0, out, tempo_mode="off")
        check("public API still succeeds via fallback", result == out)
        check("fallback output is valid",
              os.path.exists(out) and sf.info(out).duration > 0)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_fallback_on_empty_output() -> None:
    """Zero exit but no file must be treated as a failure."""
    root = tempfile.mkdtemp(prefix="stitch_")
    try:
        segs, _ = make_scene(root)
        out = os.path.join(root, "out.wav")
        silent = os.path.join(root, "silent.bat")
        with open(silent, "w", encoding="utf-8") as fh:
            fh.write("@echo off\r\nexit /b 0\r\n")  # exits clean, writes nothing
        os.environ["STITCHER_BIN"] = silent
        assemble._stitcher_binary_cache.clear()
        check("clean exit with no output returns None",
              assemble._assemble_audio_with_rust(segs, 10.0, out) is None)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_env_switch() -> None:
    root = tempfile.mkdtemp(prefix="stitch_")
    try:
        segs, _ = make_scene(root)
        out = os.path.join(root, "out.wav")
        os.environ["STITCHER_BIN"] = FAKE_STITCHER
        os.environ["MAZINGER_STITCHER"] = "off"
        assemble._stitcher_binary_cache.clear()
        check("MAZINGER_STITCHER=off forbids the fast path",
              assemble._assemble_audio_with_rust(segs, 10.0, out) is None)
        result = assemble.assemble_timeline(segs, 10.0, out, tempo_mode="off")
        check("and the numpy path still produces output",
              result == out and os.path.exists(out))
    finally:
        os.environ.pop("MAZINGER_STITCHER", None)
        shutil.rmtree(root, ignore_errors=True)


def case_stale_output_not_trusted() -> None:
    """A stale WAV from a previous run must not pass as success."""
    root = tempfile.mkdtemp(prefix="stitch_")
    try:
        segs, _ = make_scene(root)
        out = os.path.join(root, "out.wav")
        dead = os.path.join(root, "dead.bat")
        with open(dead, "w", encoding="utf-8") as fh:
            fh.write("@echo off\r\nexit /b 0\r\n")
        # Pre-create a bogus output; the bridge deletes it before running.
        with open(out, "wb") as fh:
            fh.write(b"not a wav")
        os.environ["STITCHER_BIN"] = dead
        assemble._stitcher_binary_cache.clear()
        check("stale file is not mistaken for success",
              assemble._assemble_audio_with_rust(segs, 10.0, out) is None)
        check("stale file was removed before the run", not os.path.exists(out))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_empty_segments() -> None:
    root = tempfile.mkdtemp(prefix="stitch_")
    try:
        out = os.path.join(root, "out.wav")
        os.environ["STITCHER_BIN"] = FAKE_STITCHER
        assemble._stitcher_binary_cache.clear()
        check("no segments returns None (numpy path is cheaper)",
              assemble._assemble_audio_with_rust([], 10.0, out) is None)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_wrong_sample_rate() -> None:
    root = tempfile.mkdtemp(prefix="stitch_")
    try:
        segs, _ = make_scene(root)
        out = os.path.join(root, "out.wav")
        os.environ["STITCHER_BIN"] = FAKE_STITCHER
        assemble._stitcher_binary_cache.clear()
        check("sample_rate != 24 kHz declines the fast path",
              assemble._assemble_audio_with_rust(segs, 10.0, out, sample_rate=16000) is None)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="      %(message)s")
    if not os.path.isfile(FAKE_STITCHER):
        print(f"missing stand-in binary: {FAKE_STITCHER}")
        return 1

    run_case("1. binary absent -> numpy fallback", case_binary_absent)
    if run_case("2. binary present -> fast path", case_rust_used):
        run_case("3. timeline.json matches the Rust schema", case_manifest_schema)
    run_case("4. crashing binary -> fallback", case_fallback_on_crash)
    run_case("5. clean exit with no file -> fallback", case_fallback_on_empty_output)
    run_case("6. MAZINGER_STITCHER=off", case_env_switch)
    run_case("7. stale output not trusted", case_stale_output_not_trusted)
    run_case("8. empty segment list", case_empty_segments)
    run_case("9. unsupported sample rate", case_wrong_sample_rate)

    print("\n" + "=" * 62)
    if failures:
        for name in failures:
            print(f"FAILED  {name}")
        print(f"{len(failures)} check(s) failed")
        return 1
    print("All stitcher-bridge checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
