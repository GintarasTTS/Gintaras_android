# -*- coding: utf-8 -*-
# Pure-Python port of transcr4.dll's `tonai`/`tonai1` exports (F0 / pitch contour).
# Everything (constants, the 4 math kernels, the classification tree, the log-domain
# bump synthesis, the polyline decimation + per-phoneme resampling) is DUMPED/decoded
# from the DLL -- no invented model.  See tonai1 @0x10006afc.
#
# Model (faithful):
#   * base pitch = 90 Hz, scaled by 1.03**semitone (param, clamped +-24).
#   * the contour is built in LOG-F0 domain on a 10 ms grid as
#         logF0(t) = log(base) + bumps(t) + decl(t)
#     bumps(t) = sum over targets of  window((t-center)/width) * height
#       - intonation/stress targets: height=0.2 (ltkonfig "stress curve height"),
#         placed on stressed vowels (AEIOU), stressed diphthong elements and long
#         sonorants (LMNRJW); center/width scaled by phoneme dur & obstruent context.
#       - plosive targets: height=0.13 (voiceless k/p/t) or 0.065 (voiced b/d/g),
#         window = an onset pulse 9u*e^-3u, width 0.08 s.
#     window() = raised cosine (cos(pi*x)+1)/2 on [-1,1] else 0.
#     decl(t) = onsetpulse(t - t0) * declScale  (declScale = 0.1 * total_dur if >1 s).
#   * F0(t) = exp(logF0(t)) + phrase_final_shape(t).
#   * the grid is decimated to a polyline (2 Hz tol) and resampled to "<pos%> <F0>"
#     points appended to each phoneme's "<token> <dur>" line; word breaks -> "+ 0".
#
# `tonai` passes the three ltkonfig scalars (intonation 0.1, stress 0.2, plosive 0.13);
# we use those defaults (the engine only overrides them if ltkonfig.txt was loaded).
import math

# ---- constants (all dumped) ------------------------------------------------------------
BASE_HZ   = 90.0
SEMI      = 1.03      # c188   per-semitone ratio
MS        = 1000.0    # c180
T_OFF     = 0.017     # c178   plosive-target time offset
GRID      = 0.01      # c150   10 ms grid step
PLO_W     = 0.08      # c158   plosive window width (s)
PI        = 3.1415    # c0e8   (the DLL's pi)
H_INTON   = 0.1       # ltkonfig[0] intonation curve height
H_STRESS  = 0.2       # ltkonfig[1] stress curve height
H_PLOS    = 0.13      # ltkonfig[2] plosive curve height
TOL       = 2.0       # c0e0   polyline F0 tolerance & misc
HALF      = 0.5       # c088
OBS  = set("bdgkptsSzZfxh_")          # obstruents + boundary
VOW_L = set("aeiou"); VOW_U = set("AEIOU")
SON_U = set("LMNRJW")

def _ftol(x):                         # MSVCRT _ftol: truncate toward zero
    return int(x)

# ---- math kernels (sub_10006985 / sub_10006930 / sub_100069D5 / a50 / a8e) -------------
def _win(x):                          # sub_10006985 raised-cosine window on [-1,1]
    if x < -1.0 or x > 1.0:
        return 0.0
    return (math.cos(PI * x) + 1.0) / 2.0

def _pulse(x):                        # sub_10006930 onset pulse 9u*e^-3u (x>=0)
    if x < 0.0:
        return 0.0
    return 9.0 * x * math.exp(-3.0 * x)

def _range(d, sem):                   # sub_100069D5 phrase pitch range from last-word dur
    t = (1.0 - sem / 20.0) if sem > 0 else (1.0 - sem / 10.0)
    if t * 0.32 > d:                  # c118
        return d * 0.625              # c110
    if t * 0.64 > d:                  # c108
        return t / 5.0                # c0c8
    return d * 0.3125                 # c100

def _final_period(R, T1, t):          # sub_10006A50  '.'-type final fall
    if R + T1 > t:
        return 0.0
    return 26.0 / ((t - T1 - R) / 0.15 + 1.0) - 26.0     # c128/c130

def _final_quest(R, T1, t):           # sub_10006A8E  '?'-type final rise
    if R > T1:
        return 0.0
    if R + T1 <= t:                   # wait: structured below
        pass
    # faithful: if (T1+R) <= t -> first branch else second
    if T1 + R > t:
        return -((R - T1) - 0.0)      # placeholder (unused unless '?')
    a = -T1 / 0.03
    b = (26.0 / ((t - T1 - R) / 0.15 + 1.0) - 26.0) * 1.75
    return a - b

# ---- parse the ilgiai "<token> <dur>" stream -------------------------------------------
def _parse(stream):
    if isinstance(stream, (list, tuple)):
        lines = list(stream)
    else:
        lines = stream.splitlines()
    name, dur, mark = [], [], []
    last_plus = 0
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split()
        if len(parts) < 2:
            continue
        tok = parts[0]
        if tok == "+":                # word boundary marker on previous phoneme
            if mark:
                mark[-1] = "+"
                last_plus = len(name)
            continue
        name.append(tok)
        dur.append(int(parts[1]))
        mark.append("-")
    return name, dur, mark, last_plus

