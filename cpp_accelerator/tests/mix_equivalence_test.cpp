// ===========================================================================
//  tests/mix_equivalence_test.cpp -- the AVX2 mix kernels must be boring
//
//  Same contract as tests/kernel_equivalence_test.cpp, applied to the three
//  timeline-mixer entry points. Nothing here checks that AVX2 is fast; it
//  checks that the assembly does exactly what the three scalar loops in
//  stitcher/src/main.rs do, sample for sample.
//
//  The modulo removal is the whole point of td_stitcher_lay, and a modulo bug
//  is invisible in a spot check: it only shows up when the background wraps,
//  and a wrong wrap still produces plausible-sounding audio. So lengths around
//  the 8-sample vector boundary are paired with background lengths that wrap at
//  every awkward offset relative to them.
//
//  GUARD BANDS
//  -----------
//  Every payload is flanked by canary floats inside a std::vector. A kernel
//  that writes one element too far usually does NOT fault: it quietly
//  tramples the heap and the process dies later, somewhere unrelated, with
//  STATUS_HEAP_CORRUPTION and a useless stack. The canaries turn that into an
//  immediate, located failure. Static buffers are not enough for the same
//  reason -- they absorb the overflow instead of trapping it.
//
//  `base()` hands back the already-offset pointer and `store_base()` hands
//  back the allocation, so the two can never be confused: an earlier version of
//  this file added the guard offset a second time and walked 64 floats off the
//  end of the heap, which is exactly the bug this harness exists to catch.
//
//  Comparison is byte-exact on the float bit patterns, not "close": these are
//  pure multiply/add passes with no reduction, so any difference is a bug.
//
//  Run:  mix_equivalence_test          (exit 0 = identical, 1 = drift)
//  Exits 0 with a SKIP line when the build or the CPU has no AVX2 kernels,
//  because "no kernel" is a supported configuration, not a failure.
// ===========================================================================

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <random>
#include <vector>

#include "stitcher_mix.h"

