# -*- coding: utf-8 -*-
# gintaras_ref.py - faithful Gintaras reference synthesiser, built from the hlas.dll RE.
# Pipeline (see engine/RE_NOTES.md):
#   text -> transcr4 PradApdZod+KircTranskr+ilgiai+tonai (via transcr_cli.exe --full)
#        = phonemes + engine DURATIONS (ilgiai) + engine PITCH CONTOUR (tonai)
#        -> demisyllable unit selection (engine map collapses to bare-base units)
#        -> TD-PSOLA: stretch each unit to its duration AND resample each voiced pitch period to
#           the tonai F0 contour (removes the recorded per-frame pitch jitter -> engine-smooth)
#        -> overlap-add concat -> 22050 Hz PCM WAV.
# ilgiai durations are percentages; tonai gives per-phoneme (pos%,F0) breakpoints, base F0 90 Hz.
import os, sys, subprocess, wave
import numpy as np
from . import voice as W
# (voicepool_decode was the 16k Symbian path; not used in the 22050 runtime)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CLI  = os.path.join(ROOT, "transcr_cli.exe")

VOWELS = set("aeiou" "ąįęėųū" "oų")        # base vowels (cp1257 letters)
LONGV  = set("ąįęėųūo")                     # LONG vowels (ogonek/macron + o): far more stretch than short
                                            # (ilgiai alone can't tell labas long-ą from "ne" short-e: ~131 both)
GLIDE  = {"w": "u", "j": "j"}              # transcr4 glides -> unit alphabet
SONOR  = set("lmnrjvwNLMR")                # voiced sonorant consonants
GLIDES = set("jw")                         # brief offglides (diphthongs ai/ei/au...)
STOPS  = set("pbtdkgPBTDKG")               # plosives: closure (near-silent) + ONE release burst
# SR-dependent constants (set by set_sr); base values calibrated at 22050 Hz.
SR = 22050
# Engine pitch model (ltkonfig.txt line 1 field 1 = "intonation curve height" = 0.1): the synthesised
# F0 does NOT follow the raw tonai magnitude — it is heavily DAMPED toward a base. Measured on the
# ground truth, gt_gintaras stays flat ~75 Hz even though its tonai spikes to 108 on the N/t/a; only a
# 10% slice of each tonai deviation from the 90 Hz reference survives. So f0_out = F0_BASE + (tonai-90)*H.
# This reproduces gintaras (flat ~75-77), ne (~75), and labas's floor; the documented "stress curve
# height" 0.2 adds the extra rise on stressed long vowels (labas ą -> 80), applied as STRESS_H below.
INTON_H = 0.10                                    # ltkonfig field 1: intonation curve height
STRESS_H = 0.20                                   # ltkonfig field 2: extra rise on the stressed vowel
F0_BASE  = 75.0                                   # base/FLOOR output pitch (Hz). hlas.dll RE (sub_1000FC80
                                                  # tail FD66) clamps every voiced period to [minP,maxP]; the
                                                  # measured maxP=294 smp = 75.0 Hz is the floor EVERY word
                                                  # sits at (kaina/labas/mama/ne/namas/gintaras/tauta mode
                                                  # period = 294). minP=169 smp = 130.5 Hz is the ceiling.
F0_REF   = 90.0                                   # tonai reference level (transcr base F0)
F0_FLOOR = 75.0                                   # = SR/maxP(294); engine pitch NEVER drops below this
F0_CEIL  = 130.5                                  # = SR/minP(169); engine pitch clamp ceiling

def damp(f0_raw, stressed=False):
    """Map a raw tonai pitch to the engine's damped output F0, then apply the engine's hard pitch clamp
    [F0_FLOOR, F0_CEIL] (sub_1000FC80 tail FD66: period clamped to [minP=169, maxP=294])."""
    h = INTON_H + (STRESS_H if stressed else 0.0)
    f0 = F0_BASE + (f0_raw - F0_REF) * h
    return min(F0_CEIL, max(F0_FLOOR, f0))
K_VOWEL = 42.0; K_LONG = 74.0; K_SON = 34.0; K_GLIDE = 19.0     # (legacy per-type; superseded by K_DUR)
# K_DUR: the engine renders EACH phoneme to ilgiai*K_DUR samples (uniform -- measured against tts_cli
# ground truth: mama (81+77+81+129)*42 = 15456 vs engine 15559; the transcr4 ilgiai already encodes
# vowel length & stress, so there is no separate long-vowel constant). Same value at 22050 Hz.
K_DUR = 42.0
# NATIVE_DUR: the hlas.dll RE (engine/RE_NOTES.md, 2026-06-02) proved the engine does NOT compute a
# per-phoneme duration -- it plays each SELECTED unit's FULL NATIVE grain bank (one grain = one TD-PSOLA
# pitch period), so duration = the unit's recorded grain count. Verified: gintaras_ref's own selected
# units sum to exactly the engine's frame counts (kaina 63, tauta 26). So a voiced onset/body must render
# NATIVE (target None) rather than stretch to K_DUR. The accent lengthening is already in the SELECTION
# (stressed acute "a" picks a 24-grain unit, unstressed an 11-grain one).
NATIVE_DUR = True
# GRAIN_DUR: hlas.dll sub_1000E410 (epoch-placement) allots each VOICED grain a fixed output budget then
# fills it with pitch periods. The exact rule (FP @0x1000E528): per frame e4 += baseP (=rate/[1ad78]=
# 22050/100=220); place epochs while (f8+period)*100 <= a1*e4 with a1=[self+0xd4]=150 -> ratio 1.50. So a
# voiced grain occupies baseP*a1/100 = 220*1.50 = 330 samples (~1.12 periods at the 294-smp/75-Hz pitch).
# Rendering one grain as one 294-smp period (the first NATIVE_DUR cut) undercounts duration by 330/294
# ~=12%; allotting GRAIN_DUR samples/grain instead reproduces the engine length.
GRAIN_DUR = 330.0
# LONG_MULT: phonemically LONG vowels get DOUBLE the per-grain budget. Measured: sub_1000E410's baseP
# ([self+0x6c]/[self+0x1ad78]) is 220 for short vowels but 440 for long ones (lova long "o", gintaras long
# "i|"), so a long-vowel grain occupies 440*1.5 = 660 samples (~2.2 periods) vs 330 for short. kaina/tauta
# (short "ai"/"au") show baseP=440 on ZERO frames; lova shows 10, gintaras 6 -> exactly their long vowels.
LONG_MULT = 2.0
def _btarget(x):
    """Body/sonorant-onset target: None (play native grain bank) under the engine-faithful model, else the
    legacy K_DUR-stretched sample count."""
    return None if NATIVE_DUR else max(0.0, x)
def _grain_target(key, units, long=False, legacy=0.0):
    """Voiced-body output budget under the engine model: n_grains * GRAIN_DUR, DOUBLED for long vowels
    (baseP 220->440). For a SHORT vowel this equals the native n*GRAIN_DUR render (no behaviour change);
    for a LONG vowel it stretches to ~2.2 periods/grain. NATIVE_DUR off -> legacy K_DUR target."""
    if not NATIVE_DUR:
        return max(0.0, legacy)
    n = len(units.get(key, [])) if key else 0
    return n * GRAIN_DUR * (LONG_MULT if long else 1.0)
