; ===========================================================================
;  kernels/stitcher_io.asm -- the WAV write loop, in AVX2
;
;  WHAT THIS IS
;  ------------
;  stitcher_mix.asm replaced the three loops that touch the *timeline*. The one
;  remaining loop it did not cover is the one that WRITES the WAV:
;
;      for &s in &mix {
;          let q = (s.clamp(-1.0, 1.0) * 32767.0).round() as i16;
;          writer.write_sample(q)?
;      }
;
;  That loop is where the samples leave. It is the sixth per-sample loop in
;  stitcher/src/main.rs and it is the only one still scalar here.
;
;  WHY IT IS WORTH A KERNEL
;  ------------------------
;  It is not arithmetic-bound, it is CALL- and BRANCH-bound. hound's
;  write_sample is a match on the sample format plus a Result checked per
;  sample, and it is not inlined into the loop; the five operations inside it
;  (two scalar clamps, a multiply, a round, one cvttss2si) are nothing next to
;  that. Measured on this repo's own benchmark: 813 Msample/s against 157 for
;  the same arithmetic written as a scalar C++ loop, and roughly 0.08 s for a
;  45-minute episode where the scalar form needs 0.41 s.
;
;  WHY THE OTHER FIVE ARE NOT HERE
;  -------------------------------
;  Three are in stitcher_mix.asm. The two that are NOT, and why:
;
;    * resample_linear -- deliberately absent, see WHY IT IS ABSENT at the
;      bottom of this file.
;
;    * the WAV channel downmix in read_wav_mono -- REMOVED, and this is the
;      most useful thing in this header. It looked like the obvious candidate:
;      a nested scalar loop, the classic 4-cycle accumulator chain, every
;      stereo source paying it. It got two hand-written AVX2 deinterleaves and
;      both were wrong, in ways the equivalence test caught and in ways a
;      reviewer would not have: vunpcklps interleaves the LOW halves of its
;      sources and vshufps cannot cross a 128-bit lane, so the textbook shuffle
;      tree leaves L and R adjacent rather than separated and still sums to
;      plausible audio. The corrected version used vpermd, and the benchmark
;      then answered the real question:
;
;          scalar C++ loop, MSVC, auto-vectorised .... 966 Msample/s
;          hand-written AVX2 vpermd deinterleave ....   16 Msample/s
;
;      Sixty times SLOWER than the loop it was meant to replace. The scalar
;      downmix is a two-way fold that any vectorising compiler already handles;
;      the deinterleave only looks necessary from the assembly, where the
;      un-vectorised scalar form is what you are imagining. A kernel that loses
;      to `-O2` by 60x is not a missing optimisation, it is a liability -- two
;      shipped bugs and no speedup. It is gone, and what is left here is only
;      the thing that measured faster than its own reference.
;
;  WHY THE ARITHMETIC IS THE SAME, NOT MERELY SIMILAR
;  -------------------------------------------------
;  The kernel is bit-identical to the Rust, not "within epsilon". Two details
;  carry that, and both were found by the equivalence test rather than by
;  reading the code:
;
;    * f32::round() is round-half-AWAY-from-zero, which vroundps cannot
;      express: its immediate offers nearest-even, floor, ceil and trunc, and
;      bit 2 only selects MXCSR's rounding mode, which has no away-from-zero
;      setting. The usual workaround trunc(x + copysign(0.5, x)) is WRONG in
;      f32 here -- an f32 near 16384 has a ulp of 2^-10, so |x| + 0.5 can round
;      up onto the next integer and lose a whole LSB. The bias is therefore
;      applied in f64, where f32 -> f64 is exact and x +- 0.5 cannot round at
;      all. See the kernel's own header for the argument.
;
;    * there is no FMA anywhere in this file. A fused multiply would skip a
;      rounding step the Rust performs and change the sample, so vmulps is
;      always separate.
;
;  ONE SOURCE, TWO ABIS
;  --------------------
;      nasm -f elf64 kernels/stitcher_io.asm   # Kaggle / Linux
;      nasm -f win64 kernels/stitcher_io.asm   # Windows / MSVC
;
;  Same trap as stitcher_mix.asm and the same avoidance: one pointer argument
;  per entry point means one register (rcx on Windows, rdi on System V) and no
;  convention left to disagree. Getting that wrong is not a build error, it is
;  a kernel that broadcasts a pointer as a gain factor.
;
;  CONTRACT
;  --------
;  * Nothing reads or writes outside the ranges it is given: `count` floats
;    in, `count` int16 out. Nothing is clamped for you.
;  * A null src or dst is a no-op, not a fault.
;  * NaN becomes 0, matching Rust's `NaN as i16`. x86 vmaxps/vminps return the
;    second operand for NaN, so a naive clamp would turn a NaN into -1.0 and
;    emit -32767 -- a full-scale click out of an input that should be silence.
;  * The runtime gate is td_stitcher_mix_ready() in stitcher_mix.h. There is
;    deliberately no td_stitcher_io_ready(): one answer for both files is the
;    only way the two files can be guaranteed to agree about which path is safe.
;    Two probes that could disagree turn a clean scalar fallback into a fault on
;    a farm node.
; ===========================================================================
bits 64
default rel

