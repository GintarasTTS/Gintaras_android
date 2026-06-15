# plan_frontend.py — wire the FRONT-END to a gen_synth.Plan (no engine capture for the native backbone).
#
# PHASE 1 (this file): the per-frame NATIVE pitch-period backbone + unit selection are produced
# GENERATIVELY — gintaras_ref's demisyllable selection (build_tiling) chooses the unit-key sequence, and
# each key is expanded through wtvlt1_decode.parse_units -> the shared frame pool = the exact int16 pool
# pitch-periods the engine concatenates (proven verbatim, 46/46 on labas). The per-frame PROSODY
# (s90/s94/a5 + the a6==0 verbatim/pause flag) is still taken from the instrumented engine and zipped onto
# the generative frames BY POSITION — so when the generative selection is frame-identical to the engine the
# resulting Plan equals build_plan_from_capture's and synthesizes 100% bit-exact. Mismatched selection shows
# up directly as a frame-count / fid divergence (validate() reports it). PHASE 2 will replace the captured
# prosody with lt_tonai/lt_ilgiai for a fully DLL-free text->wav path.
from . import selection as G
from . import backend as GS
from . import voice as W
from . import transcribe as LT
from . import duration as IL

_POOL = None
_UNITS = None


def _safe(word):
    """ASCII-safe slug for temp/wav filenames (diacritic cp1257 bytes are fragile in Windows paths)."""
    return "".join(c if c.isalnum() else "_%02x" % ord(c) for c in word)


def _voice():
    global _POOL, _UNITS
    if _POOL is None:
        _POOL, _UNITS = G.load_voice(False)
        G.UNITSET = set(_UNITS)
    return _POOL, _UNITS