DBL_LONGV = {"o", bytes([0xf3]).decode("cp1257")}   # ONLY "o" (and its acute ó) get the double grain budget
def _is_long_v(v, key=None):
    """A vowel gets the DOUBLE grain budget (engine baseP 220->440) only for "o" (single, e.g. lova/žodis)
    or a pipe form (i|/u| = i/u ilgoji, e.g. gintaras "i|"). Measured: baseP=440 fires for o (lova 10,
    žodis 16) and i| (gintaras 6) but is ZERO for ū(sūnus)/ė(gėlė)/y(lyna)/ą(namas) -- those long vowels
    get their length from GRAIN COUNT, not baseP -- and ZERO for diphthongs (kaina ai / tauta au / duona
    uo). So the double applies to o/pipe only; everything else uses the single GRAIN_DUR budget."""
    return (len(v) == 1 and v in DBL_LONGV) or bool(key and str(key).endswith("|"))
SIL_K = 9.0; SIL_MAX = int(0.10 * 22050)
JOIN_PER = 220                              # voiced-join overlap ~ one pitch period (samples)
CLOSURE_LEN = int(0.010 * 22050)            # plosive closure: ~10 ms (220 smp = ONE base period). The engine
                                            # does NOT add a separate 50 ms gap -- its Cv- onset unit already
                                            # carries the closure (tauta ta- = 1026,222 periods incl. the
                                            # ~222-smp closure), so a long extra sil double-counted duration
                                            # (tauta 1.21). Matched to the engine's own ~222-smp closure period.
VOWEL_BASE  = int(0.015 * 22050)            # vowel content the onset/coda diphones already supply; only
                                            # duration BEYOND this gets a bare-vowel sustain (short V -> none)

def set_sr(sr):
    """Rescale all sample-domain constants from the 22050 Hz calibration to `sr`."""
    global SR, K_VOWEL, K_LONG, K_SON, K_GLIDE, K_DUR, SIL_MAX, JOIN_PER, GRAIN_DUR
    r = sr / 22050.0
    SR = sr
    K_VOWEL, K_LONG, K_SON, K_GLIDE = 42.0 * r, 74.0 * r, 34.0 * r, 19.0 * r
    K_DUR = 42.0 * r
    GRAIN_DUR = 330.0 * r
    SIL_MAX = int(0.10 * sr)
    JOIN_PER = int(round(220 * r))
    global CLOSURE_LEN, VOWEL_BASE
    CLOSURE_LEN = int(0.010 * sr)
    VOWEL_BASE = int(0.015 * sr)

# ---------------------------------------------------------------- front-end
UNITSET = set()                                   # unit keys, filled by main() so the front-end can pick
                                                  # the diphthong spelling the voice actually recorded
# transcr emits a diphthong as TWO tokens: vowel + glide ('j' or 'w'). The engine merges them into ONE
# nucleus and uses whichever spelling has an onset+body pair recorded (hook capture: "tei" e+j -> ei-/-ei,
# "tai" a+j -> aj). j -> i|j, w -> u (the only recorded w-spelling, e.g. au/ou).
GLIDE_SPELL = {"j": ["i", "j"], "w": ["u"]}
# The engine spells each diphthong with a FIXED nucleus, not by inventory scoring (e.g. /ai/ -> "aj" even
# though "ai-"/"-ai" also exist; /ei/ -> "ei"). Hook ground truth: taip a+j->aj, sveiki e+j->ei, *au a+w->au.
FIXED_DIPH = {("a", "j"): "aj", ("e", "j"): "ei", ("a", "w"): "au"}
# A LONG ogonek vowel + glide still forms a SHORT-spelled diphthong nucleus (saule ą+w -> "au", engine
# są- + au-/-au). Fold the ogonek back to its short base so the nucleus matches the recorded diphthong unit.
LONG2SHORT = {"ą": "a", "ę": "e", "į": "i", "ų": "u", "ė": "e"}

def merge_diphthongs(recs):
    """Merge [vowel, glide] token pairs into one diphthong nucleus token, choosing the spelling whose
    onset/body units exist in UNITSET. recs = [(raw_tok, dur, bps), ...]. Returns the same shape."""
    out, i = [], 0
    while i < len(recs):
        tok, dur, bps = recs[i]
        v = norm(tok)
        # a glide j/w forms a diphthong offglide ONLY if it is NOT itself an onset for a following vowel
        # (intervocalic V-j-V: the j is a consonant, e.g. vejas v-e-J-a-s -> je- onset, NOT a "ej" diphthong).
        glide_next = (i + 2 >= len(recs)) or recs[i + 2][0] == "_" or not is_vowel(norm(recs[i + 2][0]))
        if (i + 1 < len(recs) and tok != "_" and len(v) == 1 and is_vowel(v) and glide_next
                and recs[i + 1][0].lower().replace("'", "") in GLIDE_SPELL):
            g = recs[i + 1][0].lower().replace("'", "")
            vb = LONG2SHORT.get(v, v)              # diphthong UNIT spelling uses the short base...
            fixed = FIXED_DIPH.get((vb, g))
            best, score = None, -1
            if fixed and any(k in UNITSET for k in (fixed, "-" + fixed, fixed + "-")):
                best, score = fixed, 99            # engine's fixed spelling wins if it is recorded
            for sp in GLIDE_SPELL[g]:
                nuc = vb + sp
                s = 10 * (("-" + nuc) in UNITSET) + 5 * ((nuc + "-") in UNITSET) + (nuc in UNITSET)
                if s > score:
                    best, score = nuc, s
            # ...but if the first vowel was LONG, keep its ogonek char as the nucleus head so the syllable
            # ONSET uses the long form (saule -> są-, engine match); diph_units() folds it back for lookup.
            if best is not None and v != vb and best[:1] == vb:
                best = v + best[1:]
            if score > 0:                          # found a recorded diphthong unit -> merge the pair
                if any(c.isupper() for c in tok):  # carry the vowel's stress into the merged nucleus
                    best = best[0].upper() + best[1:]
                out.append((best, dur + recs[i + 1][1], bps)); i += 2; continue
        out.append((tok, dur, bps)); i += 1
    return out

def _recs_to_full(recs):
    """Shared post-processing: merge diphthongs + normalise each transcr4-format token to the unit
    alphabet, tagging stress. Used by both frontend() (transcr4) and frontend_free() (pure-Python)."""
    out = []
    for tok, dur, bps in merge_diphthongs(recs):
        # transcr marks the STRESSED phone with an uppercase letter (e.g. labas long-a "aA", gintaras "I")
        stressed = tok != "_" and any(c.isupper() for c in tok.replace("'", ""))
        # PALATALIZATION: transcr appends ' to a soft consonant (l'/tS'/dZ'). norm() strips it, but the soft
        # vs hard distinction drives the engine's pipe-body choice for a word-final long-ū (ačiū/svečių/brolių
        # -> ū| after a SOFT consonant; sūnų/draugų/namų/vyrų -> dashed -Cū after a HARD one). Keep it as a 5th
        # parallel field so build_tiling can gate on it. (Unit KEYS never contain ', so phone stays stripped.)
        palatal = "'" in tok
        low = tok.replace("'", "").lower()
        # merged diphthong nucleus (two DIFFERENT vowels); exclude doubled long vowels uu/ii (=ų/į),
        # whose 2nd char also happens to be in the glide set "iju" -> they must go through norm()'s LONG map.
        isdiph = len(low) == 2 and low[0] != low[1] and low[0] in "aeiou" and low[1] in "iju"
        phone = "_" if tok == "_" else (low if isdiph else norm(tok))
        out.append((phone, dur, bps, stressed, palatal))
    return out

