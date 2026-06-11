# -*- coding: utf-8 -*-
# hlas_dsp.py — BIT-FOR-BIT port of hlas.dll's Gintaras voiced-synthesis DSP leaves.
# Faithful integer translations of the disassembly (see engine/RE_NOTES.md 2026-06-05).
# Everything is pure-integer (the DLL uses no FPU here), so a correct port reproduces the
# engine SAMPLE-FOR-SAMPLE. All arrays are Python lists of python ints (int16 range where noted).
#
#   sub_1000EBC0  ebc0()  — period cross-fade / resampler (two source periods -> one output period)
#   sub_1000EDC0  edc0()  — grain copy into the output ring (optional 16.16 volume) + block flush
#   sub_1000EAD0  ead0()  — multi-pass moving-average JOIN DECLICK at a boundary
# Helper trunc2() = the DLL's `add;cdq;sub eax,edx;sar eax,1` = C integer (a+b)/2 toward zero.

I16_MIN, I16_MAX = -0x8000, 0x7fff


def _cdiv(a, b):
    """Signed C division truncating toward zero (x86 idiv semantics)."""
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def trunc2(a, b):
    """The DLL's neighbour-average rounding: trunc((a+b)/2) toward zero (add;cdq;sub;sar 1)."""
    s = a + b
    return -((-s) >> 1) if s < 0 else (s >> 1)


def clamp16(x):
    if x > I16_MAX:
        return I16_MAX
    if x < I16_MIN:
        return I16_MIN
    return x


# ---- sub_1000EBC0 : period cross-fade / resampler -----------------------------------------------
# VALIDATED BIT-FOR-BIT (2026-06-06): vs the engine's real blend inputs (hooked from the live DLL),
# every blend grain reproduced EXACTLY on labas/kaina/gintaras/sveiki/tauta/namas/duona (12/12, 14/14,
# 21/21, 15/15, 4/4, 16/16, 10/10). The src1/src2 the engine passes are the E6B0-filled native periods.
def ebc0(src1, len1, src2, len2, a6, a7, mode):
    """Port of sub_1000EBC0(out, src1, len1, src2, len2, a6, a7, mode) -> (out_list, outlen).
    mode==0  -> plain copy of src1[:len1] (returns len1).
    else     -> blend: OUTLEN = (a7*len1 + (a6-a7)*len2)/a6 ; for each output i,
                out[i] = clamp16( src1[idx1]*a7/a6 + src2[idx2]*(a6-a7)/a6 ),
                idx1=clamp(len1/2 - OUTLEN/2 + i, 0, len1-1), idx2 likewise for src2.
    src1/src2 are int16 lists; len1/len2 are the sample counts actually used."""
    if a6 == 0:
        return [], 0
    if mode == 0:
        return list(src1[:len1]), len1
    w1 = a7                                    # weight toward src1
    w2 = a6 - a7                               # weight toward src2
    outlen = _cdiv(w1 * len1 + w2 * len2, a6)
    if outlen <= 0:
        return [], outlen
    mid = _cdiv(outlen, 2)                      # OUTLEN/2 (trunc toward zero)
    c1 = _cdiv(len1, 2) - mid                   # base offset so src1 centre aligns to output centre
    c2 = _cdiv(len2, 2) - mid
    out = [0] * outlen
    for i in range(0, outlen):                  # asm fills [mid,outlen) then [mid-1,0]; order irrelevant
        i1 = c1 + i
        if i1 >= len1:
            i1 = len1 - 1
        if i1 < 0:
            i1 = 0
        i2 = c2 + i
        if i2 >= len2:
            i2 = len2 - 1
        if i2 < 0:
            i2 = 0
        v = _cdiv(src1[i1] * w1, a6) + _cdiv(src2[i2] * w2, a6)
        out[i] = clamp16(v)
    return out, outlen


