# -*- coding: utf-8 -*-
# Pure-Python port of transcr4.dll's `ilgiai` export (sub_10001090, durations).
# BIT-FOR-BIT reimplementation: table + constants + algorithm all DUMPED from the DLL
# (no invented heuristics). Verified == transcr4 on every token of the test corpus.
#
# transcr4 `ilgiai(in, out, outsize, speed)` computes a per-phoneme duration (centi-units)
# from the KircTranskr phoneme stream. For each phoneme it starts from a base interval
# [f2..f1] (table @0x1000d960), then a chain of context multipliers (the strchr() class
# tests with x87 fmul constants @0x1000c088..c0c0) scales an interpolation factor, and a
# global speed factor (constants 5.0 / 10.0) stretches/compresses everything:
#
#     dur = trunc( factor * speed_factor * (f1 - f2) + 0.5 + f2 )
#
# The phoneme tokens must match the table names exactly (e.g. "r'", "Ii", "ts'"); '+' is a
# word-boundary MARKER (consumed, not a phoneme) that the prev/next context window skips
# over, so cross-word coarticulation is preserved (emitted as a "+ 0" line, like the DLL).

# --- 93-entry duration table @0x1000d960  (name, f1=base/long, f2=floor/short) -----------
_TABLE = [
    ("_",100,33),("+",0,0),("i",68,25),("e",70,26),("a",68,25),("o",70,26),("u",66,24),
    ("I",75,28),("E",79,29),("A",78,29),("O",89,33),("U",75,28),("ii",115,43),("Ii",107,40),
    ("iI",119,44),("ie",122,55),("Ie",131,59),("iE",135,61),("ee",114,42),("Ee",119,44),
    ("eE",124,46),("ea",115,43),("Ea",113,42),("eA",118,44),("aa",112,41),("Aa",110,41),
    ("aA",116,43),("oo",112,41),("Oo",117,43),("oO",123,46),("uo",115,52),("Uo",112,50),
    ("uO",121,54),("uu",112,41),("Uu",107,40),("uU",117,43),("p",92,83),("p'",88,79),
    ("b",80,72),("b'",77,69),("t",87,78),("t'",90,81),("d",74,67),("d'",71,64),("k",93,84),
    ("k'",100,90),("g",78,70),("g'",76,68),("ts",111,100),("ts'",111,100),("dz",96,86),
    ("dz'",97,87),("tS",109,98),("tS'",111,100),("dZ",100,86),("dZ'",92,79),("s",108,97),
    ("s'",113,102),("z",81,73),("z'",85,77),("S",105,95),("S'",106,95),("Z",75,68),
    ("Z'",80,72),("x",105,95),("x'",99,89),("h",76,68),("h'",76,68),("f",98,88),("f'",97,87),
    ("j'",72,27),("j",77,28),("J",86,32),("v",72,65),("v'",65,59),("w",84,31),("W",97,36),
    ("l",77,28),("l'",74,27),("L",112,41),("L'",113,42),("r",74,67),("r'",68,61),("R",92,83),
    ("R'",76,68),("m",81,30),("m'",76,28),("M",122,45),("M'",122,45),("n",77,28),("n'",77,28),
    ("N",135,50),("N'",136,50),
]
_IDX = {nm: i for i, (nm, _f1, _f2) in enumerate(_TABLE)}

# strchr() character classes (first byte of a token's table name); 0xeb='ė' 0xcb='Ė' (cp1257)
_VOW  = set("aeiouAEIOU") | {"\xeb", "\xcb"}        # df40/df50/df60/df80/df90/e010/e050
_VOWB = _VOW | {"_"}                                 # dfa0..e040 (vowel OR boundary '_')
_SON  = set("bdgzZjlmnrvwh")                         # df70 (voiced/sonorant consonants)

