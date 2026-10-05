// ===========================================================================
//  normalizer_kernel.h -- optional AVX2 gate/gain kernel for the normalizer
//
//  normalizer.cpp keeps one scalar reference loop (process_block) that is
//  always correct and always present. When the build has the NASM kernel and
//  the CPU can run it, whole 16-sample groups are handed to assembly instead
//  and the tail is still finished by that same scalar loop.
//
//  The header exists so the struct layout below and the symbol name are
//  declared ONCE: normalizer.cpp, the equivalence test and the hand-written
//  NASM file all have to agree on them, and a mismatch is a memory bug, not
//  a compile error.
//
//      nasm -f elf64 kernels/normalizer_gate_gain.asm   # Kaggle / Linux
//      nasm -f win64 kernels/normalizer_gate_gain.asm   # Windows / MSVC
//
//  TD_NORMALIZER_HAVE_ASM is defined by CMake only when that object was
//  actually assembled and linked in. Without it every declaration below
//  collapses to the scalar path and nothing changes versus the original
//  tool.
// ===========================================================================

#ifndef TDUBBBER_NORMALIZER_KERNEL_H
#define TDUBBBER_NORMALIZER_KERNEL_H

#include <cstddef>
#include <cstdint>

// The kernel is x86-64 only, and the ABI in the .asm file is the 64-bit one.
#if !defined(__x86_64__) && !defined(_M_X64)
#  if defined(TD_NORMALIZER_HAVE_ASM)
#    undef TD_NORMALIZER_HAVE_ASM
#  endif
#endif

extern "C" {

/// Per-block totals produced by the kernel. Mirrors td_norm_kernel_stats in
/// kernels/normalizer_gate_gain.asm byte for byte (24 bytes, 8-byte
/// alignment): two u64 counters, then two i32 peaks.
///
/// The kernel never merges into the caller's long-running Stats -- it reports
/// what THIS block did and normalizer.cpp adds/maxes it in, exactly like the
/// scalar path does with its locals.
struct TdNormKernelStats {
    std::uint64_t gated    = 0;   ///< samples silenced by the gate
    std::uint64_t clipped  = 0;   ///< samples saturated at the int16 rails
    std::int32_t  peak_in  = 0;   ///< max |input sample| in int16 LSBs
    std::int32_t  peak_out = 0;   ///< max |output sample| in int16 LSBs
};

/// Everything the kernel needs, passed as ONE pointer.
///
/// Deliberately not six arguments: System V passes the nth float in
/// xmm0/xmm1/... while Windows passes position 4 in xmm3 and pushes position
/// 5 and 6 to the stack, so the "obvious" signature reads its floats from
/// different places depending on the platform -- and still assembles and
/// links when it is wrong. One pointer means one register (rcx on Windows,
/// rdi on System V) and no convention left to disagree about.
///
/// The offsets are part of the ABI with the .asm file; the static_asserts
/// below are what keeps the two honest.
struct TdNormKernelParams {
    const std::int16_t* in       = nullptr;  ///< +0  source samples
    std::int16_t*       out      = nullptr;  ///< +8  destination (may equal in)
    std::uint64_t       count    = 0;        ///< +16 samples; rounded down to 16
    float               gate_level = 0.0f;   ///< +24 gate in int16 LSBs
    float               gain     = 1.0f;     ///< +28 linear gain
    TdNormKernelStats*  stats    = nullptr;  ///< +32 filled with this block's totals
};

static_assert(offsetof(TdNormKernelParams, in) == 0, "asm reads in at +0");
static_assert(offsetof(TdNormKernelParams, out) == 8, "asm reads out at +8");
static_assert(offsetof(TdNormKernelParams, count) == 16, "asm reads count at +16");
static_assert(offsetof(TdNormKernelParams, gate_level) == 24, "asm reads gate at +24");
static_assert(offsetof(TdNormKernelParams, gain) == 28, "asm reads gain at +28");
static_assert(offsetof(TdNormKernelParams, stats) == 32, "asm reads stats at +32");
static_assert(sizeof(TdNormKernelParams) == 40, "asm assumes a 40-byte block");
static_assert(offsetof(TdNormKernelStats, gated) == 0, "asm writes gated at +0");
static_assert(offsetof(TdNormKernelStats, clipped) == 8, "asm writes clipped at +8");
static_assert(offsetof(TdNormKernelStats, peak_in) == 16, "asm writes peak_in at +16");
static_assert(offsetof(TdNormKernelStats, peak_out) == 20, "asm writes peak_out at +20");
static_assert(sizeof(TdNormKernelStats) == 24, "asm writes 24 bytes of stats");

#if defined(TD_NORMALIZER_HAVE_ASM)

/// Gate + gain over one block of interleaved int16 samples: byte-for-byte the
/// same result as process_block() in normalizer.cpp.
///
/// \param p  parameter block; \c p->count is rounded DOWN to a multiple of 16
///           inside the kernel, so a non-multiple caller gets a partial run
///           and must scalar-finish the tail itself. \c p->stats is filled
///           with this block's totals and never accumulated into.
void td_normalizer_gate_gain(const TdNormKernelParams* p);

#endif  // TD_NORMALIZER_HAVE_ASM

/// 1 when the AVX2 kernel is both compiled in and runnable on this CPU
/// (CPUID AVX + AVX2 + POPCNT and the OS-enabled XMM/YMM state from
/// XGETBV0). Always 0 for a build without NASM, so the caller falls back to
/// the scalar loop without any #ifdef of its own.
int td_normalizer_kernel_ready(void);

}  // extern "C"

#endif  // TDUBBBER_NORMALIZER_KERNEL_H