namespace {

constexpr std::size_t kGuard = 64;
constexpr float kCanary = -777.0f;

int failures = 0;
std::mt19937 rng(0xC0FFEEu);

void fail(const char* label, const char* detail) {
    std::printf("    FAIL %-42s %s\n", label, detail);
    ++failures;
}

/// Allocate a payload of `payload` floats flanked by kGuard canary floats.
///
/// Returns the vector; the payload starts at index kGuard, so the buffers are
/// addressed as mem[kGuard + i] and the canaries are mem[0..kGuard) and
/// mem[kGuard+payload .. ].
///
/// This started as a small helper CLASS with a base() accessor, and that
/// accessor is what broke: intact() read a data pointer of 7 instead of the
/// allocation and faulted on address 7, so the check that exists to report an
/// overrun faulted instead. One free function, one indexing convention, no
/// accessors to disagree with each other.
std::vector<float> guarded(std::size_t payload) {
    return std::vector<float>(payload + 2 * kGuard, kCanary);
}

/// Verify both canary bands of a buffer from guarded().
bool guards_intact(const std::vector<float>& mem, std::size_t payload,
                   const char* who) {
    for (std::size_t g = 0; g < kGuard; ++g) {
        if (mem[g] != kCanary) {
            char d[140];
            std::snprintf(d, sizeof(d), "%s: LOW canary at -%zu float(s) = %.1f",
                          who, g + 1, static_cast<double>(mem[g]));
            fail("canary", d);
            return false;
        }
        if (mem[kGuard + payload + g] != kCanary) {
            char d[140];
            std::snprintf(d, sizeof(d), "%s: HIGH canary at +%zu float(s) = %.1f",
                          who, g + 1, static_cast<double>(mem[kGuard + payload + g]));
            fail("canary", d);
            return false;
        }
    }
    return true;
}

// --- oracles ---------------------------------------------------------------
//
// Independent copies of the Rust loops. Kept here rather than shared with the
// caller on purpose: an oracle shared with the code under test can drift
// alongside it and agree with itself while both are wrong.

void reference_lay(float* out, const float* bg, std::size_t bg_len,
                   float bg_vol, std::size_t total) {
    for (std::size_t i = 0; i < total; ++i) {
        out[i] = (bg_len == 0) ? 0.0f : bg[i % bg_len] * bg_vol;
    }
}

void reference_duck(float* out, const TdMixSpan* spans, std::size_t span_count,
                    float duck_gain) {
    for (std::size_t s = 0; s < span_count; ++s) {
        const std::size_t start = static_cast<std::size_t>(spans[s].start);
        const std::size_t end   = static_cast<std::size_t>(spans[s].end);
        for (std::size_t i = start; i < end; ++i) {
            out[i] *= duck_gain;
        }
    }
}

void reference_add_voice(float* out, const float* voice, std::size_t count,
                         std::size_t dest) {
    for (std::size_t i = 0; i < count; ++i) {
        out[dest + i] += voice[i];
    }
}

// --- helpers ---------------------------------------------------------------

std::vector<float> rnd(std::size_t count, float lo, float hi) {
    std::uniform_real_distribution<float> dist(lo, hi);
    std::vector<float> v(count);
    for (float& x : v) {
        x = dist(rng);
    }
    return v;
}

/// Byte-exact float comparison; names the first sample that differs.
bool same(const float* want, const float* got, std::size_t count, const char* what,
          const char* label, std::size_t a, std::size_t b, float vol) {
    for (std::size_t i = 0; i < count; ++i) {
        // memcmp, not ==: -0.0f vs 0.0f and NaN payloads compare equal under ==
        // and both would hide drift.
        if (std::memcmp(&want[i], &got[i], sizeof(float)) != 0) {
            unsigned wbits = 0, gbits = 0;
            std::memcpy(&wbits, &want[i], sizeof(wbits));
            std::memcpy(&gbits, &got[i], sizeof(gbits));
            char d[220];
            std::snprintf(d, sizeof(d),
                          "%s sample %zu: ref %.9g (0x%08x) vs kernel %.9g (0x%08x) "
                          "[a=%zu b=%zu v=%g]",
                          what, i, static_cast<double>(want[i]), wbits,
                          static_cast<double>(got[i]), gbits, a, b,
                          static_cast<double>(vol));
            fail(label, d);
            return false;
        }
    }
    return true;
}

// --- the three checks ------------------------------------------------------

void check_lay(const char* label, std::size_t total, std::size_t bg_len, float vol) {
    const std::vector<float> bg = rnd(bg_len, -2.0f, 2.0f);
    const std::vector<float> seed = rnd(total, -1.0f, 1.0f);

    std::vector<float> want = guarded(total);
    std::vector<float> got = guarded(total);
    for (std::size_t i = 0; i < total; ++i) {
        want[kGuard + i] = seed[i];
        got[kGuard + i] = seed[i];
    }

    reference_lay(want.data() + kGuard, bg_len ? bg.data() : nullptr, bg_len, vol, total);

    TdStitcherLayParams params{};
    params.out    = got.data() + kGuard;
    params.bg     = bg_len ? bg.data() : nullptr;
    params.bg_len = static_cast<std::uint64_t>(bg_len);
    params.total  = static_cast<std::uint64_t>(total);
    params.bg_vol = vol;
    td_stitcher_lay(&params);

    if (guards_intact(want, total, label) && guards_intact(got, total, label)
        && same(want.data() + kGuard, got.data() + kGuard, total, "lay", label, total, bg_len, vol)) {
        std::printf("    ok   %-42s total=%zu bg_len=%zu vol=%g\n", label, total,
                    bg_len, static_cast<double>(vol));
    }
}

void check_duck(const char* label, std::size_t total,
                const std::vector<TdMixSpan>& spans, float duck_gain) {
    const std::vector<float> seed = rnd(total, -1.0f, 1.0f);

    std::vector<float> want = guarded(total);
    std::vector<float> got = guarded(total);
    for (std::size_t i = 0; i < total; ++i) {
        want[kGuard + i] = seed[i];
        got[kGuard + i] = seed[i];
    }

    reference_duck(want.data() + kGuard, spans.empty() ? nullptr : spans.data(),
                   spans.size(), duck_gain);

    TdStitcherDuckParams params{};
    params.out        = got.data() + kGuard;
    params.duck_gain  = duck_gain;
    params.spans      = spans.empty() ? nullptr : spans.data();
    params.span_count = static_cast<std::uint64_t>(spans.size());
    td_stitcher_duck(&params);

    if (guards_intact(want, total, label) && guards_intact(got, total, label)
        && same(want.data() + kGuard, got.data() + kGuard, total, "duck", label, total, spans.size(),
                duck_gain)) {
        std::printf("    ok   %-42s %zu span(s) gain=%g\n", label, spans.size(),
                    static_cast<double>(duck_gain));
    }
}

void check_add_voice(const char* label, std::size_t total, std::size_t dest,
                     std::size_t count) {
    if (dest + count > total) {
        return;  // the caller must clamp; not a kernel case
    }
    const std::vector<float> voice = rnd(count, -1.5f, 1.5f);
    const std::vector<float> seed = rnd(total, -1.0f, 1.0f);

    std::vector<float> want = guarded(total);
    std::vector<float> got = guarded(total);
    for (std::size_t i = 0; i < total; ++i) {
        want[kGuard + i] = seed[i];
        got[kGuard + i] = seed[i];
    }

    reference_add_voice(want.data() + kGuard, voice.empty() ? nullptr : voice.data(), count, dest);

    TdStitcherAddVoiceParams params{};
    params.out   = got.data() + kGuard;
    params.voice = voice.empty() ? nullptr : voice.data();
    params.count = static_cast<std::uint64_t>(count);
    params.dest  = static_cast<std::uint64_t>(dest);
    td_stitcher_add_voice(&params);

    if (guards_intact(want, total, label) && guards_intact(got, total, label)
        && same(want.data() + kGuard, got.data() + kGuard, total, "voice", label, dest, count, 0.0f)) {
        std::printf("    ok   %-42s dest=%zu count=%zu\n", label, dest, count);
    }
}

}  // namespace

