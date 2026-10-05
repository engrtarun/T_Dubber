// ===========================================================================
//  normalizer.cpp -- T_Dubber "Audio Normalizer & Noise Gate"
//
//  Cleans up background noise baked into dubbed AI voices and lifts the level
//  to a target gain, streaming the file a chunk at a time so a 100 GB archive
//  costs the same RAM as a 10-second clip.
//
//  Usage
//  -----
//      normalizer <input_wav> <output_wav> <noise_threshold> <target_gain>
//
//      input_wav         16-bit integer PCM WAV, any channel count 1..8,
//                        any sample rate (the rate is passed through untouched
//                        -- this tool never resamples).
//      output_wav        canonical 44-byte-header 16-bit PCM WAV; missing
//                        parent directories are created.
//      noise_threshold   gate threshold as a fraction of full scale, 0.0..1.0.
//                        Samples whose absolute value falls below it are
//                        silenced:
//                            0.00 -> gate disabled
//                            0.02 -> -34 dBFS, kills room hiss
//                            0.10 -> -20 dBFS, aggressive
//      target_gain       linear gain multiplier applied to every surviving
//                        sample, e.g. 2.0 = +6 dB, 0.5 = -6 dB. Output is
//                        saturated at the int16 rails, never wrapped.
//
//      Exit codes: 0 = success, 1 = runtime error, 2 = bad usage.
//
//  Choosing the threshold (read this before shipping a voice track)
//  ----------------------------------------------------------------
//  The gate is *hard and per-sample*: every individual sample quieter than
//  noise_threshold is silenced -- which also silences the instants where a
//  loud signal crosses zero, because a zero crossing dips below any threshold
//  by definition. At 0.02 that is harmless (a sub-millisecond cut at speech
//  levels), and it is the behaviour the pipeline spec asks for: "zero out
//  samples below the threshold", reproducible byte for byte for the audit
//  trail. A hysteresis gate -- open at T, close at T/2 behind a release ramp --
//  would remove that artifact entirely, and is deliberately not implemented.
//
//  Practical rules for dubbed speech:
//      * keep the threshold far below program level: 0.01 .. 0.03 (-40 ..
//        -30 dBFS) is the useful band; 0.10 (-20 dBFS) starts to chew.
//      * if quiet passages tick, lower the threshold -- not the gain.
//
//  Why it is written this way
//  --------------------------
//  * Manual RIFF parsing. The WAV container is a 12-byte RIFF preamble plus a
//    list of {fourcc, size} chunks; only 'fmt ' and 'data' matter here. Pulling
//    in libsndfile (or ffmpeg) for this would add a multi-megabyte dependency
//    to a pipeline whose Go and Rust stages are statically linked singletons.
//  * Fixed-size I/O chunks (64 KiB, always a whole number of audio frames)
//    instead of one syscall per sample, and no full-file allocation ever.
//  * The inner kernel is branch-free float arithmetic -- gate is a select, gain
//    is multiply-round-clamp -- which GCC/Clang autovectorise into SSE/AVX/NEON
//    at -O2. No per-sample calls, no heap traffic.
//  * Every I/O and range condition is checked: a corrupt or unsupported file
//    yields one readable line on stderr, never a silently bad output.
//
//  Pipeline position: between the TTS/dub stage and the uploader, e.g.
//      normalizer dub_raw.wav dub_clean.wav 0.02 1.8
// ===========================================================================

#include <algorithm>
#include <array>
#include <bit>          // std::endian -- C++20
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <string_view>
#include <system_error>
#include <vector>

#include "normalizer_kernel.h"

