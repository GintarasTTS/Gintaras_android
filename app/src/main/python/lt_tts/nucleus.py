# -*- coding: utf-8 -*-
# Bit-exact port of transcr4.dll sub_10009310 — the syllable-nucleus marker. It takes the
# PradApdZod'd word (uppercase, trailing space) in BOTH a read-only `word` view and a mutable
# `attr` buffer (both initially identical) and increments selected bytes of `attr`. The accent
# matcher's mode-1 retraction walk reads (attr_char & 2) to locate syllable nuclei.
#
# Validated byte-for-byte against the live DLL during the RE (the oracle harness lives in engine/nucleus.py).
import json
from . import paths

_CC = None
def _cc():
    global _CC
    if _CC is None:
        _CC = json.load(open(paths.data_path("nucleus_data.json"), encoding="utf-8"))
    return _CC


def _mark(w, a):
    """Core sub_10009310: `w` = word byte list (read-only), `a` = attr byte list (mutated in place).
    Both must be equal length and 0-free. Returns `a`."""
    cc = _cc()
    # char-class membership as cp1257 byte sets (the matcher compares raw bytes via strchr)
    VS  = set(cc["VS"].encode("cp1257"));  V = set(cc["V"].encode("cp1257"))
    SON = set(cc["SON"].encode("cp1257")); OBS = set(cc["OBS"].encode("cp1257"))
    SIB_S = set(cc["SIB_S"].encode("cp1257")); SIB_Z = set(cc["SIB_Z"].encode("cp1257"))
    SUBSTR = [s.encode("cp1257") for s in cc["SUBSTR"]]
    SP, Z, D, H, C, ZH = 0x20, 0x5a, 0x44, 0x48, 0x43, 0xde
    i = len(w) - 1                              # strlen-1
    if a[i] & 1:
        a[i] += 1
    while i > 0:
        while True:                            # A1: skip trailing non-VS (vowels+space)
            if w[i] in VS: break
            if i <= 0: break
            i -= 1
        while True:                            # A2: skip the vowel cluster
            if w[i] not in V: break
            if i <= 0: break
            i -= 1
        if i > 0 and (a[i] & 2) == 0 and w[i] in SON:        # B: sonorant coda
            i -= 1
        did_cluster = False                    # C: ZD / ŽD / HC cluster
        if i > 1 and (a[i] & 2) == 0:
            c, p = w[i], w[i - 1]
            if (c == Z and p == D) or (c == ZH and p == D) or (c == H and p == C):
                i -= 2
                did_cluster = True
        if not did_cluster:                    # D: obstruent coda
            if i > 0 and (a[i] & 2) == 0 and w[i] in OBS:
                i -= 1
        if i > 0 and (a[i] & 2) == 0:          # E: sibilant coda
            c = w[i]
            if c in SIB_S:
                i -= 1
            elif c in SIB_Z:
                if w[i - 1] != D:
                    i -= 1
        if a[i] & 1:                           # mark the nucleus
            a[i] += 1
        if w[i] == SP:
            if i > 0:
                i -= 1
            if a[i] & 1:
                a[i] += 1
    wb = bytes(w)                              # substring (diphthong) table pass
    for s in SUBSTR:
        idx = wb.find(s)
        if idx >= 0 and (a[idx] & 1):
            a[idx] += 1
    return a


def nucleus_attr(word):
    """sub_10009310 as the --nuc probe drives it (attr starts = the word). Used only to validate
    the core _mark against the live DLL."""
    w = list(word.encode("cp1257"))
    return _mark(w, list(w))


def kirc_nucleus(token):
    """Nucleus attr as KircTranskr produces it for the matcher: word buffer = '_' + token + ' ',
    attr initialised to all 1s, then sub_10009310. Returns the attr byte list; the matcher reads
    (attr[pos] & 2) where pos is the matcher word position (the '_' prefix gives the -1 alignment)."""
    w = list(("_" + token.upper() + " ").encode("cp1257"))
    a = [1] * len(w)
    return _mark(w, a)
