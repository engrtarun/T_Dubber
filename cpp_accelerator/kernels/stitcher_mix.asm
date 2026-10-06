; ===========================================================================
;  kernels/stitcher_mix.asm -- the timeline mixer, in three AVX2 passes
;
;  WHAT THIS IS
;  ------------
;  The second CPU-bound inner loop in the T_Dubber pipeline, and the one that
;  scales with runtime: it runs once per dub, over the WHOLE timeline, for
;  every frame of the finished video.
;
;  stitcher/src/main.rs walks that timeline with three hand-written scalar
;  loops:
;
;      1. background lay   for (i, s) in mix.iter_mut() { *s = bg[i % bg.len()] * bg_vol }
;      2. ducking          for s in &mut mix[start..end] { *s *= DUCK_FACTOR }
;      3. voice overlay    for (i, &v) in voice.iter().take(take) { mix[off+i] += v }
;
;  Three things in there are worth an assembly rewrite, and none of them is
;  "the compiler is slow":
;
;    * `i % bg.len()` is an integer division PER SAMPLE. On a 2-hour feature
;      film at 24 kHz that is 172.8M divisions; on the 107 s trailer this was
;      written against it is 2.57M. A 64-bit idiv is 20-40 cycles and does
;      not pipeline. Removing the modulo is worth more than the vectorising.
;    * every one of the three loops is purely elementwise -- multiply, scale,
;      add -- so 8 samples fit in one ymm with no arithmetic change at all.
;      The results are bit-identical to the scalar loops, not "close".
;    * the Rust loops must round-trip the whole buffer through memory three
;      times, once per stage.
;
;  WHY THREE ENTRY POINTS AND NOT ONE FUSED KERNEL
;  ---------------------------------------------
;  A single fused pass is possible -- the whole thing is elementwise -- but it
;  has to cut the timeline at every background wrap, every duck-span boundary
;  and every voice-segment boundary, and carry four cursors plus two cached
;  ends through that loop. That is a lot of control flow whose only failure
;  mode is a subtly wrong sample in a shipped video. These three passes keep
;  each loop trivial to verify against its scalar twin, and they still remove
;  every division and every non-SIMD op. The division was the expensive part.
;
;      void td_stitcher_lay(const TdStitcherLayParams *p);
;      void td_stitcher_duck(const TdStitcherDuckParams *p);
;      void td_stitcher_add_voice(const TdStitcherAddVoiceParams *p);
;
;  ONE SOURCE, TWO ABIS
;  --------------------
;  Same reason as normalizer_gate_gain.asm, and the same trap. An argument list
;  holding several floats puts them in different registers under System V
;  (queued BY TYPE: nth float -> xmm0, xmm1, ...) than under Windows (BY
;  POSITION: position 4 -> xmm3, position 5+ -> stack). Get it wrong and the
;  kernel broadcasts a pointer as a gain factor: it assembles, it links, and
;  it scales the wrong audio. One pointer per entry point means one register
;  -- rcx on Windows, rdi on System V -- and no convention left to disagree.
;
;      nasm -f elf64 kernels/stitcher_mix.asm   # Kaggle / Linux
;      nasm -f win64 kernels/stitcher_mix.asm   # Windows / MSVC
;
;  CONTRACT
;  --------
;  Shared by all three, and each is also restated at its own definition:
;
;  * Nothing reads or writes outside the range it is given. The layout pass
;    writes exactly `total` samples; the other two only touch what they are
;    told to.
;  * All three require float samples (f32), mono. The caller saturates to
;    int16 on write, which is why none of these needs a min/max.
;  * `total` samples are ALWAYS written, including when there is no
;    background at all -- a short buffer is a corrupt WAV, so the empty
;    background case writes silence rather than skipping.
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
; %macro PROLOGUE -- push the callee-saved registers these loops use.
;
; rbx, rbp and r12-r15 are callee-saved on BOTH ABIs, so one push list covers
; both. No vector register needs saving: the loops below stay inside
; ymm0/ymm1/ymm2 and x0-x5, all of which are volatile everywhere. That is the
; one place these kernels are simpler than normalizer_gate_gain.asm, whose
; KERNEL_VEC macro needs ymm6..ymm15 and therefore has to park xmm6-xmm15 on
; Windows.
;
; rsi AND rdi ARE pushed, and this is not optional. They are non-VOLATILE on
; Windows x64 but volatile on System V, so a kernel that clobbers them and
; pushes them anyway is correct on both -- and one that clobbers them WITHOUT
; pushing is correct only on Linux. That asymmetry hides until a Windows caller
; loses a pointer it was holding in rdi: the symptom is not a wrong sample but a
; heap corruption (0xC0000374) at some unrelated point later, which is exactly
; how this was found. The three loops here all use rdi as a computed base and
; rdi/rsi as cursors, so both are saved unconditionally.
;
; Eight pushes is 64 bytes, so rsp stays 16-byte aligned without the extra
; `sub rsp, 8` the earlier six-push version needed.
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
; %macro SCALE_VEC 3 -- 8 samples at *src, scaled by xmm<3>, stored to *dst
;
;   vmovups / vmulps / vmovups. The factor is already broadcast in a ymm.
; ---------------------------------------------------------------------------
%macro SCALE_VEC 3
    vmovups     %1, [%2]
    vmulps      %1, %1, %3
    vmovups     [%1], %1