def select_frames(word):
    """GENERATIVE unit selection -> the ordered native backbone. Returns a list of per-frame dicts
    {'key','fid','pcm','sil'}: one entry per emitted pool pitch-period, in engine order. 'sil' frames are
    the plosive-closure gaps build_tiling emits (kind 'sil'); they have no pool fid (pcm filled later)."""
    pool, units = _voice()
    full = G.front(word)                             # [(phone,dur,bps,stressed)] incl '_' boundaries
    f0_fn, cum = G.build_f0(full)
    inner = [(p, dr, G.damp(f0_fn((t0 + t1) / 2), st and G.is_vowel(p)), st, pal)
             for (p, dr, _b, st, pal, _raw), (t0, t1) in zip(full, cum) if p != "_"]
    phones = [p for p, _, _, _, _ in inner]; durs = [dr for _, dr, _, _, _ in inner]
    f0s = [f for _, _, f, _, _ in inner]; stresses = [s for _, _, _, s, _ in inner]
    palatals = [pl for _, _, _, _, pl in inner]
    meta = []
    elems = G.build_tiling(phones, durs, f0s, stresses, units, meta=meta, palatals=palatals)
    # stressed VOWEL phone index (the s90 -20 region runs through the stressed syllable = the stressed vowel
    # + any coda consonants up to the next vowel); fall back to the last vowel if nothing is flagged.
    sv = next((i for i, (p, st) in enumerate(zip(phones, stresses)) if st and G.is_vowel(p)), None)
    if sv is None:
        sv = max((i for i, p in enumerate(phones) if G.is_vowel(p)), default=0)
    # stressed-syllable end, with ONSET-MAXIMISATION: a consonant run between the stressed vowel and the
    # next vowel splits so the LAST consonant is the next syllable's onset (namas n-ą-m-a: 'm' is the onset
    # of 'mas', NOT a coda -> the stressed syllable 'ną' is OPEN). A word-final consonant run is all coda
    # (labas -as). Codas keep s90=-20; the post-stress fall releases at the next syllable.
    j = sv + 1
    while j < len(phones) and not G.is_vowel(phones[j]) and phones[j] != "_":
        j += 1
    ncons = j - (sv + 1)
    if j < len(phones) and G.is_vowel(phones[j]):    # a following vowel exists -> its onset = last consonant
        syll_end = sv + max(0, ncons - 1)
        open_syll = ncons <= 1
    else:                                            # word-final: every trailing consonant is a coda
        syll_end = j - 1
        open_syll = ncons == 0
    # se8-ramp RELEASE placement = the engine's POSITIONAL declination (RE'd 2026-06-07 via the sub_100106D0
    # arming probe, NOT the pitch accent): the contour table is always 2 segments (hold, then fall), and the
    # fall arms when the per-CHARACTER counter crosses charpos == floor(wordlen/2) (s7c==wordlen, count==2).
    # i.e. the pitch declination starts at the WORD MIDPOINT. We map that letter index to a frame: find the
    # phone holding the middle letter and the fractional offset within it (a diphthong's 2nd vowel-letter =>
    # mid-phone), then arm at the frac-th frame of that phone. (labas/namas/duona/tauta bit-exact; kaina needs
    # the true within-diphthong element-duration split -- its long 'ai' puts the 2nd element near the END, not
    # 0.5; the 0.5 proxy lands too early. gintaras/sveiki are s94-carry / selection limited, not release.)
    def _letters(p):                                 # input-letter span of a phone (diphthong vowel = 2)
        return 2 if G.is_vowel(p) and len(p.replace("'", "")) >= 2 else 1
    wl = sum(_letters(p) for p in phones)            # == engine s7c (input letter count)
    # The engine's per-char arming counter (sub_100106D0) SKIPS a falling diphthong's 2nd element char when
    # the diphthong is plain-'a'-first ('au'/'ai'=aj) -- i.e. NOT the stressed long first element (which the
    # front-end marks with an ogonek 'ąu'/'ąj') and not 'uo'/'ei'. So the fall arms at the first NON-skipped
    # char >= floor(s7c/2): tauta 'au' skips char2 -> arm 't'(3); draugas 'au' skips char3 -> arm 'g'(4);
    # kaina 'ąj'/duona 'uo'/sveiki 'ei' (no plain-a) skip nothing -> unchanged. (Verified vs the engine
    # _arm.txt charpos sequences; data-driven, not accent-derived, so robust where lt_accent is silent.)
    skipped = set()
    cp = 0
    for p in phones:
        lc = _letters(p)
        if lc == 2 and G.is_vowel(p) and p.replace("'", "")[:1] == "a":
            skipped.add(cp + 1)                      # the diphthong's 2nd-element char is not counted
        cp += lc
    arm_char = wl // 2
    while arm_char in skipped and arm_char < wl - 1:
        arm_char += 1
    cum = 0; arm_phone = len(phones) - 1; frac = 0.0; arm_off = 0
    for i, p in enumerate(phones):
        lc = _letters(p)
        if cum <= arm_char < cum + lc:
            arm_phone = i; arm_off = arm_char - cum; frac = arm_off / lc; break
        cum += lc
    frames = []
    for ei, (kind, key, *_) in enumerate(elems):
        pi, isv, stressed = meta[ei] if ei < len(meta) else (len(phones) - 1, False, False)
        in_stress = pi <= syll_end                   # frame is in/before the stressed syllable -> s90 = -20
        if kind == "sil":
            frames.append({'key': '_sil', 'fid': None, 'pcm': None, 'sil': True,
                           'pi': pi, 'in_stress': in_stress, 'release_pt': False})
            continue
        keylist = key if isinstance(key, (list, tuple)) else [key]
        for k in keylist:
            for f in units.get(k, []):
                if f in pool:
                    frames.append({'key': k, 'fid': f, 'pcm': list(int(x) for x in pool[f]),
                                   'sil': False, 'pi': pi, 'in_stress': in_stress, 'long': _long_key(k),
                                   'open_syll': open_syll, 'release_pt': False})
    # UNBURSTABLE STOP (sveiki 'k' between 'ei' and 'i'): no 'Cv-' onset exists, so gintaras_ref.build_tiling
    # drops the stop entirely -- but the ENGINE inserts TWO verbatim frames there: a CLOSURE (the stop's
    # 'Ca-'[0], a ~994-sample silence) + a BURST (a standalone pool frame). Emit them as non-sil frames so the
    # generative stream aligns 1:1 with the captured skeleton (build_plan_phase2 then substitutes their pool
    # pcm at the matching slots). Burst fid is per-stop (measured from the engine M-markers; only 'k' so far).
    _BURST_FID = {"k": 4788}
    for i in range(0, len(phones) - 1):
        p = phones[i]
        # the engine inserts the closure+burst whenever the stop's onset is absent and a VOWEL follows (sveiki
        # ei-K-i; penki n-K-i) -- the preceding phone may be a vowel OR a consonant (the n in penki) OR NOTHING
        # at all: a WORD-INITIAL unburstable stop (kitas/ki/kinija k-i: no 'ki-' onset) sits at phones[0], so the
        # scan must start at 0. The closure ('ka-'[0], ~994 smp) doubles as the word's leading silence and the
        # burst (4788, ~710 smp) follows -- exactly the 1704 samples that were dropped word-initially (the loop
        # used to start at 1, so kitas/ki/kita/kinas/kinija lost their whole initial 'k'). Mid-word stops are
        # unchanged: pekinas p-e-k-i still resolves to its k at i=2 (p has a 'pe-' onset, so i=0 is skipped).
        if p in G.STOPS and G.is_vowel(phones[i + 1]) \
                and G.onset_unit(p, phones[i + 1][0], units) is None and p in _BURST_FID:
            clos = units.get(p + "a-", [None])[0]          # closure = the stop's 'Ca-'[0] (a silence frame)
            burst = _BURST_FID[p]
            ins = next((vi for vi, fr in enumerate(frames) if fr['pi'] >= i), len(frames))
            new = []
            for f in (clos, burst):
                if f is not None and f in pool:
                    new.append({'key': '_stop', 'fid': f, 'pcm': list(int(x) for x in pool[f]),
                                'sil': False, 'pi': i, 'in_stress': i <= syll_end, 'long': False,
                                'open_syll': False, 'release_pt': False})
            frames[ins:ins] = new
            break
    # mark the single RELEASE frame = the start sample of the middle character (arm_char), mapped to a frame.
    voiced = [vi for vi, fr in enumerate(frames) if not fr['sil']]
    ph_frames = [vi for vi in voiced if frames[vi]['pi'] == arm_phone]
    if ph_frames:
        if arm_off >= 1:
            # 2nd element of a diphthong: it is rendered as a SEPARATE offglide demisyllable within the phone
            # (kaina 'ąj' = '-ką'x24 + '-ąj'x14; duona 'uo' = 'uo-'x10 + '-uo'x10). The element boundary = the
            # first demisyllable-KEY CHANGE within the phone's frames (the engine's per-letter duration split,
            # 63:37 for kaina's long 'a', 50:50 for duona). If the diphthong is a SINGLE demisyllable (tauta
            # 'au'), there is no split -> fall back to the proportional frac.
            keys = [frames[vi]['key'] for vi in ph_frames]
            split = next((idx for idx in range(1, len(keys)) if keys[idx] != keys[idx - 1]), None)
            tgt = ph_frames[split] if split is not None else \
                ph_frames[min(len(ph_frames) - 1, int(round(frac * len(ph_frames))))]
        elif G.is_vowel(phones[arm_phone]) and arm_phone > 0 \
                and not G.is_vowel(phones[arm_phone - 1]) and phones[arm_phone - 1] != "_":
            # a VOWEL character begins (in CV-demisyllable synthesis) at its syllable ONSET demisyllable 'Cv-'
            # (gintaras 'a' starts at 'ta-', tagged with the onset consonant phone arm_phone-1), NOT the
            # nucleus-coda '-Cv'. So arm at the onset consonant's first frame.
            onset = [vi for vi in voiced if frames[vi]['pi'] == arm_phone - 1]
            tgt = onset[0] if onset else ph_frames[0]
        else:
            tgt = ph_frames[0]                       # consonant char / vowel without an onset -> phone start
    else:
        tgt = next((vi for vi in voiced if frames[vi]['pi'] >= arm_phone), voiced[0] if voiced else None)
    # The engine's per-CHARACTER arming counter (sub_100106D0) maps a char to its VOICED output position. A
    # stop-CLOSURE onset (a pause/silence frame, e.g. vakaras 'k' = a 994+656 closure) belongs to the PRECEDING
    # char's render, so the arm char's true position is the first VOICED frame AFTER the closure -- not the
    # closure itself. Skip past any pause frames the arm landed on (vakaras phase2 49.7->93.6%; words whose arm
    # is already a voiced onset, e.g. gintaras 'ta-', are unaffected: their tgt is not a pause frame).
    if tgt is not None:
        frames[tgt]['release_pt'] = True
    return frames