# ---- sub_1000EAD0 : join declick (multi-pass moving average) -------------------------------------
# VALIDATED BIT-FOR-BIT (2026-06-06): the engine works on its big pre-zeroed sample ring, so `buf`
# MUST be the ring (a fixed, zero-initialised, padded buffer) and grains written at ABSOLUTE positions
# — NOT a growing list. The declick deliberately reads buf[joinpos] (which EDC0 just wrote, or the
# zero it sets) when smoothing the previous grain's tail; a per-grain growing list drops that sample.
def ead0(buf, fwdpos, joinpos):
    """Port of sub_1000EAD0(buf, fwdpos, joinpos), IN PLACE on the ring `buf` (list of int, padded so
    indices [fwdpos-15 .. joinpos+1] are valid). Three forward 2-tap moving-average passes over widening
    windows centred at fwdpos: [fwdpos-5,fwdpos], [fwdpos-10,fwdpos+1], [fwdpos-15,fwdpos+3]; then
    buf[joinpos]=0; then two backward passes at joinpos: [joinpos-9,joinpos-1], [joinpos-4,joinpos-1].
    Each tap (scan order): buf[j] = trunc2(buf[j-1], buf[j+1]) (uses the already-updated near neighbour)."""
    def fwd(lo, count):                         # write buf[lo .. lo+count-1] left-to-right
        for k in range(count):
            j = lo + k
            buf[j] = trunc2(buf[j - 1], buf[j + 1])
    edx, eax = fwdpos - 5, fwdpos + 1           # asm windows edx=fwd-5/-0xa/-0xf, eax=fwd+1/+2/+4
    if edx < eax:
        fwd(edx, eax - edx)                     # 6 taps  -> buf[fwd-5 .. fwd]
    edx, eax = fwdpos - 0xa, fwdpos + 2
    if edx < eax:
        fwd(edx, eax - edx)                     # 12 taps -> buf[fwd-10 .. fwd+1]
    edx, eax = fwdpos - 0xf, fwdpos + 4
    if edx < eax:
        fwd(edx, eax - edx)                     # 19 taps -> buf[fwd-15 .. fwd+3]

    buf[joinpos] = 0                            # zero the exact join sample

    def bwd(hi, count):                         # write buf[hi], buf[hi-1], ... right-to-left
        for k in range(count):
            j = hi - k
            buf[j] = trunc2(buf[j - 1], buf[j + 1])
    ecx, eax = joinpos - 1, joinpos - 0xa       # asm ecx=join-1, eax=join-0xa then join-5
    if ecx > eax:
        bwd(ecx, ecx - eax)                     # 9 taps  -> buf[join-1 .. join-9]
    ecx, eax = joinpos - 1, joinpos - 5
    if ecx > eax:
        bwd(ecx, ecx - eax)                     # 4 taps  -> buf[join-1 .. join-4]
    return buf


# ---- sub_1000E6B0 (mirror branch) : native pool period -> output grain --------------------------
# VALIDATED (2026-06-06): reproduces every VOICED grain from its native wtvlt1 pool period (50/50 voiced
# grains on labas). The branch taken for Gintaras voiced frames is node+8==0 && node+0xc==-1 (mode 0x41).
# Pipeline: (1) sub_100127f0 cross-grain DETREND — add a decaying ramp to the period start so out[0]
# connects from prev_last (= [this+0xce], the previous emitted grain's last sample); ramp slope =
# D/target -/+ 0x64, applied until it decays to 0. (2) FLAT-PAD from native length to `target` with the
# last native sample (the engine's "mirror" loop copies out[nc-1] forward — a constant pad). (3) SEAM
# DECLICK at nc-1 (same widening moving-average windows as ead0). prev_last chains via the emitted grain.
def e6b0_mirror(native, target, prev_last):
    """native = the node's recorded pitch period (int16 list from the wtvlt1 pool); target = FC80 period
    length; prev_last = the previous emitted grain's final sample ([this+0xce]). Returns the output grain.

    VALIDATED BIT-FOR-BIT (2026-06-06): when nc == target the native period is ALREADY the exact length,
    so the engine emits it VERBATIM — no cross-grain detrend, no flat-pad, no seam declick (proven on the
    long pause grains n=2200/2800 of labas, which equal their native pool PCM exactly). The detrend +
    flat-pad + seam-declick path runs only when the native must be stretched (nc < target)."""
    nc = len(native)
    if nc >= target:                                # native already >= target -> verbatim (no detrend/
        return list(native)                         # pad/declick); emits the FULL native length (nc).
    out = list(native)                              # (nc==target pause grains AND nc>target long frames)
    D = prev_last - out[0]                          # sub_100127f0 second block (cross-grain detrend)
    if D != 0:
        slope = _cdiv(D, target)
        if D >= 0:
            step = slope + 0x64; esi = D; k = 0
            while esi >= 0 and k < len(out):
                out[k] = clamp16(out[k] + esi); esi -= step; k += 1
        else:
            step = slope - 0x64; esi = D; k = 0
            while esi < 0 and k < len(out):
                out[k] = clamp16(out[k] + esi); esi -= step; k += 1
    if nc < target:                                 # flat-pad to target with the last native sample
        out = out + [out[nc - 1]] * (target - nc)
    # SEAM DECLICK (sub_1000E837 @0x1000E8A2): three widening 2-tap moving-average passes around seam=nc-1,
    # but each pass is SKIPPED WHOLE when its right edge would reach/exceed `target` (asm `cmp eax,esi; jge
    # skip`, esi = the fc80 target period) — NOT clamped per-sample. So a near-target mirror grain (native
    # within 1-3 of target, e.g. darbas's 292/294) drops the ±15 (and maybe ±10) pass, leaving the period
    # tail un-over-smoothed. Lower bound: pass runs only if seam > {5,0xa,0xf}.
    seam = nc - 1
    for half, hi_off in ((5, 1), (0xa, 2), (0xf, 4)):
        if seam > half and seam + hi_off < target:   # else the engine skips this pass entirely
            for j in range(seam - half, seam + hi_off):
                out[j] = trunc2(out[j - 1], out[j + 1])
    return out[:target]