# x87 fmul constants
_M_VV   = 1.25   # c0c0  vowel + following vowel
_M_pSON = 1.22   # c0b8  vowel preceded by sonorant
_M_pVOW = 1.35   # c0b0  vowel preceded by vowel
_M_CC   = 0.87   # c0a8  (shared) vowel-before-cluster / consonant-in-cluster
_M_CEND = 1.26   # c0a0  consonant at word end
_M_VEND = 1.31   # c098  vowel in final (closed) syllable
_M_VOPEN= 1.68   # c090  word-final (open) vowel
_SP_POS = 10.0   # c0d0  speed>0 divisor
_SP_NEG = 5.0    # c0c8  speed<=0 divisor


def _first(arr, c):
    """First char of the table-name of arr[c]; out-of-range -> '_' (boundary, index 0)."""
    if 0 <= c < len(arr):
        return _TABLE[arr[c]][0][0]
    return "_"


def _factor(arr, c):
    """Context multiplier chain for phoneme at index c (== transcr4's [ebp-0x10])."""
    f = 1.0
    cur = _first(arr, c)
    nx, nx2, nx3 = _first(arr, c + 1), _first(arr, c + 2), _first(arr, c + 3)
    pv = _first(arr, c - 1)
    cur_v = cur in _VOW
    # A: vowel + following vowel
    if cur_v and nx in _VOW:
        f *= _M_VV
    # B: vowel preceded by sonorant (else by vowel)   [only if not the first token]
    if cur_v and c > 0:
        if pv in _SON:
            f *= _M_pSON
        elif pv in _VOW:
            f *= _M_pVOW
    # C: vowel before a >=2 consonant cluster
    if cur_v and (nx not in _VOWB) and (nx2 not in _VOWB):
        f *= _M_CC
    # D: consonant inside a cluster (prev OR next is a consonant)
    if (cur not in _VOWB):
        if c > 0 and (pv not in _VOWB):
            f *= _M_CC
        elif nx not in _VOWB:
            f *= _M_CC
    # E: consonant at the word end (next is '_', or next-cons then '_')
    if (cur not in _VOWB):
        if nx == "_" or (nx not in _VOWB and nx2 == "_"):
            f *= _M_CEND
    # F: vowel in a final closed syllable (V C _ or V C C _)
    if cur_v:
        if (nx not in _VOWB and nx2 == "_") or \
           (nx not in _VOWB and nx2 not in _VOWB and nx3 == "_"):
            f *= _M_VEND
    # G: word-final open vowel (next is '_')
    if cur_v and nx == "_":
        f *= _M_VOPEN
    return f


def _speed_factor(speed):
    spd = -10 if speed < -10 else (10 if speed > 10 else speed)
    if spd > 0:
        return (_SP_POS - spd) / _SP_POS
    return (_SP_NEG - spd) / _SP_NEG


def _tokenize(stream):
    """Accept a token list or a KircTranskr newline/space string -> flat token list."""
    if isinstance(stream, (list, tuple)):
        toks = list(stream)
    else:
        toks = stream.replace("\n", " ").split()
    return [t for t in toks if t != ""]


def ilgiai(stream, speed=0):
    """Return [(token, duration), ...] reproducing transcr4 ilgiai, incl. '+ 0' boundary
    lines. `stream` = KircTranskr phoneme tokens (list or whitespace/newline string)."""
    toks = _tokenize(stream)
    # split '+' markers off: arr = phonemes only; mark[k]=True if a '+' followed phoneme k
    arr, mark = [], []
    for t in toks:
        if t == "+":
            if mark:
                mark[-1] = True
            continue
        arr.append(_IDX.get(t, 0))      # unknown token -> '_' (index 0), as in the DLL
        mark.append(False)
    sf = _speed_factor(speed)
    out = []
    for c in range(len(arr)):
        f1 = _TABLE[arr[c]][1]
        f2 = _TABLE[arr[c]][2]
        dur = int(_factor(arr, c) * sf * (f1 - f2) + 0.5 + f2)   # trunc, like _ftol
        out.append((_TABLE[arr[c]][0], dur))
        if mark[c]:
            out.append(("+", 0))
    return out


# ---- validation against the real transcr4 (--full) -------------------------------------