%endmacro

; ===========================================================================
;  td_stitcher_lay -- loop the background into the timeline, scaled
;
;  TdStitcherLayParams (stitcher_mix.h):
;      +0  out       (8)  f32 timeline, `total` samples, always fully written
;      +8  bg        (8)  f32 background, may be NULL/empty
;      +16 bg_len    (8)  background samples
;      +24 total     (8)  samples to produce
;      +32 bg_vol    (4)  float
;
;  Note the field order: `total` is a u64 and has to sit on 8-byte alignment,
;  so the float comes after it rather than before. The header's static_asserts
;  are what pinned this down; guessing would have read a pointer's low half as
;  a gain factor.
;
;  This is the pass that kills the per-sample modulo. The wrap is handled by
;  copying `min(bg_left, timeline_left)` samples per inner run and then
;  RESETTING the read pointer, so the phase is continuous across every run
;  boundary and no division is ever executed. `bg_len` may exceed `total` (no
;  wrap at all) or not divide it (wrap lands mid-chunk) -- both are just the
;  loop running twice.
;
;  Scalars match `bg[i % bg_len] * bg_vol` exactly: one multiply per sample,
;  same operand order, no re-association.
; ===========================================================================
global td_stitcher_lay

td_stitcher_lay:
    PROLOGUE

%if ABI_WIN64
    mov         r10, rcx                      ; const TdStitcherLayParams *
%else
    mov         r10, rdi
%endif

    mov         rbx, [r10 + 0]               ; out (advances)
    mov         rbp, [r10 + 8]               ; bg base (kept for the wrap)
    mov         r12, rbp                     ; bg read cursor
    mov         r13, [r10 + 16]              ; bg_len, in samples
    mov         r14, [r10 + 24]              ; total
    mov         eax, [r10 + 32]              ; bg_vol bits
    vmovd       xmm0, eax
    vbroadcastss ymm0, xmm0                  ; ymm0 = bg_vol in all 8 lanes

    test        r13, r13
    jz         .lay_silence                   ; no background: write silence

    xor         r15d, r15d                   ; samples produced so far

.lay_run:
    cmp         r15, r14
    jae         .lay_done

    ; Samples in this run: the smaller of "timeline left" and "background
    ; left". This min() is what makes the wrap seamless without a modulo.
    mov         rax, r14
    sub         rax, r15
    cmp         rax, r13
    cmova      rax, r13

    mov         rcx, rax
    shl         rcx, 2                       ; run bytes

    ; The run length in bytes is kept in a REGISTER, not at [rsp].
    ;
    ; The earlier version stored it at [rsp], which is the red-zone scratch that
    ; Linux allows a leaf function to use and Windows does not. Here it was
    ; simply wrong on both: after the prologue's pushes, [rsp] holds the saved
    ; rbx, so this store overwrote the caller's rbx with a byte count. On Linux
    ; it happened to be harmless only because the callee then reloaded rbx from
    ; memory before use; the symptom was a wrong base pointer and heap damage.
    ;
    ; r9 is volatile on both ABIs and holds nothing else in this loop.
    mov         r9, rcx                      ; run bytes, for the advance below

    ; --- vector body: floor(n/8) groups of 8 ---
    ;
    ; The entry test is BEFORE the body, not at the bottom. NASM has no
    ; while-loop construct, so a bottom-tested loop runs its body once even
    ; when the count is zero -- and one unguarded `vmovups` is a 32-byte store
    ; into a buffer that may be 4 bytes long. That is a heap corruption, not a
    ; wrong sample, so it has to be guarded rather than merely bounded.
    mov         rdx, rax
    and         rdx, -8
    shl         rdx, 2
    xor         r8, r8
    test        rdx, rdx
    jz         .lay_after_vec
.lay_vec:
    vmovups     ymm1, [r12 + r8]
    vmulps      ymm1, ymm1, ymm0
    vmovups     [rbx + r8], ymm1
    add         r8, 32
    cmp         r8, rdx
    jb          .lay_vec
