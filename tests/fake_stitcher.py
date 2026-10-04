"""Stand-in for the Rust `stitcher` binary.

Reads the timeline.json exactly as stitcher/src/main.rs does (same field
names, same validation, same output path) and writes the mixed WAV with
soundfile.  Used to prove the Python bridge generates a manifest the real
binary would accept, and that the fast path is actually taken.
"""

import json
import sys

import numpy as np
import soundfile as sf

TARGET_SR = 24000
DUCK_FACTOR = 0.5


def main() -> int:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        tl = json.load(fh)

    # --- Same validation the Rust binary performs -----------------------
    if not tl["duration"] > 0:
        print("bad duration", file=sys.stderr)
        return 2
    if not 0.0 <= tl["background_volume"] <= 1.0:
        print("background_volume must be in [0, 1]", file=sys.stderr)
        return 2
    for seg in tl["segments"]:
        if not (seg["end"] > seg["start"] >= 0.0):
            print(f"bad span {seg}", file=sys.stderr)
            return 2
        import os
        if not os.path.isfile(seg["file"]):
            print(f"missing segment file {seg['file']}", file=sys.stderr)
            return 2

    total = round(tl["duration"] * TARGET_SR)
    mix = np.zeros(total, dtype=np.float32)

    bg_path = tl.get("background_audio") or ""
    if bg_path:
        import os
        if not os.path.isfile(bg_path):
            print(f"missing background {bg_path}", file=sys.stderr)
            return 2
        bg, _ = sf.read(bg_path, dtype="float32")
        vol = float(tl["background_volume"])
        for i in range(total):
            mix[i] = bg[i % len(bg)] * vol

    # Duck the union of voice spans.
    spans = sorted(
        (round(s["start"] * TARGET_SR), round(s["end"] * TARGET_SR))
        for s in tl["segments"]
    )
    merged: list[list[int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    for a, b in merged:
        mix[a:b] *= DUCK_FACTOR

    for seg in tl["segments"]:
        voice, _ = sf.read(seg["file"], dtype="float32")
        start = round(seg["start"] * TARGET_SR)
        window = round((seg["end"] - seg["start"]) * TARGET_SR)
        take = min(len(voice), window, total - start)
        mix[start:start + take] += voice[:take]

    sf.write(tl["output"], np.clip(mix, -1.0, 1.0), TARGET_SR, subtype="PCM_16")

    # Echo what the Rust binary logs so the bridge's parsing is exercised.
    print(f"[stitcher] [  0.01s] timeline: {tl['duration']:.2f}s -> {total} samples",
          file=sys.stderr)
    print("[stitcher] [  0.02s] mixing complete", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