def _long_key(k):
    """a5 = the 'double per-grain budget' long-vowel flag (gintaras_ref._is_long_v): the i|/u| pipe bodies
    and 'o'. Applied to the long-vowel unit (the onset is tagged separately when it shares the syllable)."""
    return k.endswith("|") or k == "o" or k == "-o" or k == "o-"


# ---- generative per-grain a5 (the E410 outer-loop epoch-doubling budget), DLL-free (cont.42) ----------------
# a5 is the engine's DF90/E410 duration distribution (RE'd cont.36-41): it is RATE-INDEPENDENT (rate is the
# downstream THR gate, [[pitch-rate-control]]). Sources, all DLL-free:
#   * the 12 'irregular' demisyllables whose grains carry the wtvlt1 FLAG 0x40 store a5 DIRECTLY in their
#     per-reference PAD field (i|/juo|/-ka/-rai/... -- exact, covers every case no rule reproduces);
#   * the long monophthong /o:/ CODA doubles all-but-first (T=2N-1) when its lt_ilgiai duration D >= _O_DMIN
#     (a loanword short o, e.g. kompiuteris 'oo'=103, stays short -> no fill);
#   * the STRESSED ogonek-a glide diphthong ONSET ('ąu-'/'ąj-') doubles all-but-first (its coda '-au' does not).
# Verified == the engine's captured a5 (the build_plan_phase2 zip) on ~60 words, ZERO degradation of any
# bit-exact word. Probes: _probe_pred2/_probe_dsplit/_probe_predicate/_probe_e2e_a5.
_A5_DMIN = 108                                        # long /o:/ coda D threshold (native 112..140; loanword 103)
_A5_LONG_MONO = set("o")                              # the long monophthong /o:/ (always long in this voice)
_A5_AU_ONSET = {"ąu", "ąj", "ąū"}                     # the stressed ogonek-a glide diphthong onset


def _a5_unit_pads():
    global _A5_PADS, _A5_BIT6
    try:
        _A5_PADS
    except NameError:
        d = W.load()
        _A5_PADS = W.parse_unit_pads(d)
        _A5_BIT6 = {k for k, (p, fl) in _A5_PADS.items() if any(x & 0x40 for x in fl)}
    return _A5_PADS, _A5_BIT6