.lay_after_vec:

    ; --- scalar remainder: 0..7 samples ---
    cmp         r8, rcx
    jae         .lay_advance
.lay_tail:
    vmovss      xmm1, [r12 + r8]
    vmulss      xmm1, xmm1, xmm0
    vmovss      [rbx + r8], xmm1
    add         r8, 4
    cmp         r8, rcx
    jb          .lay_tail

.lay_advance:
    add         rbx, r9                      ; run bytes
    add         r12, r9
    add         r15, rax
    sub         r13, rax
    jnz         .lay_run                     ; more background left
    mov         r12, rbp                     ; wrap: reset to the start
    mov         r13, [r10 + 16]              ; ... and its full length
    jmp         .lay_run

.lay_silence:
    ; No background at all. The timeline still has to exist in full, so this
    ; writes zeros rather than leaving `out` undefined for the caller to read
    ; back as a truncated WAV.
    ;
    ; The vector/tail split below is the same shape as .lay_run on purpose. An
    ; earlier version zeroed whole ymm registers straight through and overran
    ; `out` by up to 7 samples whenever total % 8 != 0 -- total=9 wrote samples
    ; 8..15 of a 9-sample buffer. Bounding the vector body by
    ; floor(total/8)*8 is what keeps the last store inside the allocation.
    mov         rbx, [r10 + 0]
    mov         r14, [r10 + 24]
    xor         r15d, r15d                   ; samples written

.lay_zero_vec:
    ; floor(total/8) groups of 8.
    ;
    ; The bound is computed as total AND -8 rather than as (total - 7) AND -8.
    ; The subtraction wraps to a huge UNSIGNED value when total < 7, and the
    ; loop test below is an unsigned compare, so the wrapped bound looked like
    ; "almost 2^64 samples left" and the first store landed on a null pointer.
    mov         rax, r14
    and         rax, -8
    cmp         r15, rax
    jae         .lay_zero_tail
    vxorps      ymm1, ymm1, ymm1
    vmovups     [rbx + r15 * 4], ymm1
    add         r15, 8
    jmp         .lay_zero_vec

.lay_zero_tail:
    ; the remaining 0..7 samples, one at a time
    cmp         r15, r14
    jae         .lay_done
    vxorps      xmm1, xmm1, xmm1
    vmovss      [rbx + r15 * 4], xmm1
    add         r15, 1
    jmp         .lay_zero_tail

.lay_done:
    EPILOGUE

; ===========================================================================
;  td_stitcher_duck -- scale the ducked spans down
;
;  TdStitcherDuckParams (stitcher_mix.h):
;      +0  out        (8)  f32 timeline, modified in place
;      +8  duck_gain  (4)  float
;      +16 spans      (8)  TdMixSpan[]: { uint64 start; uint64 end; }
;      +24 span_count (8)
;
;  `out[start..end] *= duck_gain` for every span. Identical to the Rust
;  `for sample in &mut mix[start..end] { *sample *= DUCK_FACTOR }`, including
;  for a span whose length is not a multiple of 8 (the remainder goes through
;  the scalar tail).
;
;  PRECONDITION: every span must lie inside the timeline. This struct carries no
;  length, so the kernel has no way to clamp, and a span reaching past the end
;  writes past the buffer. Rust's range slice would have panicked instead --
;  so this is that same requirement, previously only implicit.
;
;  The spans are NOT re-read after this pass, so unlike the fused layout pass
;  they need no sorting guarantee: overlapping spans simply scale twice,
;  which is what the Rust does too.
; ===========================================================================
global td_stitcher_duck

td_stitcher_duck:
    PROLOGUE

%if ABI_WIN64
    mov         r10, rcx                      ; const TdStitcherDuckParams *
%else
    mov         r10, rdi
%endif

    mov         rbx, [r10 + 0]               ; out base
    mov         eax, [r10 + 8]                ; duck_gain bits
    vmovd       xmm0, eax
    vbroadcastss ymm0, xmm0
    mov         r12, [r10 + 16]              ; spans
    mov         r13, [r10 + 24]              ; span_count

    xor         r14d, r14d                   ; span index