def render_from_grains(grains, vol=0xffff, flag=1, pad=64, flush_idx=None):
    """BIT-FOR-BIT back-end: turn the engine's per-EDC0 grain stream into the final PCM, reproducing
    sub_1000EDC0's ring write + per-grain sub_1000EAD0 declick EXACTLY. `grains` = list of int16 lists
    (one recorded/blended pitch period per EDC0 call, in order). Returns the int16 list == engine WAV.
    The EDC0 declick guard tests the RING write index b320 ([+0xb320]); the declick fwdpos = ebx, seeded from
    b320 at the grain's entry and decremented by BLOCKP at each mid-grain flush (asm `add ebx, 0x400-b320`).
    sub_1000EDC0 flushes whenever b320 reaches [+0x11c]+0x400 = BLOCKP(22144)+0x400 = 23168 (verified vs the
    captured pos field on every word): it emits BLOCKP samples, slides the kept 0x400 overlap to the ring
    start, and sets b320=0x400. A grain that crosses 23168 while starting BELOW 22144 (e.g. lietuviškai's long
    closure at b320=21246) drives ebx negative (21246-22144=-898) so its declick is SKIPPED (guard ebx>0x1e
    fails) -> the join is NOT smoothed, matching the engine. A grain starting >=22144 keeps ebx>0x1e and its
    declick maps to the same OUTPUT region as the linear position (so gintaras, which crosses 23168 at its
    tail, stays bit-exact). Word-boundary `flush_idx` grains reset b320=0 (the segment-end full flush -> the
    next word's onset is at b320 0 <=0x1e, not declicked -> sharp onset). The linear `buf`/ead0 positions are
    output coordinates; only the GUARD uses the wrapped b320/ebx (the modified output region coincides).
    Validated == _eng_<word>.wav sample-for-sample on labas/kaina/gintaras/sveiki/tauta/namas/duona + lietuviškai."""
    BLOCKP = 22144; KEEP = 0x400; THRESH = BLOCKP + KEEP   # [+0x11c]=22144 (Gintaras); flush at b320==23168
    total = sum(len(g) for g in grains)
    buf = [0] * (total + pad)                    # the pre-zeroed output (linear; == the flushed stream in order)
    scale = (flag != 0) and (vol != 0xffff)
    flush_idx = flush_idx or ()
    pos = 0                                       # cumulative OUTPUT write position into buf
    b320 = 0                                      # EDC0 ring write index (wraps via the 23168 flush)
    for gi, g in enumerate(grains):
        if gi in flush_idx:                      # segment-end full flush before the next word -> ring restarts
            b320 = 0
        ebx = b320                               # declick fwdpos seed = ring index at this grain's entry
        n = len(g)
        for i in range(n):                       # EDC0 sample write (optional 16.16 volume)
            buf[pos + i] = clamp16((g[i] * vol) >> 16) if scale else g[i]
            b320 += 1
            if b320 == THRESH:                   # ring flush: emit BLOCKP, slide 0x400 overlap, b320=0x400
                b320 = KEEP
                ebx -= BLOCKP                    # asm: ebx += (0x400 - 23168) = ebx - 22144
        if ebx > 0x1e:                           # EDC0 guard on the (wrapped) ring index -> negative = skip
            ead0(buf, pos, pos + n)              # fwdpos = pos_before, joinpos = pos_after (output coords)
        pos += n
    return buf[:total]


# ---- sub_1000EDC0 : grain copy into the output stream + per-boundary declick ----------------------
def edc0_copy(out, grain, n, vol=0xffff, flag=0):
    """Port of the SAMPLE-WRITE part of sub_1000EDC0(this, buf, n): append n grain samples to the
    growing output list `out`, optionally 16.16-volume-scaled. The DLL keeps a sliding 0x800 ring +
    block flush — that's pure buffering and does NOT change the emitted sample VALUES, so for an
    offline port we append straight to `out`. Returns the boundary position (len(out)) BEFORE this
    grain, i.e. where the join with the previous grain sits (used by the declick).
    vol==0xffff or flag==0 -> verbatim copy (the measured Gintaras case)."""
    boundary = len(out)
    scale = (flag != 0) and (vol != 0xffff)
    for i in range(n):
        s = grain[i]
        if scale:
            s = (s * vol) >> 16                  # 16.16 fixed-point volume (movsx;imul;shr 16)
        out.append(clamp16(s) if scale else s)
    return boundary


if __name__ == "__main__":
    # self-test of trunc2 / clamp / cdiv against the exact x86 semantics on a few values
    assert trunc2(3, 4) == 3 and trunc2(-3, -4) == -3 and trunc2(-1, 0) == 0 and trunc2(-3, 0) == -1
    assert _cdiv(-7, 2) == -3 and _cdiv(7, 2) == 3 and _cdiv(-7, -2) == 3
    assert clamp16(40000) == 0x7fff and clamp16(-40000) == -0x8000
    print("hlas_dsp self-test OK")