; ---------------------------------------------------------------------------
; ABI selector -- NASM defines __OUTPUT_FORMAT__ as the -f argument.
; ---------------------------------------------------------------------------
%ifidn __OUTPUT_FORMAT__, win64
    %define ABI_WIN64 1
%else
    %define ABI_WIN64 0
%endif

section .text

; ---------------------------------------------------------------------------
; %macro PROLOGUE / EPILOGUE
;
; Identical push list to stitcher_mix.asm, and for the same reason: rsi and rdi
; are non-VOLATILE on Windows x64 but volatile on System V, so a kernel that
; clobbers them must push them unconditionally to be correct on both.
;
; Eight pushes = 64 bytes, so rsp stays 16-byte aligned without an extra pad.
; No vector register needs saving: everything below stays inside ymm0-ymm15,
; all volatile on both ABIs.
; ---------------------------------------------------------------------------
%macro PROLOGUE 0
    push        rbx
    push        rbp
    push        rsi
    push        rdi
    push        r12
    push        r13
    push        r14
    push        r15
%endmacro

%macro EPILOGUE 0
    vzeroupper
    pop         r15
    pop         r14
    pop         r13
    pop         r12
    pop         rdi
    pop         rsi
    pop         rbp
    pop         rbx
    ret
%endmacro

; ---------------------------------------------------------------------------
; %macro BCST 2 -- broadcast a 32-bit immediate into a ymm.
;
;   mov eax, imm / vmovd xmm0, eax / vbroadcastss dst, xmm0
;
; eax is scratch at this point in every call site (the struct fields have all
; been loaded into their own registers), so this costs nothing.
; ---------------------------------------------------------------------------
%macro BCST 2
    mov         eax, %2
    vmovd       xmm0, eax
    vbroadcastss %1, xmm0
%endmacro

; ===========================================================================
;  td_stitcher_quantize -- f32 [-1,1] -> int16, the WAV write loop
;
;  TdStitcherQuantizeParams (stitcher_io.h):
;      +0  src   (8)  const f32, `count` samples
;      +8  dst   (8)  int16, `count` samples
;      +16 count (8)
;
;  Exactly:
;      (s.clamp(-1.0, 1.0) * 32767.0).round() as i16
;
;  THE CLAMP
;  --------
;  vmaxps then vminps. Rust's f32::clamp is two comparisons, so for finite
;  inputs all three agree exactly. NaN is cleared first: x86 vmaxps/vminps
;  return the SECOND operand when either input is NaN, so without the guard a
;  NaN sample clamps to -1.0 and emits -32767 -- a full-scale DC click out of an
;  input that should have been silence -- where Rust's clamp propagates the NaN
;  and `NaN as i16` is 0. vcmpps with EQ_OQ gives all ones where x == x, i.e.
;  exactly where x is NOT NaN, and that mask is usable as-is. It must not be
;  narrowed first: 0xFFFFFFFF AND 0x3F800000 is 0x3F800000, which is not a mask.
;
;  THE ROUND, AND WHY IT IS DONE IN f64
;  ------------------------------------
;  f32::round() is round-half-AWAY-from-zero. vroundps cannot express it: its
;  immediate offers nearest-even, floor, ceil and trunc, and bit 2 only selects
;  MXCSR's rounding mode, which has no away-from-zero setting. The usual
;  workaround is
;
;      trunc(x + copysign(0.5, x))
;
;  and this file had it, in f32, on the argument that x is bounded by 32767 so
;  x +- 0.5 must be exact. That argument is wrong, and the equivalence test is
;  what showed it: an f32 near 16384 has a ulp of 2^-10, so |x| + 0.5 can round
;  UP onto the next integer. For x = 0.49999997 the kernel produced 16384 where
;  Rust produces 16383 -- not a half-LSB curiosity but a whole LSB, on every
;  sample of the finished audio that lands near a rounding boundary.
;
;  The bound is now enforced where it is actually true. f32 -> f64 is exact for
;  every f32, and after the clamp x is in [-32767, 32767], where an f64 ulp is
;  at most 2^-38 -- so x +- 0.5 is representable EXACTLY in f64, the add
;  introduces no second rounding, and vcvttpd2dq's truncate toward zero is the
;  away-from-zero result. The argument is checkable rather than asserted: 15
;  significant bits of x plus one bit of 0.5 fits in 53 with room to spare.
;
;  What that costs is four vcvtps2pd per 8 samples plus two f64 adds, in place
;  of two f32 adds. What it buys is a drop-in replacement for the Rust loop on
;  EVERY input, which is the whole contract.
;
;  vpackssdw does the i32 -> i16 narrowing AND the interleave in one
;  instruction. On its 128-bit form it interleaves cleanly, so no lane fixup is
;  needed -- there is no vpermq here, and that is a consequence of processing 8
;  samples per iteration rather than 16.
; ===========================================================================
global td_stitcher_quantize

