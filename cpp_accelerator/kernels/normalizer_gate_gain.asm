; ===========================================================================
;  kernels/normalizer_gate_gain.asm -- AVX2 gate + gain kernel (NASM syntax)
;
;  WHAT THIS IS
;  ------------
;  The one place in the T_Dubber pipeline where a CPU burns cycles per sample
;  over the whole audio stream: the gate + gain inner loop of
;  cpp_accelerator/normalizer.cpp (process_block). This file is the same
;  arithmetic, hand-scheduled onto AVX2 registers, so the scalar loop in
;  normalizer.cpp stays as the reference and the fallback.
;
;      void td_normalizer_gate_gain(const TdNormKernelParams *params);
;
;  ONE SOURCE, TWO ABIS
;  --------------------
;  The Kaggle pack ships a static Linux x86-64 ELF (built by g++ in Docker);
;  the local dev box builds a Windows x64 PE with MSVC. NASM emits both from
;  this one file.
;
;  Both ABIs are dodged entirely by the signature: the function takes ONE
;  pointer to a parameter block, so the only argument register involved is
;  rcx on Windows and rdi on System V. That is not a stylistic choice -- the
;  obvious six-argument signature is a trap, because the two conventions
;  disagree about floating point in a way that compiles silently:
;
;      void f(const int16_t *in, int16_t *out, uint64_t count,
;             float gate, float gain, TdNormKernelStats *stats);
;
;      | argument | System V (elf64)      | Microsoft (win64)          |
;      | in       | rdi                   | rcx                        |
;      | out      | rsi                   | rdx                        |
;      | count    | rdx                   | r8                         |
;      | gate     | xmm0 (1st float)      | xmm3 (4th POSITION)        |
;      | gain     | xmm1 (2nd float)      | stack, position 5          |
;      | stats    | rcx (4th integer)     | stack, position 6          |
;
;  System V queues registers by TYPE (nth float -> xmm0, xmm1, ...), while
;  Windows assigns them by POSITION (position p -> rcx/xmm0, rdx/xmm1,
;  r8/xmm2, r9/xmm3, stack from position 5 on). Get it wrong and the kernel
;  happily broadcasts a pointer as a float -- it assembles, it links, and it
;  produces wrong audio. Verified against MSVC's own codegen for the call
;  site, which loads gain from [rsp+20h] and stats from [rsp+28h].
;
;  Build it with:
;
;      nasm -f elf64 normalizer_gate_gain.asm   # Kaggle / Linux
;      nasm -f win64 normalizer_gate_gain.asm   # Windows / MSVC
;
;  TdNormKernelParams (see normalizer_kernel.h) is the parameter block:
;
;      +0   in         (8 bytes)
;      +8   out        (8 bytes)
;      +16  count      (8 bytes)
;      +24  gate_level (4 bytes, float bits)
;      +28  gain       (4 bytes, float bits)
;      +32  stats      (8 bytes)
;
;  CONTRACT
;  --------
;  * count is rounded DOWN to a whole group of 16 samples here, so a caller
;    that passes an unaligned length still gets a safe (partial) run and must
;    finish the tail itself -- which normalizer.cpp does with its own scalar
;    loop. Nothing is ever written past out[count].
;  * stats is filled with THIS BLOCK's totals (never merged with previous
;    blocks): gated, clipped, peak_in, peak_out. The C++ caller merges them.
;  * Every register that is callee-saved on either ABI is handled: rbx, r12,
;    r13, r14 are pushed and popped on both, and Windows additionally parks
;    xmm6..xmm15 (whose low halves are non-volatile only there). Everything
;    else is caller-saved. No stack argument is ever read, so the prologue
;    cannot be offset-sensitive.
;
;  WHY THESE INSTRUCTIONS
;  ----------------------
;  Per sample the reference does: |raw|, one float compare against the gate,
;  one multiply, one nearbyint (round-to-nearest-even), one clamp, one store,
;  plus four statistics. Each maps 1:1 onto an AVX2 instruction:
;
;      vpmovsxwd  int16 -> int32            (sign extend 8 lanes at once)
;      vpabsd     |raw|
;      vcmpps     mask = (|raw| < gate)     (branch free -- no mispredictions)
;      vandnps    zero the lanes the mask marked as noise
;      vmulps     gain
;      vroundps   0 = nearest-even          (== nearbyint under default MXCSR)
;      vminps/vmaxps  clamp to the int16 rails
;      vcvtps2dq  int32 result
;      vpackssdw  int32 -> int16            (values are pre-clamped)
;
;  Statistics ride along in the same pass (peak via vpmaxsd, gated/clipped
;  via vpmovmskb + popcnt), so there is never a second sweep over the audio.
;
;  WHY 16 SAMPLES AT A TIME
;  ------------------------
;  One int16 load of 8 samples sign-extends into one ymm of int32, so two
;  loads (xmm14/xmm15) become two ymm work vectors and the two packed results
;  are stitched back into a single 32-byte store with vinserti128.
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
; KERNEL_VEC <src:xmm> <dst:xmm>
;
;   8 x int16 in <src>  ->  8 x int16 in <dst>, statistics accumulated into
;   ymm8 (peak_in), ymm9 (peak_out), r8 (gated), r9 (clipped).
;
;   Clobbers: eax, rax, r8, r9, ymm4..ymm7, ymm10, xmm11.
;   Preserved: ymm0..ymm3 (gate, gain, clamp constants), ymm8/ymm9,
;              <src> and the other output vector.
; ---------------------------------------------------------------------------
%macro KERNEL_VEC 2
    vpmovsxwd  ymm4, %1                       ; raw: 8 x int16 -> 8 x int32
    vpabsd     ymm5, ymm4                     ; |raw|
    vcvtdq2ps  ymm6, ymm5                     ; float(|raw|)
    vcmpps     ymm7, ymm6, ymm0, 0x01         ; mask = (|raw| < gate_level)

    ; gated += lanes where the mask is set (one set lane = 0xFF x 4 bytes)
    vpmovmskb  eax, ymm7
    popcnt     eax, eax
    shr        eax, 2
    add        r8, rax

    vcvtdq2ps  ymm6, ymm4                     ; float(raw)
    vandnps    ymm6, ymm7, ymm6               ; ~mask & raw -> 0 for noise
    vmulps     ymm6, ymm6, ymm1               ; * gain
    vroundps   ymm6, ymm6, 0x00               ; nearest-even == nearbyint()
    vmovaps    ymm7, ymm6
    vminps     ymm7, ymm7, ymm2               ; clamp high  (32767.0f)
    vmaxps     ymm7, ymm7, ymm3               ; clamp low (-32768.0f)

    ; clipped += lanes where the clamp actually changed the value
    vcmpps     ymm10, ymm6, ymm7, 0x04        ; NEQ
    vpmovmskb  eax, ymm10
    popcnt     eax, eax
    shr        eax, 2
    add        r9, rax

    vcvtps2dq  ymm4, ymm7                     ; int32 result (pre-clamped)
    vpabsd     ymm10, ymm4
    vpmaxsd    ymm8, ymm8, ymm5               ; peak_in  = max(|raw|)
    vpmaxsd    ymm9, ymm9, ymm10              ; peak_out = max(|value|)

    ; 8 x int32 -> 8 x int16. vpackssdw takes the low 4 lanes of each source,
    ; so feeding (low half, high half) puts the words back in sample order.
    vextracti128 xmm11, ymm4, 1
    vpackssdw  %2, xmm4, xmm11