int main() {
    // Unbuffered: a fault inside the assembly would otherwise lose the whole
    // buffered output, and the one thing needed to debug it is the last case
    // that ran.
    std::setvbuf(stdout, nullptr, _IONBF, 0);

    if (td_stitcher_mix_ready() == 0) {
        std::printf("mix_equivalence_test: SKIP (no AVX2 mix kernel in this build/CPU)\n");
        return 0;
    }
    std::printf("mix_equivalence_test: comparing AVX2 mix kernels to scalar reference\n");

    char label[128];

    // --- layout: every vector remainder against every wrap alignment --------
    //
    // total 0..40 covers every remainder modulo 8; bg_len sweeps the same range
    // so the wrap lands at every alignment relative to the vector body. That
    // pairing is what catches a modulo-phase bug.
    for (std::size_t total = 0; total <= 40; ++total) {
        for (std::size_t bg_len = 0; bg_len <= 20; ++bg_len) {
            std::snprintf(label, sizeof(label), "lay total=%zu bg_len=%zu", total, bg_len);
            check_lay(label, total, bg_len, 0.75f);
        }
    }
    // Backgrounds that wrap more than once, and one longer than the timeline.
    for (std::size_t total : {41u, 64u, 100u, 257u, 1000u}) {
        for (std::size_t bg_len : {3u, 7u, 8u, 9u, 17u, 33u, 64u, 257u, 4000u}) {
            std::snprintf(label, sizeof(label), "lay wrap total=%zu bg_len=%zu",
                          total, bg_len);
            check_lay(label, total, bg_len, 1.0f);
        }
    }
    check_lay("lay vol=0", 200, 37, 0.0f);
    check_lay("lay vol=1", 200, 37, 1.0f);
    check_lay("lay vol=-1.25", 200, 37, -1.25f);
    check_lay("lay bg_len=0 (silence)", 200, 0, 2.0f);
    check_lay("lay total=0", 0, 37, 1.0f);
    check_lay("lay total=0 bg=0", 0, 0, 1.0f);

    // --- ducking: every span length, adjacent, overlapping, out of range ----
    check_duck("duck no spans", 128, {}, 0.5f);
    for (std::size_t len = 0; len <= 20; ++len) {
        std::vector<TdMixSpan> spans{{7, 7 + len}};
        std::snprintf(label, sizeof(label), "duck one span len=%zu", len);
        check_duck(label, 256, spans, 0.5f);
    }
    check_duck("duck adjacent", 256, {{0, 8}, {8, 16}, {16, 24}}, 0.5f);
    check_duck("duck overlapping", 256, {{4, 40}, {8, 24}, {0, 3}}, 0.5f);
    check_duck("duck inverted span", 256, {{40, 10}}, 0.5f);
    check_duck("duck empty span at 0", 256, {{0, 0}}, 0.5f);
    // Reaches the last sample exactly. A span reaching PAST the end is a caller
    // error, not a kernel case: this kernel has no length to clamp against, and
    // Rust's range slice would have panicked too. The canary bands caught the
    // difference, which is what they are for.
    check_duck("duck span at end", 256, {{250, 256}}, 0.5f);
    check_duck("duck gain=0", 256, {{3, 200}}, 0.0f);
    check_duck("duck gain=1", 256, {{3, 200}}, 1.0f);
    check_duck("duck gain=4", 256, {{3, 200}}, 4.0f);
    check_duck("duck long span", 4096, {{0, 4096}}, 0.5f);
    check_duck("duck long span offset", 4096, {{13, 4001}}, 0.5f);

    // --- voice overlay: every count and dest alignment ---------------------
    for (std::size_t count = 0; count <= 20; ++count) {
        for (std::size_t dest : {0u, 1u, 7u, 8u, 9u, 15u, 16u}) {
            if (dest + count > 256) {
                continue;
            }
            std::snprintf(label, sizeof(label), "voice dest=%zu count=%zu", dest, count);
            check_add_voice(label, 256, dest, count);
        }
    }
    check_add_voice("voice long", 4096, 100, 3000);

    // Adjacent overlays, which is what a real timeline looks like: eleven
    // segments in a row with gaps between them. `got` starts as a copy of the
    // SAME seed (two rnd() calls would differ), then each overlay is applied to
    // both sides.
    {
        const std::vector<float> seed = rnd(4096, -1.0f, 1.0f);
        std::vector<float> want = guarded(4096);
        std::vector<float> got = guarded(4096);
        for (std::size_t i = 0; i < 4096; ++i) {
            want[kGuard + i] = seed[i];
            got[kGuard + i] = seed[i];
        }

        std::size_t at = 0;
        for (int seg = 0; seg < 11; ++seg) {
            const std::size_t len = 90 + static_cast<std::size_t>(seg) * 13;
            const std::vector<float> voice = rnd(len, -1.5f, 1.5f);
            reference_add_voice(want.data() + kGuard, voice.data(), len, at);
            TdStitcherAddVoiceParams params{};
            params.out   = got.data() + kGuard;
            params.voice = voice.data();
            params.count = static_cast<std::uint64_t>(len);
            params.dest  = static_cast<std::uint64_t>(at);
            td_stitcher_add_voice(&params);
            at += len + 137;
        }
        if (guards_intact(want, 4096, "voice 11 adjacent") && guards_intact(got, 4096, "voice 11 adjacent")
            && same(want.data() + kGuard, got.data() + kGuard, 4096, "voice", "voice 11 adjacent",
                    11, 0, 0.0f)) {
            std::printf("    ok   %-42s\n", "voice 11 adjacent segments");
        }
    }

    if (failures == 0) {
        std::printf("mix_equivalence_test: PASS (kernels identical to scalar reference)\n");
        return 0;
    }
    std::printf("mix_equivalence_test: FAIL (%d difference(s))\n", failures);
    return 1;
}