# ---------------------------------------------------------------- transcr4-FREE front-end
# Same shape as frontend(), but NO transcr4.dll: phonemes come from lt_transcribe (rule G2P + the
# stress lexicon extracted from transcr4), durations from lt_ilgiai (BIT-EXACT port of `ilgiai`),
# pitch breakpoints from lt_tonai (faithful port of `tonai`). This makes the whole pipeline
# text->audio run with only the Gintaras voice data (wtvlt1.dta) + pure Python.
def frontend_free(text):
    from . import transcribe as lt_transcribe, duration as lt_ilgiai, tonai as lt_tonai
    words = [w for w in text.split() if w]
    toks = ["_"]
    for wi, w in enumerate(words):
        phones = [t for t in lt_transcribe.transcribe(w) if t != "_"]   # drop per-word boundaries
        if not phones:
            continue
        if len(toks) > 1:
            toks.append("+")                       # word boundary marker (as KircTranskr emits)
        toks.extend(phones)
    toks.append("_")
    dur_pairs = lt_ilgiai.ilgiai(toks)             # [(tok, dur), ...] incl ("+", 0)
    dur_lines = ["%s %d" % (t, d) for t, d in dur_pairs]
    recs = [(t, d, bps) for (t, d, bps) in lt_tonai.tonai(dur_lines)]
    return _recs_to_full(recs)

# pick the front-end: "transcr4" (uses transcr_cli.exe) or "free" (pure-Python ports)
FRONTEND = "transcr4"
def front(text):
    return frontend_free(text)   # the pure-Python (DLL-free) front-end

# transcr DOUBLED long vowels (aA/aa/Aa ...) map to the cp1257 ogonek chars the unit keys use
# (debugger-verified: labas's long-a "aA" -> unit 'lą-', byte 0xe0). Single stressed (A/E/I) stay plain.
# IMPORTANT: transcr distinguishes ė="ee" (closed long e -> ė 0xeb) from ę="eA"/"ea" (open long e ->
# ę 0xe6, handled by VLONG2); both ų and ū come as "uu" -> ų 0xf8 (SAME long-u sound, as intended); y
# (long i) -> "ii" -> į like į itself.
LONG = {"aa": "ą", "ee": "ė", "ii": "į", "uu": "ų", "oo": "o"}

AFFRIC = {"ts": "c", "dz": "dz"}                  # transcr affricates -> unit-key chars (c=ts -> "ts'" -> c)
# transcr writes the LETTER (not stress) in caps for the hush/affricate consonants: š=S, ž=Z, č=tS, dž=dZ.
# The voice's unit keys spell these with the cp1257 single chars (š=0xf0, ž=0xfe, č=0xe8, dž=0x…). Map them
# BEFORE lowercasing so č("tS") is not confused with c("ts"), and š("S") not with s.
HUSH = {"S": "š", "Z": "ž", "tS": "č", "dZ": "dž"}  # š ž č dž
VLONG2 = {"ea": "ę"}                                # transcr's open-long-e digraph -> ę unit char (metai mę-)

def norm(tok):
    tok = tok.replace("'", "")
    if tok in HUSH: return HUSH[tok]         # š/ž/č/dž (capital S/Z = the letter, not stress)
    low = tok.lower()
    if low in VLONG2: return VLONG2[low]     # "ea" -> ę
    if low in GLIDE: return GLIDE[low]
    if low in AFFRIC: return AFFRIC[low]     # affricate "ts"/"dz" -> single unit char (engine: cu-)
    if len(low) == 2 and low[0] == low[1]:   # doubled long vowel -> ogonek unit char
        return LONG.get(low, low[0])
    return low

def is_vowel(p): return p != "_" and p[0] in VOWELS
def is_voiced(p): return is_vowel(p) or p in SONOR

# ---------------------------------------------------------------- F0 contour
def build_f0(full, smooth=False):
    """Piecewise-linear F0(t) over the whole word, t in cumulative ilgiai-duration units.
    Returns (f0_fn, spans) where spans[i]=(t0,t1) for full[i].

    smooth (say.py sentence path only -- OFF by default so the BIT-EXACT selection path is byte-identical):
    the per-phone tonai breakpoints make the contour BOUNCE -- obstruent consonants carry their own low f0 and
    the per-vowel stress boost (damp h=0.30) pops stressed vowels ~8 Hz above unstressed, so 'turiu DU' steps
    riu(84)->d(78)->du(83) = audible jumps; and a stressed vowel with NO tonai bp ('du'->'U 124') slides down
    into the phrase fall instead of holding its accent. The engine keeps the tonai gentle and adds prominence
    via the smooth se8 declination (which the say.py TD-PSOLA lacks). So in smooth mode the contour is built
    from the VOICED nuclei only (vowels + sonorants) -- one breakpoint per such phone at the MEAN of its tonai
    bps (or the held incoming level if it has none) -- and obstruent consonants get the smoothly-interpolated
    value. This removes the per-consonant dips and the empty-bp slide; the syllable-to-syllable prominence
    (the natural part) is kept."""
    cum, t = [], 0.0
    for _, dur, _bps, _st, *_ in full:
        cum.append((t, t + dur)); t += dur
    if not smooth:
        xs, ys = [], []
        for (t0, t1), (_, dur, bps, _st, *_) in zip(cum, full):
            for pos, f0 in bps:
                xs.append(t0 + pos / 100.0 * dur); ys.append(float(f0))
        if not xs:
            xs, ys = [0.0, t], [90.0, 90.0]
        order = np.argsort(xs); xs = np.array(xs)[order]; ys = np.array(ys)[order]
        def f0_fn(tt): return float(np.interp(tt, xs, ys))
        return f0_fn, cum
    # smooth: one breakpoint per VOICED nucleus at its DAMPED mean tonai f0 (empty-bp -> previous level/hold).
    # The damp (incl. the per-syllable stress boost) is baked IN here so obstruent consonants interpolate the
    # already-damped value -> the stress prominence becomes a smooth syllable bump, not a per-phone step. The
    # caller (say.py) samples f0_fn directly WITHOUT re-applying damp. Returned f0 is the final output pitch.
    xs, ys = [], []
    prevraw = 90.0
    for (t0, t1), (p, dur, bps, st, *_) in zip(cum, full):
        if not is_voiced(p):
            continue                                  # obstruents/closures: no breakpoint -> interpolated
        raw = (sum(f for _pos, f in bps) / len(bps)) if bps else prevraw   # mean of the phone's bps, or hold
        prevraw = raw
        xs.append((t0 + t1) / 2.0); ys.append(damp(raw, st and is_vowel(p)))
    if not xs:
        xs, ys = [0.0, t], [F0_BASE, F0_BASE]
    xs = np.asarray(xs); ys = np.asarray(ys)
    def f0_fn(tt): return float(np.interp(tt, xs, ys))
    return f0_fn, cum