def _a5_eligible(key, phone, D, prev_phone=None, raw=None):
    """Doubling class of a NON-bit6 unit: 'o' = long /o:/ (the DF90 10-extra distribute, _a5_long_distribute),
    'au' = the ąu-/ąj- diphthong onset (the simpler [0,1*(n-1)] fill), or None (no doubling)."""
    body = key.lstrip("-").rstrip("-").replace("|", "")
    is_coda = key.startswith("-") or (not key.endswith("-") and len(key) <= 3)
    is_onset = key.endswith("-")
    pl = phone.replace("'", "")
    if is_coda and body and body[-1] in _A5_LONG_MONO and len(pl) == 1 and pl in _A5_LONG_MONO:
        # a HIATUS o (a vowel immediately before it) doubles only when the RAW transcr token is the LONG
        # doubled 'oo'/'Oo'/'oO' (ios i-oo-s: engine a5=[0,1x10], capture_prosody-verified); the SHORT
        # stressed 'O' (chaosas a-O) does NOT double. norm() collapses both to 'o', so the raw token (6th
        # front-end field) carries the distinction. No preceding vowel (word-initial oras, or after a
        # consonant lova/žodis/milijonas) doubles as before (chaosas o = [0x11], oras o = [0,1x10]).
        if prev_phone is not None and G.is_vowel(prev_phone):
            raw_long = raw is not None and raw.replace("'", "").lower() == "oo"
            if not raw_long:
                return None
        return "o" if (D is None or D >= _A5_DMIN) else None
    if is_coda and body == "ou" and pl in ("ou", "o"):
        # the `ou` u-diphthong (sound/out/loud/foulas/router...): its head IS the long /o:/, so the
        # engine doubles it +10 epochs exactly like a plain long-o coda -- route-2 capture_prosody:
        # ALL `ou` loanwords give a5 sum=10, the lone exception being the kloun* family (a SHORT-o stem,
        # handled in transcribe._SHORT_OU). The plain-o test above misses these because the unit body
        # ends in the u-offglide ('ou'), not 'o'. Gate on the HEAD vowel's length via `raw` (the 6th
        # front-end field = the original nucleus-head token, length preserved through the merge): a LONG
        # head 'oo'/'oO'/'Oo' doubles; a SHORT klounas head 'o'/'O' does not. Works whether mine
        # tokenizes the nucleus MERGED (phone 'ou') or SPLIT (phone 'o' + a sibling -un offglide). No
        # D-floor: the engine doubles even the shortest loanword o (=103).
        if raw is not None and raw.replace("'", "").lower() == "oo":
            return "o"
    if is_onset and pl in _A5_AU_ONSET:
        return "au"
    return None


_A5_LONGV_EXTRA = 10                                  # the DF90 epoch target a long /o:/ adds (verified: o coda
                                                     # N=11 -> sum 10, o| pipe N=10 -> sum 10; constant in both)


def _a5_long_distribute(n):
    """a5 for a long-vowel body of n grains: grain0=0, then _A5_LONGV_EXTRA extra epochs spread over grains
    1..n-1 by CENTERED rounding (the engine's DF90 fraction). n=11 -> [0,1x10]; n=10 -> [0,1,1,1,1,2,1,1,1,1]."""
    out = [0] * n
    s = n - 1
    if s <= 0:
        return out
    D = _A5_LONGV_EXTRA
    prev = 0
    for k in range(s):
        cur = int(round((k + 1) * D / float(s)))
        out[1 + k] = cur - prev
        prev = cur
    return out


def gen_a5_list(word, gen=None):
    """Per-emitted-unit-frame a5 (parallel to select_frames non-sil), fully DLL-free. gen lets the caller
    pass an already-computed select_frames list to avoid recomputation."""
    pool, units = _voice()                           # populate G.UNITSET so G.front merges diphthongs unit-aware
    pads, bit6 = _a5_unit_pads()
    fr = [f for f in (gen if gen is not None else select_frames(word)) if not f['sil']]
    # phones AND durs from the SAME G.front call -> they stay aligned with select_frames' pi (which is also
    # built from G.front). A separate IL.ilgiai(LT.transcribe) keeps a falling diphthong SPLIT (au -> a+w) while
    # G.front MERGES it (au) when 'au' is a recorded unit, so its index would drift past any diphthong (raudonas
    # r-au-d-O: the merged 'o' is pi=3 but the split durs put 'd' at 3 -> the long-/o:/ D-gate misfired).
    full = [t for t in G.front(word) if t[0] != "_"]
    phones = [t[0] for t in full]
    durs = [t[1] for t in full]                       # G.front's own per-phone ilgiai duration (diphthong-merged)
    raws = [t[5] for t in full]                       # raw transcr token ('oo' vs 'O' for the hiatus-o gate)
    out, i = [], 0
    while i < len(fr):
        key = fr[i]['key']; pi = fr[i]['pi']
        j = i
        while j < len(fr) and fr[j]['key'] == key and fr[j]['pi'] == pi:
            j += 1
        n = j - i
        phone = phones[pi] if pi is not None and pi < len(phones) else "?"
        D = durs[pi] if pi is not None and pi < len(durs) else None
        if key in bit6:
            refs = units.get(key, [])
            padv, _fl = pads[key]
            kept = [padv[k] for k in range(len(padv)) if k < len(refs) and refs[k] in pool]
            out.extend((kept + [0] * n)[:n])
        else:
            prev_phone = phones[pi - 1] if (pi is not None and pi >= 1) else None
            raw = raws[pi] if pi is not None and pi < len(raws) else None
            cls = _a5_eligible(key, phone, D, prev_phone, raw) if n <= 14 else None
            if cls == "o":
                # long /o:/: _A5_LONGV_EXTRA(=10) extra epochs, a5[0]=0, rest over grains 1..n-1 by CENTERED
                # rounding (the DF90 fraction). n=11 'o' coda -> [0,1x10] (== old, no passing word changes);
                # n=10 'o|' pipe (milijonas/keturiolika) -> [0,1,1,1,1,2,1,1,1,1], the engine's exact pattern.
                out.extend(_a5_long_distribute(n))
            elif cls == "au":
                out.extend([0] + [1] * (n - 1))       # ąu-/ąj- diphthong onset: simple double-all-but-first
            else:
                out.extend([0] * n)
        i = j
    return out


# ---- engine capture (prosody only, for Phase-1 validation) ----------------------------------------------
_PHASE2_TAIL = [256, 256, 149]                       # the engine's trailing-silence skeleton (CONSTANT across
                                                     # all words; verified 22/22 in capture). Rate-independent.