.duck_span:
    cmp         r14, r13
    jae         .duck_done

    ; x86 addressing only scales an index by 1, 2, 4 or 8, so the 16-byte
    ; span stride has to be formed in a register.
    ;
    ; index * 16 is (index * 2) << 3. The first version of this line computed
    ; (index * 2) * 3 * 4 = index * 24 instead, which is right for index 0 and
    ; silently reads past the array for every index after it -- so span 1 came
    ; from offset 24, and the garbage there was used as start/end and scaled
    ; the timeline out of bounds. tests/mix_equivalence_test.cpp caught it on
    ; the second span.
    lea         rax, [r14 + r14]
    shl         rax, 3                         ; rax = span index * 16
    mov         rsi, [r12 + rax + 0]          ; start
    mov         rdx, [r12 + rax + 8]          ; end

    ; An empty or inverted span contributes nothing. The Rust range slice
    ; would panic on start > end, so treating it as empty is the safe reading
    ; and keeps one bad span from taking the whole run down.
    cmp         rdx, rsi
    jbe         .duck_next

    lea         rdi, [rbx + rsi * 4]         ; span base pointer

    mov         rax, rdx
    sub         rax, rsi                      ; samples in this span
    mov         rcx, rax
    shl         rcx, 2                       ; span bytes

    ; --- vector body ---
    ; Entry test first, same reason as .lay_vec: an unguarded ymm store would
    ; write 32 bytes into a span shorter than that.
    mov         rdx, rax
    and         rdx, -8
    shl         rdx, 2
    xor         r8, r8
    test        rdx, rdx
    jz         .duck_after_vec
.duck_vec:
    vmovups     ymm1, [rdi + r8]
    vmulps      ymm1, ymm1, ymm0
    vmovups     [rdi + r8], ymm1
    add         r8, 32
    cmp         r8, rdx
    jb          .duck_vec
.duck_after_vec:

    ; --- scalar remainder ---
    cmp         r8, rcx
    jae         .duck_next
.duck_tail:
    vmovss      xmm1, [rdi + r8]
    vmulss      xmm1, xmm1, xmm0
    vmovss      [rdi + r8], xmm1
    add         r8, 4
    cmp         r8, rcx
    jb          .duck_tail

.duck_next:
    inc         r14
    jmp         .duck_span

.duck_done:
    EPILOGUE

; ===========================================================================
;  td_stitcher_add_voice -- overlay one TTS segment
;
;  TdStitcherAddVoiceParams (stitcher_mix.h):
;      +0  out    (8)  f32 timeline, modified in place
;      +8  voice  (8)  f32 segment
;      +16 count  (8)  samples to add
;      +24 dest   (8)  sample offset in the timeline
;
;  `out[dest + i] += voice[i]` for i in 0..count. Matches the Rust
;  `mix[start_idx + i] += v`, including the truncation to `take` that the
;  caller does before calling: this kernel adds exactly `count` samples and
;  trusts that the caller already clamped `count` to what fits, so it never
;  writes past the timeline. That clamp is the caller's to keep, and the
;  header says so.
;
;  vaddps is the same IEEE add as Rust's `+=`, in the same order, so a caller
;  that overlays several segments must do it in the same order either way.
; ===========================================================================
global td_stitcher_add_voice

td_stitcher_add_voice:
    PROLOGUE

%if ABI_WIN64
    mov         r10, rcx                      ; const TdStitcherAddVoiceParams *
%else
    mov         r10, rdi
%endif

    mov         rbx, [r10 + 0]               ; out base
    mov         r12, [r10 + 8]               ; voice
    mov         r13, [r10 + 16]              ; count
    mov         r14, [r10 + 24]              ; dest

    test        r13, r13
    jz         .voice_done
    test        r12, r12
    jz         .voice_done

    lea         rdi, [rbx + r14 * 4]         ; destination pointer

    mov         rcx, r13
    shl         rcx, 2                       ; count bytes

    ; --- vector body ---
    ; Entry test first, same reason as .lay_vec.
    mov         rdx, r13
    and         rdx, -8
    shl         rdx, 2
    xor         r8, r8
    test        rdx, rdx
    jz         .voice_after_vec
.voice_vec:
    vmovups     ymm1, [r12 + r8]
    vmovups     ymm2, [rdi + r8]
    vaddps      ymm1, ymm1, ymm2
    vmovups     [rdi + r8], ymm1
    add         r8, 32
    cmp         r8, rdx
    jb          .voice_vec
.voice_after_vec:

    ; --- scalar remainder ---
    cmp         r8, rcx
    jae         .voice_done
.voice_tail:
    vmovss      xmm1, [r12 + r8]
    vmovss      xmm2, [rdi + r8]
    vaddss      xmm1, xmm1, xmm2
    vmovss      [rdi + r8], xmm1
    add         r8, 4
    cmp         r8, rcx
    jb          .voice_tail

.voice_done:
    EPILOGUE

; A non-executable stack matters to the Linux linker: without this note the
; object implies an executable stack and binutils >= 2.39 refuses to link it.
%ifnidn __OUTPUT_FORMAT__, win64
section ".note.GNU-stack" noalloc noexec nowrite progbits
%endif