# ---------------------------------------------------------------- demisyllable selection (engine-exact)
# Reverse-engineered from the LIVE engine via the CMapStringToOb::Lookup hook (engine/capture_units.py
# logs every key hlas.dll looks up; the hits = units actually used). The Gintaras voice builds, per
# syllable, an ONSET demisyllable `Cv-` then a BODY demisyllable `-Cv` (the coarticulated steady vowel,
# 11-24 REAL recorded frames -- not a stretched bare vowel). Ground truth (engine_units.json):
#   labas    l aA b a s        -> la-  -la  ba-  -ba  -as          (long ą via ogonek char)
#   gintaras g' I N t a r a s  -> gi-  i|   n   ta-  -ta  ra-  -ra  -as
#   ne       n' E              -> ne-  -ne
#   sveiki   s' v' e j k' i    -> s    ve-  ei-  -ei  i|            (ki- absent -> k not rendered, as engine)
#   taip     t A J p           -> ta-  aj   p
#   aciu     a c' u            -> a    cu-  u|
#   duona    d u o n a         -> du-  uo-  -uo  na-  -na
# Rules: long/stressed vowels carry the ogonek char in the key (front-end already emits it); a STRESSED
# SHORT i/u body is `i|`/`u|` (pipe), not `-Ci`; a word-initial vowel is the bare body `v`; a DIPHTHONG
# adds its own onset `v1v2-` + body `-v1v2`; a word-final FRICATIVE folds into the `-Vc` coda (NO
# standalone copy = the old doubled "ss"); a final STOP, or a cluster consonant (nasal before a stop,
# leading s in "sv-"), is a STANDALONE unit. The ONSET key is ONLY `Cv-`: if absent the engine renders no
# onset (so "ki" -> just the `i|` body), which is exactly why the engine itself under-articulates that k.
FRIC = set("sšzžfhc")                              # fricatives/affricates -> coda folds into -Vc

def _first(cands, units): return next((c for c in cands if c in units), None)

# Phoneme -> orthographic unit-KEY spelling. The voice keys the velar fricative /x/ (the "ch" digraph, e.g.
# choras/chemija/chaosas) by its LETTERS "ch", not the phoneme `x` -- and its body takes a DOUBLE dash
# (`--cho`, not `-cho`). Without this map build_tiling looks for a nonexistent `xo-` and DROPS the x (choras
# -> "oras"). Verified vs the engine Lookup (choras -> cho-/--cho, chaosas -> cha-/--cha).
_CSPELL = {"x": "ch"}                              # /x/ -> "ch"
_CDBLBODY = {"x"}                                  # consonants whose `-Cv` body is double-dashed (`--chv`)


def _cs(c):
    return _CSPELL.get(c, c)


def onset_unit(c, v1, units):
    """`Cv-` onset demisyllable (consonant into the vowel's first char). ONLY the true onset spelling --
    no -Cv backoff (that is a BODY form); None if unrecorded == engine renders no onset for this C."""
    cs = _cs(c)
    return _first([cs + v1 + "-", cs + v1 + v1 + "-"], units)

def body_unit(c, v, pipe, units):
    """`-Cv` coarticulated steady-vowel body (real frames). A short high vowel uses the `i|`/`u|` (pipe)
    form when `pipe` is set -- see _use_pipe() for when. Otherwise the dashed `-Cv` body."""
    cs = _cs(c)
    dash = "--" if c in _CDBLBODY else "-"            # the ch body is double-dashed (`--cho`)
    cands = []
    if pipe and len(v) == 1 and (v in "iuo" or v == U_OG):
        cands.append(v + "|")                          # i|/u|/o| pipe (or long-ū ū|) -- soft-C / word-final
    cands += [dash + cs + v, "-" + v, v + "|", v]
    return _first(cands, units)

def _use_pipe(phones, j, v, prev_soft=True):
    """Whether the short high vowel phones[j] takes the `v|` form (ground truth): short `i` -> `i|` in every
    position it is recorded (gintaras gin, viso vi, sveiki final i); short `u` -> `u|` only WORD-FINALLY AND
    after a SOFT (palatalized) consonant (ačiu c'u -> u|), otherwise the `-Cu` body (du d-u -> -du, hard d;
    ruta -ru, puikus -ku). Long/diphthong vowels never use the pipe. The soft gate mirrors the word-final
    long-ū rule (soft->ū|, hard->-Cū) and is verified vs the engine Lookup (du -> -du, ačiu -> u|)."""
    if len(v) != 1:
        return False
    if v == "i":
        return True
    if v == "u":
        # u after a SOFT (palatalized) consonant -> the `u|` pipe in ANY position (the pipe carries the palatal
        # 'i' coloring: kompiuteris p'u, šiukšlės š'u, vilnius n'u, ačiū c'u all -> u|). After a HARD consonant
        # it's the `-Cu` dashed body (du d-u, niekur k-u, ruta -ru). Verified vs the engine Lookup. The earlier
        # near-final gate was wrong (it gave `-pu` for the medial p'u in kompiuteris -> sounded "komputeris").
        return prev_soft
    if v == "o":
        return prev_soft                               # long /o:/ after a SOFT consonant -> o| (milijonas jo,
                                                       # keturiolika rio); after a HARD one -> -Co (lova/žodis)
    return False

U_OG = bytes([0xf8]).decode("cp1257")              # ų (cp1257 0xf8) -- the STRESSED u-offglide spelling
A_OG = bytes([0xe0]).decode("cp1257")              # ą (cp1257 0xe0)
FALLING_GLIDE = set("iju") | {U_OG}                # 2nd element of a FALLING diphthong (ai ei au ui oi)

def glide_unit(v1, g, stressed, units):
    """SINGLE recorded unit for a FALLING diphthong (nucleus+offglide), e.g. au/aų/aj/-àj/-ej/-uj/-oj.
    The engine plays the syllable as onset `Cv1-` + this ONE glide unit -- NOT onset+body of the
    diphthong (that articulates the vowel twice = the audible doubling). Spelling/dash form is fixed
    by what the voice recorded; candidate order below reproduces every hooked engine choice:
      taip a+j->aj  metai a+j->aj  kaina ą+j->-àj  veidas ę+j->-ej  puikus u+j->-uj  o+j->-oj
      tauta a+u->au (unstressed)   laukas a+u->aų (stressed)."""
    base = LONG2SHORT.get(v1, v1)                   # ascii base of the first vowel (ą->a, ę/ė->e, ų->u)
    if g in ("u", "w", U_OG):                       # u-offglide (au): stressed picks the ų(0xf8) spelling
        cands = ([base + U_OG, "-" + base + U_OG, base + "u", "-" + base + "u"] if stressed
                 else [base + "u", "-" + base + "u", base + U_OG, "-" + base + U_OG])
    else:                                           # j/i-offglide (ai/ei/ui/oi)
        if v1 == A_OG:                              # ą keeps its ogonek (kaina -àj); ę/ė fold to ascii -ej
            cands = ["-" + v1 + "j", v1 + "j", base + "j", "-" + base + "j"]
        else:
            cands = [base + "j", "-" + base + "j", "-" + v1 + "j", base + "i", "-" + base + "i"]
    return _first(cands, units)

def diph_units(v1v2, units):
    """Diphthong nucleus: its own onset `v1v2-` (if recorded) and body. With an onset the body is the
    dashed `-v1v2` (duona uo-/-uo, sveiki ei-/-ei); WITHOUT an onset the engine uses the BARE body
    `v1v2` (taip: no `aj-` -> just `aj`, not `-aj`). A long-ogonek head (saule "ąu") is folded to its
    short base for the diphthong UNIT lookup (the recorded diphthongs are short-spelled: au-, -au)."""
    s = LONG2SHORT.get(v1v2[:1], v1v2[:1]) + v1v2[1:]
    on = (s + "-") if (s + "-") in units else None
    bod = _first((["-" + s, s] if on else [s, "-" + s]), units)
    return on, bod

def init_vowel(v, units):
    """Bare vowel body for a word-initial / post-vowel vowel that has no onset consonant."""
    return _first([v, "-" + v, v + "-"], units)

def coda_unit(v, c, units):
    """`-Vc` coda: the preceding vowel gliding into a word-final consonant (engine: -as). A short coda
    `u` ROUNDS to `o` before an obstruent (the voice recorded only `-os/-ok/-op/-ot`, never `-us/-uk`;
    puikus -os, auksas -ok), so try the `-oc` spelling after the literal `-uc`."""
    cands = ["-" + v + c]
    if v == "u":
        cands.append("-o" + c)
    cands += ["-" + c, c, c + "-"]
    return _first(cands, units)