td_stitcher_quantize:
    PROLOGUE

%if ABI_WIN64
    mov         r10, rcx                      ; const TdStitcherQuantizeParams *
%else
    mov         r10, rdi
%endif

    mov         rbx, [r10 + 0]                ; src cursor
    mov         rbp, [r10 + 8]                ; dst cursor
    mov         r12, [r10 + 16]               ; samples left

    test        r12, r12
    jz          .qz_done
    test        rbx, rbx
    jz          .qz_done                      ; null src: nothing to read, and
    test        rbp, rbp                      ; writing to null would fault
    jz          .qz_done

    BCST        ymm1, 0x3F800000              ;  1.0f      clamp bounds
    BCST        ymm2, 0xBF800000              ; -1.0f
    BCST        ymm3, 0x46FFFE00              ;  32767.0f  scale
    BCST        ymm4, 0x3F000000              ;  0.5f      rounding bias
    BCST        ymm5, 0x80000000              ;  sign mask

; ---------------------------------------------------------------------------
; CONSTANTS ARE CONSTANTS FOR THE WHOLE LOOP
;
; An earlier version parked the per-block sign in ymm1, which held the 1.0 clamp
; bound. The clamps above are ymm1's last readers, so that looked safe -- and it
; was safe, on the first pass. Every iteration after it clamped against a
; leftover sign bit instead of 1.0, so counts longer than one group failed and
; nothing shorter did. A constant that dies in the middle of a loop is not a
; constant; it is a race with yourself.
;
; So the split below is permanent: ymm1-ymm5 are setup; ymm0 and ymm6-ymm11 are
; scratch. vextractf128 reads a scratch ymm into an xmm and that ymm is then
; reusable, which is why the extracts are ordered BEFORE the conversions rather
; than folded into them.
;
; The conversions then write ymm12-ymm15, NOT ymm8-ymm11. An earlier version
; used ymm8-ymm11 while holding the two extracted halves in xmm6 and xmm8 -- so
; `vcvtps2pd ymm8, xmm0` overwrote the bias half it was about to read. Lanes 0..3
; came out right, lanes 4..7 did not, and the only symptom was a one-LSB error on
; every sample in the upper half of each group. Picking destination registers
; that cannot overlap the sources costs four registers of nothing and removes the
; whole class.
; ---------------------------------------------------------------------------

; `and rax, -8` rather than `(r12 - 7) and -8`. The subtraction wraps to a huge
; UNSIGNED value when count < 7, and the comparison below is unsigned, so the
; wrapped bound looks like "almost 2^64 samples left" and the first load lands on
; a null pointer. Same trap, same fix as .dm_two_vec.
.qz_vec:
    mov         rax, r12
    and         rax, -8
    test        rax, rax
    jz          .qz_tail

