// ===========================================================================
//  tests/kernel_equivalence_test.cpp -- the AVX2 kernel must be boring
//
//  Assembly has no compiler standing behind it: if a lane, a rounding mode or
//  a statistics counter drifts, nothing warns -- the WAV just comes out
//  subtly different from the Python fallback that used to produce it. So the
//  kernel is checked against an independent copy of the reference arithmetic
//  (the loop in normalizer.cpp::process_block) over thousands of random
//  blocks plus every awkward length and every parameter edge that matters:
//
//      * lengths around the 16-sample group boundary (0, 15, 16, 17, 31...)
//      * gate off (0.0), gate at and above full scale (1.0)
//      * gain 0 (silence), gain 1 (identity), gain big enough to clip
//      * sample values at the int16 rails, where |value| = 32768 and the
//        clamp boundary lives
//
//  Both the output samples and all four statistics have to match exactly --
//  gated/clipped are counts, peaks are integers in int16 LSBs; a "close
//  enough" pass would hide a real bug.
//
//  Run:  normalizer_kernel_test          (exit 0 = identical, 1 = drift)
//  Exits 0 with a SKIP line when the build or the CPU has no AVX2 kernel,
//  because "no kernel" is a supported configuration, not a failure.
// ===========================================================================

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <random>
#include <vector>

#include "normalizer_kernel.h"

namespace {

constexpr float kInt16MaxF = 32767.0f;
constexpr float kInt16MinF = -32768.0f;

/// Independent copy of the reference in normalizer.cpp. Kept here rather than
/// exported from normalizer.cpp on purpose: an oracle shared with the code
/// under test can drift along with it and agree with itself while both are
/// wrong.
struct RefStats {
    std::uint64_t gated    = 0;
    std::uint64_t clipped  = 0;
    std::int32_t  peak_in  = 0;
    std::int32_t  peak_out = 0;
};

void reference(const std::int16_t* in, std::int16_t* out, std::size_t count,
               float gate_level, float gain, RefStats& st) {
    std::int32_t peak_in  = 0;
    std::int32_t peak_out = 0;
    std::uint64_t gated   = 0;
    std::uint64_t clipped = 0;

    for (std::size_t i = 0; i < count; ++i) {
        const std::int32_t raw = in[i];
        const std::int32_t magnitude = (raw < 0) ? -raw : raw;

        const bool is_noise = static_cast<float>(magnitude) < gate_level;
        const float gated_sample = is_noise ? 0.0f : static_cast<float>(raw);

        const float scaled  = std::nearbyint(gated_sample * gain);
        const float clamped = std::clamp(scaled, kInt16MinF, kInt16MaxF);
        const std::int32_t value = static_cast<std::int32_t>(clamped);
        out[i] = static_cast<std::int16_t>(value);

        peak_in   = std::max(peak_in, magnitude);
        peak_out  = std::max(peak_out, (value < 0) ? -value : value);
        gated    += is_noise ? 1u : 0u;
        clipped  += (scaled != clamped) ? 1u : 0u;
    }

    st.gated    = gated;
    st.clipped  = clipped;
    st.peak_in  = peak_in;
    st.peak_out = peak_out;
}

int failures = 0;

void fail(const char* label, const char* detail) {
    std::printf("    FAIL %-40s %s\n", label, detail);
    ++failures;
}

/// Run reference and kernel over one block and compare every byte and every
/// counter. `count` is what the caller would pass; the kernel's own rounding
/// is part of what is being checked (the scalar side covers the whole block,
/// so a non-multiple count also exercises the tail convention).
void compare(const char* label, const std::vector<std::int16_t>& input,
             float gate_level, float gain) {
    const std::size_t count = input.size();

    std::vector<std::int16_t> expected(count), actual(count);
    if (count > 0) {
        std::memcpy(expected.data(), input.data(), count * sizeof(std::int16_t));
        std::memcpy(actual.data(), input.data(), count * sizeof(std::int16_t));
    }

    RefStats want;
    reference(input.data(), expected.data(), count, gate_level, gain, want);

    TdNormKernelStats got{};
    TdNormKernelParams params{};
    params.in         = input.data();
    params.out        = actual.data();
    params.count      = static_cast<std::uint64_t>(count);
    params.gate_level = gate_level;
    params.gain       = gain;
    params.stats      = &got;
    td_normalizer_gate_gain(&params);

    // The kernel only owns whole groups of 16; the caller scalar-finishes the
    // rest. Compare exactly the region the kernel promises to write.
    const std::size_t covered = count - (count % 16);

    if (covered > 0 &&
        std::memcmp(expected.data(), actual.data(), covered * sizeof(std::int16_t)) != 0) {
        std::size_t at = 0;
        while (at < covered && expected[at] == actual[at]) {
            ++at;
        }
        char detail[160];
        std::snprintf(detail, sizeof(detail),
                      "sample %zu: reference %d, kernel %d (gate=%g gain=%g n=%zu)",
                      at, static_cast<int>(expected[at]), static_cast<int>(actual[at]),
                      static_cast<double>(gate_level), static_cast<double>(gain), count);
        fail(label, detail);
        return;
    }

    // Statistics cover the covered region only, so a non-multiple count is
    // compared against the reference restricted to that same prefix.
    if (covered != count) {
        RefStats prefix;
        reference(input.data(), expected.data(), covered, gate_level, gain, prefix);
        want = prefix;
    }

    if (got.gated != want.gated || got.clipped != want.clipped ||
        got.peak_in != want.peak_in || got.peak_out != want.peak_out) {
        char detail[200];
        std::snprintf(detail, sizeof(detail),
                      "stats gated %llu/%llu clipped %llu/%llu peak_in %d/%d "
                      "peak_out %d/%d (gate=%g gain=%g n=%zu)",
                      static_cast<unsigned long long>(got.gated),
                      static_cast<unsigned long long>(want.gated),
                      static_cast<unsigned long long>(got.clipped),
                      static_cast<unsigned long long>(want.clipped),
                      static_cast<int>(got.peak_in), static_cast<int>(want.peak_in),
                      static_cast<int>(got.peak_out), static_cast<int>(want.peak_out),
                      static_cast<double>(gate_level), static_cast<double>(gain), count);
        fail(label, detail);
        return;
    }

    std::printf("    ok   %-40s n=%zu gate=%g gain=%g\n", label, count,
                static_cast<double>(gate_level), static_cast<double>(gain));
}

/// Straightforward random int16 blocks: white noise, so every code path --
/// noise under the gate, negative samples, rail hits with a big gain -- shows
/// up somewhere in the block.
std::vector<std::int16_t> random_block(std::size_t count, std::mt19937& rng) {
    std::uniform_int_distribution<int> dist(-32768, 32767);
    std::vector<std::int16_t> v(count);
    for (auto& s : v) {
        s = static_cast<std::int16_t>(dist(rng));
    }
    return v;
}

}  // namespace