def standalone_unit(c, units):
    """Standalone consonant unit (final stop, cluster nasal, leading fricative of an onset cluster)."""
    cs = _cs(c)
    return _first([cs, cs + "-", "-" + cs], units)

def _kv(v):
    # A DIPHTHONG nucleus (len 2) carries its FULL ilgiai duration (the merged a+u value), so it scales
    # like a (short) vowel, NOT a brief offglide -- K_GLIDE truncated the recorded glide (laukas "lakas"),
    # while K_LONG double-counted it (kaina 1.16s). K_VOWEL on the already-summed ilgiai is right.
    if len(v) == 2:
        return K_VOWEL
    return K_LONG if v in LONGV else K_VOWEL

def _onset_len(on, f0, units):
    """Approx samples the native onset unit `on` will occupy (one pitch period per frame at this F0).
    The body must fill only the REMAINDER of the vowel's duration -- otherwise a long-vowel onset (e.g.
    lą- = 9 vowel periods) plus a full body = the vowel played ~twice (the "aa/ee/ii" doubling)."""
    return (len(units[on]) * SR / max(60.0, f0)) if (on and on in units) else 0.0

def build_tiling(phones, durs, f0s, stresses, units, meta=None, palatals=None):
    """Walk phones left-to-right, emitting the engine's onset/body/coda demisyllables (see note above).
    Element = (kind, key, target, vin, vout, f0); kind 'dip'=play native, 'body'=PSOLA-stretch to target
    samples, 'sil'=stop closure silence. A C+V pair is consumed together (onset+body); leftover
    consonants are codas/clusters.
    If `meta` is a list, it is filled PARALLEL to elems with (phone_index, is_vowel, stressed) for the
    primary phone each element came from (the C+V pair tags both onset and body with the VOWEL's phone) —
    used by plan_frontend to place the s90 stress contour."""
    n = len(phones)
    elems = []
    i = 0
    prev_pipe = False                              # did the previous vowel body use the i|/u| pipe form?

    def _tag(pi):                                  # tag every elem appended since the last _tag with phone pi
        if meta is not None:
            while len(meta) < len(elems):
                meta.append((pi, is_vowel(phones[pi]), bool(stresses[pi])))

    while i < n:
        p, dur, f0 = phones[i], durs[i], f0s[i]
        prev = phones[i - 1] if i > 0 else None
        nxt = phones[i + 1] if i + 1 < n else None
        if is_vowel(p):
            # vowel with NO onset consonant (word-initial or hiatus): bare body / diphthong onset+body.
            # Rendered to its own ilgiai*K_DUR (per-phoneme duration, like the engine).
            prev_pipe = False
            wi_falling = (len(p) == 2 and p[1] in FALLING_GLIDE
                          and not (p[1] in ("u", "w", U_OG) and p[0] in LONGV))
            if wi_falling:
                # word-initial SHORT falling diphthong. J/I-offglide (eiti ei): bare init vowel `e` +
                # dashed offglide `-ej` (the offglide needs a nucleus laid first). U/W-offglide (aukštas
                # au): glide_unit returns the COMPLETE non-dashed nucleus `aų`, which the engine plays
                # ALONE word-initially -- NO bare init vowel (verified: aukštas ENG = just 5118 'aų', my
                # old `init_vowel('a')` 4470 was spurious). Ogonek-head 'ąu'/'ąj' never reach here (they
                # take the elif-diph_units branch, p[0] in LONGV), and stay correct (auksas/aitvaras).
                gl = glide_unit(p[0], p[1], stresses[i], units)
                ivk = None if p[1] in ("u", "w", U_OG) else init_vowel(p[0], units)
                nd = _onset_len(gl, f0, units) if gl else 0.0
                if ivk: elems.append(("body", ivk, _grain_target(ivk, units, _is_long_v(p[0], ivk),
                                      dur * K_DUR - nd), True, True, f0))
                if gl: elems.append(("dip", gl, None, True, True, f0))
            elif len(p) == 2:
                on, bod = diph_units(p, units)
                if on:  elems.append(("dip", on, None, True, True, f0))
                if bod: elems.append(("body", bod, _grain_target(bod, units, _is_long_v(p, bod),
                                      dur * K_DUR - _onset_len(on, f0, units)), True, True, f0))
            else:
                bod = init_vowel(p, units)
                if bod: elems.append(("body", bod, _grain_target(bod, units, _is_long_v(p, bod),
                                      dur * K_DUR), True, True, f0))
            _tag(i)
            i += 1
        elif p != "_":
            c = p
            if nxt and is_vowel(nxt):
                # ONSET consonant + its vowel: Cv- (native) then the body (-Cv / v|), consumed together
                v, vst, vdur, vf0 = nxt, stresses[i + 1], durs[i + 1], f0s[i + 1]
                kv = _kv(v); v1 = v[0] if len(v) == 2 else v
                # LAST-VOWEL short pipe-vowel: the voice recorded a dedicated combined ONSET `Cv|--`
                # (eiti ti|--, naktis/dantis ti|--, lu|--) -- a clean 2-frame take of the C-into-pipe-vowel
                # transition the engine uses for the word's FINAL i/u syllable, whether the vowel is the last
                # phoneme (eiti) OR is followed by a coda consonant (naktis n-a-k-t-i-s -> ti|-- + i| + s).
                # Gated on the unit EXISTING (only lo|--/lu|--/ti|-- recorded), so consonants without a combo
                # back off to the plain `Cv-` (brolis li|-- absent -> li-; the `i|`/`u|` pipe body still follows).
                last_vowel = not any(is_vowel(phones[t]) for t in range(i + 2, n))
                combo = (c + v + "|--") if (len(v) == 1 and v in "iu" and last_vowel) else None
                # COMBINED consonant + rising-diphthong pipe unit `Cuo|`/`Cie|` (juodas juo|): the voice recorded
                # the whole C-into-diphthong syllable as ONE stretchable body -> NO separate onset. Gated on the
                # unit existing (only juo| so far), so other C+uo/ie back off to the onset+body pair below.
                diph_combo = (c + v + "|") if (len(v) == 2 and (c + v + "|") in units) else None
                on = None if diph_combo else (combo if (combo and combo in units) else onset_unit(c, v1, units))
                # engine: vowel | closure gap | burst -- but ONLY when the stop's burst onset exists; with
                # no onset unit (e.g. "ki" has no ki-) there is no burst, so a closure would be a dead gap.
                # Build the post-onset chain of unit keys for this vowel/diphthong. `chain` = ordered keys
                # after the consonant onset; `dipkeys` = those that are native diphthong onsets (ei-/uo-)
                # played native in the split path; `pbod` = the final stretchable body (None if a bare
                # diphthong glide sustains the nucleus itself).
                falling = (len(v) == 2 and v[1] in FALLING_GLIDE
                           and not (v[1] in ("u", "w", U_OG) and v[0] in LONGV))
                chain, dipkeys, pbod = [], set(), None
                if falling and v == "ei":
                    # short e+j: recorded as the `ei-`/`-ei` onset+body pair (sveiki), not `-ej`.
                    dion, dbod = diph_units(v, units)
                    if dion: chain.append(dion); dipkeys.add(dion)
                    if dbod: chain.append(dbod); pbod = dbod
                elif falling:
                    # FALLING diphthong: onset `Cv1-` laid the first vowel. BARE glide (`aj`/`au`/`aų`) is a
                    # self-contained nucleus+offglide (taip, laukas) -> the glide IS the body. DASHED `-Vj`
                    # (`-uj`/`-àj`/`-ej`) -> engine sustains the nucleus (`-Cv` body) THEN the offglide
                    # (kaina kà-/-kà/-àj). No diphthong onset+body pair -> no vowel doubling.
                    gl = glide_unit(v[0], v[1], vst, units)
                    if gl and gl.startswith("-"):
                        nbody = body_unit(c, v[0], False, units)
                        if nbody: chain.append(nbody); pbod = nbody
                        if gl: chain.append(gl); dipkeys.add(gl)
                    elif gl:
                        # BARE glide (`aj`/`au`/`aų`) is a self-contained nucleus+offglide recording -> play
                        # NATIVE, never stretch (stretching ping-pongs a->i->a = a re-articulated diphthong,
                        # the taip 0.51s bug). In the sonorant grp path it is compressed with the onset.
                        chain.append(gl); dipkeys.add(gl)
                elif diph_combo:
                    chain.append(diph_combo); pbod = diph_combo   # the Cuo|/Cie| combined body (no onset)
                elif len(v) == 2:
                    # RISING diphthong (uo/ie) or long-ą+u (saule): recorded onset+body pair.
                    dion, dbod = diph_units(v, units)
                    if dion: chain.append(dion); dipkeys.add(dion)
                    if dbod: chain.append(dbod); pbod = dbod
                else:
                    # A word-final LONG-ū (0xf8) after a SOFT (palatalized) consonant takes the pipe body `ū|`
                    # (ačiū/svečių/brolių -> ū|); after a HARD consonant it keeps the dashed -Cū (sūnų/vyrų).
                    # Verified vs the engine Lookup on 11 words (7 soft->pipe, 4 hard->dashed). Gated on the
                    # ū| unit existing; no passing word ends in soft-C + long-ū so this can't degrade them.
                    # long-ū pipe after a soft consonant: word-final OR before exactly ONE final consonant
                    # (jūs j-ū-s -> ū| before the final s; sūnų hard s -> dashed).
                    u_near_final = (i + 1 == n - 1) or (i + 2 == n - 1 and not is_vowel(phones[n - 1]))
                    soft_finalU = (v == U_OG and u_near_final and palatals is not None and palatals[i])
                    prev_soft = (palatals[i] if palatals is not None else True)   # preceding consonant soft?
                    bod = body_unit(c, v, _use_pipe(phones, i + 1, v, prev_soft) or soft_finalU, units)
                    if bod: chain.append(bod); pbod = bod
                prev_pipe = bool(pbod and pbod.endswith("|"))
                # PER-PHONEME DURATION (matches the engine, measured vs tts_cli ground truth): every
                # phoneme is rendered to its OWN ilgiai*K duration (K_DUR uniform -- ilgiai already encodes
                # vowel length, so there is NO extra long-vowel multiplier). The consonant gets ic=durs[i],
                # the vowel iv=durs[i+1]. A SONORANT onset (m/l/n/r/v) is sweep-stretched to ic*K (the m in
                # "mama" lasts ~0.15s, its own ilgiai); a STOP/fricative onset plays its burst/frication
                # native after a closure (stretching tiles the noise). The body fills iv*K minus the native
                # diphthong dips (diph-onset ei-/uo-, offglide -àj/-ej).
                ic, iv = durs[i], vdur
                nat_dips = sum(_onset_len(k, vf0, units) for k in chain if (k in dipkeys or k is not pbod))
                if on and c in SONOR:
                    elems.append(("dip", on, _btarget(ic * K_DUR), True, True, vf0))
                else:
                    if on and c in STOPS and prev and is_vowel(prev):
                        elems.append(("sil", None, CLOSURE_LEN, False, False, f0))
                    if on: elems.append(("dip", on, None, True, True, vf0))
                    nat_dips += _onset_len(on, vf0, units)     # stop/fricative onset plays native -> subtract
                _tag(i)                             # sil + onset belong to the CONSONANT phone (i)
                vlong = _is_long_v(v, pbod)
                for k in chain:
                    if k in dipkeys or k is not pbod:
                        elems.append(("dip", k, None, True, True, vf0))
                    else:
                        elems.append(("body", k, _grain_target(k, units, vlong, iv * K_DUR - nat_dips),
                                      True, True, vf0))
                _tag(i + 1)                         # the vowel body/diphthong belongs to the VOWEL phone (i+1)
                i += 2
            else:
                # leftover consonant. A consonant PRECEDED BY A VOWEL (coda position -- whether word-final
                # or before another consonant) folds into the `-Vc` coda demisyllable if that exact unit was
                # recorded (engine: darbas -ąr, spalva -al, vanduo -an, akmuo -ak, labas -as). Only the FULL
                # `-Vc` form counts: if absent the engine plays the consonant STANDALONE (gintaras n: no -in
                # unit -> bare `n`). This single rule covers final fricatives AND medial cluster codas.
                pv = (prev[-1] if (prev and len(prev) == 2) else prev)
                # a vowel rendered with the i|/u| pipe body already closes its syllable, so the following
                # coda consonant is STANDALONE (gintaras i|+n -> bare `n`, though `-in` exists); after a
                # dashed `-Cv` body the coda takes the `-Vc` form (darbas -dą then -ąr).
                # short coda `u` rounds to `o` before an obstruent (puikus -os, auksas -ok): the voice
                # never recorded `-us/-uk`, only `-os/-ok`, so fall through to the `-oc` spelling.
                # If the NEXT consonant is an UNBURSTABLE stop (a stop whose `Cv-` onset for the following
                # vowel was not recorded, so the engine drops it -- penki n-K-i: no `ki-`), this consonant is
                # rendered STANDALONE, not as a `-Vc` coda (engine penki: ...-pe `n` i|, NOT `-en`). The
                # dropped stop leaves the preceding consonant syllable-final-but-onsetless -> bare unit.
                nxt_unburst = (nxt in STOPS and i + 2 < n and is_vowel(phones[i + 2])
                               and onset_unit(nxt, phones[i + 2][0], units) is None)
                # An `n` before `k` (the velar `nk` cluster, n->ŋ) is rendered STANDALONE (the long nasal), not
                # as the `-Vc` coda: engine penki/penkiolika e-n-K -> `n`, not `-en`. This is k-specific: dantis
                # a-n-T keeps the `-an` coda, dangus a-n-G keeps `-an` (gintaras n-t is already standalone via
                # the i| pipe rule). Verified vs the engine Lookup.
                n_before_k = (c in "nN" and nxt in "kK") if nxt else False
                # `r` before `t` -> STANDALONE `r` (gerti r-t -> `r`, not the `-ęr` coda which DOES exist). Like
                # n-before-k, this is a specific cluster: r before a VOICED stop keeps its coda (darbas r-b -> -ąr).
                # (nxt may be None for a word-final consonant, e.g. 'ir' -> r is last; guard against that.)
                r_before_t = (c in "rR" and nxt in "tT") if nxt else False
                # A syllable-final `l` before a SOFT (palatalized) consonant uses the recorded `l|` pipe (the
                # long soft-l): kalbėti l-b'(ė) / valgyti l-g'(y) / vilnius l-n'(iu) -> `l|`. Before a HARD
                # consonant the engine keeps the `-Vl` coda (kalba l-b(a) -> `-al`). Only `l|` is recorded (no
                # r|/n|/m|), so this is l-specific; word-final l or l-before-vowel is unaffected.
                nxt_soft = (palatals[i + 1] if (palatals is not None and i + 1 < len(palatals)) else False)
                l_pipe = (c in "lL" and "l|" in units and nxt not in ("", "_") and not is_vowel(nxt) and nxt_soft)
                if l_pipe:
                    elems.append(("dip", "l|", _btarget(dur * K_DUR), True, True, f0))
                    _tag(i); i += 1; prev = "l"; prev_pipe = True; continue
                coda_cands = []
                if prev and is_vowel(prev) and not prev_pipe and not nxt_unburst and not n_before_k \
                        and not r_before_t:
                    # try `-Vc` for the exact vowel, then its SHORT form (gatvė à-coda -> the voice only recorded
                    # the plain `-at`, not `-àt`), then `-oc` for a coda-u (puikus -os, auksas -ok).
                    coda_cands = ["-" + pv + c]
                    if pv in LONG2SHORT:
                        coda_cands.append("-" + LONG2SHORT[pv] + c)
                    if pv == "u":
                        coda_cands.append("-o" + c)
                full_coda = _first(coda_cands, units)
                if full_coda:
                    elems.append(("dip", full_coda, None, is_voiced(c), is_voiced(c), f0))
                else:
                    # stretch only VOICED SONORANTS (the long nasal/liquid, gintaras n=124) to their own
                    # ilgiai*K_DUR; fricatives and stops stay native -- stretching a fricative tiles bright
                    # frication (sveiki "s" too bright) and the engine's own s/k units are ~1 frame.
                    tgt = _btarget(dur * K_DUR) if c in SONOR else None
                    elems.append(("dip", standalone_unit(c, units), tgt, is_voiced(c), is_voiced(c), f0))
                _tag(i)                             # leftover/coda consonant: tag with its own phone
                i += 1
    return elems

