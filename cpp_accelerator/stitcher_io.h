// ===========================================================================
//  stitcher_io.h -- AVX2 WAV write loop for the stitcher
//
//  One entry point, replacing the per-sample write loop in
//  stitcher/src/main.rs. See kernels/stitcher_io.asm for why the arithmetic is
//  bit-identical to the Rust, why the rounding bias has to happen in f64, and
//  -- most usefully -- why the WAV channel downmix that used to live here is
//  gone.
//
//      nasm -f elf64 kernels/stitcher_io.asm   # Kaggle / Linux
//      nasm -f win64 kernels/stitcher_io.asm   # Windows / MSVC
//
//  The symbol lives in the same static library as stitcher_mix.h and shares its
//  runtime gate: call td_stitcher_mix_ready() once and the answer covers both
//  files. There is deliberately no td_stitcher_io_ready().
// ===========================================================================

#ifndef TDUBBER_STITCHER_IO_H
#define TDUBBER_STITCHER_IO_H

#include <cstddef>
#include <cstdint>

extern "C" {

// --- td_stitcher_quantize --------------------------------------------------

/// `dst[i] = (src[i].clamp(-1,1) * 32767).round() as i16` for i in 0..count.
///
/// Replaces the Rust write loop:
///
/// ```text
/// for &s in &mix {
///     let q = (s.clamp(-1.0, 1.0) * 32767.0).round() as i16;
///     writer.write_sample(q)?
/// }
/// ```
///
/// Bit-identical, including the round-half-away-from-zero. The obvious way to
/// express that round -- trunc(|x| + 0.5) -- is NOT exact in f32 here: an f32
/// near 16384 has a ulp of 2^-10, so |x| + 0.5 can round up onto the next
/// integer and lose a whole LSB. The kernel applies the bias in f64 instead,
/// where the widening is exact and the add cannot round. The clamp is not
/// optional and is not merely tidy: it is what bounds |x| to 32767, which is
/// the precondition the f64 argument needs.
///
/// A null `src` or `dst` is a no-op rather than a fault. hound's write loop
/// cannot produce one, but a Rust caller that has already failed to open its
/// output file very plausibly has a null handle, and a segfault there would
/// mask the error that actually caused it.
///
/// NaN becomes 0, matching Rust's `NaN as i16`. The kernel clears NaN lanes
/// before the clamp on purpose: x86 vmaxps/vminps return the second operand for
/// NaN, so without it a NaN sample would clamp to -1.0 and emit -32767 -- a
/// full-scale click out of an input that should be silence.
struct TdStitcherQuantizeParams {
    const float*    src   = nullptr;  ///< +0  f32 samples, `count` of them
    std::int16_t*   dst   = nullptr;  ///< +8  i16 output, `count` samples
    std::uint64_t   count = 0;        ///< +16 samples to convert
};

static_assert(offsetof(TdStitcherQuantizeParams, src) == 0, "asm reads src at +0");
static_assert(offsetof(TdStitcherQuantizeParams, dst) == 8, "asm reads dst at +8");
static_assert(offsetof(TdStitcherQuantizeParams, count) == 16, "asm reads count at +16");
static_assert(sizeof(TdStitcherQuantizeParams) == 24, "asm assumes a 24-byte block");

void td_stitcher_quantize(const TdStitcherQuantizeParams* p);

// --- what used to be here, and why it is not ------------------------------
//
// A `td_stitcher_downmix` (interleaved multichannel f32 -> mono f32) used to be
// declared here. It is gone, and the reason is worth keeping because it is the
// shape of mistake this repo makes easily: assuming a scalar loop is slow
// because you are reading it as assembly.
//
// The downmix is `acc += src[f*channels+c]` over a runtime trip count -- a
// textbook 4-cycle accumulator chain, and every stereo source pays it. It also
// got two hand-written AVX2 deinterleaves, both wrong in ways that still summed
// to plausible audio: vunpcklps interleaves the LOW halves of its sources and
// vshufps cannot cross a 128-bit lane, so the textbook shuffle tree leaves the
// two channels adjacent rather than separated.
//
// Then the benchmark answered the question the code review should have:
//
//     scalar C++ loop, MSVC, auto-vectorised .... 966 Msample/s
//     hand-written AVX2 vpermd deinterleave ....   16 Msample/s
//
// Sixty times slower than the loop it replaced. The scalar fold is a two-way
// add chain that any vectorising compiler already handles; the deinterleave
// only looks necessary when you imagine the un-optimised scalar form, which is
// what the assembly in front of you is not. A kernel that loses to `-O2` by 60x
// is not an unclaimed optimisation, it is two shipped bugs with no speedup to
// show for them.
//
// If quad or 5.1 input ever has to be fast, the honest starting point is a
// benchmark of the plain loop, not a shuffle tree.

// --- runtime gate ----------------------------------------------------------

/// td_stitcher_mix_ready() in stitcher_mix.h answers for BOTH files -- same
/// CPUID/XGETBV probe, same cached answer. This kernel adds no second probe on
/// purpose: two implementations of "can this CPU run the fast path" is how one
/// of them ends up saying yes while the other says no, and the symptom is a
/// fault on a farm node rather than a clean scalar fallback.

}  // extern "C"

#endif  // TDUBBER_STITCHER_IO_H