namespace {

// ---------------------------------------------------------------------------
// Tuning
// ---------------------------------------------------------------------------

/// One read/write pair. 64 KiB amortises the syscall while staying small
/// enough to sit in L2 beside the output buffer.
constexpr std::size_t kChunkBytes = 64 * 1024;

constexpr float kInt16MaxF = 32767.0f;
constexpr float kInt16MinF = -32768.0f;

constexpr std::uint16_t kFormatPcm        = 0x0001;  // WAVE_FORMAT_PCM
constexpr std::uint16_t kFormatExtensible = 0xFFFE;  // WAVE_FORMAT_EXTENSIBLE

/// Classic RIFF stores its sizes in 32 bits; bigger inputs need RF64 and are
/// rejected with a clear message instead of written with a wrapped header.
constexpr std::uint64_t kClassicWavMaxBytes = 0xFFFF'FFF0ull;

/// WAV is little-endian by specification. On a big-endian host the buffers are
/// swapped around the kernel; on x86/ARM this compiles away to nothing.
constexpr bool kSwapBytes = (std::endian::native == std::endian::big);

// ---------------------------------------------------------------------------
// Byte helpers (explicit shifts: endian-correct on every target)
// ---------------------------------------------------------------------------

std::uint16_t load_u16_le(const unsigned char* p) {
    return static_cast<std::uint16_t>(
        static_cast<std::uint16_t>(p[0]) | (static_cast<std::uint16_t>(p[1]) << 8));
}

std::uint32_t load_u32_le(const unsigned char* p) {
    return static_cast<std::uint32_t>(p[0]) | (static_cast<std::uint32_t>(p[1]) << 8) |
           (static_cast<std::uint32_t>(p[2]) << 16) | (static_cast<std::uint32_t>(p[3]) << 24);
}

void store_u16_le(unsigned char* p, std::uint16_t v) {
    p[0] = static_cast<unsigned char>(v & 0xFFu);
    p[1] = static_cast<unsigned char>((v >> 8) & 0xFFu);
}

void store_u32_le(unsigned char* p, std::uint32_t v) {
    p[0] = static_cast<unsigned char>(v & 0xFFu);
    p[1] = static_cast<unsigned char>((v >> 8) & 0xFFu);
    p[2] = static_cast<unsigned char>((v >> 16) & 0xFFu);
    p[3] = static_cast<unsigned char>((v >> 24) & 0xFFu);
}

/// Full conversion of a CLI token: rejects empty input, trailing junk, NaN/Inf
/// and values outside the float range.
bool parse_real(const char* text, float& out) {
    if (text == nullptr || *text == '\0') {
        return false;
    }
    errno = 0;
    char* end = nullptr;
    const float value = std::strtof(text, &end);
    if (end == text || *end != '\0' || errno == ERANGE || !std::isfinite(value)) {
        return false;
    }
    out = value;
    return true;
}

bool read_u32_le(std::ifstream& in, std::uint32_t& out) {
    unsigned char bytes[4];
    if (!in.read(reinterpret_cast<char*>(bytes), 4)) {
        return false;
    }
    out = load_u32_le(bytes);
    return true;
}

// ---------------------------------------------------------------------------
// Reporting helpers
// ---------------------------------------------------------------------------

std::string format_db(double db) {
    std::ostringstream os;
    os << std::fixed << std::setprecision(1) << db;
    return os.str();
}

/// Amplitude in int16 LSBs -> "-34.0 dBFS".
std::string format_dbfs(float amplitude) {
    if (amplitude <= 0.0f) {
        return "-inf dBFS";
    }
    return format_db(20.0 * std::log10(static_cast<double>(amplitude) / 32767.0)) + " dBFS";
}

/// Linear multiplier -> "+6.0 dB".
std::string format_gain_db(float gain) {
    if (gain <= 0.0f) {
        return "-inf dB";
    }
    const double db = 20.0 * std::log10(static_cast<double>(gain));
    return (db >= 0.0 ? "+" : "") + format_db(db) + " dB";
}

// ---------------------------------------------------------------------------
// WAV container
// ---------------------------------------------------------------------------

struct WavHeader {
    std::uint16_t format_tag  = 0;
    std::uint16_t channels    = 0;
    std::uint32_t sample_rate = 0;
    std::uint16_t block_align = 0;
    std::uint16_t bits        = 0;
    std::uint64_t data_offset = 0;   // byte position of the first audio sample
    std::uint64_t data_size   = 0;   // audio bytes, already clamped to the file
    bool data_truncated       = false;  // header promised more bytes than exist
};

/// Walk the RIFF chunk list and locate 'fmt ' and 'data'. Extra chunks (LIST,
/// fact, cue, bext, ...) are stepped over; a streamed 0xFFFFFFFF data size is
/// clamped to whatever bytes actually exist.
bool parse_wav_header(const std::string& path, WavHeader& wav, std::string& err) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        err = "cannot open input '" + path + "'";
        return false;
    }

    in.seekg(0, std::ios::end);
    const std::streamoff file_end = in.tellg();
    in.seekg(0, std::ios::beg);
    if (file_end < 12) {
        err = "'" + path + "' is too small to be a WAV file";
        return false;
    }

    std::array<char, 12> riff{};
    in.read(riff.data(), 12);
    if (in.gcount() != 12 || std::memcmp(riff.data(), "RIFF", 4) != 0 ||
        std::memcmp(riff.data() + 8, "WAVE", 4) != 0) {
        err = "'" + path + "' is not a RIFF/WAVE file";
        return false;
    }

    std::array<unsigned char, 40> fmt_bytes{};
    std::size_t fmt_read = 0;
    bool have_fmt  = false;
    bool have_data = false;

    while (!have_fmt || !have_data) {
        std::array<char, 4> id{};
        if (!in.read(id.data(), 4)) {
            break;  // clean end of the chunk list
        }
        std::uint32_t size = 0;
        if (!read_u32_le(in, size)) {
            err = "'" + path + "' has a truncated chunk header";
            return false;
        }

        const std::streamoff payload = in.tellg();
        if (payload < 0 || payload > file_end) {
            break;
        }

        const bool is_fmt  = (std::memcmp(id.data(), "fmt ", 4) == 0);
        const bool is_data = (std::memcmp(id.data(), "data", 4) == 0);

        if (is_data) {
            const std::uint64_t available = static_cast<std::uint64_t>(file_end - payload);
            const bool streamed = (size == 0xFFFF'FFFFu);
            const std::uint64_t declared =
                streamed ? available : static_cast<std::uint64_t>(size);
            wav.data_offset = static_cast<std::uint64_t>(payload);
            wav.data_size   = std::min(declared, available);
            wav.data_truncated = (!streamed && declared > available);
            have_data = true;
            if (have_fmt) {
                break;
            }
            // 'data' arrived before 'fmt': step over it and keep looking.
            in.seekg(static_cast<std::streamoff>(wav.data_size) + (size & 1u), std::ios::cur);
            continue;
        }

        if (is_fmt) {
            const std::size_t take =
                std::min<std::uint32_t>(size, static_cast<std::uint32_t>(fmt_bytes.size()));
            if (take > 0 && !in.read(reinterpret_cast<char*>(fmt_bytes.data()),
                                     static_cast<std::streamsize>(take))) {
                err = "'" + path + "' has a truncated 'fmt ' chunk";
                return false;
            }
            fmt_read = take;
            have_fmt = true;
            if (have_data) {
                break;
            }
            if (size > take) {
                in.seekg(static_cast<std::streamoff>(size - take), std::ios::cur);
            }
            if ((size & 1u) != 0) {
                in.seekg(1, std::ios::cur);  // chunks are word-aligned
            }
            continue;
        }

        // Unknown chunk: reject anything that runs past EOF, otherwise skip it.
        if (static_cast<std::uint64_t>(payload) + size > static_cast<std::uint64_t>(file_end)) {
            err = "'" + path + "' has a chunk that runs past the end of the file";
            return false;
        }
        in.seekg(static_cast<std::streamoff>(size) + (size & 1u), std::ios::cur);
    }

    if (!have_fmt) {
        err = "'" + path + "' has no 'fmt ' chunk";
        return false;
    }
    if (!have_data) {
        err = "'" + path + "' has no 'data' chunk";
        return false;
    }
    if (fmt_read < 16) {
        err = "'" + path + "' has a 'fmt ' chunk shorter than 16 bytes";
        return false;
    }

    const unsigned char* f = fmt_bytes.data();
    std::uint16_t tag = load_u16_le(f + 0);
    wav.channels    = load_u16_le(f + 2);
    wav.sample_rate = load_u32_le(f + 4);
    wav.block_align = load_u16_le(f + 12);
    wav.bits        = load_u16_le(f + 14);
    if (tag == kFormatExtensible && fmt_read >= 40) {
        // WAVE_FORMAT_EXTENSIBLE: the effective codec is the first two bytes of
        // the 16-byte SubFormat GUID that begins at offset 24 of the fmt chunk.
        tag = load_u16_le(f + 24);
    }
    wav.format_tag = tag;
    return true;
}

