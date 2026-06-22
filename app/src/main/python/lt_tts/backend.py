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

# ---- pitch / rate control (SAPI4-exact; fully disassembled 2026-06-11) --------------------------
# hlas.dll SetRate/SetPitch DECODED (sub_10010C00 / sub_10010A50, Gintaras mode flag [+0x1ad68]=1 = RAW units):
#   SetRate(S):  S = speed in the mode's [Dmin=100 .. Dmax=800] WPM-style units (0 sentinel = min = 100,
#                0xFFFF = max = 800; these are SAPI4's TTSATTR_MINSPEED/MAXSPEED); ebp = trunc(200000/S);
#                a1 = [+0xd4] = trunc(trunc(150*ebp/200)/10).  S=100 -> a1=150 (the bit-exact baseline every
#                capture ran at, because tts_cli passes rate=0 = the MIN sentinel); S=450 -> 33; S=800 -> 18.
#   SetPitch(H): H = pitch in Hz, mode range [75..130] (0 sentinel = 75 Hz, 0xFFFF = 130 = TTSATTR_MIN/MAXPITCH);
#                stores P_DC = [+0xdc] = trunc(22050/H) and resets the s90/s94 contour state.  NOT a stub: the
#                period generator sub_1000FC80 computes period = clamp(contour + P_DC, 169, 294) -- the "294"
#                in the old _period() was P_DC at the 75 Hz sentinel, not a constant.  At the utterance-initial
#                fda0 (reseed flag) s94 = P_DC - trunc(22050/[+0x1ad78]=100Hz) = P_DC - 220, then one IIR step.
#                (All verified bit-exact vs tts_cli --one-rp captures at 80/90/103/110/120/130 Hz.)
# A SAPI4 host (NVDA's sapi4 driver) read min/max via the sentinels and mapped its 0..100 sliders LINEARLY:
#   rate r ->  SpeedSet(round(100 + 7r));   pitch p -> PitchSet(round(75 + 0.55p))
# so those exact mappings below make the NVDA sliders reproduce the original SAPI4 voice 1:1.
DMIN, DMAX, DBASE = 100, 800, 200            # SetRate bounds/base [+0x1ad7c]/[+0x1ad80]/[+0x1ad84]
PDC_NAT = 294                                # P_DC at the 75 Hz sentinel = every bit-exact capture's setting
PDC_DEF_PERIOD = 220                         # trunc(22050 / [+0x1ad78]=100 Hz): the s94 seed anchor