# ---------------------------------------------------------------- TD-PSOLA (formant-preserving)
# NOTE: we deliberately do NOT resample individual pitch periods. Resampling a single period scales
# its spectrum and wrecks the timbre. The recorded wtvlt1 periods already carry Gintaras's natural
# formants AND pitch; for duration we repeat WHOLE periods (cycling the steady part), so the voice
# is reproduced faithfully. Pitch is left at the recorded value (sounded "very good" plain-concat).

def _align(a, b, ov, maxlag):
    """Return the start index into b that best phase-aligns b's head with a's tail over `ov`
    samples (max normalised cross-correlation, shift 0..maxlag). Pitch-synchronous join = no click."""
    ov = int(min(ov, len(a), len(b)))
    if ov < 8 or maxlag <= 0:
        return 0, ov
    aend = a[-ov:]; an = np.linalg.norm(aend) + 1e-9
    best, bestc = 0, -2.0
    hi = min(maxlag, len(b) - ov)
    for d in range(0, max(1, hi)):
        seg = b[d:d + ov]
        c = float(np.dot(aend, seg) / (an * (np.linalg.norm(seg) + 1e-9)))
        if c > bestc:
            bestc, best = c, d
    return best, ov

def trailing_voiced(x, chunk=80, frac=0.2):
    """Length of the contiguous non-silent run at the END of x (chunk-RMS, no convolution edge
    artifact). Used to cap a join overlap so it never bleeds into a preceding stop-closure SILENCE
    (which would erase the abrupt vowel onset = the only cue for a stop, e.g. ta- = 1026 sil + 222)."""
    n = len(x)
    if n < 2 * chunk:
        return n
    a = x.astype(float)
    nch = n // chunk
    rms = np.array([np.sqrt(np.mean(a[k * chunk:(k + 1) * chunk] ** 2)) for k in range(nch)])
    thr = frac * (rms.max() + 1e-9)
    k = nch - 1
    while k >= 0 and rms[k] > thr:
        k -= 1
    return (nch - 1 - k) * chunk

