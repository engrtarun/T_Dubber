"""Stand-in for the C++ `cpp_accelerator/normalizer` binary.

Implements the real tool's contract so the Python bridge can be proven
without a C++ toolchain:

  * exact CLI:   normalizer <input> <output> <threshold> <gain>
  * exact exits: 0 success, 1 runtime error, 2 bad usage
  * manual RIFF/WAVE parsing (no soundfile), rejecting anything that is
    not 16-bit integer PCM with 1..8 channels and a sane sample rate
  * input == output is refused, exactly like `same_path()` in the C++
  * the same gate + gain kernel, in float32, with round-half-to-even
  * frame count preserved, canonical 44-byte header on output

Fault injection (for the bridge's validation rails):

  * ``FAKE_NORMALIZER_LOG``     append each argv line to this file
  * ``FAKE_NORMALIZER_EMPTY``   exit 0 without writing any file
  * ``FAKE_NORMALIZER_TRUNCATE``n  write n frames fewer than the input

Every invocation appends a line to $FAKE_NORMALIZER_LOG so the tests can
prove whether the fast path was actually taken or silently declined.
"""

from __future__ import annotations

import os
import struct
import sys

import numpy as np

CHUNK = 64 * 1024
USAGE = (
    "Usage:\n"
    "  normalizer <input_wav> <output_wav> <noise_threshold> <target_gain>\n"
)


def log_invocation(argv: list[str]) -> None:
    path = os.environ.get("FAKE_NORMALIZER_LOG", "").strip()
    if not path:
        return
    # Tab-separated: unlike a space, a tab cannot appear in a filename, so
    # the tests can split the line without guessing at quoting.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\t".join(argv) + "\n")


def parse_real(text: str) -> float:
    """Mirrors C++ parse_real: reject empty, junk, NaN/Inf and overflow."""
    if not text:
        raise ValueError("empty")
    value = float(text)
    if not np.isfinite(value):
        raise ValueError("not finite")
    return value