# NEUTRAL (bit-exact) sentinels: rate=None -> THR 150 (= SetRate min sentinel, the captured baseline);
# pitch=None -> P_DC 294 (= SetPitch min sentinel, ditto). Both byte-identical to every prior validation.
def rate_thr(r):
    """NVDA slider r (0..100) -> the epoch-gate threshold a1/[+0xd4], EXACTLY as NVDA's SAPI4 driver +
    hlas SetRate produced it: wpm = round(100+7r) (linear over the engine-reported [100..800]), then the
    engine's integer chain ebp=trunc(200000/wpm); a1=trunc(trunc(150*ebp/200)/10). Anchors: r=0 -> 150
    (bit-exact natural = the SAPI4 minimum), r=43 -> 37, r=50 -> 33 (the SAPI4-host default position),
    r=100 -> 18 (the SAPI4 maximum). r=None -> 150."""
    if r is None:
        return THR
    wpm = int(round(100 + 7.0 * r))
    ebp = 200000 // wpm
    return max(1, (150 * ebp // 200) // 10)

def pitch_pdc(p):
    """NVDA slider p (0..100) -> the pitch base period P_DC ([+0xdc]), EXACTLY as NVDA's SAPI4 driver +
    hlas SetPitch produced it: Hz = round(75 + 0.55p) (linear over the engine-reported [75..130]), then
    P_DC = trunc(22050/Hz). Anchors: p=0 -> 294 (75 Hz, bit-exact natural = the SAPI4 minimum), p=50 ->
    Hz 102 -> 216 (the SAPI4-host default position), p=100 -> 169 (130 Hz max). p=None -> 294.
    The synthesis period is clamp(s94 + P_DC, 169, 294) (sub_1000FC80), so P_DC shifts the whole F0 base
    while the s90/s94/se8 contour offsets stay untouched -- the engine's own pitch mechanism, no resampling."""
    if p is None:
        return PDC_NAT
    hz = int(round(75 + 0.55 * p))
    hz = 75 if hz < 75 else 130 if hz > 130 else hz
    return 22050 // hz

def pitch_s94_seed(pdc):
    """The utterance-initial s94: fda0's reseed (s94 = P_DC - 220) followed by its own IIR step toward the
    base s90=-20 (sub_1000FDA0 @1000FEF9/1000FF53; the seed write precedes the step in the same call).
    pdc=294 -> 68 (the value every natural capture showed); verified exact for 7 pitches 75..130 Hz."""
    s0 = pdc - PDC_DEF_PERIOD
    return _iir(s0, -20)


def _trunc(a, b):            # truncate toward zero (the FPU round helper sub_10022d90 sets RC=11)
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def _iir(s94, s90):          # sub_1000FDA0 pitch smoother (per placed epoch)
    x = s90 - s94
    x = (x >> 4) + 1 if x > 0 else (x >> 4)
    return s94 + x


def _period(s94, pdc=PDC_NAT):   # sub_1000FC80 voiced period = clamp(s94 + P_DC, 169, 294); P_DC=[+0xdc]
    v = s94 + pdc                # (the default 294 = the SetPitch min sentinel every capture ran at)
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
SE8_SEC = 100          # [self+0xec] chase step AT THE NATURAL a1=150. [+0xec] is the RATE-SCALED frame
                       # duration (sub_1001073D: base_dur * a1 / 75, base_dur=50) -> the burst chase fires
                       # proportionally more often at faster rates: se8_sec = 50*thr//75 (150->100, 33->22,
                       # 18->12). Verified: namas @450wpm 44.98% -> audible-exact with the scaled chase.
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
    pdc = pitch_pdc(pitch)                          # pitch=None -> 294 (the SetPitch min sentinel; bit-exact)
    # INNER-EPOCH IIR LAG: the engine emits the 2nd+ epoch of a pass at the PRE-IIR period (P0,P1,P1,P2..) --
    # but ONLY the SHIFTED rate/pitch path needs it. At the NEUTRAL (bit-exact) sentinel the captures show NO
    # lag: a long-vowel rising contour climbs 286,287,288 (not 286,286,288). The lag here skipped 287 and made
    # dantis/naktis/rankos/liaudis (long-stressed-vowel + combo words) 1 sample short -> ~71%. So the lag is
    # gated OFF when neutral (matches the proven research gen_synth + the engine wav bit-for-bit), ON when
    # rate/pitch is set (the rework's E5C5-refetch model that the @450wpm/@90Hz validations needed).
    _lag = (rate is not None) or (pitch is not None)
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

    # PITCH: the engine's own mechanism -- P_DC base-shift inside the [169,294] clamp (sub_1000FC80), exactly
    # what hlas SetPitch does. No resampling, no period scaling. (The formant-preserving PSOLA repitch
    # _psola_repitch is KEPT in the file for future use but is no longer wired -- the true engine mechanism
    # subsumed the old pitch_factor scaling it piggybacked on.)
    def pper(s):                             # voiced period for epoch placement + grain target.
        return _period(s, pdc)               # pdc=294 (pitch=None) => the old _period verbatim => bit-exact

    se8_sec = 50 * thr // 75                 # [+0xec] = base_dur(50) * a1 / 75: the rate-scaled chase step
                                             # (thr=150 -> 100 = the validated natural constant, bit-exact)

    def _se8_burst():
        # fda0 tail loop (sub_1000FFAD): se8 += sd2 while s_e0(=rpos) > s_f0 + s_ec, clamped to [0, s104].
        # ARMED (sd2=20, s104=160) only after the release node; before it sd2=0 so se8 holds 0 (=> s90=-20).
        nonlocal se8, s_f0
        sd2 = SE8_SD2 if (release is not None and rpos >= release) else 0
        hi = SE8_HI if sd2 else 0
        c = 0
        while rpos > s_f0 + se8_sec and c < 50:
            se8 = SE8_HI if se8 + sd2 > hi else (0 if se8 + sd2 < 0 else se8 + sd2)
            s_f0 += se8_sec
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
          # LITERAL sub_1000E410 epoch loop (disassembly 2026-06-11). Two engine quirks the old approximation
          # missed (caused boundary-epoch mis-attribution at shifted pitch/rate, e.g. namas @90 Hz):
          #   * the CONTINUE-gate after emitting an epoch is trunc((f8 + JUST-EMITTED per)*100/e4) < thr --
          #     the engine re-fetches fc80 BEFORE that epoch's fda0 IIR, so the gate sees the emitted period,
          #     not the next one;
          #   * the next epoch EMITS that same pre-IIR fetch (the period lags one IIR step: P0,P1,P1,P2,...).
          #   * a pass whose first gate fails yields NO epoch (continue to the next a5 pass) -- the engine has
          #     NO force-one for voiced frames (its count=1 floor at E667 is only for a2==0 verbatim frames,
          #     which our pause path emits directly). The old force never fired at neutral; removed.
          per = pper(s94l)                       # frame-initial fetch (E474/E47A: s94 from the previous frame)
          cnt = 0
          for _ in range(fr.get('a5', 0) + 1):
            e4 += BASE_P
            if _trunc((f8 + per) * 100, e4) > thr:
                continue                         # no epoch this pass (e4 keeps growing)
            first = True
            while cnt < 12:
                pers.append(per)
                s90e = ((se8 >> 3) - base) if ramp else s90   # s90 from se8 BEFORE this epoch's burst
                f8 += per
                rpos += per
                cnt += 1
                cand = _trunc((f8 + per) * 100, e4)   # continue-gate with the JUST-EMITTED period
                if ramp:
                    _se8_burst()             # this epoch's fda0 se8 burst (s_f0 chases the advanced s_e0)
                if first or not _lag:        # pass-initial epoch (or the NEUTRAL bit-exact path): refetch AFTER
                    s94l = _iir(s94l, s90e)  # its fda0 (E595->E5A0) -> post-IIR period (no lag)
                    per = pper(s94l)
                    first = False
                else:                        # SHIFTED rate/pitch inner epochs: the E5C5 refetch PRECEDES that
                    nper = pper(s94l)        # epoch's fda0 -> period lags one IIR step (engine emits P0,P1,P1,P2)
                    s94l = _iir(s94l, s90e)
                    per = nper
                if not (cand < thr):
                    break
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
    if rate is None:
        for n in plan.tail_silence:              # capture/natural path: verbatim (bit-exact)
            grains.append([0] * n)
    else:
        # EXACT engine law (8-point fit vs hooked DLL tails at S=200..1000, 2026-06-11): the utterance-final
        # silence flush = trunc(a1/5) MILLISECONDS -> samples = 22050*(a1//5)//1000. (a1=150 -> 30 ms = 661 =
        # the natural [256,256,149]; 75->330, 49->198, 42->176, 33->132, 24->88, 18->66, 15->66 -- all exact.)
        # Emitted as one zero grain (grain boundaries inside trailing zeros are inaudible, past every declick).
        tot = 22050 * (thr // 5) // 1000
        if tot > 0:
            grains.append([0] * tot)
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