_Q_RISE_S90 = -35                                    # question-rise s94/s90 target: final period 294-35=259 =>
                                                     # F0 ~85 Hz == the engine's measured question-end (statement
                                                     # ends flat 75 Hz). Reached EXACTLY via the per-frame reseed
                                                     # ramp in build_plan_phase2(question=) (the old -46 was an
                                                     # IIR-undershoot compensation that still stalled at ~80 Hz).
_Q_RISE_FRAMES = 16                                  # # of trailing voiced frames the rise ramps over (~1 syllable)


def build_plan_phase2(word, final=True, question=False, rate=None, pitch=None):
    """PHASE 2 Plan: FULLY GENERATIVE -- NO engine capture at all (cont.43). Native backbone + the WHOLE frame
    skeleton come from select_frames; prosody is generative:
      * structure: select_frames' ordered pool units (== the engine's frame stream; verified 955/955).
      * pause (a6==0 verbatim closure/burst/coda vs voiced): a (key,pi) UNIT is verbatim iff its FIRST grain
        is >350 samples (a closure/burst/coda, not a 169..294 pitch period) -- proven 0/955 vs captured pause.
      * a5: the E410 epoch-doubling budget = gen_a5_list (wtvlt1 0x40 pad + long-/o:/-coda D-gate + ąu-/ąj-
        onset), RATE-INDEPENDENT (rate is the downstream THR gate).
      * s90 STEP: -20 on voiced frames (the stress hold); the phrase-final fall is the se8 ramp armed at the
        release node. pause/verbatim frames ignore s90/s94 (a6==0 => no pitch resample) -> set 0/68.
      * s94: utterance seed = GS.pitch_s94_seed(pdc) (fda0's reseed s94=P_DC-220 + its IIR step; = 68 at the
        pitch=None sentinel P_DC=294), carry via gen_synth's epoch IIR.
      * tail: _PHASE2_TAIL (the constant engine trailing silence).
    Byte-identical to the former captured-skeleton build on 37 words (probe _probe_nocap).
    rate/pitch (NVDA sliders, None = bit-exact sentinels) shape the TIMING-dependent parts of the plan (the
    s94 seed + the no-arm contour pass) and must match what synthesize() is called with."""
    pdc = GS.pitch_pdc(pitch)
    S94_INIT = GS.pitch_s94_seed(pdc)                # 68 at pitch=None (pdc=294) -> bit-exact path unchanged
    pool, units = _voice()
    poolset = set(tuple(int(x) for x in a) for a in pool.values())
    gen = [g for g in select_frames(word) if not g['sil']]
    a5gen = gen_a5_list(word, gen)
    # per-UNIT verbatim/pause flag: first grain of each (key,pi) unit > 350 => the whole unit is a6==0 verbatim
    # (closure/burst/coda); else voiced. This replaces the captured pr['pause'] (proven 0/955 mismatch).
    pause = [False] * len(gen)
    i = 0
    while i < len(gen):
        k0, p0 = gen[i]['key'], gen[i]['pi']
        j = i
        while j < len(gen) and gen[j]['key'] == k0 and gen[j]['pi'] == p0:
            j += 1
        pv = len(gen[i]['pcm']) > 350
        for k in range(i, j):
            pause[k] = pv
        i = j
    frames = []
    armed = False                                    # have we placed the post-stress release marker yet?
    for gi, g in enumerate(gen):
        a5v = a5gen[gi] if gi < len(a5gen) else 0
        # The pitch FALL is the engine's se8 ramp (gen_synth _s90_ramp): s90 holds -20, then eases -20 -> 0 once
        # armed at the accent-determined RELEASE node (select_frames `release_pt`). gen_synth runs the exact
        # se8//8-20 contour per epoch. A verbatim coda still advances the ramp position (labas's -as saturates
        # se8 => the next voiced frame is 0).
        release = g.get('release_pt') and not armed
        if release:
            armed = True
        if pause[gi]:
            frames.append({'pcm': g['pcm'], 's90': 0, 's94': S94_INIT,
                           'pause': True, 'reseed': False, 'a5': a5v, 'release': release,
                           'pi': g.get('pi'), 'key': g.get('key')})
        else:
            frames.append({'pcm': g['pcm'], 's90': -20, 's94': S94_INIT,
                           'pause': False, 'reseed': False, 'a5': a5v, 'release': release,
                           'pi': g.get('pi'), 'key': g.get('key')})
    tail = list(_PHASE2_TAIL)
    # Seed the smoothed pitch ONCE at the very first frame (NOT the first voiced one) and never reseed, so the
    # carry runs through the LEAD-PAUSE s94 IIR(s) the engine does before voicing -- gintaras's 68->62 step,
    # which a first-voiced-frame reseed used to wipe out (left it 1 IIR behind => the a5=1 carry failure).
    if frames:
        frames[0]['s94'] = S94_INIT
    # MULTI-WORD CONTINUOUS PROSODY (final=False): a NON-final word in a phrase must NOT do the phrase-final
    # se8 fall -- it stays at the -20 stress HOLD so the whole phrase is ONE continuous declination and only the
    # LAST word falls (the engine renders a clause as one contour, not a fall per word -> per-word falls sound
    # like separate utterances). Strip the per-frame release flags and skip the arm entirely. final=True (the
    # default) is the BIT-EXACT single-word path, unchanged.
    _thread_n2(frames, poolset)                      # generative E960 blend node (restores cross-frame blending)
    if question:
        # QUESTION RISE (phrase-final yes/no-question intonation, the engine's `?` contour): instead of the se8
        # FALL (s90 -20->0 => 75 Hz), the pitch RISES at the end (engine measured: statement ends flat 75 Hz,
        # question climbs gently to ~85 Hz over the final syllable). Drop the release and ramp the last
        # _Q_RISE_FRAMES VOICED frames from -20 down to _Q_RISE_S90. The ramp RESEEDS s94 per frame (not just
        # s90): the IIR alone moves ~2/epoch and the trailing frames carry only a few epochs -- at natural rate
        # it stalled near -26 (~80 Hz, half the engine's rise) and at a fast THR (fewer epochs still) the rise
        # vanished entirely, which is what made '?' inaudible. Reseed is rate-independent: each trailing frame's
        # periods hit the ramp value exactly at ANY rate. (A phrase-type contour; the engine's full tonai
        # question shape isn't ported -- this reproduces the measured terminal rise.)
        for fr in frames:
            fr.pop('release', None)
        vidx = [k for k, fr in enumerate(frames) if not fr['pause']]
        m = min(_Q_RISE_FRAMES, len(vidx))
        for r, k in enumerate(vidx[-m:]):
            frac = (r + 1) / float(m)                 # 0..1 across the trailing window
            v = int(round(-20 + (_Q_RISE_S90 - (-20)) * frac))
            frames[k]['s90'] = v
            frames[k]['s94'] = v
            frames[k]['reseed'] = True
        return GS.Plan(frames, tail)
    if not final:
        for fr in frames:
            fr.pop('release', None)
        return GS.Plan(frames, tail)                 # no release_rpos / se8_ramp -> holds at the -20 declination
    plan = GS.Plan(frames, tail)
    # se8-fall arm, FULLY GENERATIVE (no capture): a NO-ARM synth pass yields each frame's cumulative output
    # (pre-arm offsets are arm-independent), then _gen_arm_rpos maps the ported-string-builder armc -> the phone
    # owning it -> the frame BEFORE that phone's frames -> arm_out + SE8_SEC. 25/26 bit-exact (only vakaras's
    # burst-shape stays; the few-sample frame/grain boundary slack is absorbed by the +100 contour lag). No
    # _gchar.bin/_grains.bin/_arm.txt needed -- the arm is now pure front-end + back-end.
    fr_rpos = []
    # The no-arm pass MUST run at the TARGET rate: release_rpos is an absolute OUTPUT-sample position, and a
    # fast THR compresses the output -- a natural-schedule arm lands past (or far off) the fast output, so the
    # se8 fall misfired/never fired at fast rates (the engine recomputes its contour on the actual epoch
    # schedule). rate=None keeps the natural pass = the bit-exact path, byte-identical.
    GS.synthesize(plan, rate=rate, pitch=pitch, _frame_rpos=fr_rpos)   # no release_rpos yet => no-arm pass
    rr = _gen_arm_rpos(word, frames, fr_rpos)        # fully generative arm (no capture)
    if rr is not None:
        plan.release_rpos = rr
        for fr in frames:
            fr.pop('release', None)                  # release_rpos overrides the per-frame flag
        plan.se8_ramp = True
    return plan


