# gen_synth.py - GENERATIVE bit-for-bit Gintaras synthesis back-end.
#
# Given the front-end control sequence for a word, this regenerates the engine's exact PCM output using
# only the ported integer laws (no engine, no DLL):
#   * frame BACKBONE: the ordered list of wtvlt1 pool pitch-periods = concatenation of each selected unit
#     key's parse_units fid-list (proven == the engine's native-frame order, 46/46 on labas).
#   * per-frame PITCH: s94 integer IIR  s94 += (s90-s94)>>4 (+1 if s90>s94); period = clamp(s94+294,169,294).
#   * per-frame EPOCHS (sub_1000E410): e4 += 220/frame and f8 += period/epoch, BOTH continuous; place an
#     epoch while  trunc((f8+period)*100 / e4)  <= 150 (first epoch) / < 150 (subsequent); s94/period
#     advance per placed epoch.
#   * per-epoch GRAIN (sub_1000E960): src1 = e6b0(current frame), src2 = e6b0(next frame); EBC0 blend with
#     a6 = #epochs, a7 = #epochs - i, mode 1 (i=0 => src1 only; later i => crossfade toward the next frame).
#   * e6b0: native >= target -> verbatim (full native length); native < target -> mirror (detrend + flat-pad
#     + seam declick).  Render: EDC0 ring write + EAD0 per-boundary declick (hlas_dsp.render_from_grains).
#   * VERBATIM frame (E960 marker a6==0: e6b0 mode 0 pause/closure OR mode 0x80 short unit) -> emitted
#     whole at native length, leaves e4/f8/s94 untouched.
#   * trailing SILENCE -> zero grains.
#
# A `Plan` bundles the control sequence (front-end output). build_plan_from_capture() derives one from the
# instrumented engine for validation; the eventual transcr4 front-end port produces the same Plan directly.
import struct, sys, os
from . import dsp as H

THR = 150                    # default epoch-gate threshold = 30000/rate_period at the natural rate_period=Dbase=200
BASE_P = 22050 // 100        # = 220 added to e4 per voiced E410 pass (=[+0x1ad78]-derived; rate-INDEPENDENT)
USE_PSOLA_PITCH = False       # pitch method: False = engine-LEGACY period-scaling (default); True = formant-
                              # preserving PSOLA repitch (raising only). KEPT for future use. (cont.56/57)

# ---- pitch / rate control ----------------------------------------------------------------------
# hlas.dll exposes SetRate/SetPitch but only RATE actually drives synthesis (pitch is a reported-but-unwired
# stub); see memory/pitch-rate-control.md. RATE is the EPOCH-GATE THRESHOLD: instrumenting sub_1000E410 (hook
# _e410.bin field a1) shows the e4 clock advances by 220/pass at EVERY rate -- what rate changes is the gate
# threshold a1 = [+0xd4] = 30000/rate_period (natural=150). Lower threshold => the gate closes sooner =>
# fewer epochs per frame => shorter (faster) audio. Captured: engine rate=400 => a1=37 => 0.51x. We reproduce
# that EXACTLY by scaling THR; pitch is IMPLEMENTED FRESH (period scale) since the DLL never wired it.
DMIN, DMAX, DBASE = 100, 800, 200            # rate period [+0x1ad7c]/[+0x1ad80]/Dbase [+0x1ad84] (neutral=Dbase)
_PMID = 232.0                                # pitch_period(50) = the neutral period (~95 Hz center)

# NEUTRAL (bit-exact) sentinel: rate=None / pitch=None => no scaling, byte-identical to the engine default
# (rate_period=Dbase=200 => THR=150; pitch is an unwired stub => factor 1.0).
def rate_thr(r):
    """Host rate r in 0..100 -> the epoch-gate threshold THR = round(30000/rate_period) (engine field a1/[+0xd4]).
    Centered so r=50 = neutral (rate_period=Dbase=200 => THR=150 => bit-exact). r<50 lowers rate_period toward
    Dmin=100 (THR up to 300 => slower); r>50 raises it toward Dmax=800 (THR down to ~37 => faster, ~0.5x at 100,
    matching the engine's measured rate=400). r=None -> THR=150 (natural)."""
    if r is None:
        return THR
    if r <= 50:
        rate_period = DMIN + (DBASE - DMIN) * r / 50.0          # 100..200 over r=0..50
    else:
        rate_period = DBASE + (DMAX - DBASE) * (r - 50) / 50.0  # 200..800 over r=50..100
    return round(30000.0 / rate_period)