/// Everything the processing kernel assumes, checked once up front.
bool validate_wav(const WavHeader& w, std::string& err) {
    if (w.format_tag != kFormatPcm) {
        err = "unsupported WAV encoding (format tag " + std::to_string(w.format_tag) +
              "); only 16-bit integer PCM is supported";
        return false;
    }
    if (w.bits != 16) {
        err = "unsupported bit depth " + std::to_string(w.bits) + "; only 16-bit PCM is supported";
        return false;
    }
    if (w.channels < 1 || w.channels > 8) {
        err = "unsupported channel count " + std::to_string(w.channels) + " (expected 1..8)";
        return false;
    }
    if (w.sample_rate < 1000 || w.sample_rate > 768000) {
        err = "implausible sample rate " + std::to_string(w.sample_rate) + " Hz";
        return false;
    }
    if (w.block_align != static_cast<std::uint16_t>(w.channels * (w.bits / 8))) {
        err = "inconsistent 'fmt ' chunk: block align " + std::to_string(w.block_align) +
              " does not match " + std::to_string(w.channels) + " ch x 16 bits";
        return false;
    }
    if (w.data_size == 0) {
        err = "the 'data' chunk is empty";
        return false;
    }
    return true;
}

/// Canonical 44-byte header for the output. Sizes are known up front because
/// the frame count is preserved, so no seek-back patching is needed.
bool write_wav_header(std::ofstream& out, const WavHeader& wav, std::string& err) {
    if (wav.data_size > kClassicWavMaxBytes) {
        err = "audio payload exceeds 4 GiB; classic WAV cannot address it (RF64 not implemented)";
        return false;
    }
    const auto data_size = static_cast<std::uint32_t>(wav.data_size);
    const auto byte_rate  = wav.sample_rate * static_cast<std::uint32_t>(wav.block_align);

    std::array<unsigned char, 44> h{};
    std::memcpy(h.data() + 0, "RIFF", 4);
    store_u32_le(h.data() + 4, 36u + data_size);
    std::memcpy(h.data() + 8, "WAVE", 4);
    std::memcpy(h.data() + 12, "fmt ", 4);
    store_u32_le(h.data() + 16, 16);
    store_u16_le(h.data() + 20, kFormatPcm);
    store_u16_le(h.data() + 22, wav.channels);
    store_u32_le(h.data() + 24, wav.sample_rate);
    store_u32_le(h.data() + 28, byte_rate);
    store_u16_le(h.data() + 32, wav.block_align);
    store_u16_le(h.data() + 34, 16);
    std::memcpy(h.data() + 36, "data", 4);
    store_u32_le(h.data() + 40, data_size);

    out.write(reinterpret_cast<const char*>(h.data()), static_cast<std::streamsize>(h.size()));
    if (!out) {
        err = "failed to write the output WAV header (disk full?)";
        return false;
    }
    return true;
}

