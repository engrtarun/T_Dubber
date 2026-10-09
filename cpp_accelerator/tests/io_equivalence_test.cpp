// ===========================================================================
//  tests/io_equivalence_test.cpp -- the AVX2 quantise kernel must be boring
//
//  Nothing here checks that AVX2 is fast -- the benchmark at the bottom reports
//  that separately and no assertion depends on it, because a machine that is
//  merely slow is not a broken kernel. What IS checked is that the assembly
//  does exactly what the write loop in stitcher/src/main.rs does, sample for
//  sample.
//
//  BIT-EXACT, AND WHY THAT IS A REAL BAR HERE
//  -------------------------------------------
//  The kernel is a pure clamp/multiply/round pass with no reduction, so every
//  lane is independent and any difference at all is a bug. Two classes of bug
//  are specifically hunted:
//
//    * round-half-AWAY-from-zero, which is what Rust's f32::round() does.
//      vroundps cannot express it. The obvious replacement,
//      trunc(|x| + 0.5) in f32, is WRONG, and not by a rounding error: an f32
//      near 16384 has a ulp of 2^-10, so |x| + 0.5 can round up onto the next
//      integer. The rounding sweep below steps the input across [0,1] in
//      exactly the increments that put x*32767 on and around every
//      half-integer, which is where that bug showed up. A regression to
//      vroundps-with-nearest-even shows here as thousands of half-LSB
//      differences; an ordinary random sweep would likely miss it.
//    * lane and register lifetime: a scratch register that overlaps a constant,
//      or a conversion destination that overwrites its own source, produces
//      errors confined to particular lanes or particular counts. That is why
//      every tail length from 0 to 33 is checked, and why the sweep is dense.
//
//  GUARD BANDS
//  -----------
//  Every payload is flanked by canary values inside a std::vector. A kernel
//  that writes one element too far usually does NOT fault: it quietly tramples
//  the heap and the process dies later, somewhere unrelated, with a useless
//  stack. The canaries turn that into an immediate, located failure -- which is
//  the entire reason the vector/tail split is bounded by `count AND -8` rather
//  than by `count - 7`.
//
//  Comparison is exact: the output is integer, so there is no tolerance to
//  loosen and none to hide behind.
//
//  Run:  io_equivalence_test          (exit 0 = identical, 1 = drift)
//  Exits 0 with a SKIP line when the build or the CPU has no AVX2 kernels,
//  because "no kernel" is a supported configuration, not a failure.
// ===========================================================================

#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>
#include <random>
#include <vector>

#include "stitcher_io.h"
#include "stitcher_mix.h"  // for td_stitcher_mix_ready(), which gates both files