def pitch_factor(p):
    """Host pitch p in 0..100 -> F0-period multiplier. Intended engine curve pitch_period(p)=294.55-1.25*p
    (p=0->75Hz, 50->95Hz, 100->130Hz), normalised so p=50 is identity. <1 raises pitch, >1 lowers it.
    p=None or p=50 -> 1.0 (identity; bit-exact)."""
    if p is None:
        return 1.0
    return round(294.55 - 1.25 * p) / _PMID


def _trunc(a, b):            # truncate toward zero (the FPU round helper sub_10022d90 sets RC=11)
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def _iir(s94, s90):          # sub_1000FDA0 pitch smoother (per placed epoch)
    x = s90 - s94
    x = (x >> 4) + 1 if x > 0 else (x >> 4)
    return s94 + x


def _period(s94):            # sub_1000FC80 voiced period = clamp(s94 + maxP, minP, maxP)
    v = s94 + 294
    return 169 if v < 169 else 294 if v > 294 else v


def _recon_s90(s94_0, periods):
    """Reconstruct the exact per-frame s90 from a voiced frame's CAPTURED epoch periods (ground truth).
    The engine holds one fixed s90 per frame; the per-epoch IIR (_iir) climbs s94 toward it, and each epoch's
    period = _period(s94). Given the captured periods, search for the s90 that reproduces them EXACTLY when
    simulated through the same IIR+_period laws. Returns that s90, or None if none reproduces (caller then
    keeps the nxt_s90 capture-lag heuristic). Because we only override when the simulation reproduces the
    captured periods bit-for-bit, the synthesized output for that frame is IDENTICAL by construction -> this
    can only FIX frames the heuristic got wrong (e.g. cross-word multi-epoch frames), never degrade a correct
    one. Single-/no-epoch frames don't exercise the within-frame IIR, so s90 is immaterial -> return None."""
    if len(periods) < 2:
        return None
    if _period(s94_0) != periods[0]:             # reseed s94 must match the first captured period
        return None
    for s90 in range(s94_0 - 32, s94_0 + 33):    # deltas are tiny (+-2/epoch); a small window suffices
        s = s94_0
        ok = True
        for tgt in periods[1:]:
            s = _iir(s, s90)
            if _period(s) != tgt:
                ok = False
                break
        if ok:
            return s90
    return None


def _e6b0(native, target, prev_last):
    # sub_1000E6B0 voiced path (mode 0x41). native < target -> the e6b0 mirror (stretch). native >= target
    # -> sub_1000E837 ([ebx+8]==0 && [ebx+0xc]==-1, confirmed for every Gintaras voiced frame): memcpy the
    # FULL native, run sub_100127f0 (the cross-grain detrend), then return X = target -> emit out[:target].
    # (gen_synth only calls _e6b0 for VOICED frames -- pauses emit their pcm directly -- and the demo words
    # are all native < target, so this path is only hit by the low-pitched 'á' frames of darbas/rytas/vakaras.)
    #
    # cont.25: DISASSEMBLED sub_100127f0(out, a1=1, a2=1, nc, X=target). It is TWO detrend ramps, BOTH with
    # divisor X (= the target period, NOT nc -- my earlier block2 wrongly divided by nc):
    #   BLOCK A (END detrend, runs only when nc > X): D = out[nc-1] - out[X-1]; ramp out[X-1] BACKWARD by esi
    #     (esi from D toward 0 by step = cdiv(D,X) -/+ 0x64) until esi crosses 0. This pulls out[X-1] toward
    #     the TRUE period end out[nc-1] before the [:X] truncation -> out[X-1] == native[nc-1] (the engine's
    #     observed last sample, e.g. darbas call16 out[293] = native[294] = -280, NOT the truncated -1886).
    #   BLOCK B (START detrend, a2!=0): D = prev_last - out[0]; ramp out[0] FORWARD the same way (block2).
    # Order in the DLL is A then B; they touch disjoint ends so order is immaterial for the short ramps.
    # The missing BLOCK A is exactly why truncation/block2-only mis-set the period seam and the prev cascade
    # diverged at the first nc>=target frame. nc<target words never enter here -> zero mirror-path change.
    if len(native) >= target:
        nc = len(native)
        X = target
        o = list(native)                              # memcpy FULL native (nc samples)
        if nc > X:                                    # BLOCK A: end detrend toward the true period end
            D = o[nc - 1] - o[X - 1]
            if D != 0:
                step = H._cdiv(D, X) + (-0x64 if D < 0 else 0x64)
                esi = D
                k = X - 1
                while (esi < 0 if D < 0 else esi >= 0) and k >= 0:
                    o[k] = H.clamp16(o[k] + esi)
                    esi -= step
                    k -= 1
        D = prev_last - o[0]                           # BLOCK B: start detrend (cross-grain connect)
        if D != 0:
            step = H._cdiv(D, X) + (-0x64 if D < 0 else 0x64)
            esi = D
            k = 0
            while (esi < 0 if D < 0 else esi >= 0) and k < nc:
                o[k] = H.clamp16(o[k] + esi)
                esi -= step
                k += 1
        return o[:X]
    return H.e6b0_mirror(native, target, prev_last)


