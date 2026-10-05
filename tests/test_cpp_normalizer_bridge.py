"""Verify the C++ normalizer bridge in mazinger.assemble.

Run:  python tests/test_cpp_normalizer_bridge.py

Requires STITCHER_TEST_DEPS=<dir> holding soundfile/tqdm when the ambient
environment does not have them (same shim as test_stitcher_bridge.py).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile

# --- optional dependency shim, before mazinger is imported -----------------
_deps = os.environ.get("STITCHER_TEST_DEPS", "").strip()
if _deps and os.path.isdir(_deps):
    sys.path.insert(0, _deps)
    # The stand-in binary runs as a child process and needs the same libs.
    os.environ["PYTHONPATH"] = (
        _deps + os.pathsep + os.environ.get("PYTHONPATH", "")
    ).rstrip(os.pathsep)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "mazinger"))

import numpy as np
import soundfile as sf

from mazinger import assemble

HERE = os.path.dirname(os.path.abspath(__file__))
# Windows uses a .bat launcher; POSIX shells need +x on the .sh.
FAKE_NORMALIZER = os.path.join(
    HERE, "fake_normalizer.bat" if sys.platform == "win32" else "fake_normalizer.sh"
)
if sys.platform != "win32":
    os.chmod(FAKE_NORMALIZER, 0o755)

SR = assemble.TARGET_SR
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"    ok   {label}")
    else:
        print(f"    FAIL {label} {detail}")
        failures.append(label)


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


def reset(**env: str) -> None:
    """Clear every bridge knob so each case starts from a known state."""
    for name in (
        "NORMALIZER_BIN",
        "MAZINGER_NORMALIZER",
        "FAKE_NORMALIZER_LOG",
        "FAKE_NORMALIZER_EMPTY",
        "FAKE_NORMALIZER_TRUNCATE",
        "STITCHER_BIN",
        "MAZINGER_STITCHER",
    ):
        os.environ.pop(name, None)
    assemble._normalizer_binary_cache.clear()
    assemble._stitcher_binary_cache.clear()
    os.environ.update(env)


def write_pcm16(path: str, data: np.ndarray, sr: int = SR) -> str:
    sf.write(path, data.astype(np.int16), sr, format="WAV", subtype="PCM_16")
    return path


def speech_like(root: str, name: str = "in.wav", duration: float = 1.0) -> str:
    """Write a tone with a quiet tail (so the gate silences part of it).

    Returns the path, which is what every caller wants to hand straight to
    the bridge.
    """
    n = int(duration * SR)
    t = np.arange(n, dtype=np.float32) / SR
    tone = 0.5 * np.sin(2 * np.pi * 220.0 * t)
    # Last 0.3 s fades to a level below the 0.02 gate.
    tail = int(0.3 * SR)
    tone[-tail:] *= np.linspace(0.02, 0.0, tail).astype(np.float32)
    path = os.path.join(root, name)
    write_pcm16(path, np.clip(tone * 32767.0, -32768, 32767))
    return path


def read_i16(path: str) -> np.ndarray:
    return np.asarray(sf.read(path, dtype="int16")[0])


def invocations(log_path: str) -> list[str]:
    if not os.path.exists(log_path):
        return []
    with open(log_path, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


# ---------------------------------------------------------------------------
# 1-3: discovery
# ---------------------------------------------------------------------------

def case_repo_root() -> None:
    """Regression: the root must be the workspace, not its parent."""
    root = assemble._repo_root()
    check("_repo_root resolves to the workspace", os.path.isdir(root), root)
    check("workspace root holds mazinger/", os.path.isdir(os.path.join(root, "mazinger")), root)
    check(
        "workspace root holds cpp_accelerator/",
        os.path.isdir(os.path.join(root, "cpp_accelerator")),
        root,
    )


def case_normalizer_discovery() -> None:
    """The CMake output directory is where discovery looks."""
    fake_root = tempfile.mkdtemp(prefix="root_")
    try:
        built = os.path.join(fake_root, "cpp_accelerator", "build")
        os.makedirs(built)
        exe = "normalizer.exe" if sys.platform == "win32" else "normalizer"
        target = os.path.join(built, exe)
        with open(target, "wb") as fh:
            fh.write(b"MZ")

        assemble._normalizer_binary_cache.clear()
        reset()
        import unittest.mock as mock
        with mock.patch.object(assemble, "_repo_root", lambda: fake_root):
            found = assemble._normalizer_binary()
        check("single-config build/ is discovered", found == target, str(found))

        # And a multi-config (MSVC) layout is found too.
        os.remove(target)
        nested = os.path.join(fake_root, "cpp_accelerator", "build", "Release")
        os.makedirs(nested, exist_ok=True)
        target2 = os.path.join(nested, exe)
        with open(target2, "wb") as fh:
            fh.write(b"MZ")
        assemble._normalizer_binary_cache.clear()
        with mock.patch.object(assemble, "_repo_root", lambda: fake_root):
            found2 = assemble._normalizer_binary()
        check("multi-config build/Release/ is discovered", found2 == target2, str(found2))
    finally:
        shutil.rmtree(fake_root, ignore_errors=True)
        reset()


def case_stitcher_discovery() -> None:
    """Regression for the 4-vs-3 dirname bug: Rust discovery works now too."""
    fake_root = tempfile.mkdtemp(prefix="root_")
    try:
        built = os.path.join(fake_root, "stitcher", "target", "release")
        os.makedirs(built)
        exe = "stitcher.exe" if sys.platform == "win32" else "stitcher"
        target = os.path.join(built, exe)
        with open(target, "wb") as fh:
            fh.write(b"MZ")

        reset()
        import unittest.mock as mock
        with mock.patch.object(assemble, "_repo_root", lambda: fake_root):
            found = assemble._stitcher_binary()
        check("cargo release/ target is discovered", found == target, str(found))
    finally:
        shutil.rmtree(fake_root, ignore_errors=True)
        reset()


def case_foreign_normalizer_shadowing() -> None:
    """A foreign PATH `normalizer` must be rejected, never silently used.

    Kaggle's base image ships a Python argparse tool also named `normalizer`
    (``-t THRESHOLD``). It shadowed the pack, exited 2 on our four-argument
    call, and dropped every run onto the slow Python engine. Discovery must
    recognize the impostor by its CLI and refuse it, while an explicit
    ``NORMALIZER_BIN`` and a repo-local build still outrank a hostile PATH.
    """
    root = tempfile.mkdtemp(prefix="norm_")
    empty_root = tempfile.mkdtemp(prefix="root_")
    try:
        import unittest.mock as mock

        foreign_dir = os.path.join(root, "foreign_path")
        os.makedirs(foreign_dir)
        foreign_marker = os.path.join(foreign_dir, "foreign_calls.txt")
        if sys.platform == "win32":
            foreign = os.path.join(foreign_dir, "normalizer.bat")
            with open(foreign, "w", encoding="utf-8") as fh:
                fh.write(
                    "@echo off\r\n"
                    f'echo foreign>>"{foreign_marker}"\r\n'
                    "echo usage: normalizer [-h] [-v] [-a] [-n] [-m] [-r] [-f] [-i] [-t THRESHOLD] 1>&2\r\n"
                    "echo normalizer: error: the following arguments are required: files 1>&2\r\n"
                    "exit /b 2\r\n"
                )
        else:
            foreign = os.path.join(foreign_dir, "normalizer")
            with open(foreign, "w", encoding="utf-8") as fh:
                fh.write(
                    "#!/bin/sh\n"
                    f'echo foreign >> "{foreign_marker}"\n'
                    "echo 'usage: normalizer [-h] [-v] [-a] [-n] [-m] [-r] [-f] [-i] [-t THRESHOLD]' >&2\n"
                    "echo 'normalizer: error: the following arguments are required: files' >&2\n"
                    "exit 2\n"
                )
            os.chmod(foreign, 0o755)

        hostile_path = {"PATH": foreign_dir}

        # 1. The impostor alone: rejected, and it was the probe that proved it.
        with mock.patch.dict(os.environ, hostile_path), \
                mock.patch.object(assemble, "_repo_root", lambda: empty_root):
            reset()
            assemble._normalizer_binary_cache.clear()
            found = assemble._normalizer_binary()
        check("foreign PATH normalizer is rejected", found is None, str(found))
        check("the impostor was probed (its CLI caused the rejection)",
              os.path.exists(foreign_marker), foreign_marker)

        # 2. An explicit override is the operator's deliberate choice: it is
        #    trusted without a probe and wins over the hostile PATH.
        with mock.patch.dict(os.environ, hostile_path), \
                mock.patch.object(assemble, "_repo_root", lambda: empty_root):
            reset(NORMALIZER_BIN=FAKE_NORMALIZER)
            assemble._normalizer_binary_cache.clear()
            found_override = assemble._normalizer_binary()
        check("explicit NORMALIZER_BIN wins over the hostile PATH",
              found_override == FAKE_NORMALIZER, str(found_override))

        # 3. A repository build outranks a PATH hit of any kind.
        built = os.path.join(empty_root, "cpp_accelerator", "build")
        os.makedirs(built)
        exe = "normalizer.exe" if sys.platform == "win32" else "normalizer"
        target = os.path.join(built, exe)
        with open(target, "wb") as fh:
            fh.write(b"MZ")
        with mock.patch.dict(os.environ, hostile_path), \
                mock.patch.object(assemble, "_repo_root", lambda: empty_root):
            reset()
            assemble._normalizer_binary_cache.clear()
            found_repo = assemble._normalizer_binary()
        check("repo-local build outranks the hostile PATH",
              found_repo == target, str(found_repo))

        # 4. End to end: with only the impostor on PATH, the public entry
        #    point must clean the file via Python -- correct output, impostor
        #    never invoked with our four arguments.
        fresh_root = tempfile.mkdtemp(prefix="root_")
        try:
            input_path = speech_like(root)
            out = os.path.join(root, "out.wav")
            with mock.patch.dict(os.environ, hostile_path), \
                    mock.patch.object(assemble, "_repo_root", lambda: fresh_root):
                reset()
                assemble._normalizer_binary_cache.clear()
                result = assemble.normalize_audio(input_path, out)
            check("normalize_audio falls back to Python instead of the impostor",
                  result == out and os.path.exists(out) and sf.info(out).subtype == "PCM_16",
                  str(result))
            check("the fallback output actually gated the quiet tail",
                  not np.any(read_i16(out)[int(0.7 * SR):]))
        finally:
            shutil.rmtree(fresh_root, ignore_errors=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(empty_root, ignore_errors=True)
        reset()


# ---------------------------------------------------------------------------
# 4-6: the two engines
# ---------------------------------------------------------------------------

def case_binary_absent() -> None:
    root = tempfile.mkdtemp(prefix="norm_")
    log = os.path.join(root, "invocations.log")
    try:
        input_path = speech_like(root)
        out = os.path.join(root, "out.wav")
        reset(FAKE_NORMALIZER_LOG=log)
        assemble._normalizer_binary_cache["path"] = None   # force "not built"

        check("_normalize_audio_with_cpp declines without a binary",
              assemble._normalize_audio_with_cpp(input_path, out) is None)
        result = assemble.normalize_audio(input_path, out)
        check("normalize_audio falls back to Python", result == out, str(result))
        info = sf.info(out)
        check("output exists and is non-empty",
              os.path.exists(out) and os.path.getsize(out) > 0)
        check("duration preserved", abs(info.duration - sf.info(input_path).duration) < 1e-6)
        check("still 16-bit PCM at 24 kHz",
              info.subtype == "PCM_16" and info.samplerate == SR)
        check("the binary was never spawned", invocations(log) == [], str(invocations(log)))
        # The gate must actually have silenced the quiet tail.
        data = read_i16(out)
        src = read_i16(input_path)
        tail = data[int(0.7 * SR):]
        check("quiet tail was gated to silence", not np.any(tail), str(tail[:8]))
        check("loud head survived", np.abs(data[: int(0.5 * SR)]).max() > 1000)
        check("gain was applied to the head",
              int(np.abs(data[: int(0.5 * SR)]).max())
              > int(np.abs(src[: int(0.5 * SR)]).max()))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        reset()


def case_binary_present() -> None:
    root = tempfile.mkdtemp(prefix="norm_")
    log = os.path.join(root, "invocations.log")
    try:
        input_path = speech_like(root)
        out = os.path.join(root, "out.wav")
        reset(NORMALIZER_BIN=FAKE_NORMALIZER, FAKE_NORMALIZER_LOG=log)
        assemble._normalizer_binary_cache.clear()

        result = assemble._normalize_audio_with_cpp(
            input_path, out, threshold=0.02, gain=1.8
        )
        check("fast path returns the output path", result == out, str(result))
        info = sf.info(out)
        check("output written by the binary", os.path.exists(out) and os.path.getsize(out) > 0)
        check("duration preserved", abs(info.duration - sf.info(input_path).duration) < 1e-6)
        check("format untouched",
              info.samplerate == SR and info.channels == 1 and info.subtype == "PCM_16")

        calls = invocations(log)
        check("exactly one invocation", len(calls) == 1, str(calls))
        if calls:
            # argv[0] is the launcher's script path; the four real arguments
            # follow, and the output argument must be the *staged* temp file
            # (the tool refuses input == output, and the final path is only
            # written by os.replace once the result validates).
            parts = calls[0].split("\t")
            staged = parts[2] if len(parts) == 5 else ""
            check(
                "argv is <in> <staged_out> <threshold> <gain>",
                len(parts) == 5
                and parts[0].endswith("fake_normalizer.py")
                and parts[1] == input_path
                and staged != out
                and os.path.dirname(staged) == os.path.dirname(out)
                and staged.endswith(".norm.tmp.wav")
                and parts[3] == "0.020000"
                and parts[4] == "1.800000",
                calls[0],
            )
        check("staging temp cleaned up",
              not [f for f in os.listdir(root) if f.endswith(".norm.tmp.wav")],
              str(os.listdir(root)))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        reset()


def case_parity() -> None:
    """Both engines must agree sample for sample — that is the contract."""
    root = tempfile.mkdtemp(prefix="norm_")
    try:
        # Values straddling the gate edge (0.02 * 32767 = 655.34) and the
        # rails, so gating, rounding and saturation are all exercised.
        base = np.array(
            [0, 1, 654, 655, 656, 657, 1000, -655, -656, 32767, -32768, 18203, -18204],
            dtype=np.int16,
        )
        ramp = np.arange(-32768, 32768, 7, dtype=np.int32).astype(np.int16)
        rng = np.random.default_rng(1234)
        noise = rng.integers(-32768, 32768, size=4096, dtype=np.int16)
        src = np.concatenate([base, ramp, noise])

        input_path = os.path.join(root, "parity.wav")
        write_pcm16(input_path, src)

        out_py = os.path.join(root, "python.wav")
        out_cpp = os.path.join(root, "cpp.wav")
        reset(NORMALIZER_BIN=FAKE_NORMALIZER)
        assemble._normalizer_binary_cache.clear()

        assemble._normalize_audio_python(input_path, out_py, threshold=0.02, gain=1.8)
        got_cpp = assemble._normalize_audio_with_cpp(
            input_path, out_cpp, threshold=0.02, gain=1.8
        )
        check("both engines produced output", got_cpp == out_cpp and os.path.exists(out_py))

        a, b = read_i16(out_py), read_i16(out_cpp)
        check("same frame count", a.shape == b.shape, f"{a.shape} vs {b.shape}")
        if a.shape == b.shape:
            diff = np.flatnonzero(a != b)
            check("byte-for-byte identical", diff.size == 0,
                  f"{diff.size} samples differ, first at {diff[:5] if diff.size else '-'}")

        # And the shared contract really did gate, gain and saturate.
        # base layout:  0:0  1:1  2:654  3:655  4:656  5:657  6:1000
        #               7:-655  8:-656  9:32767  10:-32768  11:18203  12:-18204
        # The gate level is 0.02 * 32767 = 655.34, so 655 falls under it and
        # 656 does not -- that single unit is the whole edge, pinned here.
        check("samples below the gate edge were zeroed",
              a[0] == 0 and a[1] == 0 and a[2] == 0 and a[3] == 0,
              f"{a[0]},{a[1]},{a[2]},{a[3]}")
        check("samples above it survived", a[4] != 0 and a[8] != 0, f"{a[4]},{a[8]}")
        check("gain lifted surviving samples", int(a[4]) == 1181, str(a[4]))
        check("the positive rail saturated, not wrapped", a[9] == 32767, str(a[9]))
        check("the negative rail saturated, not wrapped", a[10] == -32768, str(a[10]))
        # Just below the rail: multiplied but not clipped, so saturation
        # only bites where it must.
        check("a loud-but-legal sample was not clipped", int(a[11]) == 32765, str(a[11]))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        reset()


# ---------------------------------------------------------------------------
# 7-11: the validation rails
# ---------------------------------------------------------------------------

def case_crashing_binary() -> None:
    root = tempfile.mkdtemp(prefix="norm_")
    try:
        input_path = speech_like(root)
        out = os.path.join(root, "out.wav")
        crashing = os.path.join(root, "crash.bat")
        with open(crashing, "w", encoding="utf-8") as fh:
            fh.write("@echo off\r\necho boom: something exploded 1>&2\r\nexit /b 3\r\n")
        reset(NORMALIZER_BIN=crashing)

        check("non-zero exit returns None",
              assemble._normalize_audio_with_cpp(input_path, out) is None)
        result = assemble.normalize_audio(input_path, out)
        check("public API still succeeds via the Python path", result == out, str(result))
        check("fallback output is valid", os.path.exists(out) and sf.info(out).frames > 0)
        check("no staging temp left behind",
              not [f for f in os.listdir(root) if f.endswith(".norm.tmp.wav")])
    finally:
        shutil.rmtree(root, ignore_errors=True)
        reset()


def case_empty_output() -> None:
    root = tempfile.mkdtemp(prefix="norm_")
    try:
        input_path = speech_like(root)
        out = os.path.join(root, "out.wav")
        reset(NORMALIZER_BIN=FAKE_NORMALIZER, FAKE_NORMALIZER_EMPTY="1")
        assemble._normalizer_binary_cache.clear()
        check("clean exit with no file returns None",
              assemble._normalize_audio_with_cpp(input_path, out) is None)
        check("nothing was left at the output path", not os.path.exists(out))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        reset()


def case_wrong_frame_count() -> None:
    root = tempfile.mkdtemp(prefix="norm_")
    try:
        input_path = speech_like(root, duration=1.0)
        out = os.path.join(root, "out.wav")
        reset(NORMALIZER_BIN=FAKE_NORMALIZER, FAKE_NORMALIZER_TRUNCATE="4800")
        assemble._normalizer_binary_cache.clear()
        check("a short file is rejected",
              assemble._normalize_audio_with_cpp(input_path, out) is None)
        check("the bad output was not moved into place", not os.path.exists(out))
        check("the staging temp was cleaned up",
              not [f for f in os.listdir(root) if f.endswith(".norm.tmp.wav")])
    finally:
        shutil.rmtree(root, ignore_errors=True)
        reset()


def case_env_switch() -> None:
    root = tempfile.mkdtemp(prefix="norm_")
    log = os.path.join(root, "invocations.log")
    try:
        input_path = speech_like(root)
        out = os.path.join(root, "out.wav")
        reset(NORMALIZER_BIN=FAKE_NORMALIZER, MAZINGER_NORMALIZER="off",
              FAKE_NORMALIZER_LOG=log)
        assemble._normalizer_binary_cache.clear()

        check("MAZINGER_NORMALIZER=off forbids the fast path",
              assemble._normalize_audio_with_cpp(input_path, out) is None)
        check("use_cpp=False forbids the fast path",
              assemble._normalize_audio_with_cpp(input_path, out, use_cpp=False) is None)
        result = assemble.normalize_audio(input_path, out)
        check("and the Python path still produces output", result == out)
        check("the binary was never spawned", invocations(log) == [],
              str(invocations(log)))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        reset()


def case_unsupported_input() -> None:
    root = tempfile.mkdtemp(prefix="norm_")
    log = os.path.join(root, "invocations.log")
    try:
        # A float32 WAV: soundfile reports FLOAT, the C++ tool cannot read it.
        float_path = os.path.join(root, "float.wav")
        sf.write(float_path, np.zeros(SR, dtype=np.float32), SR, format="WAV",
                 subtype="FLOAT")
        out = os.path.join(root, "out.wav")
        reset(NORMALIZER_BIN=FAKE_NORMALIZER, FAKE_NORMALIZER_LOG=log)
        assemble._normalizer_binary_cache.clear()

        check("float32 input is declined before spawning",
              assemble._normalize_audio_with_cpp(float_path, out) is None)
        check("the binary was not invoked", invocations(log) == [],
              str(invocations(log)))
        # The Python path still handles it (soundfile converts to int16).
        result = assemble.normalize_audio(float_path, out)
        check("the Python path still cleans it", result == out and os.path.exists(out))
        check("output promoted to 16-bit PCM", sf.info(out).subtype == "PCM_16")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        reset()


def case_missing_input() -> None:
    root = tempfile.mkdtemp(prefix="norm_")
    try:
        reset(NORMALIZER_BIN=FAKE_NORMALIZER)
        assemble._normalizer_binary_cache.clear()
        missing = os.path.join(root, "nope.wav")
        out = os.path.join(root, "out.wav")
        try:
            result = assemble.normalize_audio(missing, out)
            raised = None
        except Exception as exc:  # noqa: BLE001
            result, raised = None, exc
        check("a missing input never raises", raised is None, repr(raised))
        check("and returns None so the caller can pass audio through",
              result is None, str(result))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        reset()


# ---------------------------------------------------------------------------
# 12-14: in-place, post_process integration
# ---------------------------------------------------------------------------

def case_in_place() -> None:
    root = tempfile.mkdtemp(prefix="norm_")
    try:
        for engine, env in (("C++", {"NORMALIZER_BIN": FAKE_NORMALIZER}),
                            ("Python", {"NORMALIZER_BIN": FAKE_NORMALIZER,
                                        "MAZINGER_NORMALIZER": "off"})):
            path = os.path.join(root, f"inplace_{engine}.wav")
            src = speech_like(root, f"src_{engine}.wav")
            shutil.copy2(src, path)
            before = read_i16(path)

            reset(**env)
            assemble._normalizer_binary_cache.clear()
            result = assemble.normalize_audio(path, path, threshold=0.02, gain=1.8)
            check(f"{engine}: in-place call returns the path", result == path, str(result))
            check(f"{engine}: in-place file is valid PCM_16",
                  os.path.exists(path) and sf.info(path).subtype == "PCM_16")
            after = read_i16(path)
            check(f"{engine}: in-place call changed the audio",
                  after.shape == before.shape and not np.array_equal(after, before))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        reset()


def _scene(root: str) -> tuple[str, str]:
    """A dubbed file plus an original, for post_process()."""
    speech_like(root, "dubbed.wav", duration=2.0)
    dubbed = os.path.join(root, "dubbed.wav")
    original = os.path.join(root, "original.wav")
    t = np.arange(2 * SR, dtype=np.float32) / SR
    write_pcm16(original, (0.3 * np.sin(2 * np.pi * 110.0 * t) * 32767).astype(np.int16))
    return dubbed, original


def case_post_process_with_binary() -> None:
    root = tempfile.mkdtemp(prefix="norm_")
    log = os.path.join(root, "invocations.log")
    try:
        dubbed, original = _scene(root)
        out = os.path.join(root, "final.wav")
        reset(NORMALIZER_BIN=FAKE_NORMALIZER, FAKE_NORMALIZER_LOG=log)
        assemble._normalizer_binary_cache.clear()

        assemble.post_process(dubbed, original, out,
                              loudness_match=False, mix_background=False,
                              noise_gate=True)
        check("post_process produced output", os.path.exists(out) and sf.info(out).frames > 0)
        check("duration preserved", abs(sf.info(out).duration - 2.0) < 0.05,
              str(sf.info(out).duration))
        check("the C++ engine was used", len(invocations(log)) == 1,
              str(invocations(log)))
        check("no .gate.wav left behind",
              not os.path.exists(out + ".gate.wav"),
              str([f for f in os.listdir(root) if "gate" in f]))
        check("gate silenced the quiet tail",
              not np.any(read_i16(out)[int(1.7 * SR):]))
        # gate_gain defaults to 1.0 here, so the gate must clean without
        # changing level: the loud head's peak is untouched even though the
        # tone's zero crossings (which dip under the threshold) are nulled.
        head = slice(0, int(1.5 * SR))
        before, after = read_i16(dubbed)[head], read_i16(out)[head]
        check("the gate changed the audio", not np.array_equal(before, after))
        check("default gate does not change the level",
              int(np.abs(after).max()) == int(np.abs(before).max()),
              f"{int(np.abs(after).max())} vs {int(np.abs(before).max())}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        reset()


def case_post_process_without_binary() -> None:
    root = tempfile.mkdtemp(prefix="norm_")
    try:
        dubbed, original = _scene(root)
        out = os.path.join(root, "final.wav")
        reset()
        assemble._normalizer_binary_cache["path"] = None   # force "not built"

        assemble.post_process(dubbed, original, out,
                              loudness_match=False, mix_background=False,
                              noise_gate=True)
        check("post_process still works with no C++ binary",
              os.path.exists(out) and sf.info(out).frames > 0)
        check("the Python path gated the tail",
              not np.any(read_i16(out)[int(1.7 * SR):]))
        check("no .gate.wav left behind", not os.path.exists(out + ".gate.wav"))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        reset()


def case_gate_disabled() -> None:
    root = tempfile.mkdtemp(prefix="norm_")
    log = os.path.join(root, "invocations.log")
    try:
        dubbed, original = _scene(root)
        out = os.path.join(root, "final.wav")
        reset(NORMALIZER_BIN=FAKE_NORMALIZER, FAKE_NORMALIZER_LOG=log)
        assemble._normalizer_binary_cache.clear()

        assemble.post_process(dubbed, original, out,
                              loudness_match=False, mix_background=False,
                              noise_gate=False)
        check("noise_gate=False produces output", os.path.exists(out))
        check("the binary was not invoked", invocations(log) == [],
              str(invocations(log)))
        # Without the gate the quiet tail is left alone.
        tail = read_i16(out)[int(1.7 * SR):]
        check("the quiet tail is untouched", np.any(tail))

        # ...and with all three stages off, post_process is a pure copy.
        out2 = os.path.join(root, "copy.wav")
        assemble.post_process(dubbed, original, out2,
                              loudness_match=False, mix_background=False,
                              noise_gate=False)
        check("all stages off degrades to a copy",
              os.path.exists(out2)
              and np.array_equal(read_i16(out2), read_i16(dubbed)))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        reset()


def case_post_process_with_loudness() -> None:
    """The gate must run *before* loudnorm, and compose with it cleanly."""
    root = tempfile.mkdtemp(prefix="norm_")
    log = os.path.join(root, "invocations.log")
    try:
        dubbed, original = _scene(root)
        out = os.path.join(root, "final.wav")
        reset(NORMALIZER_BIN=FAKE_NORMALIZER, FAKE_NORMALIZER_LOG=log)
        assemble._normalizer_binary_cache.clear()

        assemble.post_process(dubbed, original, out,
                              loudness_match=True, mix_background=False,
                              noise_gate=True, gate_gain=assemble.NORMALIZER_GAIN)
        check("gated + loudness-matched output exists",
              os.path.exists(out) and sf.info(out).frames > 0)
        check("duration still preserved", abs(sf.info(out).duration - 2.0) < 0.05,
              str(sf.info(out).duration))
        check("the gate ran", len(invocations(log)) == 1, str(invocations(log)))
        leftovers = [f for f in os.listdir(root)
                     if f.endswith((".gate.wav", ".norm.wav"))]
        check("no intermediate files left behind", leftovers == [], str(leftovers))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        reset()


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="      %(message)s")
    if not os.path.isfile(FAKE_NORMALIZER):
        print(f"missing stand-in binary: {FAKE_NORMALIZER}")
        return 1

    run_case("1. repo root regression", case_repo_root)
    run_case("2. normalizer binary discovery", case_normalizer_discovery)
    run_case("3. stitcher binary discovery (dirname bug)", case_stitcher_discovery)
    run_case("3b. foreign PATH normalizer is rejected", case_foreign_normalizer_shadowing)
    run_case("4. binary absent -> Python fallback", case_binary_absent)
    if run_case("5. binary present -> C++ fast path", case_binary_present):
        run_case("6. both engines agree exactly", case_parity)
    run_case("7. crashing binary -> fallback", case_crashing_binary)
    run_case("8. clean exit, no file -> fallback", case_empty_output)
    run_case("9. wrong frame count -> fallback", case_wrong_frame_count)
    run_case("10. MAZINGER_NORMALIZER=off", case_env_switch)
    run_case("11. unsupported input declined early", case_unsupported_input)
    run_case("12. missing input never raises", case_missing_input)
    run_case("13. in-place input == output", case_in_place)
    run_case("14. post_process with the C++ engine", case_post_process_with_binary)
    run_case("15. post_process without the binary", case_post_process_without_binary)
    run_case("16. noise_gate=False leaves audio alone", case_gate_disabled)
    run_case("17. gate composes with loudness matching", case_post_process_with_loudness)

    print("\n" + "=" * 64)
    if failures:
        for name in failures:
            print(f"FAILED  {name}")
        print(f"{len(failures)} check(s) failed")
        return 1
    print("All C++ normalizer-bridge checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