%endmacro

; ===========================================================================
; td_normalizer_gate_gain
; ===========================================================================
global td_normalizer_gate_gain

td_normalizer_gate_gain:
    push     rbx
    push     r12
    push     r13
    push     r14

%if ABI_WIN64
    ; Windows x64 keeps xmm6..xmm15 (low 128 bits) non-volatile -- System V
    ; keeps none. The kernel uses several of them as scratch, so they are
    ; parked on the stack here and put back at the end; the upper 128 bits of
    ; ymm6..ymm15 are volatile on Windows and need no saving. Once per block,
    ; not once per sample, so it costs nothing measurable.
    sub      rsp, 0A0h
    movdqu   [rsp + 00h], xmm6
    movdqu   [rsp + 10h], xmm7
    movdqu   [rsp + 20h], xmm8
    movdqu   [rsp + 30h], xmm9
    movdqu   [rsp + 40h], xmm10
    movdqu   [rsp + 50h], xmm11
    movdqu   [rsp + 60h], xmm12
    movdqu   [rsp + 70h], xmm13
    movdqu   [rsp + 80h], xmm14
    movdqu   [rsp + 90h], xmm15
%endif

%if ABI_WIN64
    mov      r10, rcx                        ; const TdNormKernelParams *
%else
    mov      r10, rdi