# --- sub_1000FDA0 F0-contour (the transcr4->hlas intonation bridge, [0x186d7f40]==0 branch) -------------
# s90 = se8/8 - s118 - s100 (s118=s100=10 => s90 = se8//8 - 20). se8 ramps in fda0's tail loop by SD2 per
# SEC units of cumulative voiced sample position, clamped to [-s108, S104], and only after the post-stress
# release node arms it (sd2 0->20, s104 0->160). In gen_synth's f8 (cumulative period) coordinate:
SE8_SD2 = 20            # [self+0xd2] step per ramp iteration
SE8_SEC = 100          # [self+0xec] sample-position units per iteration
SE8_HI = 160           # [self+0x104] high clamp (=> s90 reaches 0)
SE8_BASE = 20          # s118 + s100 (both 10)


def _s90_ramp(f8, release):
    """The engine's per-epoch s90 = se8//8 - 20, with se8 = min(160, 20*(f8-release)//100) for f8>=release.
    Before the release (release is None or f8<release) se8=0 => s90 = -20 (the stressed-syllable target)."""
    if release is None or f8 < release:
        se8 = 0
    else:
        se8 = SE8_SD2 * ((f8 - release) // SE8_SEC)
        if se8 > SE8_HI:
            se8 = SE8_HI
    return (se8 >> 3) - SE8_BASE


class Plan:
    """The front-end control sequence for one utterance.
    frames : list of dicts, one per E960 segment in order, each:
        {'pcm': int16 list (the native pool pitch-period for this frame),
         's90': int (per-frame pitch target from the FDA0 flag-build),
         's94': int (the smoothed-pitch seed; used at frame 0 and at each post-pause reseed),
         'pause': bool (True => emit pcm verbatim, no pitch/epoch advance),
         'reseed': bool (True => load 's94' as the smoothed-pitch state for this frame),
         'release': bool (optional; True on the first post-stress voiced frame => arm the se8 fall ramp)}
    tail_silence : list of int (lengths of trailing all-zero grains).
    se8_ramp : bool (when any frame carries 'release', synthesize computes s90 PER EPOCH from the se8 ramp
        law _s90_ramp(f8, f8_release) instead of the per-frame constant 's90' -- the fully-generative
        intonation path; the capture-derived Plan leaves it off and keeps the captured per-frame s90)."""
    def __init__(self, frames, tail_silence):
        self.frames = frames
        self.tail_silence = tail_silence
        self.se8_ramp = any(f.get('release') for f in frames)


def synthesize(plan, rate=None, pitch=None, _frame_rpos=None):
    """Render a Plan to the final int16 PCM, purely via the ported laws.
    rate/pitch are host 0..100 knobs (None = engine-natural, bit-exact). rate scales the e4 duration clock
    (faithful port of the hlas 2*Dbase/rate_period law); pitch scales the F0 epoch period (the DLL leaves
    pitch unwired -- this is the fresh implementation noted in memory/pitch-rate-control.md)."""
    frames = plan.frames
    # rate=None => use the capture's own a1 (natural=150 => bit-exact; a FAST capture carries its THR<150 =>
    # renders fast bit-exact). An explicit host rate (0..100) overrides via rate_thr (50=neutral).
    thr = getattr(plan, 'cap_thr', THR) if rate is None else rate_thr(rate)
    pfac = pitch_factor(pitch)                      # pitch=None/50 -> 1.0 (bit-exact)
    # release_rpos: arm the se8 fall at an EXACT output sample (the engine's true arm = cumulative output of
    # the chars before arm_char, recovered from the per-grain char hook). Overrides the per-frame 'release'
    # flag. None => frame-based arm / captured contour (the bit-exact demo path is unaffected => 14/14 held).
    release_rpos = getattr(plan, 'release_rpos', None)
    # MULTI-WORD: a LIST of EXACT per-word arm output samples (build_plan_phrase). One arm per word; the arm
    # pointer advances at each 'word_start'. Sample-exact like the single-word release_rpos (vs the coarse
    # per-frame 'release' flag). None => single-word path unchanged.
    arm_list = getattr(plan, 'release_rpos_list', None)
    arm_i = 0
    ramp = getattr(plan, 'se8_ramp', False) or release_rpos is not None or arm_list is not None
    e4 = 0
    f8 = 0
    prev = 0
    rpos = 0                                 # s_e0 = cumulative OUTPUT sample position (incl. pauses)
    release = release_rpos                   # the se8-ramp arm point (s_e0 at the post-stress release node)
    se8 = 0                                  # [self+0xe8] F0 contour; s90 = se8//8 - 20  (see _s90_ramp note)
    s_f0 = 0                                 # [self+0xf0] contour chase, advanced s_ec at a time toward s_e0
    s94 = frames[0]['s94'] if frames else 0
    grains = []
    flush_idx = set()                        # grain indices that begin a fresh ring block (word boundaries)
    prev_silence = False                     # was the previous frame an inter-word silence gap?

    # PITCH method: DEFAULT is the engine-LEGACY period-scaling (USE_PSOLA_PITCH=False). The formant-preserving
    # PSOLA repitch is KEPT (set gen_synth.USE_PSOLA_PITCH=True, or plan._psola_pitch=True, to use it for
    # RAISING) but OFF by default per user preference for the legacy Gintaras pitch. pitch=None -> bit-exact
    # either way. _psola only ever runs for RAISING (pfac<1).
    _use_psola = getattr(plan, '_psola_pitch', None)
    if _use_psola is None:
        _use_psola = USE_PSOLA_PITCH
    _psola = (pfac < 1.0) and _use_psola
    _ppfac = 1.0 if _psola else pfac

    def pper(s):                             # voiced period for epoch placement + grain target.
        return max(60, round(_period(s) * _ppfac))  # _ppfac=1.0 -> _period (>=169) verbatim => bit-exact

    def _se8_burst():
        # fda0 tail loop (sub_1000FFAD): se8 += sd2 while s_e0(=rpos) > s_f0 + s_ec, clamped to [0, s104].
        # ARMED (sd2=20, s104=160) only after the release node; before it sd2=0 so se8 holds 0 (=> s90=-20).
        nonlocal se8, s_f0
        sd2 = SE8_SD2 if (release is not None and rpos >= release) else 0
        hi = SE8_HI if sd2 else 0
        c = 0
        while rpos > s_f0 + SE8_SEC and c < 50:
            se8 = SE8_HI if se8 + sd2 > hi else (0 if se8 + sd2 < 0 else se8 + sd2)
            s_f0 += SE8_SEC
            c += 1

    for k, fr in enumerate(frames):
        # MULTI-WORD se8 contour (cont.48): each word runs its OWN se8 fall (the engine arms per word at
        # floor(wordlen/2) and se8 RESETS per word; the s90 base s118+s100 is 20 for non-final words, 6 for the
        # final word). 'word_start' resets se8/s_f0 and re-arms; 'se8_base' overrides SE8_BASE for the frame.
        # Single-word plans set neither -> byte-identical to before.
        base = fr.get('se8_base', SE8_BASE)
        if ramp and fr.get('word_start'):
            se8 = 0; s_f0 = rpos; release = None     # fresh per-word se8 ramp
            e4 = 0; f8 = 0                           # fresh epoch CLOCK per word (else e4 grows across the phrase
                                                     # and the gate under-places epochs in later words -- each
                                                     # word's epoch placement must be independent, as standalone)
            if arm_list is not None:
                arm_i += 1                           # point at this word's arm sample
        if arm_list is not None:
            # arm the fall at the EXACT per-word output sample (arm_list[arm_i]); the burst then checks the
            # sample-precise rpos>=release each epoch (same as the single-word release_rpos path).
            if release is None and arm_i < len(arm_list) and arm_list[arm_i] is not None \
                    and rpos >= arm_list[arm_i]:
                release = arm_list[arm_i]
        elif ramp and release is None and fr.get('release'):
            release = rpos                   # arm the post-stress fall at this node's cumulative position
        if prev_silence and not fr.get('silence'):
            flush_idx.add(len(grains))       # first grain after an inter-word gap = start of a fresh ring block
        prev_silence = bool(fr.get('silence'))
        if fr['pause']:
            g = list(fr['pcm'])
            grains.append(g)
            prev = g[-1]
            rpos += len(g)
            # Each a6==0 pause frame still triggers ONE fda0 call => ONE s94 IIR (using the pre-burst se8) AND
            # ONE se8 burst. The IIR matters for a LEAD pause (gintaras: s94 68->62 before the first voiced
            # frame); the burst matters for a CLOSURE pause spanning the fall (gintaras's 't': se8 ramps across
            # it). (On the capture Plan ramp=False and each voiced frame reseeds, so this is a no-op => 7/7.)
            if ramp:
                s90e = (se8 >> 3) - base
                s94 = _iir(s94, s90e)
                _se8_burst()
            if _frame_rpos is not None:
                _frame_rpos.append(sum(len(x) for x in grains))   # cumulative GRAIN-length after this frame
            continue
        if fr.get('reseed'):                 # engine carries smoothed pitch across pauses; Plan seeds it
            s94 = fr['s94']
        s90 = fr['s90']
        # E960 src2 = the engine's CAPTURED n2 node (the M marker's 2nd node). sub_1000E960 cross-fades the
        # frame's epochs toward n2 ONLY when n2 != 0 (`test ebp,ebp; je no_blend` @0x1000E9FB); an EMPTY n2 (a
        # word's last voiced frame before a closure, or across a word boundary) => NO blend, all epochs pure
        # p1. Using the captured n2 directly is exact -- it subsumes the next-frame search AND the word-boundary
        # stop (a boundary frame's n2 is empty), and fixes the cross-word case where the next-frame search wrongly
        # blended an n2-less multi-epoch frame (labas tauta seq702: n2 empty, 2 epochs -> pure p1, not a blend).
        nxt = fr.get('n2') or None
        # E410 OUTER loop runs a5+1 times (sub_1000E410's a5 arg = extra sub-frames). Each pass does
        # `e4 += 220` then the inner epoch-placement loop; e4/f8/s94 carry across passes. a5=0 (the common
        # case) => one pass; a5=1 (gintaras's first 6 voiced frames) => two passes, so the frame advances
        # e4 by 2*220, f8 by 2*period and emits two epochs. The E960 blend spans the WHOLE frame: its epoch
        # count a6 = the TOTAL epochs placed across all passes (=2 for an a5=1 frame), and a7 = a6 - i over
        # the global epoch index i — so the frame's epochs cross-fade from this node toward the next node.
        s94l = s94
        pers = []
        ng = fr.get('ngrain') if rate is None else None   # captured epoch count (only at natural rate)
        if ng:
            # GROUND-TRUTH epoch count: place exactly ng epochs (the engine's captured count for this frame).
            # e4 still advances BASE_P per E410 pass and f8/s94 per epoch, so the global clock matches the gate
            # path -> subsequent frames are unaffected. For every bit-exact word the gate already yields ng, so
            # this is a no-op there; it only corrects the count where the gate mis-rounds (cross-word carry).
            e4 += BASE_P * (fr.get('a5', 0) + 1)
            for _e in range(ng):
                per = pper(s94l)
                pers.append(per)
                s90e = ((se8 >> 3) - base) if ramp else s90
                f8 += per
                rpos += per
                if ramp:
                    _se8_burst()
                s94l = _iir(s94l, s90e)
        else:
          for _ in range(fr.get('a5', 0) + 1):
            e4 += BASE_P
            per = pper(s94l)
            cnt = 0
            while cnt < 12:
                cand = _trunc((f8 + per) * 100, e4)
                gate = (cand <= thr) if cnt == 0 else (cand < thr)
                if not gate:
                    break
                pers.append(per)
                s90e = ((se8 >> 3) - base) if ramp else s90   # s90 from se8 BEFORE this epoch's burst
                f8 += per
                rpos += per
                cnt += 1
                if ramp:
                    _se8_burst()             # this epoch's fda0 se8 burst (s_f0 chases the advanced s_e0)
                s94l = _iir(s94l, s90e)
                per = pper(s94l)
            if cnt == 0 and thr >= THR:          # gate failed: at neutral/slower force one (no f8/s94 advance);
                pers.append(pper(s94l))          # at a faster THR the engine instead DROPS the frame (0 epochs
                                                 # -> no grain) -- this is the rate speed-up. (Never fires at
                                                 # neutral for any passing word, so the bit-exact path is intact.)
        total = len(pers)
        for i, tgt in enumerate(pers):
            if total > 1 and nxt is not None:
                p1 = _e6b0(fr['pcm'], tgt, prev)
                p2 = _e6b0(nxt, tgt, prev)
                out = H.ebc0(p1, len(p1), p2, len(p2), total, total - i, 1)[0]
            else:
                out = _e6b0(fr['pcm'], tgt, prev)
            grains.append(out)
            prev = out[-1]
        s94 = s94l
        if _frame_rpos is not None:
            _frame_rpos.append(sum(len(x) for x in grains))   # cumulative GRAIN-length after this (voiced) frame
    for n in plan.tail_silence:
        grains.append([0] * n)
    if _psola:                                # formant-preserving repitch (RAISING only; OFF by default)
        grains, flush_idx = _psola_repitch(grains, flush_idx, pfac)
    return H.render_from_grains(grains, flush_idx=flush_idx)


def _psola_repitch(grains, flush_idx, pfac):
    """FORMANT-PRESERVING pitch (TD-PSOLA): instead of resampling each grain to the scaled period (which shifts
    formants -- chipmunk/dark), OVERLAP-ADD the natural-period voiced grains at the SCALED spacing. F0 changes
    (closer/farther pitch marks), formants stay (grain content untouched), DURATION preserved (output marks
    span the same total). Only VOICED grains (a single pitch period, len ~60..340) are repitched; silence/
    closure grains (>340 or all-zero) pass through. flush_idx (word-boundary ring resets) is remapped to the
    new grain indices. pfac<1 => higher pitch (shorter periods => more grains); pfac>1 => lower.
    NOTE: this runs ONLY when pitch is set (pfac!=1.0); the bit-exact pitch=None path never calls it."""
    import numpy as np

    def voiced(g):
        return 60 <= len(g) <= 340 and any(v != 0 for v in g)

    out = []
    new_flush = set()
    i, n = 0, len(grains)
    while i < n:
        if i in flush_idx:
            new_flush.add(len(out))
        if not voiced(grains[i]):
            out.append(grains[i]); i += 1; continue
        j = i                                          # collect a maximal voiced run
        while j < n and voiced(grains[j]) and j not in (flush_idx - {i}):
            j += 1
        run = grains[i:j]
        marks = np.cumsum([0] + [len(g) for g in run])  # marks[k]=start of grain k; marks[-1]=run length
        total = int(marks[-1])
        wav = np.concatenate([np.asarray(g, dtype=float) for g in run]) if run else np.zeros(0)
        # OLA at the scaled spacing, preserving total duration. Each source grain windowed (Hann, 2-period)
        # centred on its pitch mark; output pitch marks step by (local period * pfac).
        acc = np.zeros(total + 400)
        wsum = np.zeros(total + 400)
        q = 0.0
        guard = 0
        while q < total and guard < 20000:
            guard += 1
            # source grain k whose span contains q (the natural period at this time position)
            k = int(np.searchsorted(marks, q, side='right')) - 1
            k = max(0, min(len(run) - 1, k))
            c = marks[k]                                # this grain's pitch mark (start)
            lo = marks[k - 1] if k > 0 else marks[k]    # 2-period window bounds
            hi = marks[k + 1] if k < len(run) - 1 else marks[len(run)]
            seg = wav[lo:hi]
            if len(seg) > 2:
                w = np.hanning(len(seg))
            else:
                w = np.ones(len(seg))
            g = seg * w
            start = int(round(q)) - (int(c) - int(lo)) # align the grain's mark to q
            a = max(0, start); b = min(len(acc), start + len(g))
            if b > a:
                gs = a - start
                acc[a:b] += g[gs:gs + (b - a)]; wsum[a:b] += w[gs:gs + (b - a)]
            q += max(40.0, len(run[k]) * pfac)          # next output pitch mark
        res = acc[:total] / np.maximum(wsum[:total], 1e-3)
        out.append([int(round(v)) for v in res])
        i = j
    return out, new_flush


# ---- validation: derive a Plan from the instrumented engine capture --------------------------------------