def parse_wav(path: str) -> tuple[dict, int, int]:
    """Return (header, data_offset, data_size) or raise with a readable line.

    Walks the RIFF chunk list looking only for 'fmt ' and 'data', the same
    way normalizer.cpp does.
    """
    size_on_disk = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(12)
        if len(head) < 12:
            raise ValueError(f"'{path}' is too small to be a WAV file")
        if head[0:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError(f"'{path}' is not a RIFF/WAVE file")

        fmt: bytes | None = None
        data_offset = data_size = -1

        while True:
            hdr = fh.read(8)
            if len(hdr) < 8:
                break
            cid = hdr[0:4]
            declared = struct.unpack("<I", hdr[4:8])[0]
            payload = fh.tell()

            if cid == b"data":
                available = size_on_disk - payload
                if declared == 0xFFFFFFFF:
                    declared = available
                data_offset = payload
                data_size = min(declared, available)
                if fmt is not None:
                    break
                fh.seek(data_offset + data_size + (declared & 1))
                continue

            if cid == b"fmt ":
                take = min(declared, 40)
                fmt = fh.read(take)
                if data_offset >= 0:
                    break
                if declared > take:
                    fh.seek(declared - take, 1)
                if declared & 1:
                    fh.seek(1, 1)
                continue

            # Unknown chunk: refuse to step past EOF, otherwise skip.
            if payload + declared > size_on_disk:
                raise ValueError(f"'{path}' has a chunk that runs past the end of the file")
            fh.seek(declared + (declared & 1), 1)

        if fmt is None:
            raise ValueError(f"'{path}' has no 'fmt ' chunk")
        if data_offset < 0:
            raise ValueError(f"'{path}' has no 'data' chunk")
        if len(fmt) < 16:
            raise ValueError(f"'{path}' has a 'fmt ' chunk shorter than 16 bytes")

    tag, channels, rate, _byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", fmt, 0)
    if tag == 0xFFFE and len(fmt) >= 40:
        tag = struct.unpack_from("<H", fmt, 24)[0]
    if tag != 0x0001:
        raise ValueError(
            f"unsupported WAV encoding (format tag {tag}); only 16-bit integer PCM is supported"
        )
    if bits != 16:
        raise ValueError(f"unsupported bit depth {bits}; only 16-bit PCM is supported")
    if not 1 <= channels <= 8:
        raise ValueError(f"unsupported channel count {channels} (expected 1..8)")
    if not 1000 <= rate <= 768000:
        raise ValueError(f"implausible sample rate {rate} Hz")
    if block_align != channels * (bits // 8):
        raise ValueError(
            f"inconsistent 'fmt ' chunk: block align {block_align} does not match "
            f"{channels} ch x 16 bits"
        )
    if data_size == 0:
        raise ValueError("the 'data' chunk is empty")

    # A ragged tail forms no whole frame and is dropped, like the C++ does.
    whole = (data_size // block_align) * block_align
    return {"channels": channels, "rate": rate, "block_align": block_align}, data_offset, whole


def write_header(fh, channels: int, rate: int, data_size: int) -> None:
    block_align = channels * 2
    fh.write(b"RIFF")
    fh.write(struct.pack("<I", 36 + data_size))
    fh.write(b"WAVE")
    fh.write(b"fmt ")
    fh.write(struct.pack("<IHHIIHH", 16, 0x0001, channels, rate,
                         rate * block_align, block_align, 16))
    fh.write(b"data")
    fh.write(struct.pack("<I", data_size))


def process_block(samples: np.ndarray, gate_level: np.float32, gain: np.float32) -> np.ndarray:
    """The C++ kernel: gate is a select, gain is multiply-round-clamp."""
    raw = samples.astype(np.float32, copy=False)
    magnitude = np.abs(raw)
    gated = np.where(magnitude < gate_level, np.float32(0.0), raw)
    scaled = np.rint(gated * gain)              # nearbyint == round-half-even
    return np.clip(scaled, np.float32(-32768.0), np.float32(32767.0)).astype(np.int16)


def main(argv: list[str]) -> int:
    log_invocation(argv)

    if len(argv) == 2 and argv[1] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    if len(argv) != 5:
        sys.stderr.write(USAGE)
        return 2

    in_path, out_path = argv[1], argv[2]

    try:
        threshold = parse_real(argv[3])
    except ValueError:
        sys.stderr.write(f"normalizer: invalid noise_threshold '{argv[3]}'\n")
        return 2
    if not 0.0 <= threshold <= 1.0:
        sys.stderr.write("normalizer: noise_threshold must be in 0.0 .. 1.0\n")
        return 2

    try:
        gain = parse_real(argv[4])
    except ValueError:
        sys.stderr.write(f"normalizer: invalid target_gain '{argv[4]}'\n")
        return 2
    if gain < 0.0:
        sys.stderr.write("normalizer: target_gain must not be negative\n")
        return 2

    if os.path.abspath(in_path) == os.path.abspath(out_path):
        sys.stderr.write("normalizer: input and output refer to the same file\n")
        return 1

    try:
        info, offset, data_size = parse_wav(in_path)
    except FileNotFoundError:
        sys.stderr.write(f"normalizer: cannot open input '{in_path}'\n")
        return 1
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"normalizer: {exc}\n")
        return 1

    gate_level = np.float32(threshold) * np.float32(32767.0)
    gain32 = np.float32(gain)
    channels = info["channels"]
    block = info["block_align"]

    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    frames = data_size // block
    peak_in = peak_out = 0
    gated_count = 0
    total = 0

    # --- fault injection (only ever set by the test harness) ------------
    if os.environ.get("FAKE_NORMALIZER_EMPTY"):
        sys.stdout.write("normalizer: ok (but nothing was written)\n")
        return 0
    truncate = int(os.environ.get("FAKE_NORMALIZER_TRUNCATE", "0") or 0)
    if truncate > 0:
        data_size = max(block, data_size - truncate * block)
        frames = data_size // block

    try:
        with open(in_path, "rb") as src, open(out_path, "wb") as dst:
            src.seek(offset)
            write_header(dst, channels, info["rate"], data_size)

            remaining = data_size
            while remaining > 0:
                want = min(remaining, (CHUNK // block) * block)
                buf = src.read(want)
                if not buf:
                    sys.stderr.write("normalizer: unexpected end of file\n")
                    return 1
                remaining -= len(buf)

                usable = len(buf) - (len(buf) % block)   # whole frames only
                chunk = np.frombuffer(buf[:usable], dtype="<i2")

                out = process_block(chunk, gate_level, gain32)
                peak_in = max(peak_in, int(np.abs(chunk).max()))
                peak_out = max(peak_out, int(np.abs(out).max()))
                gated_count += int((np.abs(chunk.astype(np.float32)) < gate_level).sum())

                dst.write(out.astype("<i2").tobytes())
                total += out.size
    except OSError as exc:
        sys.stderr.write(f"normalizer: {exc}\n")
        return 1

    duration = frames / info["rate"]
    pct = (100.0 * gated_count / total) if total else 0.0
    sys.stdout.write(
        "normalizer: ok\n"
        f"  format     : {channels} ch, {info['rate']} Hz, 16-bit PCM\n"
        f"  duration   : {duration:.2f} s ({frames} frames)\n"
        f"  gate       : {threshold * 100.0:.1f}% FS -> {pct:.2f}% of samples silenced\n"
        f"  gain       : x{gain}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