# ---- classification: build intonation (B) and plosive (A) targets ----------------------
def _targets(name, dur):
    n = len(name)
    A = []   # plosive: (center, height)
    B = []   # intonation: (center, width, height)
    accum = 0.0
    for i in range(n):
        c0 = name[i][0]
        c1 = name[i][1] if len(name[i]) > 1 else ""
        d = dur[i] / MS
        pv = name[i-1][0] if i > 0 else "_"
        nx = name[i+1][0] if i+1 < n else "_"
        # --- array A: plosive perturbation ---
        if c0 in "bdg" and c1 not in ("z", "Z"):
            A.append((d + accum + T_OFF, H_PLOS / 2.0))
        elif c0 in "kpt" and c1 not in ("s", "S"):
            A.append((d + accum + T_OFF, H_PLOS))
        # --- array B: intonation / stress bump ---
        # decide block: alpha (stressed-2nd diphthong or sonorant) vs beta
        alpha = ((c0 in VOW_L and c1 and c1 in VOW_U) or (c0 in SON_U))
        if alpha:
            # center/width by obstruent context
            if pv in OBS and nx in OBS:
                cm, wm = 0.5, 2.0
            elif pv not in OBS and nx in OBS:
                cm, wm = 0.66, 1.66
            else:                      # prev in OBS & next not, or neither
                cm, wm = 1.0, 2.0
            B.append((d * cm + accum, d * wm, H_STRESS))
        else:
            # beta: only stressed mono vowels / certain stressed diphthongs emit a target
            emit = False
            if c0 in VOW_U and c1 and c1 in VOW_L:
                emit = True            # 1000735A path
            elif c0 in VOW_U and not c1 and nx not in SON_U:
                emit = True            # 1000761C path (stressed mono vowel)
            if emit:
                if pv in OBS and nx in OBS:
                    cm, wm = 0.75, 1.75
                elif pv not in OBS and nx in OBS:
                    cm, wm = 0.75, 1.75
                elif pv in OBS and nx not in OBS:
                    cm, wm = 1.0, 2.0
                else:
                    cm, wm = 1.0, 2.0
                B.append((d * cm + accum, d * wm, H_STRESS))
        accum += d
    return A, B, accum

# ---- log-domain bump sum ---------------------------------------------------------------
def _bumps(t, A, B):
    s = 0.0
    for j, (c, w, h) in enumerate(B):
        hh = h * HALF if j == len(B) - 1 else h
        if w != 0:
            s += _win((t - c) / w) * hh
    for j, (c, h) in enumerate(A):
        hh = h * HALF if j == len(A) - 1 else h
        s += _win((t - c) / PLO_W) * hh
    return s

# ---- main entry ------------------------------------------------------------------------
def tonai(stream, semitone=0, punct=0):
    """Return [(token, dur, [(pos,f0),...]), ...] reproducing transcr4 tonai.
    `stream` = ilgiai output (list of '<token> <dur>' or '\n'-joined string)."""
    name, dur, mark, last_plus = _parse(stream)
    n = len(name)
    if n == 0:
        return []
    sem = max(-24, min(24, semitone))
    base = BASE_HZ * (SEMI ** sem)
    logbase = math.log(base)
    A, B, total = _targets(name, dur)
    t0 = dur[0] / MS
    # declination scale: 0.1, or total*0.1 if utterance > 1 s
    T1 = sum(dur[i] / MS for i in range(0, last_plus))
    T2 = sum(dur[i] / MS for i in range(last_plus, n - 1))
    declScale = (T1 + T2) * H_INTON if (T1 + T2) > 1.0 else H_INTON
    R = _range(T2, sem)
    # phrase-final shape selector
    if punct == ord('.'):
        fin = lambda t: _final_period(R, T1, t)
    elif punct == ord('?'):
        fin = lambda t: _final_quest(R, T1, t)
    else:
        fin = lambda t: _final_period(R, T1, t) / 2.0
    # ---- evaluate F0 on the 10 ms grid ----
    gt, gf = [], []
    t = 0.0
    k = 0
    while t < total and k < 10000:
        lf = logbase + _bumps(t, A, B)
        val = math.exp(lf + _pulse(t - t0) * declScale) + fin(t)
        gt.append(t); gf.append(val)
        t += GRID; k += 1
    K = len(gt)
    # ---- resample per phoneme with polyline decimation (tol 2 Hz) ----
    out = []
    accum = 0.0
    gi = 0
    g = 2
    for i in range(n):
        pts = []
        if i == 0 and K:
            pts.append((0, _ftol(gf[0] + HALF)))
        d = dur[i] / MS
        phon_end = d + accum
        while g < K:
            if gt[g] >= phon_end:
                break
            # linearity test of segment gi..g over interior points
            flag = 1
            for m in range(gi + 1, g):
                denom = (gt[g] - gt[gi])
                if denom == 0:
                    continue
                interp = (gf[g] - gf[gi]) * (gt[m] - gt[gi]) / denom + gf[gi] - gf[m]
                if abs(interp) >= TOL:
                    flag = 0
                    break
            if flag == 0:
                # emit the breaking grid point g (segment gi..g lost linearity)
                if gt[g] < accum:
                    pos = 0
                else:
                    pos = _ftol((gt[g] - accum) / d * 100.0 + HALF)
                f0 = _ftol(gf[g] + HALF)
                pts.append((pos, f0))
                gi = g - 1
            g += 1
        # phrase-end "100 <f0>" point: only when the grid is exhausted (last phoneme)
        if g >= K and K >= 2:
            phon_end = d + accum
            denom = gt[K-1] - gt[K-2]
            if denom != 0:
                f0e = (gf[K-1] - gf[K-2]) * (phon_end - gt[K-2]) / denom + gf[K-2]
                pts.append((100, _ftol(f0e + HALF)))
        accum += d
        out.append((name[i], dur[i], pts))
        if mark[i] == "+":
            out.append(("+", 0, []))
    return out