def xfade(a, b, ov, maxlag=0, amp=False):
    """Phase-aligned raised-cosine crossfade of segment b onto the tail of a. With amp=True the seam
    is also amplitude-matched: b is scaled so its head level equals a's tail level, the gain ramping
    back to 1.0 over the next couple of periods so no loudness STEP is heard at the join (the main
    audible artifact when concatenating two different recordings of the same vowel)."""
    if len(a) == 0: return b.copy()
    if len(b) == 0: return a.copy()
    bstart, ov = _align(a, b, ov, maxlag)
    b = b[bstart:]
    ov = int(min(ov, len(a), len(b)))
    if ov < 2:
        return np.concatenate([a, b])
    if amp:
        ra = float(np.sqrt(np.mean(a[-ov:] ** 2))) + 1e-9
        rb = float(np.sqrt(np.mean(b[:ov] ** 2))) + 1e-9
        g = float(np.clip(ra / rb, 0.6, 1.7))
        ramp = min(len(b), 2 * ov)
        gain = np.ones(len(b)); gain[:ramp] = np.linspace(g, 1.0, ramp)
        b = b * gain
    w = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, ov))
    mixed = a[-ov:] * (1 - w) + b[:ov] * w
    return np.concatenate([a[:-ov], mixed, b[ov:]])

def resample(x, n):
    """Linear-resample period x to n samples (the engine refills its grain buffer with a period
    resampled to the contour length; small ratios so formants are preserved)."""
    n = int(n); x = x.astype(float)
    if n < 2 or len(x) < 2:
        return x[:max(1, n)]
    return np.interp(np.linspace(0.0, len(x) - 1, n), np.arange(len(x)), x)

def is_period(f):
    return 90 * SR // 22050 <= len(f) <= 430 * SR // 22050      # pitch-period length window