// ---------------------------------------------------------------------------
// Processing kernel
// ---------------------------------------------------------------------------

struct Stats {
    std::uint64_t frames     = 0;
    std::uint64_t samples    = 0;
    std::uint64_t gated      = 0;   // samples silenced by the gate
    std::uint64_t clipped    = 0;   // samples saturated at the rails
    std::uint64_t tail_bytes = 0;   // trailing bytes that formed no whole frame
    float peak_in  = 0.0f;
    float peak_out = 0.0f;
};

/// Only instantiated on big-endian hosts; [[maybe_unused]] keeps little-endian
/// builds (x86/ARM) from warning that the helper is unreachable.
[[maybe_unused]] void byte_swap(std::int16_t* data, std::size_t count) {
    for (std::size_t i = 0; i < count; ++i) {
        const auto v = static_cast<std::uint16_t>(data[i]);
        data[i] = static_cast<std::int16_t>((v << 8) | (v >> 8));
    }
}

/// Gate + gain over one block of interleaved samples.
///
/// Written as straight-line arithmetic on purpose:
///   * the gate is a comparison + select, so there is no branch to mispredict,
///   * multiply / nearbyint / clamp map onto packed SIMD instructions,
///   * statistics ride along on the same pass (peak, gated and clipped counts)
///     so no second sweep over the data is needed.
///
/// The peaks are tracked as *integer* max reductions for a specific reason:
/// a loop-carried float `std::max` is an ordered `select` that LLVM cannot
/// prove is a reduction without -ffast-math, and one unrecognised reduction
/// makes the compiler refuse to vectorise the whole loop (confirmed with
/// -Rpass-analysis=loop-vectorize: "value that could not be identified as
/// reduction is used outside the loop"). Integer max/add reductions are
/// recognised unconditionally, so the kernel compiles to packed SIMD.
///
/// When the build carries kernels/normalizer_gate_gain.asm and the CPU can
/// run it, whole 16-sample groups take the AVX2 path instead; the loop below
/// then only finishes the block's tail. Nothing else about this function
/// changes -- it stays the reference implementation, and
/// tests/kernel_equivalence_test.cpp proves the two paths agree.
void process_block(const std::int16_t* in, std::int16_t* out, std::size_t count,
                   float gate_level, float gain, Stats& st) {
    std::int32_t peak_in  = 0;   // max |input|  in int16 LSBs
    std::int32_t peak_out = 0;   // max |output| in int16 LSBs
    std::uint64_t gated   = 0;
    std::uint64_t clipped = 0;

    std::size_t i = 0;

#if defined(TD_NORMALIZER_HAVE_ASM)
    // Optional AVX2 fast path (kernels/normalizer_gate_gain.asm). Whole
    // 16-sample groups only: the kernel rounds count down to that multiple,
    // and the loop below then finishes the tail with the reference
    // arithmetic, so an unaligned block is still processed exactly once.
    // The gate/gain/round/clamp chain is the same in both paths -- if they
    // ever disagree, tests/kernel_equivalence_test.cpp reports it.
    if (count >= 16 && td_normalizer_kernel_ready() != 0) {
        const std::size_t vector_count = count - (count % 16);
        TdNormKernelStats ks{};
        TdNormKernelParams params{};
        params.in        = in;
        params.out       = out;
        params.count     = static_cast<std::uint64_t>(vector_count);
        params.gate_level = gate_level;
        params.gain      = gain;
        params.stats     = &ks;
        td_normalizer_gate_gain(&params);
        st.gated    += ks.gated;
        st.clipped  += ks.clipped;
        st.peak_in   = std::max(st.peak_in, static_cast<float>(ks.peak_in));
        st.peak_out  = std::max(st.peak_out, static_cast<float>(ks.peak_out));
        i = vector_count;
    }
#endif

    for (; i < count; ++i) {
        const std::int32_t raw = in[i];
        const std::int32_t magnitude = (raw < 0) ? -raw : raw;

        // Noise gate: anything under the threshold becomes silence.
        const bool is_noise = static_cast<float>(magnitude) < gate_level;
        const float gated_sample = is_noise ? 0.0f : static_cast<float>(raw);

        // Gain, rounded to nearest int16, then saturated -- never wrapped.
        const float scaled  = std::nearbyint(gated_sample * gain);
        const float clamped = std::clamp(scaled, kInt16MinF, kInt16MaxF);
        const std::int32_t value = static_cast<std::int32_t>(clamped);
        out[i] = static_cast<std::int16_t>(value);

        peak_in   = std::max(peak_in, magnitude);
        peak_out  = std::max(peak_out, (value < 0) ? -value : value);
        gated   += is_noise ? 1u : 0u;
        clipped += (scaled != clamped) ? 1u : 0u;
    }

    st.peak_in   = std::max(st.peak_in, static_cast<float>(peak_in));
    st.peak_out  = std::max(st.peak_out, static_cast<float>(peak_out));
    st.gated    += gated;
    st.clipped  += clipped;
}