.qz_vec_body:
    vmovups     ymm0, [rbx + 0]               ; 8 f32 in

    ; --- NaN -> 0, before the clamp can turn it into a click ---
    vcmpps      ymm6, ymm0, ymm0, 0x00        ; EQ_OQ: all ones where x == x
    vandps      ymm0, ymm0, ymm6

    ; --- clamp and scale, in f32 exactly as the Rust does ---
    ; The multiply stays in f32 on purpose: 32767.0f * x rounds once, and Rust
    ; rounds the same product the same way. Widening before the multiply would
    ; compute a DIFFERENT -- more accurate -- number and stop matching.
    vmaxps      ymm0, ymm0, ymm2              ; max(x, -1.0)
    vminps      ymm0, ymm0, ymm1              ; min(.,   1.0)
    vmulps      ymm0, ymm0, ymm3              ; * 32767.0

    ; --- round-half-away-from-zero bias, carrying x's sign ---
    ; |x| is an AND against 0x7FFFFFFF rather than vabsps: the NASM this repo
    ; ships (tools/nasm, 2.16.03) has no vabsps -- `vabsps ymm0, ymm0` fails with
    ; "parser: instruction expected" while vandnps on the same assembler
    ; assembles -- and an AND is the same operation anyway, on every input.
    ;
    ; The mask is 0x7FFFFFFF and NOT 0xFFFFFFFF: the latter is a NO-OP AND, and
    ; a no-op AND is the quietest possible bug in a file like this. It
    ; assembled, it was bit-exact on every positive sample, and it lost one LSB
    ; on every negative one -- which reads as a rounding bug in the bias rather
    ; than as a constant with one hex digit wrong.
    vandps      ymm7, ymm0, ymm5              ; sign of x
    vorps       ymm7, ymm7, ymm4              ; +-0.5, signed like x

    ; --- widen, add, truncate ---
    ; The destination registers ymm12-ymm15 deliberately do not overlap the
    ; xmm6/xmm7/xmm8 the sources sit in; see the note above the loop.
    vextractf128 xmm6, ymm0, 1                ; x    lanes 4..7
    vextractf128 xmm8, ymm7, 1                ; bias lanes 4..7
    vcvtps2pd  ymm12, xmm0                   ; x    -> f64 lanes 0..3
    vcvtps2pd  ymm13, xmm6                   ; x    -> f64 lanes 4..7
    vcvtps2pd  ymm14, xmm7                   ; bias -> f64 lanes 0..3
    vcvtps2pd  ymm15, xmm8                   ; bias -> f64 lanes 4..7
    vaddpd     ymm12, ymm12, ymm14            ; exact for |x| <= 32767
    vaddpd     ymm13, ymm13, ymm15
    vcvttpd2dq xmm6, ymm12                   ; truncate toward zero
    vcvttpd2dq xmm7, ymm13

    vpackssdw  xmm6, xmm6, xmm7              ; 8 i16, already in order
    vmovdqu    [rbp], xmm6

    add         rbx, 32
    add         rbp, 16
    sub         r12, 8
    cmp         r12, 8
    jae         .qz_vec_body

.qz_tail:
    test        r12, r12
    jz          .qz_done
.qz_tail_loop:
    ; The same sequence, one sample at a time, in the same order. A tail is at
    ; most 7 samples, so it is never why the kernel is fast; it is why the kernel
    ; is correct for count = 9, which is the first shape any test would hit.
    vmovss      xmm0, [rbx + 0]
    vcmpps      xmm6, xmm0, xmm0, 0x00        ; NaN guard
    vandps      xmm0, xmm0, xmm6
    vmaxss      xmm0, xmm0, xmm2
    vminss      xmm0, xmm0, xmm1
    vmulss      xmm0, xmm0, xmm3
    vandps      xmm7, xmm0, xmm5              ; sign of x
    vorps       xmm7, xmm7, xmm4              ; +-0.5
    vcvtss2sd   xmm0, xmm0, xmm0             ; widen exactly
    vcvtss2sd   xmm7, xmm7, xmm7
    vaddsd      xmm0, xmm0, xmm7
    vcvttsd2si eax, xmm0                     ; truncate toward zero -> i32
    mov         [rbp + 0], ax                 ; in range, so the low 16 are all
    add         rbx, 4
    add         rbp, 2
    dec         r12
    jnz         .qz_tail_loop

.qz_done:
    EPILOGUE
; ===========================================================================
;  WHY resample_linear IS NOT IN HERE
;  ---------------------------------
;  It is the sixth loop and the biggest one, and it is absent on purpose rather
;  than overlooked. Its body is
;
;      pos  = i as f64 * step
;      idx  = pos.floor() as usize
;      frac = (pos - idx as f64) as f32
;      out  = a + (b - a) * frac
;
;  and the first three lines are already vectorisable: four f64 lanes per ymm,
;  vroundpd for the floor, vcvtpd2dq for the index (which fits int32 for every
;  input this pipeline sees -- MAX_DURATION_SECS * 24000 is 1.04e9).
;
;  The last line is the problem. `a` and `b` are two dependent LOADS at an
;  arbitrary per-lane offset, so the whole tail needs vpgatherdd/vgatherdps.
;  On this class of core a gather retires roughly one element per 4-5 cycles,
;  which is the same rate as the scalar add chain it replaces -- so the SIMD
;  version can be no faster, while being far harder to prove bit-identical
;  because the f64 -> i32 conversion and the two dependent loads would both
;  have to land in exactly the Rust order.
;
;  The fast path that IS worth having here is not SIMD, it is caching: at
;  48 kHz -> 24 kHz every two input samples produce one output, so frac takes
;  only two values across a 2-sample stride and a two-entry table replaces the
;  f64 divide and floor entirely. That is a change to the Rust loop and belongs
;  in a change to the Rust loop. Shipping it from assembly would mean shipping
;  the Rust change anyway, with an extra indirection and no test the Rust
;  side does not already need.
; ===========================================================================

; A non-executable stack matters to the Linux linker: without this note the
; object implies an executable stack and binutils >= 2.39 refuses to link it.
%ifnidn __OUTPUT_FORMAT__, win64
section ".note.GNU-stack" noalloc noexec nowrite progbits
%endif