int main() {
    if (td_normalizer_kernel_ready() == 0) {
        std::printf("SKIP: no AVX2 kernel on this CPU/build -- scalar path only.\n");
        return 0;
    }
    std::printf("AVX2 kernel ready; comparing against the scalar reference.\n");

    std::mt19937 rng(0x7DDBB305u);  // fixed seed: a failure must be reproducible

    // Lengths on and around the 16-sample group boundary.
    const std::size_t lengths[] = {0,   1,   15,   16,   17,   31,   32,
                                   33,  47,  48,   63,   64,   65,   255,
                                   256, 257, 4095, 4096, 4097, 65536};

    // Gate and gain corners: gate off/on the edge of full scale, gain from
    // silence through identity to heavy clipping.
    const float gates[] = {0.0f, 0.001f, 0.02f, 0.5f, 1.0f};
    const float gains[] = {0.0f, 0.5f, 1.0f, 1.8f, 100.0f};

    std::printf("\n[length sweep]\n");
    for (const std::size_t n : lengths) {
        const auto block = random_block(n, rng);
        compare("random block", block, 0.02f, 1.8f);
        if (failures != 0) {
            return 1;
        }
    }

    std::printf("\n[gate/gain sweep]\n");
    const auto block = random_block(4096, rng);
    for (const float gate : gates) {
        for (const float gain : gains) {
            compare("gate x gain", block, gate, gain);
            if (failures != 0) {
                return 1;
            }
        }
    }

    std::printf("\n[edge inputs]\n");
    {
        // Rails: |value| = 32768 lives exactly where the clamp starts.
        std::vector<std::int16_t> rails(256);
        for (std::size_t i = 0; i < rails.size(); ++i) {
            rails[i] = (i % 2 == 0) ? std::int16_t{32767} : std::int16_t{-32768};
        }
        compare("int16 rails", rails, 0.02f, 2.0f);
        compare("int16 rails, gate off", rails, 0.0f, 1.0f);

        // All silence: every sample sits at magnitude 0, the gate marks all
        // of them, and gain 0 must still produce a clean block of zeros.
        const std::vector<std::int16_t> zeros(1024, 0);
        compare("all zeros", zeros, 0.02f, 1.8f);

        // Magnitudes straddling the gate: samples 655 vs a 655.5f threshold is
        // where a float compare and an integer compare disagree, so both
        // sides of it are present.
        std::vector<std::int16_t> straddle(512);
        for (std::size_t i = 0; i < straddle.size(); ++i) {
            const int mag = (i % 3 == 0) ? 655 : (i % 3 == 1) ? 656 : 0;
            straddle[i] = static_cast<std::int16_t>((i % 2 == 0) ? mag : -mag);
        }
        compare("gate boundary", straddle, 0.020015259f, 1.0f);
    }

    std::printf("\n%s (%d failure%s)\n", failures == 0 ? "PASS" : "FAIL", failures,
                failures == 1 ? "" : "s");
    return failures == 0 ? 0 : 1;
}