/// Stream input -> gate/gain -> output. Never holds more than two 64 KiB
/// buffers in memory, regardless of how long the file is.
bool run_pipeline(const WavHeader& wav, const std::string& in_path, const std::string& out_path,
                  float gate_level, float gain, Stats& st, std::string& err) {
    std::ifstream in(in_path, std::ios::binary);
    if (!in) {
        err = "cannot open input '" + in_path + "'";
        return false;
    }
    in.seekg(static_cast<std::streamoff>(wav.data_offset), std::ios::beg);
    if (!in) {
        err = "cannot seek to the audio payload in '" + in_path + "'";
        return false;
    }

    const std::filesystem::path out_fs(out_path);
    if (const std::filesystem::path parent = out_fs.parent_path(); !parent.empty()) {
        std::error_code ec;
        std::filesystem::create_directories(parent, ec);
        if (ec) {
            err = "cannot create output directory '" + parent.string() + "': " + ec.message();
            return false;
        }
    }

    std::ofstream out(out_path, std::ios::binary | std::ios::trunc);
    if (!out) {
        err = "cannot open output '" + out_path + "'";
        return false;
    }
    if (!write_wav_header(out, wav, err)) {
        return false;
    }

    const std::size_t channels        = wav.channels;
    const std::size_t block           = wav.block_align;
    const std::size_t frames_per_chunk = std::max<std::size_t>(1, kChunkBytes / block);
    const std::size_t bytes_per_chunk  = frames_per_chunk * block;
    const std::size_t samples_per_chunk = frames_per_chunk * channels;

    std::vector<std::int16_t> in_buf(samples_per_chunk);
    std::vector<std::int16_t> out_buf(samples_per_chunk);

    std::uint64_t remaining = wav.data_size;

    while (remaining > 0) {
        const auto want = static_cast<std::size_t>(
            std::min<std::uint64_t>(remaining, static_cast<std::uint64_t>(bytes_per_chunk)));

        in.read(reinterpret_cast<char*>(in_buf.data()), static_cast<std::streamsize>(want));
        const std::streamsize got = in.gcount();
        if (got <= 0) {
            err = "unexpected end of file while reading audio data from '" + in_path + "'";
            return false;
        }
        remaining = (static_cast<std::uint64_t>(got) >= remaining)
                        ? 0
                        : remaining - static_cast<std::uint64_t>(got);

        // Keep only whole frames so channels stay in lock-step.
        std::size_t usable = static_cast<std::size_t>(got);
        usable -= usable % (sizeof(std::int16_t) * channels);
        st.tail_bytes += static_cast<std::uint64_t>(static_cast<std::size_t>(got) - usable);
        if (usable == 0) {
            continue;
        }

        const std::size_t sample_count = usable / sizeof(std::int16_t);

        if constexpr (kSwapBytes) {
            byte_swap(in_buf.data(), sample_count);
        }
        process_block(in_buf.data(), out_buf.data(), sample_count, gate_level, gain, st);
        if constexpr (kSwapBytes) {
            byte_swap(out_buf.data(), sample_count);
        }

        out.write(reinterpret_cast<const char*>(out_buf.data()),
                  static_cast<std::streamsize>(usable));
        if (!out) {
            err = "write to '" + out_path + "' failed (disk full?)";
            return false;
        }

        st.frames  += sample_count / channels;
        st.samples += sample_count;
    }

    out.close();
    if (!out) {
        err = "failed to flush '" + out_path + "'";
        return false;
    }
    return true;
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

void print_usage(std::ostream& os) {
    os << "Usage:\n"
       << "  normalizer <input_wav> <output_wav> <noise_threshold> <target_gain>\n"
       << "\n"
       << "Arguments:\n"
       << "  input_wav        16-bit PCM WAV to read (1..8 channels, any sample rate)\n"
       << "  output_wav       16-bit PCM WAV to write (parent directories are created)\n"
       << "  noise_threshold  gate threshold as a fraction of full scale, 0.0 .. 1.0;\n"
       << "                   each sample quieter than it is zeroed (hard, per-sample --\n"
       << "                   keep it well below program level, 0.01 .. 0.03 for speech).\n"
       << "                   0.0 disables the gate (0.02 = -34 dBFS = hiss removal)\n"
       << "  target_gain      linear gain multiplier (2.0 = +6 dB, 0.5 = -6 dB);\n"
       << "                   output saturates at the 16-bit rails instead of wrapping\n"
       << "\n"
       << "Example:\n"
       << "  normalizer dub_raw.wav dub_clean.wav 0.02 1.8\n";
}

/// Case-folded canonical comparison so `in.wav` and `IN.WAV` cannot silently
/// truncate the source on a case-insensitive filesystem.
bool same_path(const std::string& a, const std::string& b) {
    if (a == b) {
        return true;
    }
    std::error_code ec;
    const auto pa = std::filesystem::weakly_canonical(a, ec);
    if (ec) {
        return false;
    }
    const auto pb = std::filesystem::weakly_canonical(b, ec);
    if (ec) {
        return false;
    }
#if defined(_WIN32)
    auto fold = [](std::string s) {
        std::transform(s.begin(), s.end(), s.begin(),
                       [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        return s;
    };
    return fold(pa.string()) == fold(pb.string());
#else
    return pa == pb;
#endif
}

void print_summary(const WavHeader& wav, const Stats& st, float threshold, float gain,
                   double seconds) {
    const double duration =
        (wav.sample_rate > 0) ? static_cast<double>(st.frames) / wav.sample_rate : 0.0;
    const double gated_pct =
        (st.samples > 0) ? 100.0 * static_cast<double>(st.gated) / static_cast<double>(st.samples)
                         : 0.0;
    const double mb_per_s = (seconds > 0.0)
                                ? (static_cast<double>(wav.data_size) / 1.0e6) / seconds
                                : 0.0;

    std::cout << "normalizer: ok\n"
              << "  format     : " << wav.channels << " ch, " << wav.sample_rate
              << " Hz, 16-bit PCM\n"
              << std::fixed << std::setprecision(2) << "  duration   : " << duration << " s ("
              << st.frames << " frames)\n"
              << "  gate       : " << std::setprecision(1) << (threshold * 100.0f)
              << "% FS (" << format_dbfs(threshold * 32767.0f) << ") -> " << std::setprecision(2)
              << gated_pct << "% of samples silenced\n"
              << "  gain       : x" << gain << " (" << format_gain_db(gain) << ")\n"
              << "  peak       : " << format_dbfs(st.peak_in) << " -> "
              << format_dbfs(st.peak_out) << "\n"
              << "  throughput : " << std::setprecision(1) << mb_per_s << " MB/s ("
              << std::setprecision(2) << seconds << " s)\n";

    if (st.clipped > 0) {
        std::cerr << "normalizer: warning: " << st.clipped
                  << " sample(s) saturated at the 16-bit rails -- lower target_gain if that "
                     "distorts\n";
    }
}

}  // namespace

// ===========================================================================
// main
// ===========================================================================

int main(int argc, char* argv[]) {
    if (argc == 2) {
        const std::string_view flag(argv[1]);
        if (flag == "-h" || flag == "--help" || flag == "help") {
            print_usage(std::cout);
            return 0;
        }
    }
    if (argc != 5) {
        print_usage(std::cerr);
        return 2;
    }

    const std::string in_path  = argv[1];
    const std::string out_path = argv[2];

    float threshold = 0.0f;
    if (!parse_real(argv[3], threshold)) {
        std::cerr << "normalizer: invalid noise_threshold '" << argv[3]
                  << "' (expected a number such as 0.02)\n";
        return 2;
    }
    if (threshold < 0.0f || threshold > 1.0f) {
        std::cerr << "normalizer: noise_threshold must be in 0.0 .. 1.0 (fraction of full "
                     "scale), got "
                  << threshold << "\n";
        return 2;
    }

    float gain = 0.0f;
    if (!parse_real(argv[4], gain)) {
        std::cerr << "normalizer: invalid target_gain '" << argv[4]
                  << "' (expected a multiplier such as 1.8)\n";
        return 2;
    }
    if (gain < 0.0f) {
        std::cerr << "normalizer: target_gain must not be negative, got " << gain << "\n";
        return 2;
    }
    if (gain == 0.0f) {
        std::cerr << "normalizer: warning: target_gain 0 produces silence\n";
    }

    if (same_path(in_path, out_path)) {
        std::cerr << "normalizer: input and output refer to the same file\n";
        return 1;
    }

    WavHeader wav{};
    std::string err;
    if (!parse_wav_header(in_path, wav, err) || !validate_wav(wav, err)) {
        std::cerr << "normalizer: " << err << "\n";
        return 1;
    }
    if (wav.data_truncated) {
        std::cerr << "normalizer: warning: the 'data' chunk is truncated -- the header promised "
                     "more bytes than the file contains; processing the bytes that are present\n";
    }

    // A ragged data chunk (bytes that form no whole frame) is trimmed so the
    // kernel only ever sees complete frames.
    const std::uint64_t whole_frames = (wav.data_size / wav.block_align) * wav.block_align;
    const bool input_ragged = (whole_frames != wav.data_size);
    wav.data_size = whole_frames;
    if (wav.data_size == 0) {
        std::cerr << "normalizer: '" << in_path << "' contains no whole audio frames\n";
        return 1;
    }

    const float gate_level = threshold * 32767.0f;
    Stats stats;

    const auto t0 = std::chrono::steady_clock::now();
    if (!run_pipeline(wav, in_path, out_path, gate_level, gain, stats, err)) {
        std::cerr << "normalizer: " << err << "\n";
        return 1;
    }
    const auto t1 = std::chrono::steady_clock::now();

    const double seconds = std::chrono::duration<double>(t1 - t0).count();
    print_summary(wav, stats, threshold, gain, seconds);
    if (input_ragged) {
        std::cerr << "normalizer: warning: input 'data' chunk was not a whole number of frames; "
                     "the partial tail was dropped\n";
    }
    if (stats.tail_bytes > 0) {
        std::cerr << "normalizer: warning: " << stats.tail_bytes
                  << " trailing byte(s) did not form a whole audio frame and were dropped\n";
    }

    return 0;
}