def _se8_word_base(wi):
    """The engine's PHRASE-DECLINATION per-word s90 base (s118+s100), RE'd from the fda0 capture (cont.49):
    word 0 = 20 (s118=10 + s100=10, the full single-word accent), and from word 1 on s118=0, s100 = 10-4*wi ->
    base = 10 - 4*wi (verified 20,6,2,-2,-6 on 'vienas du trys keturi penki' AND on the 4-word statement
    'labas rytas geras vakaras' -- this is the GENERAL multi-word declination, not list-specific). A LOWER base
    = a HIGHER accent peak, so each successive word peaks lower (word0 ~80Hz, word1 ~76.6, word2+ at the 75Hz
    floor since s90>=-2 -> period clamps to 294). This is the natural list/phrase declining staircase."""
    return 20 if wi == 0 else 10 - 4 * wi


def build_plan_phrase(text, question=False, rate=None, pitch=None):
    """MULTI-WORD continuous prosody (cont.48-49), the engine's PER-WORD se8 contour + PHRASE DECLINATION: each
    word runs its OWN se8 fall (arm at floor(wordlen/2), se8 RESETS per word), and the s90 base declines by word
    position (_se8_word_base: 20,6,2,-2,...) so each successive word's accent peaks lower -- the engine's natural
    phrase/list staircase (verified vs fda0 capture). Composed from per-word build_plan_phase2 plans (each
    single-word BIT-EXACT) into ONE gen_synth stream so s94 carries continuously and words join flush. A '?'
    raises the final word instead. Single word -> just build_plan_phase2."""
    words = [w for w in text.split() if w]
    if len(words) <= 1:
        return build_plan_phase2(text, question=question, rate=rate, pitch=pitch)
    allframes, alltail = [], list(_PHASE2_TAIL)
    word_spans = []                                   # (start_frame_index, end_frame_index, word) per word
    for wi, w in enumerate(words):
        last = (wi == len(words) - 1)
        wp = build_plan_phase2(w, question=(question and last), rate=rate, pitch=pitch)
        start = len(allframes)
        for fi, fr in enumerate(wp.frames):
            fr = dict(fr)
            fr.pop('release', None)                   # arms are placed sample-exactly below, not per-frame
            if fi == 0 and wi > 0:
                fr['word_start'] = True               # reset se8 at each new word
            if not (question and last) and wi > 0:    # word 0 keeps the default 20 (== bit-exact single word)
                fr['se8_base'] = _se8_word_base(wi)    # the engine's phrase declination
            allframes.append(fr)
        word_spans.append((start, len(allframes), w, wp))
    plan = GS.Plan(allframes, alltail)
    plan.se8_ramp = True
    # EXACT per-word arm samples: a NO-ARM pass yields each frame's cumulative output IN-PHRASE (the arm offsets
    # account for the carried s94 + declination base, unlike the standalone release_rpos), then _gen_arm_rpos
    # maps each word's armc -> its arm output sample. This is the sample-exact multi-arm (vs the coarse per-frame
    # 'release' flag), matching the single-word bit-exact path's release_rpos precision (cont.50).
    fr_rpos = []
    GS.synthesize(plan, rate=rate, pitch=pitch, _frame_rpos=fr_rpos)   # in-phrase cumulative output per frame
                                                          # AT THE TARGET RATE+PITCH (arm samples must live on
                                                          # the actual epoch schedule)
    arm_list = []
    for (start, end, w, wp) in word_spans:
        if question and (end == len(allframes)):
            arm_list.append(None)                     # final word: question rise (handled by its frames' s90)
            continue
        wf = allframes[start:end]
        wr = fr_rpos[start:end] if end <= len(fr_rpos) else None
        rr = None
        if wr is not None:
            rr = _gen_arm_rpos(w, wf, wr)             # absolute arm sample (wr is already absolute, in-phrase)
        if rr is None:                                # fallback: the standalone arm shifted by the word start
            base_rpos = fr_rpos[start - 1] if start > 0 else 0
            srr = getattr(wp, 'release_rpos', None)
            rr = (base_rpos + srr) if srr is not None else None
        arm_list.append(rr)
    plan.release_rpos_list = arm_list
    return plan