def psola_render(frames, target, f0a, f0b, loop):
    """TD-PSOLA pitch/duration change that PRESERVES FORMANTS (timbre). The recorded periods are NOT
    resampled (that scales the spectrum -> dark/low timbre, the bug); instead each native period is taken
    as a 2-period Hann grain centred on its pitch mark and OVERLAP-ADDED at the OUTPUT pitch spacing
    T=SR/f0. Spacing sets F0, the grain's own samples keep the formants. loop=True cycles the central
    periods to sustain a vowel to `target`; False plays the unit once at the glided pitch. Units that
    contain non-periodic frames (stop closures / frication) are returned native (no pitch change)."""
    parts = [f.astype(float) for f in frames if len(f)]
    if not parts:
        return np.zeros(0)
    if not all(is_period(p) for p in parts):       # consonant/closure unit -> keep native, formants intact
        nat = np.concatenate(parts)
        if loop and target and len(nat):           # fricative duration: tile the noise frames to target
            reps = int(np.ceil(target / len(nat)))
            nat = np.tile(nat, reps)[:int(target)]
        return nat
    src = np.concatenate(parts)
    marks = np.cumsum([0] + [len(p) for p in parts])      # marks[k] = pitch mark (start) of period k
    n = len(parts)
    grains = []                                    # (centre offset within grain, windowed grain, window)
    for k in range(n):
        lo = marks[k - 1] if k > 0 else marks[k]
        hi = marks[k + 1] if k < n - 1 else marks[n]
        seg = src[lo:hi]
        w = np.hanning(len(seg)) if len(seg) > 2 else np.ones(len(seg))
        grains.append((marks[k] - lo, seg * w, w))
    # MONOTONIC PERIOD SWEEP: map output time fraction -> source period index 0..n across the WHOLE unit.
    # Stretch (target>native) => some periods repeat (sweep forward, no ping-pong jump). Compress
    # (target<native) => steady MIDDLE periods are dropped while the onset (period 0) and coda (period
    # n-1) are preserved -- the engine respaces periods, it does NOT truncate the tail (truncation cut the
    # vowel that follows a long sonorant onset: mama "mma", namas "nams"; the old ping-pong instead
    # re-articulated "aa"). Grains are overlap-added at the OUTPUT pitch spacing T=SR/F0 (sets F0; the
    # grain's own samples keep the formants).
    if target and target > 0:
        out_len = int(target)
    else:
        # NATIVE (engine epoch-placement): each voiced grain is allotted GRAIN_DUR output samples
        # (sub_1000E410 budget baseP*a1/100=330), then filled with 294-smp pitch periods (~1.12/grain).
        out_len = int(round(n * GRAIN_DUR))
    if out_len < 1:
        return np.zeros(0)
    pad = 2 * (430 * SR // 22050)
    out = np.zeros(out_len + pad); wsum = np.zeros(out_len + pad)
    u = 0.0
    while u < out_len:
        frac = u / out_len
        ksrc = min(n - 1, int(frac * n))            # sweep period 0 -> n-1 across the output span
        T = SR / max(50.0, f0a + (f0b - f0a) * frac)
        c, g, w = grains[ksrc]
        start = int(round(u)) - c
        a, b = max(0, start), min(len(out), start + len(g))
        if b > a:
            gs = a - start
            out[a:b] += g[gs:gs + (b - a)]; wsum[a:b] += w[gs:gs + (b - a)]
        u += T
    return out[:out_len] / np.maximum(wsum[:out_len], 1e-3)

def smooth_join(buf, pos):
    """The engine's actual join (hlas.dll sub_1000EAD0): no crossfade — concatenate, then DECLICK the
    boundary with repeated 2-tap moving-average passes `buf[i]=(buf[i-1]+buf[i+1])/2` over WIDENING
    windows (±5, ±10, ±15 samples) around the join. A local low-pass that removes the discontinuity
    while preserving everything outside ~15 samples (so stop closures etc. survive)."""
    n = len(buf)
    for half, fwd in ((5, 1), (10, 2), (15, 4)):
        lo = max(1, pos - half); hi = min(n - 1, pos + fwd)
        for i in range(lo, hi):
            buf[i] = 0.5 * (buf[i - 1] + buf[i + 1])

def synth(elems, units, pool, time_scale=1.0):
    """Render each tiling element to a unit-signal via TD-PSOLA (pitch from the glided tonai contour,
    formants preserved), concatenate the unit-signals, then DECLICK each unit boundary (engine
    sub_EDC0/sub_EAD0 moving-average). Pitch periods inside a unit are already seam-free from the PSOLA
    overlap-add, so only the unit joins need the declick.
    time_scale (rate control, default 1.0 = NO CHANGE -> identical output): when != 1.0, every unit's
    output length is forced to time_scale*native (loop=True) -- a UNIFORM TD-PSOLA time-stretch that
    compresses/expands ALL units (not just long vowels) while preserving pitch+formants. <1 = faster."""
    rendered, miss, last_frames = [], [], []       # rendered = [(sig, voiced_in, voiced_out)]
    prev_f0 = elems[0][5] if elems else 90.0       # glide starts from the first unit's pitch
    for kind, key, target, vin, vout, f0 in elems:
        if kind == "sil":                          # plosive closure: a short near-silent gap
            rendered.append((np.zeros(int((target or 0) * time_scale)), False, False)); prev_f0 = f0; continue
        if key is None:                            # body unit absent -> sustain the previous unit's periods
            if kind == "body" and last_frames:
                t = int((target or 0) * time_scale)
                rendered.append((psola_render(last_frames, t, prev_f0, f0, True), vin, vout))
            else:
                miss.append((kind, "nokey"))
            prev_f0 = f0; continue
        keylist = key if isinstance(key, (list, tuple)) else [key]   # 'grp' = onset+body frames as ONE stream
        frames = [pool[f] for k in keylist for f in units.get(k, []) if f in pool]
        if not frames:
            miss.append((key, "noframes")); prev_f0 = f0; continue
        tgt = int(target or 0)
        loop = bool(target)
        nat = sum(len(f) for f in frames)
        if time_scale != 1.0:
            # RATE: override the ilgiai target -> force every unit to time_scale*native, uniformly
            # compressing/expanding the whole utterance (durs/contour already set; only timing moves).
            tgt = max(1, int(round(time_scale * nat))); loop = True
        elif kind == "grp":
            # compress the onset+vowel frames to the ilgiai duration, but never STRETCH past the recorded
            # length -- the ping-pong sustain would loop back over the onset consonant and re-articulate it.
            if tgt >= nat:
                tgt, loop = 0, False
            else:
                loop = True
        sig = psola_render(frames, tgt, prev_f0, f0, loop)
        rendered.append((sig, vin, vout)); last_frames = frames; prev_f0 = f0
    rendered = [(s, vi, vo) for (s, vi, vo) in rendered if len(s)]
    if not rendered:
        return np.zeros(1, dtype="<i2"), miss
    # Assemble: a VOICED->VOICED unit join (e.g. gi- -> i|, two different recordings of the same vowel) is
    # smoothed with a SHORT pitch-synchronous, amplitude-matched overlap (phase-aligned ~half a pitch
    # period) so the g->i transition has no phase/level step; every other join (into/out of a closure or a
    # fricative) keeps the engine's moving-average DECLICK so stop cues and frication edges are preserved.
    out = rendered[0][0].astype(float).copy()
    prev_vout = rendered[0][2]
    declicks = []
    for sig, vin, vout in rendered[1:]:
        b = sig.astype(float)
        if prev_vout and vin and len(out) and len(b):
            ov = int(min(JOIN_PER // 2, len(out), len(b)))
            out = xfade(out, b, ov, maxlag=JOIN_PER, amp=True)
        else:
            declicks.append(len(out)); out = np.concatenate([out, b])
        prev_vout = vout
    for pos in declicks:
        smooth_join(out, pos)
    return np.clip(np.round(out), -32767, 32767).astype("<i2"), miss

def save_wav(path, sig):
    w = wave.open(path, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes(np.asarray(sig, dtype="<i2").tobytes()); w.close()

def load_voice(use16k):
    """Return (pool, units) where pool = {frame_id: int16 array}, units = {key: [frame_id,...]}.
    use16k -> the Symbian 16 kHz pool (blob0+blob2); else the x86 22050 Hz wtvlt1.dta."""
    if use16k:
        set_sr(16000)
        n, ep, pcm = VP.load()
        raw = VP.load_units()
        # Trim each unit's trailing boundary-gap period(s) (the inter-unit pause that otherwise leaks
        # a "trailing consonant"/silence into the phoneme, e.g. a->"are"). Unit-relative so unvoiced
        # consonants (s, k, ve-) whose frames are uniformly long are preserved.
        units = {k: VP.trim_unit(r, ep) for k, r in raw.items()}
        ids = {f for r in units.values() for f in r}
        # A 16k consonant/closure region has sparse pitch marks, so one "frame" can be a long muffled
        # segment (e.g. -ab=2044 samples). Played raw it drones ("sf"); cap kept periods to ~1.8 pitch
        # periods so a stop/closure stays brief (the vowels are made of short periods, unaffected).
        cap = int(round(280 * SR / 16000))
        def pcap(i):
            seg = VP.period(ep, pcm, i)
            return seg[:cap] if len(seg) > cap else seg
        pool = {i: pcap(i) for i in ids if i + 1 <= n}
        return pool, units
    set_sr(22050)
    d = W.load()
    return W.build_frame_pool(d), W.parse_units(d)