namespace {

constexpr std::size_t kGuard = 64;
constexpr std::int16_t kCanaryI = -31337;

int failures = 0;
std::mt19937 rng(0x1BADB002u);

void fail(const char* label, const char* detail) {
    std::printf("    FAIL %-44s %s\n", label, detail);
    ++failures;
}

void note(const char* label, const char* detail) {
    std::printf("    ok   %-44s %s\n", label, detail);
}

// --- guarded buffer --------------------------------------------------------
//
// One free function, one indexing convention: the payload always starts at
// index kGuard. An earlier version of the sibling test kept these as a class
// with base()/store_base() accessors, and the two could disagree -- which
// produced an overrun check that itself walked off the heap.

std::vector<std::int16_t> guarded(std::size_t payload) {
    return std::vector<std::int16_t>(payload + 2 * kGuard, kCanaryI);
}

bool guards_intact(const std::vector<std::int16_t>& mem, std::size_t payload,
                   const char* who) {
    for (std::size_t g = 0; g < kGuard; ++g) {
        if (mem[g] != kCanaryI) {
            char d[160];
            std::snprintf(d, sizeof(d), "%s: LOW canary at -%zu sample(s) = %d",
                          who, g + 1, static_cast<int>(mem[g]));
            fail("canary", d);
            return false;
        }
        if (mem[kGuard + payload + g] != kCanaryI) {
            char d[160];
            std::snprintf(d, sizeof(d), "%s: HIGH canary at +%zu sample(s) = %d",
                          who, g + 1, static_cast<int>(mem[kGuard + payload + g]));
            fail("canary", d);
            return false;
        }
    }
    return true;
}

// --- oracle ----------------------------------------------------------------
//
// An independent copy of the Rust loop, kept here rather than shared with the
// code under test on purpose: an oracle shared with the code under test can
// drift alongside it and agree with itself while both are wrong.
//
// float_control(precise) is not decoration. Without it a compiler is free to
// contract the multiply into an FMA elsewhere in the build, and the oracle
// would then compute a different -- more accurate -- number from the same
// source the kernel sees. The resulting test failure has nothing to do with the
// assembly and gets "fixed" by loosening the comparison until it proves nothing.

#pragma float_control(precise, on)

void reference_quantize(std::int16_t* dst, const float* src, std::size_t count) {
    for (std::size_t i = 0; i < count; ++i) {
        const float s = src[i];
        const float clamped = s < -1.0f ? -1.0f : (s > 1.0f ? 1.0f : s);
        // `(clamped * 32767).round()`, in THAT order. An earlier version of this
        // oracle rounded first and multiplied afterwards, which is a different
        // function -- and every resulting "failure" pointed at the kernel's
        // rounding path, which is exactly where a wrong oracle sends you.
        const float q = std::round(clamped * 32767.0f);
        // |q| <= 32767 after the clamp, so the truncation Rust's `as i16` does
        // is exact and never has to saturate. NaN is the one case where it does
        // saturate, and Rust saturates it to 0.
        dst[i] = std::isfinite(q) ? static_cast<std::int16_t>(q)
                                  : static_cast<std::int16_t>(0);
    }
}

// --- generators ------------------------------------------------------------

std::vector<float> rnd(std::size_t count, float lo, float hi) {
    std::uniform_real_distribution<float> dist(lo, hi);
    std::vector<float> v(count);
    for (float& x : v) x = dist(rng);
    return v;
}

/// Reproducible rather than random: the whole point of these is to sit on the
/// interesting boundaries, and a random draw walks past them.
std::vector<float> adversarial(std::size_t count) {
    const float edges[] = {
        0.0f, -0.0f, 1.0f, -1.0f,
        0.99999994f, -0.99999994f,          // largest f32 below 1.0
        1.0000001f, -1.0000001f,            // just past the clamp
        1e-30f, -1e-30f, 1e30f, -1e30f,     // denormal-ish and absurd
        1.52587890625e-05f, -1.52587890625e-05f,   // 0.5 / 32767
        2.98023223876953125e-08f, -2.98023223876953125e-08f,  // 2^-25
        3.0517578125e-05f, -3.0517578125e-05f,   // 1.0 / 32767
        0.49999997f, -0.49999997f,           // the f64-vs-f32 rounding boundary
        // NaN and the infinities are here on purpose: they are the inputs the
        // two implementations are most likely to disagree on, and a random draw
        // only reaches NaN by accident. NaN -> 0, +/-inf -> +/-32767.
        std::numeric_limits<float>::quiet_NaN(),
        std::numeric_limits<float>::infinity(),
        -std::numeric_limits<float>::infinity(),
    };
    const std::size_t n = sizeof(edges) / sizeof(edges[0]);
    std::vector<float> v(count);
    for (std::size_t i = 0; i < count; ++i) v[i] = edges[i % n];
    return v;
}

// ===========================================================================

void check(const std::vector<float>& src, const char* shape) {
    const std::size_t n = src.size();
    auto want_mem = guarded(n);
    auto got_mem = guarded(n);
    std::int16_t* want = want_mem.data() + kGuard;
    std::int16_t* got = got_mem.data() + kGuard;
    reference_quantize(want, src.data(), n);

    TdStitcherQuantizeParams p;
    p.src = src.data();
    p.dst = got;
    p.count = n;
    td_stitcher_quantize(&p);

    for (std::size_t i = 0; i < n; ++i) {
        if (got[i] != want[i]) {
            char d[220];
            std::snprintf(d, sizeof(d), "%s: sample %zu (in %.9g) kernel %d rust %d",
                          shape, i, static_cast<double>(src[i]),
                          static_cast<int>(got[i]), static_cast<int>(want[i]));
            fail("quantise value", d);
            return;
        }
    }
    if (!guards_intact(got_mem, n, shape)) return;
    if (!guards_intact(want_mem, n, shape)) return;
    note(shape, "");
}

void every_tail_length() {
    std::printf("\n== every tail length, and both vector boundaries ==\n");
    // 0..33 covers every possible scalar tail (count % 8) at least four times,
    // and the boundaries 7/8 and 15/16 where an off-by-one in the loop bound
    // would either skip a sample or overrun.
    for (std::size_t n = 0; n <= 33; ++n) {
        char shape[64];
        std::snprintf(shape, sizeof(shape), "count=%zu", n);
        check(adversarial(n), shape);
    }
    for (std::size_t n : {64u, 1000u, 4097u, 65537u, 1u << 20}) {
        char shape[64];
        std::snprintf(shape, sizeof(shape), "count=%zu random", n);
        check(rnd(n, -1.0f, 1.0f), shape);
    }
}

void rounding_sweep() {
    std::printf("\n== the rounding region, densely ==\n");
    // k/65536 for k in [0, 65536] steps the input across [0,1] in exactly the
    // increments where x*32767 lands on and around every half-integer. This is
    // the sweep that caught trunc(|x| + 0.5) rounding up onto the next integer
    // in f32.
    constexpr std::size_t kSteps = 1 << 16;
    std::vector<float> sweep(kSteps + 1);
    for (std::size_t k = 0; k <= kSteps; ++k) {
        sweep[k] = static_cast<float>(k) / 65536.0f;
    }
    check(sweep, "sweep [0,1] over 65537 steps");

    std::vector<float> neg(sweep.size());
    for (std::size_t k = 0; k <= kSteps; ++k) neg[k] = -sweep[k];
    check(neg, "sweep [-1,0] over 65537 steps");

    std::vector<float> wide(kSteps + 1);
    for (std::size_t k = 0; k <= kSteps; ++k) {
        // near the clamp, where the f32 ulp is smallest in relative terms
        wide[k] = 1.0f - static_cast<float>(k) * 1e-7f;
    }
    check(wide, "sweep just below 1.0");

    std::vector<float> wild(4096);
    for (std::size_t i = 0; i < wild.size(); ++i) {
        wild[i] = (i & 1) ? 3.0f + static_cast<float>(i)
                          : -3.0f - static_cast<float>(i);
    }
    check(wild, "far outside [-1,1], clamp dominates");

    std::vector<float> nulls(64, 0.0f);
    nulls[17] = -0.0f;
    check(nulls, "signed zero");
}

void null_is_a_noop() {
    std::printf("\n== null pointers are a no-op, not a fault ==\n");
    // A Rust caller whose output file failed to open very plausibly holds a
    // null handle, and faulting there would mask the error that caused it.
    TdStitcherQuantizeParams p;
    p.src = nullptr;
    p.dst = nullptr;
    p.count = 4096;
    td_stitcher_quantize(&p);
    note("null src+dst", "returned");

    p.src = reinterpret_cast<const float*>(0x1000);
    p.dst = nullptr;
    p.count = 4096;
    td_stitcher_quantize(&p);
    note("null dst", "returned");

    auto mem = guarded(0);
    p.src = reinterpret_cast<const float*>(0x1000);
    p.dst = mem.data() + kGuard;
    p.count = 0;
    td_stitcher_quantize(&p);
    note("zero count", "returned");
}

// ===========================================================================
//  Benchmark
// ===========================================================================
//
// No assertion depends on any of this. The numbers exist so the decision to
// keep this kernel is made on a measurement rather than on theory -- and so the
// kernel that was REMOVED can be argued about with a number instead of a hunch.

using Clock = std::chrono::steady_clock;
using Tick = Clock::duration::rep;

/// Elapsed seconds between two clock readings, plus the raw tick delta so the
/// measurement can be checked instead of believed.
///
/// The obvious `duration<double>(Clock::now() - t0).count()` reported 9470
/// seconds for a loop the entire test binary finishes in three. Rewriting it by
/// hand from `period` produced 1587315 seconds for the same loop, which is
/// worse, not better: two different conversions of the same clock, disagreeing
/// with each other and with the enclosing wall clock by six orders of
/// magnitude. Neither number is a measurement. So this prints the raw delta
/// alongside the converted one, and the honest output is whichever one agrees
/// with the enclosing wall time -- which is why bench_quantize() brackets its
/// own numbers with that total.
struct Elapsed {
    Tick ticks = 0;
    double seconds = 0.0;
};

Elapsed measure(Clock::time_point t0) {
    const Clock::time_point t1 = Clock::now();
    Elapsed e;
    e.ticks = (t1 - t0).count();
    e.seconds = std::chrono::duration_cast<std::chrono::duration<double, std::milli>>(
                    t1 - t0).count() / 1000.0;
    return e;
}
void bench_quantize() {
    std::printf("\n== benchmark: 24 kHz mono mix -> int16 ==\n");
    constexpr std::size_t kSamples = 14'400'000;  // 10 min at 24 kHz
    auto src_mem = std::vector<float>(kSamples + 2 * kGuard, 0.0f);
    auto dst_mem = guarded(kSamples);
    const float* src = src_mem.data() + kGuard;
    std::int16_t* dst = dst_mem.data() + kGuard;
    for (std::size_t i = 0; i < kSamples; ++i) {
        src_mem[kGuard + i] =
            static_cast<float>(std::sin(static_cast<double>(i) * 0.003)) * 0.7f;
    }

    TdStitcherQuantizeParams p;
    p.src = src;
    p.dst = dst;
    p.count = kSamples;

    // Three passes of each, best-of. A single timed run on a shared box is a
    // anecdote; the best-of-three is the one number that can be compared with
    // the next run's best-of-three.
    const Clock::time_point wall0 = Clock::now();
    double k_best = 1e30, s_best = 1e30;
    Tick k_ticks = 0, s_ticks = 0;
    for (int rep = 0; rep < 3; ++rep) {
        const Clock::time_point a = Clock::now();
        td_stitcher_quantize(&p);
        const Elapsed k = measure(a);
        if (k.seconds < k_best) { k_best = k.seconds; k_ticks = k.ticks; }

        const Clock::time_point b = Clock::now();
        reference_quantize(dst, src, kSamples);
        const Elapsed s = measure(b);
        if (s.seconds < s_best) { s_best = s.seconds; s_ticks = s.ticks; }
    }
    const Elapsed wall = measure(wall0);
    std::printf("    clock: %lld ticks/s   3 reps of each inside %.3f s\n",
                static_cast<long long>(Clock::period::den), wall.seconds);
    std::printf("    best kernel %lld ticks, best scalar %lld ticks\n",
                static_cast<long long>(k_ticks), static_cast<long long>(s_ticks));

    std::printf("    kernel  %7.3f s  (%7.1f Msample/s)\n", k_best,
                kSamples / k_best / 1e6);
    std::printf("    scalar  %7.3f s  (%7.1f Msample/s)\n", s_best,
                kSamples / s_best / 1e6);
    std::printf("    speedup %6.2fx   projected 45 min: %.2f s vs %.2f s\n",
                s_best / k_best, k_best * 4.5, s_best * 4.5);
}

}  // namespace

int main() {
    std::printf("io_equivalence_test: AVX2 WAV write kernel\n");

    // stitcher_io.h deliberately has no probe of its own. This one function
    // answers for both files, and a build with no NASM reports 0 here rather
    // than leaving the test to call into missing symbols.
    if (td_stitcher_mix_ready() == 0) {
        std::printf("SKIP: no AVX2 kernels in this build/CPU "
                    "(stitcher_io shares stitcher_mix's probe by design)\n");
        return 0;
    }

    every_tail_length();
    rounding_sweep();
    null_is_a_noop();
    bench_quantize();

    std::printf("\n==============================================================\n");
    if (failures == 0) {
        std::printf("io_equivalence_test: PASS "
                    "(kernel bit-identical to the Rust write loop)\n");
        return 0;
    }
    std::printf("io_equivalence_test: FAIL (%d)\n", failures);
    return 1;
}