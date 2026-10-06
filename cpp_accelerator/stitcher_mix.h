// ===========================================================================
//  stitcher_mix.h -- AVX2 timeline mixer for the stitcher
//
//  Three entry points, each replacing one hand-written scalar loop in
//  stitcher/src/main.rs. See kernels/stitcher_mix.asm for why the loops are
//  worth rewriting and why they are three rather than one fused pass.
//
//  The header exists for the same reason normalizer_kernel.h does: the struct
//  layouts and the symbol names are declared ONCE, so the assembly, this
//  header and the equivalence test cannot disagree about them. They can, and
//  a mismatch is a wrong sample in a shipped video rather than a build error.
//
//      nasm -f elf64 kernels/stitcher_mix.asm   # Kaggle / Linux
//      nasm -f win64 kernels/stitcher_mix.asm   # Windows / MSVC
// ===========================================================================

#ifndef TDUBBER_STITCHER_MIX_H
#define TDUBBER_STITCHER_MIX_H

#include <cstddef>
#include <cstdint>

extern "C" {

/// A half-open sample range of the timeline that gets ducked.
/// Overlapping spans are allowed here: each is scaled independently, so an
/// overlap simply scales twice, which is what the Rust range loop does too.
struct TdMixSpan {
    std::uint64_t start = 0;
    std::uint64_t end   = 0;
};

// --- td_stitcher_lay -------------------------------------------------------

/// Loop `bg` into `out` for `total` samples, scaling by `bg_vol`.
///
/// Replaces the Rust `for (i, s) in mix.iter_mut() { *s = bg[i % bg.len()] * bg_vol }`
/// loop. The kernel's reason for existing is that modulo: the Rust pays one
/// 64-bit integer division per sample, which at 24 kHz is 2.57M divisions for
/// a 107 s trailer and 172.8M for a two-hour feature film. The kernel copies
/// `min(bg samples left, timeline samples left)` per run and resets the read
/// pointer, so no division is executed at all.
///
/// `bg` may be null and/or `bg_len` may be zero, in which case `total` samples
/// of silence are written. A short buffer would be a corrupt WAV, so the
/// kernel always fills the whole timeline.
/// Field order follows what the struct layout actually produces, not what reads
/// nicely: `total` is a u64, so it has to sit on 8-byte alignment and `bg_vol`
/// therefore ends up after it. The static_asserts below are what caught that
/// on the first build.
struct TdStitcherLayParams {
    float*          out    = nullptr;  ///< +0  f32 timeline, `total` samples
    const float*    bg     = nullptr;  ///< +8  f32 background, may be null
    std::uint64_t   bg_len = 0;        ///< +16 background samples
    std::uint64_t   total  = 0;        ///< +24 samples to produce
    float           bg_vol = 1.0f;      ///< +32 linear gain on the background
};

static_assert(offsetof(TdStitcherLayParams, out) == 0, "asm reads out at +0");
static_assert(offsetof(TdStitcherLayParams, bg) == 8, "asm reads bg at +8");
static_assert(offsetof(TdStitcherLayParams, bg_len) == 16, "asm reads bg_len at +16");
static_assert(offsetof(TdStitcherLayParams, total) == 24, "asm reads total at +24");
static_assert(offsetof(TdStitcherLayParams, bg_vol) == 32, "asm reads bg_vol at +32");
static_assert(sizeof(TdStitcherLayParams) == 40, "asm assumes a 40-byte block");

void td_stitcher_lay(const TdStitcherLayParams* p);

// --- td_stitcher_duck ------------------------------------------------------

/// Scale `out[start..end]` by `duck_gain` for every span, in place.
///
/// Replaces the Rust `for sample in &mut mix[start..end] { *sample *= DUCK_FACTOR }`.
/// An empty span (`end <= start`) is skipped rather than treated as an error:
/// the Rust range slice would panic on `start > end`, and one malformed span
/// should not be able to take down a finished dub.
///
/// PRECONDITION, and it is the same one the Rust already relied on: every span
/// must lie inside the timeline. The kernel has no idea how long the timeline
/// is -- the struct carries no length -- so a span reaching past the end writes
/// past the buffer. tests/mix_equivalence_test.cpp found exactly that with its
/// canary bands (a {250,300} span on a 256-sample buffer scaled 44 samples
/// past the end). Rust's `&mut mix[start..end]` would have panicked there, so
/// this is not a new requirement; it is the one that was previously only
/// implicit. A caller that cannot guarantee it should clamp before calling.
struct TdStitcherDuckParams {
    float*            out        = nullptr;  ///< +0  f32 timeline, modified
    float             duck_gain  = 1.0f;    ///< +8  multiplier inside a span
    const TdMixSpan*  spans      = nullptr;  ///< +16 span list
    std::uint64_t     span_count = 0;        ///< +24 spans
};

static_assert(sizeof(TdMixSpan) == 16, "asm strides spans by 16");
static_assert(offsetof(TdStitcherDuckParams, duck_gain) == 8, "asm reads duck_gain at +8");
static_assert(offsetof(TdStitcherDuckParams, spans) == 16, "asm reads spans at +16");
static_assert(offsetof(TdStitcherDuckParams, span_count) == 24, "asm reads span_count at +24");
static_assert(sizeof(TdStitcherDuckParams) == 32, "asm assumes a 32-byte block");

void td_stitcher_duck(const TdStitcherDuckParams* p);

// --- td_stitcher_add_voice -------------------------------------------------

/// `out[dest + i] += voice[i]` for `i` in `0..count`, in place.
///
/// Replaces the Rust `for (i, &v) in voice.iter().take(take) { mix[off+i] += v }`.
///
/// The caller must already have clamped `count` to what fits inside the
/// timeline. The kernel deliberately does NOT clamp: it trusts the count it
/// was given and writes exactly that many samples, so a wrong `count` writes
/// past `dest + count` rather than being quietly corrected. Silently
/// shortening an overlay would drop the end of a spoken line.
struct TdStitcherAddVoiceParams {
    float*          out   = nullptr;  ///< +0  f32 timeline, modified
    const float*    voice = nullptr;  ///< +8  f32 segment
    std::uint64_t   count = 0;        ///< +16 samples to add
    std::uint64_t   dest  = 0;        ///< +24 sample offset in the timeline
};

static_assert(offsetof(TdStitcherAddVoiceParams, voice) == 8, "asm reads voice at +8");
static_assert(offsetof(TdStitcherAddVoiceParams, count) == 16, "asm reads count at +16");
static_assert(offsetof(TdStitcherAddVoiceParams, dest) == 24, "asm reads dest at +24");
static_assert(sizeof(TdStitcherAddVoiceParams) == 32, "asm assumes a 32-byte block");

void td_stitcher_add_voice(const TdStitcherAddVoiceParams* p);

// --- runtime gate ----------------------------------------------------------

/// 1 when the mix kernels are both compiled in and runnable on this CPU.
///
/// Same CPUID/XGETBV probe as the normalizer kernel (OSXSAVE + AVX + POPCNT,
/// then the OS-enabled XMM/YMM state, then AVX2), because it is the same
/// hardware question and answering it twice with two implementations is how
/// the two kernels end up disagreeing. Always 0 in a build without NASM, so
/// the caller falls back to its scalar loops with no #ifdef of its own.
int td_stitcher_mix_ready(void);

}  // extern "C"

#endif  // TDUBBER_STITCHER_MIX_H