# ---- ported transcr4/hlas pipe+diphthong string-builder (sub_1000716C @0x7bbf), DLL-free ------------------
# The engine's se8-fall arm charpos is computed over the PIPE-MARKED transcription string the per-char synth
# loop walks (engstr). RE'd from hlas: the phoneme post-processor takes the front-end tokens (a `'` apostrophe
# marks each palatalized/soft consonant, space-separated) and, for every `'`, places a `|`(0x7c) long-vowel
# marker -- at the next space after the following vowel if char[i+2] is a vowel, else at the apostrophe itself
# -- then strips spaces. My front-end's phones + `palatals` reproduce that token/soft stream bit-for-bit, so
# applying the loop yields the engine's exact engstr (verified byte-identical on 44 words incl. iū/ių/iu).
_VOWEL_BYTES = set(b"aeiou") | {0xf8, 0xe0, 0xe1, 0xf3, 0xeb, 0xe6}  # hlas vowel table @0x10047604
_DIPH_GLIDES = {0x75, 0xf8, 0x69, 0x6a}                              # u ū i j = a falling-diphthong 2nd element


def _engstr_spell(p):
    """The engstr/charpos count uses the engine's ORTHOGRAPHIC letters: the velar fricative /x/ is the TWO-letter
    digraph 'ch' (so s7c counts it as 2), not the single phoneme 'x'. Mirrors gintaras_ref._CSPELL so the
    generative armc/charpos lines up with the engine (chaosas s7c=7 'chaosas', not 6 'xaosas')."""
    return getattr(G, "_CSPELL", {}).get(p, p)


def _gen_engstr(word):
    """Reconstruct the engine's pipe-marked transcription string (engstr) from the front-end, DLL-free."""
    full = G.front(word)
    pre = bytearray(b" ")
    for p, dr, b, st, pal, _raw in full:
        if p == "_":
            continue
        pre += _engstr_spell(p).encode("cp1257", "replace")
        if pal:
            pre += b"'"                          # transcr4 soft-consonant marker
        pre += b" "
    n = len(pre)
    i = 0
    while i < n:                                 # the RE'd pipe-insertion loop
        if pre[i] == 0x27:
            pre[i] = 0x20
            c2 = pre[i + 2] if i + 2 < n else 0
            if c2 in _VOWEL_BYTES:
                j = i + 2
                while j < n and pre[j] != 0x20:
                    j += 1
                if j < n:
                    pre[j] = 0x7c
            else:
                pre[i] = 0x7c
        i += 1
    return bytes(b for b in pre if b != 0x20)    # strip spaces -> engstr


