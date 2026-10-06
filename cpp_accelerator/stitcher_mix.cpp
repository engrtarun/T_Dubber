// ===========================================================================
//  stitcher_mix.cpp -- "can this CPU actually run the AVX2 mix kernels?"
//
//  The same question normalizer_kernel.cpp answers, deliberately asked in the
//  same way and from the same source of truth. Two copies of a CPUID probe
//  would be two places for them to disagree -- one saying "use the fast path"
//  while the other says "fall back" -- and the symptom would be a run that
//  works on the dev box and faults on a farm node.
//
//  The binary is still shipped to machines we do not control (Kaggle nodes,
//  the render farm), so the decision is made at RUNTIME:
//
//      CPUID.1:ECX   OSXSAVE (27) + AVX (28) + POPCNT (23)
//      XGETBV(0)     XMM (bit 1) and YMM (bit 5) state enabled by the OS
//      CPUID.7:EBX   AVX2 (bit 5)
//
//  The answer is computed once and cached: the stitcher asks per segment, and
//  the answer cannot change while the process lives.
// ===========================================================================

#include "stitcher_mix.h"

#if defined(TD_STITCHER_HAVE_ASM)

#if defined(_MSC_VER)
// __cpuidex and _xgetbv are intrinsics rather than library functions, and their
// declarations are not in any header this file includes.
#include <intrin.h>
#else
// The other half of the same declaration problem. On GCC and Clang
// __get_cpuid_count and __cpuid_count live in <cpuid.h>, which nothing else
// here pulls in, so without this the Linux pack build stops at:
//
//     stitcher_mix.cpp:54:9: error: '__get_cpuid_count' was not declared
//
// while the MSVC build stays green -- the local Windows test cannot see it,
// because MSVC's intrinsics are available without a header. Same trap, same
// shape as the <cpuid.h> fix in normalizer_kernel.cpp.
#include <cpuid.h>
#endif

namespace {

struct CpuidRegs {
    unsigned eax = 0;
    unsigned ebx = 0;
    unsigned ecx = 0;
    unsigned edx = 0;
};

/// CPUID with a sub-leaf index. MSVC spells that __cpuidex; GCC and Clang ship
/// <cpuid.h> with __get_cpuid_count, which returns 0 when the leaf is not
/// supported -- exactly the behaviour wanted for leaf 7.
CpuidRegs cpuid(unsigned leaf, unsigned subleaf) {
    CpuidRegs r;
#if defined(_MSC_VER)
    int regs[4] = {0, 0, 0, 0};
    __cpuidex(regs, static_cast<int>(leaf), static_cast<int>(subleaf));
    r.eax = static_cast<unsigned>(regs[0]);
    r.ebx = static_cast<unsigned>(regs[1]);
    r.ecx = static_cast<unsigned>(regs[2]);
    r.edx = static_cast<unsigned>(regs[3]);
#else
    unsigned a = 0, b = 0, c = 0, d = 0;
    if (__get_cpuid_count(leaf, subleaf, &a, &b, &c, &d)) {
        r.eax = a;
        r.ebx = b;
        r.ecx = c;
        r.edx = d;
    }
#endif
    return r;
}

/// XGETBV(0): which register families the OS has enabled. MSVC provides
/// _xgetbv; GCC/Clang need the instruction spelled out, because they refuse to
/// emit it without -mxsave and this build does not force that.
std::uint64_t xgetbv0() {
#if defined(_MSC_VER)
    return _xgetbv(0);
#else
    unsigned lo = 0, hi = 0;
    __asm__ volatile("xgetbv" : "=a"(lo), "=d"(hi) : "c"(0));
    return (static_cast<std::uint64_t>(hi) << 32) | lo;
#endif
}

bool avx2_usable() {
    const CpuidRegs f1 = cpuid(1, 0);
    constexpr unsigned kOsxsave = 1u << 27;
    constexpr unsigned kAvx     = 1u << 28;
    constexpr unsigned kPopcnt  = 1u << 23;
    if ((f1.ecx & (kOsxsave | kAvx | kPopcnt)) != (kOsxsave | kAvx | kPopcnt)) {
        return false;
    }

    // Without OSXSAVE this would fault instead of answering, which is why the
    // check above comes first.
    constexpr std::uint64_t kXmmYmm = 0x6u;  // bit 1 = XMM, bit 5 = YMM
    if ((xgetbv0() & kXmmYmm) != kXmmYmm) {
        return false;
    }

    const CpuidRegs f7 = cpuid(7, 0);
    constexpr unsigned kAvx2 = 1u << 5;
    return (f7.ebx & kAvx2) != 0;
}

}  // namespace

extern "C" int td_stitcher_mix_ready(void) {
    static const bool ready = avx2_usable();
    return ready ? 1 : 0;
}

#else  // TD_STITCHER_HAVE_ASM not defined: pure scalar build.

extern "C" int td_stitcher_mix_ready(void) { return 0; }

#endif