%endif

    mov      rbx, [r10 + 0]                  ; in
    mov      r12, [r10 + 8]                  ; out
    mov      r13, [r10 + 16]                 ; count
    mov      eax, [r10 + 24]                 ; gate_level (float bits)
    vmovd    xmm0, eax
    vbroadcastss ymm0, xmm0                  ; ymm0 = gate_level (all lanes)
    mov      eax, [r10 + 28]                 ; gain (float bits)
    vmovd    xmm1, eax
    vbroadcastss ymm1, xmm1                  ; ymm1 = gain
    mov      r14, [r10 + 32]                 ; stats

    xor      r8d, r8d                        ; gated
    xor      r9d, r9d                        ; clipped
    vpxor    ymm8, ymm8, ymm8                ; peak_in accumulator
    vpxor    ymm9, ymm9, ymm9                ; peak_out accumulator

    ; The clamp bounds are compile-time constants.
    mov      eax, 0x46FFFE00                 ; 32767.0f
    vmovd    xmm2, eax
    vpbroadcastd ymm2, xmm2
    mov      eax, 0xC7000000                 ; -32768.0f
    vmovd    xmm3, eax
    vpbroadcastd ymm3, xmm3

    shl      r13, 1                          ; samples -> bytes
    and      r13, -32                        ; whole groups of 16 samples only
    xor      r10d, r10d                      ; byte offset
    test     r13, r13
    jz       .done

.loop:
    vmovdqu  xmm14, [rbx + r10]              ; samples  0.. 7
    vmovdqu  xmm15, [rbx + r10 + 16]         ; samples  8..15

    KERNEL_VEC xmm14, xmm12
    KERNEL_VEC xmm15, xmm13

    ; vpackssdw in KERNEL_VEC is a 128-bit op, so it WIPES ymm12[255:128]
    ; (VEX.128 zero-extends). imm must therefore be 1, which takes the low
    ; lane from SRC1 (packed samples 0..7) and the high lane from SRC2 --
    ; imm 0 would stitch in ymm12's already-zeroed upper half and silently
    ; drop the first eight samples.
    vinserti128 ymm4, ymm12, xmm13, 1        ; [ packed 0..7 | packed 8..15 ]
    vmovdqu  [r12 + r10], ymm4

    add      r10, 32
    cmp      r10, r13
    jb       .loop

.done:
    mov      [r14], r8                       ; stats.gated
    mov      [r14 + 8], r9                   ; stats.clipped

    ; Horizontal max of the two peak accumulators (8 lanes -> 1).
    vextracti128 xmm6, ymm8, 1
    vpmaxsd  xmm8, xmm8, xmm6
    vpshufd  xmm6, xmm8, 0x4E                ; [2,3,0,1]
    vpmaxsd  xmm8, xmm8, xmm6
    vpshufd  xmm6, xmm8, 0xB1                ; [1,0,3,2]
    vpmaxsd  xmm8, xmm8, xmm6
    vmovd    eax, xmm8
    mov      [r14 + 16], eax                 ; stats.peak_in

    vextracti128 xmm6, ymm9, 1
    vpmaxsd  xmm9, xmm9, xmm6
    vpshufd  xmm6, xmm9, 0x4E
    vpmaxsd  xmm9, xmm9, xmm6
    vpshufd  xmm6, xmm9, 0xB1
    vpmaxsd  xmm9, xmm9, xmm6
    vmovd    eax, xmm9
    mov      [r14 + 20], eax                 ; stats.peak_out

    vzeroupper                               ; never leak dirty upper lanes

%if ABI_WIN64
    movdqu   xmm6,  [rsp + 00h]
    movdqu   xmm7,  [rsp + 10h]
    movdqu   xmm8,  [rsp + 20h]
    movdqu   xmm9,  [rsp + 30h]
    movdqu   xmm10, [rsp + 40h]
    movdqu   xmm11, [rsp + 50h]
    movdqu   xmm12, [rsp + 60h]
    movdqu   xmm13, [rsp + 70h]
    movdqu   xmm14, [rsp + 80h]
    movdqu   xmm15, [rsp + 90h]
    add      rsp, 0A0h
%endif

    pop      r14
    pop      r13
    pop      r12
    pop      rbx
    ret

; A non-executable stack matters to the Linux linker: without this note the
; object implies an executable stack and binutils >= 2.39 refuses to link it.
%ifnidn __OUTPUT_FORMAT__, win64
section ".note.GNU-stack" noalloc noexec nowrite progbits
%endif
