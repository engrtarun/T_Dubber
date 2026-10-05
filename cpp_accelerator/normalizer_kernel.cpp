// ===========================================================================
//  normalizer_kernel.cpp -- "can this CPU actually run the AVX2 kernel?"
//
//  A binary built with the NASM kernel is still shipped to machines we do not
//  control (Kaggle nodes, the render farm), so the decision to use assembly
//  is made at RUNTIME, not at build time:
//
//      CPUID.1:ECX   OSXSAVE (27) + AVX (28) + POPCNT (23)
//      XGETBV(0)     XMM (bit 1) and YMM (bit 5) state enabled by the OS
//      CPUID.7:EBX   AVX2 (bit 5)
//
//  OSXSAVE/XGETBV matter because AVX2 instructions fault with #UD on a kernel
//  that has not enabled YMM state (e.g. an old OS under a new CPU), and
//  POPCNT matters because the kernel counts gated/clipped lanes with it.
//
//  The answer is computed once and cached: the normalizer calls it per block
//  and the answer cannot change while the process lives.
// ===========================================================================

#include "normalizer_kernel.h"

#if defined(TD_NORMALIZER_HAVE_ASM)

#if defined(_MSC_VER)
// __cpuidex and _xgetbv live here on MSVC (they are intrinsics, not
// library functions, but the declarations are not in any header we include).
#include <intrin.h>
#else
// __get_cpuid_count lives here on GCC/Clang -- same story, opposite header.
// Without this line the Linux pack build fails with "'__get_cpuid_count' was
// not declared", which is exactly what happened the first time the Kaggle
// pack pipeline compiled this file: the binary never got linked, and the
// Dockerfile's trailing `; echo` turned that failed build into a "successful"
// stage, so the failure only surfaced much later as
//   COPY --from=cpp-forge /src/build/normalizer: not found
#include <cpuid.h>
#endif

namespace {

struct CpuidRegs {
    unsigned eax = 0;
    unsigned ebx = 0;
    unsigned ecx = 0;
    unsigned edx = 0;
};

/// CPUID with a sub-leaf index. MSVC spells that __cpuidex; GCC and Clang
/// ship <cpuid.h> with __get_cpuid_count, which fails (returns 0) when the
/// leaf is not supported -- exactly the behaviour we want for leaf 7.
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
/// _xgetbv; GCC/Clang need the instruction spelled out (they refuse to emit
/// it without -mxsave, which we deliberately do not force on this build).
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

    // Without OSXSAVE this would fault rather than answer, which is why the
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

extern "C" int td_normalizer_kernel_ready(void) {
    static const bool ready = avx2_usable();
    return ready ? 1 : 0;
}

#else  // TD_NORMALIZER_HAVE_ASM not defined: pure scalar build.

extern "C" int td_normalizer_kernel_ready(void) { return 0; }

#endif