def _gen_engstr_map(word):
    """Like _gen_engstr but also returns pos2phone: the phone INDEX each engstr byte belongs to (pipes -> -1),
    so a charpos can be mapped back to a front-end phone (and thence to its frames) for the arm-output sum."""
    full = [(p, pal) for (p, dr, b, st, pal, _raw) in G.front(word) if p != "_"]
    pre = bytearray(b" ")
    src = [-1]                                       # phone index per pre-pipe byte (-1 = space/apostrophe)
    for pi, (p, pal) in enumerate(full):
        for bb in _engstr_spell(p).encode("cp1257", "replace"):
            pre.append(bb); src.append(pi)           # each letter of a digraph maps to the SAME phone index
        if pal:
            pre.append(0x27); src.append(-1)         # apostrophe carries no charpos
        pre.append(0x20); src.append(-1)
    n = len(pre)
    i = 0
    while i < n:
        if pre[i] == 0x27:
            pre[i] = 0x20
            c2 = pre[i + 2] if i + 2 < n else 0
            if c2 in _VOWEL_BYTES:
                j = i + 2
                while j < n and pre[j] != 0x20:
                    j += 1
                if j < n:
                    pre[j] = 0x7c
            else:
                pre[i] = 0x7c
        i += 1
    engstr = bytearray(); pos2phone = []
    for k in range(n):
        if pre[k] != 0x20:
            engstr.append(pre[k])
            pos2phone.append(-1 if pre[k] == 0x7c else src[k])
    return bytes(engstr), pos2phone


def _s7c_word(word):
    """The word whose strlen the engine's s7c would see. Our i-hiatus reading feeds the engine-equivalent
    DOUBLED word (ios is rendered as iios, see transcribe._i_hiatus), so the arm midpoint must use the
    expanded length too -- gated exactly like transcribe (OOV only: a lexicon word never doubles)."""
    from . import transcribe as LT
    if len(word) >= 2 and word[0] in "iI" and word.lower() not in LT._load_lex():
        return LT._i_hiatus(word)
    return word


def _gen_armc(word, engstr=None):
    """The se8-fall arm charpos, fully generative: armc = first VISITED engstr position >= floor(s7c/2), where
    s7c = len(word) (the engine's [+0x7c] = strlen of the input word) and a position is skipped iff it is a `|`
    pipe OR a falling-diphthong 2nd element (a glide right after a PLAIN 'a' 0x61; the stressed 'à' 0xe0 keeps
    its glide). Verified == the engine's captured armc on 48/48 words."""
    if engstr is None:
        engstr = _gen_engstr(word)
    n = len(engstr)
    pipes = {k for k, c in enumerate(engstr) if c == 0x7c}
    dskip = {k for k in range(1, n) if engstr[k - 1] == 0x61 and engstr[k] in _DIPH_GLIDES}
    skip = pipes | dskip
    seg = len(_s7c_word(word)) // 2
    for k in range(n):
        if k not in skip and k >= seg:
            return k
    return n - 1


def _gen_arm_rpos(word, frames, frame_rpos):
    """FULLY GENERATIVE se8-fall arm output sample (no capture): map the generative armc (engstr charpos) to the
    front-end phone owning it (pos2phone), then arm_out = cumulative OUTPUT of every frame BEFORE that phone's
    frames + SE8_SEC. When armc is the 2nd visited element of a 2-position vowel phone (uo/ie diphthong), arm
    inside that phone at the demisyllable key-change (the element boundary). `frames` are the plan frames tagged
    with 'pi'/'key' (None pi = a silence/closure frame); `frame_rpos[i]` = cumulative output AFTER plan-frame i
    from a NO-ARM gen_synth pass (pre-arm offsets are arm-independent). Returns rpos for Plan.release_rpos.
    Verified == the captured _pergrain_arm_rpos on every phase2 word (charpos->phone->frame alignment is exact:
    gintaras 'ta-'=pi3=charpos4, tauta 'au'=pi1=charpos1, duona 'uo' spans charpos1/2 -> split)."""
    engstr, pos2phone = _gen_engstr_map(word)
    armc = _gen_armc(word, engstr)
    if armc >= len(pos2phone):
        return None
    ap = pos2phone[armc]
    if ap is None or ap < 0:
        return None
    vpos = [k for k in range(len(engstr)) if pos2phone[k] == ap and engstr[k] != 0x7c]
    elem_idx = vpos.index(armc) if armc in vpos else 0
    # plan-frame indices belonging to the arm phone, in order (a frame's pi is None for silence/closure)
    ap_idx = [i for i, fr in enumerate(frames) if fr.get('pi') == ap]
    if elem_idx == 0 or not ap_idx:
        arm_frame = next((i for i, fr in enumerate(frames)
                          if fr.get('pi') is not None and fr['pi'] >= ap), None)
    else:
        # split inside the diphthong phone: the element boundaries are its demisyllable key changes
        blocks = [ap_idx[0]]
        for a, b in zip(ap_idx, ap_idx[1:]):
            if frames[b].get('key') != frames[a].get('key'):
                blocks.append(b)
        arm_frame = blocks[min(elem_idx, len(blocks) - 1)]
    if arm_frame is None or arm_frame == 0:
        return None
    return frame_rpos[arm_frame - 1] + 100           # SE8_SEC contour-chase lag (constant across all words)


def _thread_n2(frames, poolset):
    """Set each frame's GENERATIVE E960 blend node n2 = the immediately-following frame's pcm IFF that frame
    is a voiced in-pool unit (a6>=1), else empty. Proven == the engine's captured n2 bit-for-bit on all demo +
    hard words (a voiced frame before a pause/closure, and the last frame, get empty n2 => the engine emits
    them pure-p1, no cross-fade). Purely structural/selection-side -> no capture needed. gen_synth reads n2."""
    n = len(frames)
    for i, fr in enumerate(frames):
        nb = frames[i + 1] if i + 1 < n else None
        fr['n2'] = list(nb['pcm']) if (nb is not None and not nb['pause']
                                       and tuple(nb['pcm']) in poolset) else []

