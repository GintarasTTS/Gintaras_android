# -*- coding: utf-8 -*-
# Faithful port of transcr4.dll's lexical stress engine (kirčiavimas) — the accent stage inside
# KircTranskr.  This mirrors the matcher sub_10002790 + the position/type code at 0x10003D99 + the
# 16 mode routines, working on the data extracted by extract_accent.py.  See
# memory/accent-system-re.md for the full RE map; symbol/offset comments below point back to it.
#
# The real engine (driver sub_1000A95D -> mega-matcher 0x10002790..0x100068EF) runs THREE
# independent candidate sources and merges them by priority, applying the accent only when the
# survivors are unambiguous (sub_100098F3 count==1):
#   (1) VERB lexicon  @0x1000e070 (forward stems, 1089 reversed endings)   <-- ported (_verb_results)
#   (2) FOREIGN        @0x1010da40 (exact whole-word match, 2465 forms)    <-- ported (_foreign_results)
#   (3) NOUN/adj       @0x1005b908 (reversed stems, 141 main-endings)      <-- ported (_noun_results)
# See memory/accent-system-re.md for the full map. accent() merges all three in the engine's commit
# order (verb -> foreign -> noun) and applies result[0] iff sub_100098F3's distinct count == 1.
#
# VERB pipeline (per word, already UPPERCASE as the engine's PradApdZod produces):
#   strrev(WORD) -> match a REVERSED ending (a prefix of the reversed word) -> cand_stem = WORD minus
#   that ending -> (optional prefix strip) -> find verb-lexicon entry V with cand_stem ==
#   V.stem + V.group[E.f0].field and (V.e08c & E.f2)!=0 -> dispatch on E.f5 to a mode {0,1,2} ->
#   compute (stress char position, accent type) -> keep only if all survivors agree (count==1).
#
# Result: accent(word) -> (pos1, type) | None, where pos1 is a 1-based char index into the uppercase
# word. TYPE ENCODING (confirmed via the --match probe of sub_10002790, see RE notes):
#   type 1 = circumflex (tvirtagalė, rendered Xx e.g. "Aa")
#   type 2 = acute      (tvirtapradė, rendered xX e.g. "aA")
# The matcher's own result record stores pos as a 0-BASED char index at +8 and type at +0xc;
# accent() returns pos as 1-based (= probe_pos + 1).
import json
from . import paths

def _load(name):
    return json.load(open(paths.data_path(name), encoding="utf-8"))

_AT = None
def tables():
    global _AT
    if _AT is None:
        endings = _load("accent_endings.json")    # [rev_ending, flag[8]]  @0x10113ab8
        prefixes = _load("accent_prefixes.json")   # [prefix,      flag[4]] @0x10116dc8
        stems   = _load("stem_lexicon.json")       # [stem, [[field,acc4]x3], flag8] @0x1000e070
        cc_raw  = _load("accent_charclasses.json") # name -> [cp1257 byte,...]
        # decode char-class byte lists to unicode char sets (cp1257, 1 byte = 1 char)
        cc = {k: set(bytes(v).decode("cp1257")) for k, v in cc_raw.items()}
        _AT = dict(endings=endings, prefixes=prefixes, stems=stems, cc=cc)
    return _AT


# cp1257 char constants used by the candidate-augmentation stage (asm 0x10002ab7-0x10003063).
# All single cp1257 bytes -> unicode (the matcher token / lexicon strings live in unicode here).
_C = lambda b: bytes([b]).decode("cp1257")
_CH_S, _CH_Z, _CH_SH, _CH_ZH = _C(0x53), _C(0x5a), _C(0xd0), _C(0xde)   # S Z Š Ž
_CH_K, _CH_G, _CH_Y, _CH_UU  = "K", "G", "Y", _C(0xdb)                  # K G Y Ū
_CH_C, _CH_T, _CH_D, _CH_I, _CH_U = _C(0xc8), "T", "D", "I", "U"        # Č T D I U
_S90bc = set(bytes([65, 192, 79, 85, 219, 216]).decode("cp1257"))      # A Ą O U Ū Ų  (Č/ŽD gate)
_S90c4 = set(bytes([65, 69, 85, 79]).decode("cp1257"))                 # A E U O      (I->Y skip)
_S90cc = set(bytes([65, 69, 79]).decode("cp1257"))                     # A E O        (U->Ū skip)


# ---- the matcher --------------------------------------------------------------------------------
def _candidates(word):
    """Phase B+C+D: yield accepted results as dicts(prefix_idx, vidx, eidx, eflags, group).

    Faithful to the asm candidate generator (0x10002a17-0x100035fc). For each matched reversed
    ending the engine builds an array-A of candidate stems: the bare stem PLUS, keyed on the first
    char of the stripped (forward) suffix, devoicing-restored twins that recover a stem-final voiced
    consonant lost before a voiceless ending:
        suffix starts 'K' -> stem+'K', stem+'G'        (0x10002f77)
        suffix starts 'S' -> stem+'S', stem+'Z', and (I->Y / U->Ū) twin  (0x10002bc3)
        suffix starts 'Š' -> stem+'Š', stem+'Ž'        (0x10002ea6)
    plus a Č->T / DŽ->D restore on the bare stem when the suffix starts 'I'+[AĄOUŪŲ] (0x10002ab7).
    Each array-A stem is then expanded into array-B as [prefix-stripped variants..., as-is] (asm
    0x10003084) and matched against the verb lexicon: cand.startswith(V.stem) and
    cand[len(V.stem):]==V.group[g].field and (V.e08c & E.f2). The 'aug=1' (I->Y/U->Ū) twins carry
    the extra accept gate at 0x100034c9 (e08f==0 and a Y/I- or Ū/U-alternating stem)."""
    T = tables()
    endings, prefixes, stems, cc = T["endings"], T["prefixes"], T["stems"], T["cc"]
    rev = word[::-1]
    L = len(word)
    out = []
    for eidx, (estr, flg) in enumerate(endings):
        if not estr or not rev.startswith(estr):
            continue
        elen = len(estr)
        if elen >= L:                              # need a non-empty stem (asm: rev[j]!=0)
            continue
        # hiatus check (asm 0x100029C5): char before ending is AEIU and the ending's preceding
        # char is in hiatus_b -> reject. (rev[elen] = forward WORD[L-1-elen]; rev[elen-1] last ending char)
        cbar = rev[elen]
        if cbar in cc["AEIU_short"] and rev[elen - 1] in cc["hiatus_b"]:
            continue
        stemlen = L - elen
        cand0 = word[:stemlen]
        g = flg[0]                                 # ending flag[0] -> verb group selector (0..2)
        last = estr[-1]                            # 1st char of the forward suffix (= word[stemlen])
        # --- array-A: bare stem + Č/ŽD restore + devoicing twins (asm 0x10002ab7-0x10003063) ---
        # Č->T / DŽ->D restore on the bare stem when suffix = 'I' + [AĄOUŪŲ] (0x10002ab7)
        if last == _CH_I and elen >= 2 and estr[-2] in _S90bc:
            if cand0 and cand0[-1] == _CH_C:
                cand0 = cand0[:-1] + _CH_T
            elif len(cand0) >= 2 and cand0[-1] == _CH_ZH and cand0[-2] == _CH_D:
                cand0 = cand0[:-1]                 # drop trailing Ž ("…DŽ" -> "…D")
        # NOTE: the K- and S-blocks COPY the bare candidate before mutating (original kept), but the
        # Š-block (0x10002ee8) overwrites candA[cur-1] IN PLACE — so a Š-stripped word never keeps a
        # bare-stem candidate, only the +Š/+Ž devoicing twins.
        if last == _CH_SH:                         # 0x10002ea6 (bare original REPLACED)
            arrayA = [(cand0 + _CH_SH, 0), (cand0 + _CH_ZH, 0)]
        elif last == _CH_S:                        # 0x10002bc3 (bare original kept)
            arrayA = [(cand0, 0), (cand0 + _CH_S, 0), (cand0 + _CH_Z, 0)]
            if cand0 and cand0[-1] == _CH_I:       # I->Y twin (aug=1) unless prev char in AEUO
                if len(cand0) < 2 or cand0[-2] not in _S90c4:
                    arrayA.append((cand0[:-1] + _CH_Y, 1))
            elif cand0 and cand0[-1] == _CH_U:     # U->Ū twin (aug=1) unless prev char in AEO
                if len(cand0) < 2 or cand0[-2] not in _S90cc:
                    arrayA.append((cand0[:-1] + _CH_UU, 1))
        elif last == _CH_K:                        # 0x10002f77 (bare original kept)
            arrayA = [(cand0, 0), (cand0 + _CH_K, 0), (cand0 + _CH_G, 0)]
        else:
            arrayA = [(cand0, 0)]
        # --- array-B + lexicon scan: per array-A stem, [prefix variants..., as-is] (asm 0x10003084) ---
        for cstem, aug in arrayA:
            variants = []
            if flg[1] == 0:                        # flag[1]!=0 -> skip prefix stripping
                for pi in range(1, len(prefixes)):
                    p = prefixes[pi][0]
                    if not p:
                        continue
                    # asm 0x10003127: when ending flag[6]!=1, a prefix starting "TE" is skipped
                    # UNLESS it is "TEB…" (e.g. plain "TE"/"TENE" rejected, "TEB" kept).
                    if flg[6] != 1 and p[:2] == "TE" and p[:3] != "TEB":
                        continue
                    if len(p) < len(cstem) and cstem.startswith(p):
                        variants.append((pi, cstem[len(p):], aug))
            variants.append((0, cstem, aug))       # as-is committed LAST (asm 0x100032be)
            for pidx, stem, augf in variants:
                for vidx, (vstem, groups, vflag) in enumerate(stems):
                    if vstem is None or not stem.startswith(vstem):
                        continue
                    field = groups[g][0] or ""
                    if stem[len(vstem):] != field:
                        continue
                    if (vflag[0] & flg[2]) == 0:    # (V.e08c & E.f2)!=0  @0x100034a3
                        continue
                    if augf:                        # I->Y/U->Ū twin extra gate (asm 0x100034c9)
                        if vflag[3] != 0:           # e08f != 0 -> reject
                            continue
                        g0 = groups[0][0] or ""
                        g1 = groups[1][0] or ""
                        if not ((g0[:1] == _CH_Y and g1[:1] == _CH_I) or
                                (g0[:1] == _CH_UU and g1[:1] == _CH_U)):
                            continue
                    out.append(dict(prefix_idx=pidx, vidx=vidx, eidx=eidx,
                                    eflags=flg, group=g, aug=augf))
    return out


def _mode(res):
    """Dispatch on ending flag[5] -> (mode {0,1,2}, forced_type|None). The 16 routines
    @0x100036AF.. inspect FIXED verb-record group bytes (group0 e078/e079, group1 e080/e081,
    group2 e088/e089) + flags e08c..e090 + the prefix slot. Bit-exact port of all 16."""
    T = tables(); stems = T["stems"]
    f5 = res["eflags"][5]
    stem, groups, vflag = stems[res["vidx"]]
    g0a0, g0a1 = groups[0][1][0], groups[0][1][1]      # e078, e079
    g1a0, g1a1 = groups[1][1][0], groups[1][1][1]      # e080, e081
    g2a0, g2a1 = groups[2][1][0], groups[2][1][1]      # e088, e089
    e08c, e08d, e08e, e08f = vflag[0], vflag[1], vflag[2], vflag[3]
    g0field, g2field = groups[0][0] or "", groups[2][0] or ""
    has_prefix = res["prefix_idx"] != 0
    # A TRUE retraction prefix (PER-family; prefix flag[0]==1) pulls the stress onto the prefix boundary for
    # EVERY paradigm -- the result is the prefix-retraction (pf[1]+1, pf[2]). (The engine reaches this via
    # each f5's own walk landing at the prefix; the retraction formula reproduces it exactly. Confirmed vs
    # --accent-file on all PER-family verb words in the lexicon: perpus f5=14 (2,1), perniek f5=10 (2,1),
    # perviršč -- all == PER's (pf[1]+1, pf[2]). The NE-/NU-/SU- prefixes (flag[0]==0) do NOT retract. cont.33.)
    if has_prefix and tables()["prefixes"][res["prefix_idx"]][1][0] == 1:
        return 2, None
    if f5 == 0:                                         # sub_100036AF
        if has_prefix and e08d: return 2, None
        if g0a0 == 1 and g0a1 != 1: return 0, None
        return 1, None
    if f5 == 1:                                         # sub_1000373C
        if not has_prefix: return 1, None
        return (2, None) if e08d else (1, None)
    if f5 == 2:                                         # sub_10003782
        return (0, None) if e08e == 0 else (1, None)
    if f5 == 3:                                         # sub_100037B5
        return 1, None
    if f5 == 4:                                         # sub_100037C1
        return (0, None) if e08d else (1, None)
    if f5 == 5:                                         # sub_100037F4
        return (0, None) if (e08e == 0 and (e08c & 0x1f)) else (1, None)
    if f5 == 6:                                         # sub_10003848
        if e08e != 0: return 1, None
        if (e08c & 0x1f) == 0: return 1, None
        if g0a1 == 0: return 0, None
        if g0a1 != 2: return 1, None
        cc = stem + g0field                            # -0x4dc4 = &concat[len-4]
        p = cc[-4:] + "\x00\x00\x00\x00"               # p[0..2] = concat[len-4..len-2]
        p0, p1, p2 = p[0], p[1], p[2]
        if p1 != "I":
            if p2 in ("A", "E"): return 0, None
        if p0 == "I": return 1, None
        if p1 in ("A", "E"):
            return (1, None) if p2 in "IULMNR" else (0, None)
        return 1, None
    if f5 == 7:                                         # sub_100039C9
        if has_prefix and (e08c & 0xa) and g1a1 != 1: return 2, None
        if g1a0 == 1 and g1a1 != 1: return 0, None
        return 1, None
    if f5 == 8:                                         # sub_10003A78
        if has_prefix and (e08c & 0xa) and g1a1 != 1: return 2, None
        return 1, None
    if f5 == 9:                                         # sub_10003AE0
        return 1, None
    if f5 == 10:                                        # sub_10003AEC
        return 1, None
    if f5 == 11:                                        # sub_10003AF8
        return (0, None) if e08f == 0 else (1, None)
    if f5 == 12:                                        # sub_10003B2B
        return (0, None) if (e08f == 0 and g2a1 != 1) else (1, None)
    if f5 == 13:                                        # sub_10003BEB
        if e08f != 0: return 1, None
        if g2a1 == 1: return 1, None
        if has_prefix: return 2, None
        if g2a1 != 0: return 1, None
        ft = 2 if ("A" in g2field or "E" in g2field or "A" in stem or "E" in stem) else None
        return 1, ft
    if f5 == 15:                                        # sub_10003B7D
        if e08f != 0: return 1, None
        if g2a1 == 1: return 1, None
        if has_prefix: return 2, None
        return 0, None
    if f5 == 14:                                        # sub_10003D2E (paradigm 14)
        if res.get("aug"):                              # matched flag==1 (I->Y/U->Ū twin) -> type 0
            return 1, 0
        if g2a0 == 1 and g2a1 == 1:                     # e088==1 and e089==1 -> type 2
            return 1, 2
        return 1, None                                  # else default (acc[1])
    return 1, None


def _disambig_count(results):
    """Port of sub_100098F3: count DISTINCT accentuations relative to results[0]. Treats same-pos
    ±type (types summing to 3 = acute+circumflex) and adjacent-pos acute/circ pairs as "the same".
    results = [(valid, pos, type), ...] in array order. valid>1 terminates the scan."""
    if not results:
        return 0
    p0, t0 = results[0][1], results[0][2]
    n = 0
    for valid, pc, tc in results:
        if valid != 0 and valid != 1:
            break
        if n == 0:
            n = 1
            continue
        if pc == p0:                                   # same position
            if tc == t0 or tc + t0 == 3:
                continue
        if p0 == pc - 1 and t0 == 1 and tc == 2:
            continue
        if p0 == pc + 1 and t0 == 2 and tc == 1:
            continue
        n += 1
    return n


def _disambig98F3(results):
    """Full port of sub_100098F3 (PHASE1 + PHASE2). Returns the distinct-accentuation count; the
    orchestrator (sub_1000A95D @0x1000ab86) applies result[0]'s (pos,type) to the accent array iff
    this == 1. results = [(valid, pos, type, prio), ...] in matcher commit order.
      PHASE1 (0x10009907): walk the valid in {0,1} prefix (BREAK at valid>=2); count records whose
        (pos,type) is DISTINCT from result[0] — same pos with type==t0 or type+t0==3 collapses, as
        do the adjacent acute/circumflex pairs (p0==p-1,t0=1,t=2) / (p0==p+1,t0=2,t=1).
      PHASE2 (0x10009a08): among the tail at/above the running max priority (init 0 if n==0 else
        0x14), apply the same distinctness predicate; bump n + raise maxprio on a distinct record."""
    if not results:
        return 0
    p0, t0 = results[0][1], results[0][2]
    def same(pc, tc):                                  # "not distinct from result[0]"
        if pc == p0 and (tc == t0 or tc + t0 == 3):
            return True
        if p0 == pc - 1 and t0 == 1 and tc == 2:
            return True
        if p0 == pc + 1 and t0 == 2 and tc == 1:
            return True
        return False
    n = 0
    i = 0
    while i < len(results):                            # PHASE 1
        v = results[i][0]
        if v != 0 and v != 1:
            break
        if n == 0:
            n = 1
        elif not same(results[i][1], results[i][2]):
            n += 1
        i += 1
    maxprio = 0 if n == 0 else 0x14                    # PHASE 2
    while i < len(results):
        prio = results[i][3]
        if maxprio <= prio:
            if n == 0:
                n = 1; maxprio = prio
            elif not same(results[i][1], results[i][2]):
                n += 1; maxprio = prio
        i += 1
    return n


# noun/verb candidate priority. From --match: verb (valid=0) and noun (valid=2) records both carry
# priority 0x1e=30 (foreign valid=1 = 40, deferred). '*'-tail records can differ (see _prio); rare.
_PRIO_VERB = 30
_PRIO_NOUN = 30

def _verb_results(word, L, T):
    """Yield the verb-path (valid=0) results, in commit order (asm 0x10003612-0x10004622)."""
    out = []
    for res in _candidates(word):
        m, forced_type = _mode(res)
        eflags = res["eflags"]
        if m == 0:                                     # ending-fixed (asm 0x10003EF5)
            f3, f4 = eflags[3], eflags[4]
            if f3 == 0xff:
                continue
            pos1, typ = L - f3, f4
        elif m == 2:                                   # prefix retraction (asm 0x100042EE)
            pf = T["prefixes"][res["prefix_idx"]][1]
            pos1, typ = pf[1] + 1, pf[2]
        else:                                          # mode 1 retraction walk
            pos1, typ = _mode1(word, res, forced_type)
            if pos1 is None:
                continue
        out.append((0, pos1, typ, _PRIO_VERB))
    return out


def accent(word):
    """Return (pos1, type) or None for an uppercase Lithuanian word. pos1 is 1-based; type
    1=circumflex 2=acute, 0=short-final. Builds the matcher's full result array in the engine's
    commit order (verb source valid=0, then foreign valid=1, then noun valid=2), then applies
    result[0] iff sub_100098F3's distinct count == 1. (asm: orchestrator sub_1000A95D.)
    Note: a 'no stress' word like RANKA is suppressed here because its spurious verb result[0]
    and its noun candidate are DISTINCT -> 98F3 returns >1 (not because result[0] is absent)."""
    T = tables()
    word = word.upper()
    L = len(word)
    # Verb records commit first (result+0 "valid"=0 unconditionally, asm @0x1000455a — a SOURCE tag,
    # NOT the prefix index), then noun records (valid=2, asm @0x10006828). result[0] = first verb if
    # any matched, else first noun. The disambiguation scan terminates on valid>=2 in PHASE1.
    results = _verb_results(word, L, T)                # (valid=0, pos, type, prio)
    results += _foreign_results(word)                  # (valid=1, pos, type, 40) — commit after verbs
    results += _noun_results(word)                     # (valid=2, pos, type, prio) — prio per record
    if not results:
        return None
    if _disambig98F3(results) == 1:                    # unambiguous -> apply result[0]
        return results[0][1], results[0][2]
    return None


def _mode1(word, res, forced_type=None):
    """MODE 1 retraction walk (asm 0x10003F66-0x100042EC), bit-exact. Returns (pos1, type).
    `word` is the matcher token (uppercase, NO trailing space)."""
    from . import nucleus
    T = tables(); cc = T["cc"]; stems = T["stems"]
    eidx = res["eidx"]; g = res["group"]; vidx = res["vidx"]
    estr = T["endings"][eidx][0]
    _, groups, vflag = stems[vidx]
    acc = groups[g][1]
    # -0x4dc8 init 0xff; a mode routine may pre-set it (forced_type), else = V.group[g].acc[1]
    typ = forced_type if forced_type is not None else acc[1]
    L = len(word)
    # nucleus attr exactly as KircTranskr builds it: word buffer = '_' + token + ' ', attr init=1s,
    # then sub_10009310 (so a nucleus = byte 2). The '_' prefix supplies the matcher's -1 alignment,
    # so the walk reads nuc[pos] directly. e090 stem-diphthong marks: TODO.
    nuc = nucleus.kirc_nucleus(word)
    # e090 stem-diphthong marks (asm 0x10003DCC loop): for each adjacent vowel pair in the stem, if
    # the matching bit of verb.e090 (vflag[4]) is set, mark the 2nd vowel's attr position as a
    # nucleus via sub_1000185E: attr = (attr|2)&0xe. output+0x15+pos2 == nuc[pos2+1].
    e090 = vflag[4]
    prefixes = T["prefixes"]
    prefixlen = len(prefixes[res["prefix_idx"]][0] or "")
    bitcnt = 0
    for pos2 in range(prefixlen, L - 1 - len(estr)):
        if word[pos2] in cc["vowels_d8"] and word[pos2 + 1] in cc["vowels_e8"]:
            if (1 << bitcnt) & e090:
                k = pos2 + 1
                if 0 <= k < len(nuc):
                    nuc[k] = (nuc[k] | 2) & 0xe
            bitcnt += 1
    def WA(k):                                     # output+0x14+k
        return 1 if (0 <= k < len(nuc) and (nuc[k] & 2)) else 0
    pos = L - len(estr)                            # -0x46ac : first ending char index
    # 0x10003FE8: if word[pos] not a vowel -> pos++ ; if past end (nul) pos++
    if pos < L and word[pos] not in cc["vowels_f8"]:
        pos += 1
        if pos >= L:
            pos += 1
    # retract acc[0] syllables (each: pos--, then walk left while not a nucleus)
    syl = acc[0]
    cnt = 0
    while cnt < syl:                               # 0x10004054
        if pos > 0:
            pos -= 1
        while True:                                # 0x10004095 inner
            if WA(pos):
                break
            if pos > 0:
                pos -= 1
            else:
                break
        cnt += 1
    if pos > 0:                                    # 0x100040d4
        pos -= 1
    if typ == 0:                                   # 0x100040EC
        while pos > 0 and word[pos] not in cc["vset_108"]:
            pos -= 1
    elif typ == 2:                                 # 0x10004137
        while pos > 0 and word[pos] not in cc["vsonor_118"]:
            pos -= 1
        if pos > 0 and word[pos - 1] not in cc["AEIUO"] and word[pos] in cc["LMNR"]:
            pos -= 1
    else:                                          # typ == 1, 0x100041E1
        while pos > 0 and word[pos] not in cc["vset_13c"]:
            pos -= 1
        go = (word[pos] in cc["UIO"] and pos > 0 and word[pos - 1] in cc["AEOU"]) or \
             (word[pos] == "E" and pos > 0 and word[pos - 1] == "I")
        if go and not WA(pos) and pos > 0:     # 0x10004284
            pos -= 1
    return pos + 1, typ


# ---- FOREIGN path (source 1, valid=1) -----------------------------------------------------------
# Bit-for-bit port of the matcher's foreign stage (asm 0x10004627-0x10004b86). The foreign lexicon
# (@0x1010da40, 2465 x 8B = form_ptr + acc[4]) is matched as a WHOLE forward word (strcmp); EVERY
# matching entry (forms can repeat with different accents) becomes a candidate, committed valid=1,
# priority 40, in lexicon order, AFTER the verb records and BEFORE the noun records. Each entry's
# acc bytes drive a retraction walk identical in shape to the verb MODE-1 walk:
#   acc[0] = syllables to retract from the word end;  acc[1] = accent type {0,1,2};
#   acc[2] = diphthong-nucleus bitmask (which adjacent vowel pairs count as one nucleus, sets
#            0x101d9174/9184);  acc[3] = the +0x46 accent byte (a rendering detail, not pos/type).
_FRN = None
def _foreign_lex():
    """form (uppercase cp1257 bytes) -> list of acc[4] (one per lexicon entry, in order)."""
    global _FRN
    if _FRN is None:
        _FRN = {}
        for form, acc in _load("foreign_lexicon.json"):
            if form is None:
                continue
            _FRN.setdefault(form.encode("cp1257", "replace"), []).append(acc)
    return _FRN

# foreign-walk char classes (cp1257 BYTE sets) — same content as the noun/verb walk's, by VA:
# 0x101d9174/9184/9194/91c8 = full vowel set; 0x101d91a4 = vowels+LMNR; 0x101d91b8 = AEIUO;
# 0x101d91c0 = LMNR; 0x101d91d8 = UIO; 0x101d91dc = AEOU.
_FV     = {65, 192, 69, 198, 203, 73, 89, 193, 79, 85, 219, 216}   # A Ą E Č Ė I Y Į O U Ū Ų
_FVL    = _FV | {76, 77, 78, 82}                                   # + L M N R
_FAEIUO = {65, 69, 73, 85, 79}
_FLMNR  = {76, 77, 78, 82}
_FUIO   = {85, 73, 79}
_FAEOU  = {65, 69, 79, 85}

def _foreign_walk(wb, a0, a1, a2):
    """asm 0x10004749-0x10004aaf. wb = forward word cp1257 bytes (the matcher token). Returns the
    1-based stress pos (result+8 = walk_pos+1). Mirrors verb _mode1: a2-selected diphthong 2nd-
    vowels are marked as nuclei, pos starts at len, retract a0 syllables, one extra pos--, then a
    type(a1)-based landing walk. The type-1 retreat reads the nucleus of pos-1 (result+0x13+pos)."""
    from . import nucleus
    L = len(wb)
    nuc = nucleus.kirc_nucleus(wb.decode("cp1257"))    # nuc[k] = nucleus bit of word char (k-1)
    def WA(k):
        return 1 if (0 <= k < len(nuc) and (nuc[k] & 2)) else 0
    # diphthong-nucleus marks (asm 0x1000476e loop): for each adjacent vowel pair (both in _FV), if
    # the matching bit of a2 is set, mark the slot of the pair's 2nd char (sub_1000185e on +0x15+pos).
    bit = 0
    for pos in range(0, L - 1):
        if wb[pos] in _FV and wb[pos + 1] in _FV:
            if (1 << bit) & a2:
                k = pos + 1
                if 0 <= k < len(nuc):
                    nuc[k] = (nuc[k] | 2) & 0xe
            bit += 1
    pos = L                                            # asm 0x10004825: pos = wordlen
    cnt = 0
    while cnt < a0:                                    # retract a0 syllables
        if pos > 0:
            pos -= 1
        while not WA(pos) and pos > 0:                 # asm 0x1000487c inner
            pos -= 1
        cnt += 1
    if pos > 0:                                        # asm 0x100048b8
        pos -= 1
    if a1 == 0:                                        # TYPE 0 (asm 0x100048e3): walk to a vowel
        while pos > 0 and wb[pos] not in _FV:
            pos -= 1
    elif a1 == 2:                                      # TYPE 2 (asm 0x1000493a): vowel+LMNR + diphthong
        while pos > 0 and wb[pos] not in _FVL:
            pos -= 1
        if pos > 0 and wb[pos - 1] not in _FAEIUO and wb[pos] in _FLMNR:
            pos -= 1
    else:                                              # TYPE 1 (asm 0x100049d4): vowel + UIO/EI keep
        while pos > 0 and wb[pos] not in _FV:
            pos -= 1
        go = (wb[pos] in _FUIO and pos > 0 and wb[pos - 1] in _FAEOU) or \
             (pos > 0 and wb[pos] == 0x45 and wb[pos - 1] == 0x49)     # 'E' after 'I'
        if go and not WA(pos - 1) and pos > 0:         # retreat unless prev char is a nucleus
            pos -= 1
    return pos + 1

def _foreign_results(word):
    """Yield (valid=1, pos, type, prio=40) for every exact foreign-lexicon match, in lexicon order."""
    wb = word.encode("cp1257", "replace")
    out = []
    for acc in _foreign_lex().get(wb, ()):
        a0, a1, a2 = acc[0], acc[1], acc[2]
        pos1 = _foreign_walk(wb, a0, a1, a2)
        out.append((1, pos1, a1, 40))
    return out


# ---- NOUN/adj path (source 3, valid=2) ----------------------------------------------------------
# Bit-for-bit port of the matcher's noun stage (asm 0x10004B86-0x100068E3). Pipeline per word:
#   strrev(WORD) -> match a 141-entry REVERSED main-ending (@0x10112748) as a prefix -> reversed
#   remaining-stem -> binary-search the 60783 main lexicon (@0x1005b908, REVERSED stems) for an exact
#   stem -> read the record's grammatical-class byte b90c -> EXPAND it into declension paradigm
#   variants -> for each, look up the da-table (@0x10112da4) by ending index -> a column -> the
#   bb0/c60 tables (@0x10112bb0/@0x10112c60) give a MODE {0,1,2}:
#     mode 0: pos = L - mainend.flag[1]; type = mainend.flag[2]            (ending-fixed)
#     mode 1: retraction walk (b90e syllables, then b90f-typed final walk); type = b90f
#     mode 2: refine to 0/1 by a word-start walk + class/b90f checks, then apply as 0/1
#   commit to the result array with valid=2. (data: main_lexicon.json now [stem,bytes8],
#   main_endings.json, noun_accent_tables.json {bb0,c60,da}). Deferred: '*'-tail stems
#   (sub_10001BB1) and the Č / ŽD cluster + NE- prefix candidate expansions (rare).
_NT = None
def _noun_tables():
    global _NT
    if _NT is None:
        main = _load("main_lexicon.json")          # [REV_stem, b[8]]  @0x1005b908
        ends = _load("main_endings.json")          # [REV_ending, flag[4]] @0x10112748
        nt   = _load("noun_accent_tables.json")    # bb0(4x44) / c60(324) / da(44x19)
        # stem (cp1257 bytes) -> list of (idx, bytes8), preserving sorted/lexicon order
        idx = {}
        for i, (s, b) in enumerate(main):
            if s is None:
                continue
            key = s.encode("cp1257", "replace")
            idx.setdefault(key, []).append((i, b))
        _NT = dict(main=main, ends=ends, bb0=nt["bb0"], c60=nt["c60"], da=nt["da"],
                   t17=nt["t17"], idx=idx)
    return _NT

# class -> appended declension-paradigm variants (asm 0x1000550d-0x10005cbd). Original class is kept,
# variants appended after it (order matters for result[0]).
_NOUN_EXPAND = {
    0x10: [0x11, 0x1c, 0x1d], 0x25: [0x26, 0x1c], 0x12: [0x13, 0x1e, 0x1f],
    0x14: [0x15, 0x20, 0x1f], 0x16: [0x17, 0x20, 0x1f], 0x18: [0x19], 0x27: [0x28],
    0x1a: [0x1b, 0x21, 0x1f], 0x29: [0x2a, 0x2b, 0x1d],
}
_NOUN_FILTER = {0x10, 0x12, 0x14, 0x16, 0x18, 0x1a, 0x25, 0x27, 0x29}   # b90c filter (prefix-stripped)

_MAIN_STEMB = None
def _main_stemb():
    global _MAIN_STEMB
    if _MAIN_STEMB is None:
        _MAIN_STEMB = [(s.encode("cp1257", "replace") if s else b"") for s, b in _noun_tables()["main"]]
    return _MAIN_STEMB

def _prio(idx):
    """Per-entry priority = matcher-top init (asm 0x100027d4): '*'-pos+0x14 (or '*'-pos if next=='4'),
    else 0x1e for normal stems."""
    sb = _main_stemb()[idx]
    sp = sb.find(0x2a)
    if sp < 0:
        return 0x1e
    after = sb[sp + 1] if sp + 1 < len(sb) else 0
    return sp if after == 0x34 else sp + 0x14

def _bisect_a9a(key):
    """Port of sub_10001A9A: a 2-3 level strcmp bisect over main_lexicon[0..0xed2e] returning a
    COARSE [lo,hi] bracket (the caller then linear-scans it). key, stems compared as cp1257 bytes."""
    stb = _main_stemb()
    def cmp(i):                                    # strcmp(main[i], key)
        a = stb[i]
        return (a > key) - (a < key)
    lo, hi = 0, 0xed2e
    mid = (lo + hi) // 2
    if cmp(mid) > 0:                               # main[mid] > key -> lower half
        hi = mid
    elif cmp(mid) < 0:                             # main[mid] < key -> upper half
        lo = mid
    else:                                          # equal -> refine both ends
        m2 = (lo + mid) // 2
        if cmp(m2) < 0:
            lo = m2
        m3 = (hi + mid) // 2
        if cmp(m3) > 0:
            hi = m3
    return lo, hi

def _dllclasses():
    """The two char-class groups (star_classes + noun_walk_cc), extracted offline from transcr4's
    .data into accent_dllclasses.json — a shipped table, no binary needed at runtime."""
    return json.load(open(paths.data_path("accent_dllclasses.json"), encoding="utf-8"))

_V903 = None
def _star_classes():
    global _V903
    if _V903 is None:
        _V903 = {k: set(v) for k, v in _dllclasses()["star_classes"].items()}
    return _V903

_NWC = None
def _noun_walk_cc():
    """Noun mode-1 walk char classes (cp1257 char sets): 9248/927c=vowels, 9258=vowels+LMNR,
    926c=AEIUO, 9274=LMNR, 928c=UIO, 9290=AEOU. (The verb 'vowels_f8' set omits Ū/Ų — wrong here.)"""
    global _NWC
    if _NWC is None:
        _NWC = {k: set(v) for k, v in _dllclasses()["noun_walk_cc"].items()}
    return _NWC


# ---- '*'-tail cases 1/2/3: verb-derived stems (asm 0x10002219, shared case-1/2/3 handler) --------
# These derive the noun stem from the VERB lexicon (forward stems @0x1000e070): strip a prefix,
# bracket the verb stems, and accept entries whose stem+group[dcat].field == the (prefix-stripped)
# forward remaining, filtered by the digit triplet on e08c/e08e/e08f. dcat = digitstr[0]-'1' picks
# verb group 0/1/2. Validated 250/250 vs the --star oracle. See memory/accent-system-re.md.
_VB_VOW = set(bytes([65, 192, 69, 198, 203, 73, 89, 193, 79, 85, 219, 216]))  # 9074 full vowel set
_VERB_STEMB = None
def _verb_stemb():
    global _VERB_STEMB
    if _VERB_STEMB is None:
        _VERB_STEMB = [(s.encode("cp1257", "replace") if s else b"") for s, g, v in tables()["stems"]]
    return _VERB_STEMB

_PFXB = None
def _pfxb():
    global _PFXB
    if _PFXB is None:
        _PFXB = [(p.encode("cp1257", "replace") if p else b"") for p, f in tables()["prefixes"]]
    return _PFXB

def _verb_bracket(cand):
    """Port of the verb two-phase bracket sub_1000187F over the 0x2276 forward verb stems. Returns
    [save_lo, hi]: phase-1 first-char bracket, phase-2 strcmp refine. The caller linear-scans it."""
    VSTB = _verb_stemb()
    def cmpb(a, b):
        return (a > b) - (a < b)
    c0 = cand[0] if cand else 0
    lo, hi = 0, 0x226f
    while True:                                        # phase 1: first-char bracket
        lo0, hi0 = lo, hi; mid = (lo + hi) >> 1
        sc = VSTB[mid][0] if VSTB[mid] else 0
        if sc >= c0:
            hi = mid
        else:
            lo = mid
        if not (lo0 + 2 < lo or hi + 2 < hi0):
            break
    save_lo = lo; hi = 0x226f
    while True:                                        # phase 2: strcmp refine
        lo0, hi0 = lo, hi; mid = (lo + hi) >> 1
        r = cmpb(VSTB[mid], cand)
        if r > 0:
            hi = mid
        elif r < 0:
            lo = mid
        else:
            m2 = (lo0 + mid) >> 1
            if cmpb(VSTB[m2], cand) < 0:
                lo = m2
            m3 = (hi0 + mid) >> 1
            if cmpb(VSTB[m3], cand) > 0:
                hi = m3
        if not (lo0 + 2 < lo or hi + 2 < hi0):
            break
    return save_lo, hi

def _star_case123(digitstr, remaining, char_idx, b90f_arg, accout, depth, g):
    """asm 0x10002219 (shared case-1/2/3 handler). digitstr,remaining = cp1257 bytes; dcat =
    digitstr[0]-'1' selects verb group 0/1/2. Returns acc_state (0/1/2) and writes accout[depth]."""
    stems = tables()["stems"]; VSTB = _verb_stemb(); PFX = _pfxb()
    dcat = digitstr[0] - 0x31                          # 0,1,2
    fwd = remaining[::-1]                               # forward stem
    pfxmax = 1 if (len(digitstr) > 1 and digitstr[1] == 0x31) else 252
    lacc = [accout[depth], accout[depth]]; acc_state = 0
    for pi in range(0, pfxmax):
        pmatch = 0; j = 0
        if pi > 0:
            if pi >= len(PFX):
                break
            p = PFX[pi]
            while j < len(fwd) and j < len(p) and fwd[j] == p[j]:
                j += 1
            if j >= len(p) and j < len(fwd):           # prefix fully matched, remainder nonempty
                pmatch = 1
        if pi != 0 and pmatch == 0:
            continue
        cand = fwd[j:]
        lo, hi = _verb_bracket(cand)
        vidx = lo
        while vidx < 0x2276:
            s = VSTB[vidx]; sg = stems[vidx][1]; vf = stems[vidx][2]
            field = (sg[dcat][0] or "").encode("cp1257", "replace")
            if s + field != cand:
                vidx = 0x2270 if vidx == hi else vidx + 1; continue
            e08c, e08e, e08f = vf[0], vf[2], vf[3]; e090, e091 = vf[4], vf[5]
            gacc0, gacc1 = sg[dcat][1][0], sg[dcat][1][1]
            d1 = digitstr[1] if len(digitstr) > 1 else 0
            ok = (d1 == 0x30 and pmatch == 1) or (d1 == 0x31 and pmatch == 0) or (d1 == 0x32)
            if not ok:
                vidx = 0x2270 if vidx == hi else vidx + 1; continue
            d2 = digitstr[2] if len(digitstr) > 2 else 0
            if d2 != 0x30:                             # e08e/e08f digit filter (dcat-specific)
                m = False
                if dcat == 0:
                    if d2 == 0x31 and e08e == 0: m = True
                    elif d2 == 0x32 and e08e == 1: m = True
                elif dcat == 1:
                    if d2 == 0x31 and (e08f == 0 or (e08c & 0x40)): m = True
                    elif d2 == 0x32 and e08f == 1: m = True
                elif dcat == 2:
                    if d2 == 0x31 and e08f == 0: m = True
                    elif d2 == 0x32 and e08f == 1: m = True
                if not m:
                    vidx = 0x2270 if vidx == hi else vidx + 1; continue
            d3 = digitstr[3] if len(digitstr) > 3 else 0
            if d3 != 0x30:                             # e08c &0x3f / &0x40 filter
                if d3 == 0x31:
                    if not (e08c & 0x3f):
                        vidx = 0x2270 if vidx == hi else vidx + 1; continue
                elif d3 == 0x32:
                    if not (e08c & 0x40):
                        vidx = 0x2270 if vidx == hi else vidx + 1; continue
                else:
                    vidx = 0x2270 if vidx == hi else vidx + 1; continue
            # ACCEPT (asm 0x100025xx)
            if g["ce4"] == -1:
                g["ce4"] = e090; g["ce0"] = e091
            if pmatch == 1:                            # PER-prefix keeps pmatch, else cleared
                pmatch = 1 if (b"PER" in PFX[pi]) else 0
            if b90f_arg != -1 and pmatch == 0:
                accout[depth] = 0; return 1
            if acc_state == 0:
                if lacc[acc_state] == 0:
                    if pmatch == 0:
                        g["cf8"] = gacc0; g["cf4"] = gacc1
                    else:                              # PER-prefix syllable count
                        idx = fwd.find(b"PER") + 3; cnt = 1
                        while idx < len(fwd) and fwd[idx] != 0:
                            while idx < len(fwd) and fwd[idx] in _VB_VOW: idx += 1
                            while idx < len(fwd) and fwd[idx] not in _VB_VOW: idx += 1
                            cnt += 1
                        g["cf8"] = cnt; g["cf4"] = 1
                lacc[acc_state] += char_idx; acc_state = 1
            elif acc_state == 1:
                if pmatch == 0:
                    if not (g["cf8"] == gacc0 and g["cf4"] == gacc1):
                        return 2                       # conflict — NO out write (asm jumps past 0x10002769)
            vidx = 0x2270 if vidx == hi else vidx + 1
    accout[depth] = lacc[0]; return acc_state


def _star_parse(digitstr, ctx, start, char_idx, b90f_arg, accout, depth, g):
    """Bit-exact port of sub_10001BB1 case-0 (+case-4). digitstr=bytes after '*'; ctx=full stem buffer
    (bytes), start=offset so the remaining stem = ctx[start:]; char_idx,b90f_arg = args 3,4; accout=the
    out array (list), depth=arg6; g=globals dict {ce0,ce4,ce8,cf4,cf8}. Returns acc_state (0/1/2) and
    writes accout[depth]. (cases 1/2/3 = prefix/verb derivation -> not yet ported, return 0.)"""
    main = _noun_tables()["main"]; stb = _main_stemb(); t17 = _noun_tables()["t17"]; CC = _star_classes()
    if not digitstr:
        return 0
    d0 = digitstr[0] - 0x30
    if d0 > 4:
        return 0
    # local copy of the remaining stem + head Č/ŽD restore (asm 0x10001be9)
    local = bytearray(ctx[start:])
    if start >= 1 and ctx[start - 1] == 0x49 and start >= 2 and ctx[start - 2] in CC["c9034"]:
        if local and local[0] == 0xc8:
            local[0] = 0x54
        elif len(local) > 1 and local[0] == 0xde and local[1] == 0x44:
            local = local[1:]
    local = bytes(local)
    if d0 == 4:                                     # case 4 (asm 0x10002754): match iff remaining nonempty
        return 1 if local else 0
    if d0 != 0:                                     # cases 1/2/3: verb-derived stems (asm 0x10002219)
        return _star_case123(digitstr, local, char_idx, b90f_arg, accout, depth, g)
    digit2 = digitstr[1] - 0x30
    valid_cls = t17[digit2] if 0 <= digit2 < len(t17) else [-1]
    lacc = [accout[depth], accout[depth]]           # local 2-elem acc array (both init out[depth])
    acc_state = 0
    maxprio = 0
    lo, hi = _bisect_a9a(local)
    idx = lo
    while idx < 0xed6f:
        s = stb[idx]; b = main[idx][1]
        j = 0
        while j < len(s) and j < len(local) and s[j] == local[j]:
            j += 1
        # digitstr[4]=='1' special (asm 0x10001cf9): K/J + I stem with b90e>1 -> walk m
        m = 0
        if len(digitstr) > 4 and digitstr[4] == 0x31:
            if len(s) >= 2 and s[0] in (0x4b, 0x4a) and s[1] == 0x49 and b[2] > 1:
                while (m + 2) < len(s) and m < len(local) and s[m + 2] == local[m] and s[m + 2] != 0:
                    m += 1
        rec_ret = 0
        accept = False
        if j >= len(s):                             # main stem ended
            if j >= len(local):
                accept = True                       # exact full match
        elif s[j] == 0x2a:                          # nested '*' on lexicon side
            if maxprio <= _prio(idx):
                rec_ret = _star_parse(s[j + 1:], local, j, j, b[3], lacc, acc_state, g)
                if rec_ret != 0:
                    accept = True
        if not accept:
            se = (m + 2 >= len(s)) or (s[m + 2] == 0) if (m + 2) <= len(s) else True
            le = (m >= len(local)) or (local[m] == 0)
            if (m + 2 < len(s) and s[m + 2] != 0) or (m < len(local) and local[m] != 0):
                idx = 0xed2f if idx == hi else idx + 1
                continue
            accept = True
        # ACCEPT (asm 0x10001e63)
        b90c, b90d, b90e, b90f, b910, b911 = b[0], b[1], b[2], b[3], b[4], b[5]
        if maxprio < _prio(idx):
            maxprio = _prio(idx)
        if b90c not in [c for c in valid_cls if c != -1]:          # T17 class filter
            idx = 0xed2f if idx == hi else idx + 1; continue
        c2 = digitstr[2] if len(digitstr) > 2 else 0
        if c2 == 0x31:
            if b90d not in (1, 2):
                idx = 0xed2f if idx == hi else idx + 1; continue
        elif c2 == 0x32:
            if b90d not in (3, 4):
                idx = 0xed2f if idx == hi else idx + 1; continue
        elif c2 != 0x30:
            idx = 0xed2f if idx == hi else idx + 1; continue
        c3 = digitstr[3] if len(digitstr) > 3 else 0
        if c3 != 0x30:
            p = 0                                          # asm 0x10001f77: skip non-V, V, non-V
            while p < len(local) and local[p] not in CC["c903c"]: p += 1
            while p < len(local) and local[p] in CC["c904c"]: p += 1
            while p < len(local) and local[p] not in CC["c905c"]: p += 1
            ended = p >= len(local)
            if c3 == 0x31:
                if not ended:
                    idx = 0xed2f if idx == hi else idx + 1; continue
            elif c3 == 0x32:
                if ended:
                    idx = 0xed2f if idx == hi else idx + 1; continue
        # globals
        if g["ce4"] == -1:
            g["ce4"] = b910; g["ce0"] = b911
        if b90f_arg != -1:
            accout[depth] = 0
            return 1
        if rec_ret == 2:
            return 2
        if maxprio > _prio(idx):
            idx = 0xed2f if idx == hi else idx + 1; continue
        # accumulate (asm 0x100020c6)
        se = (m + 2 >= len(s) or s[m + 2] == 0) and (m >= len(local) or local[m] == 0)
        if acc_state == 0:
            if lacc[acc_state] == 0:
                ce8 = 1 if se else 0
                g["cf8"] = b90e - ce8; g["cf4"] = b90f; g["ce8"] = ce8
            lacc[acc_state] += char_idx
            acc_state = 1
        elif acc_state == 1:
            ce8 = 1 if se else 0
            if b90f == -1:
                idx = 0xed2f if idx == hi else idx + 1; continue
            if not (g["cf8"] == b90e - ce8 and g["cf4"] == b90f):
                return 2
        idx = 0xed2f if idx == hi else idx + 1
    accout[depth] = lacc[0]                          # exit (asm 0x10002769): out[depth]=lacc[0]
    return acc_state


def _noun_modes(b90c, b90d, ending_idx):
    """da/bb0/c60 lookup -> LIST of (mode, col, da5) for EVERY da entry whose ending_idx matches. The engine's
    da-scan (asm 0x10005d72-0x10005f8f) does NOT stop at the first match: it loops to the -1 terminator and
    emits a candidate PER matching column (e.g. cls 19 / 'SOI' matches col 1 AND col 7 -> two noun candidates,
    which is what makes atgalios/kokios/širdis/žmonės ambiguous). Returns [] if none / out of range."""
    T = _noun_tables()
    if not (0 <= b90c < 44) or not (1 <= b90d <= 4):
        return []
    out = []
    c60row = T["bb0"][(b90d - 1) * 44 + b90c]
    for col, da5, eidx in T["da"][b90c]:
        if eidx == -1:
            break
        if eidx == ending_idx:
            out.append((T["c60"][c60row * 0x0e + col], col, da5))
    return out


def _noun_mode(b90c, b90d, ending_idx):
    """Back-compat: first matching (mode, col) or None."""
    ms = _noun_modes(b90c, b90d, ending_idx)
    return (ms[0][0], ms[0][1]) if ms else None

def _noun_walk(wb, res, cc):
    """Noun MODE-1 retraction walk (asm 0x100064e3-0x100067b9). `wb` = word cp1257 bytes (no space).
    Returns 1-based pos. type is res['b90f'] (set by caller)."""
    from . import nucleus
    T = _noun_tables()
    L = len(wb)
    estr = T["ends"][res["ending_idx"]][0] or ""
    elen = len(estr)
    nuc = nucleus.kirc_nucleus(wb.decode("cp1257"))
    def WA(k):
        return 1 if (0 <= k < len(nuc) and (nuc[k] & 2)) else 0
    V = cc["vowels_f8"]                                  # full vowel set (chars)
    def isv(p, s):                                       # byte p in char-set s
        return 0 <= p < L and chr(wb[p]) in s
    pos = L - elen + 1 - res.get("cec", 0)               # 0x100064e3 init
    syl = res["b90e"]                                    # retract b90e syllables
    for _ in range(syl):                                 # 0x10006530
        if pos > 0:
            pos -= 1
        while not WA(pos) and pos > 0:                   # 0x1000656f
            pos -= 1
    if pos > 0:                                          # 0x100065ab
        pos -= 1
    b90f = res["b90f"]
    NW = _noun_walk_cc()                                 # cp1257 BYTE sets
    if b90f == 0xff or b90f == -1:
        return None
    if b90f == 0:                                        # 0x100065ed walk to vowel (9248)
        while pos > 0 and wb[pos] not in NW["V"]:
            pos -= 1
    elif b90f == 2:                                      # 0x1000662d walk to vowel+LMNR (9258) then diphthong
        while pos > 0 and wb[pos] not in NW["VL"]:
            pos -= 1
        if pos > 0 and wb[pos - 1] not in NW["AEIUO"] and wb[pos] in NW["LMNR"]:        # 926c/9274
            pos -= 1
    else:                                                # b90f==1: 0x100066de walk (927c) + UIO/EI rule
        while pos > 0 and wb[pos] not in NW["V1"]:
            pos -= 1
        go = (wb[pos] in NW["UIO"] and pos > 0 and wb[pos - 1] in NW["AEOU"]) or \
             (pos > 0 and wb[pos] == 0x45 and wb[pos - 1] == 0x49)                       # E after I
        if go and not WA(pos) and pos > 0:
            pos -= 1
    return pos + 1


_STAR = None
def _star_entries():
    """The 64 '*'-tail main_lexicon entries as (prefix_bytes, digitstr_bytes, bytes8), lexicon order."""
    global _STAR
    if _STAR is None:
        _STAR = []
        for s, b in _noun_tables()["main"]:
            if s and "*" in s:
                pre, dg = s.split("*", 1)
                _STAR.append((pre.encode("cp1257", "replace"), dg.encode("cp1257", "replace"), b))
    return _STAR

_STRCHR_BDG = set(b"BDGKPTC\xc8FH")           # transcr4 strchr table @0x101d923c (stop/fricative consonants)


def _mode2_walks(word, ei):
    """Port of the transcr4 noun-matcher mode-2 preprocessing + decision (asm 0x1000627f-0x1000645e),
    VERIFIED bit-exact vs the engine via the Win32 debugger (pos/p1/chars captured on
    garsiai/gerai/retai/ramiai/tyliai/siaubiai). `word` = uppercase cp1257 bytes (no '_', no trailing space).
    Returns True if mode 2 resolves to the mode-1 WALK, False if it stays ending-FIXED.
    Buffer model: the nucleus attr is indexed on '_'+word (kirc_nucleus), length n = len('_'+word) = len+1,
    BUT the char comparisons read the PLAIN word at the same numeric index (the '_' shifts the attr pointer
    but not the char pointer). pos init = n-3; two nucleus walks with a digraph adjust between them."""
    from . import nucleus
    attr = nucleus.kirc_nucleus(word.decode("cp1257", "replace")
                                if isinstance(word, bytes) else word)
    wb = word if isinstance(word, bytes) else word.encode("cp1257", "replace")
    n = len(wb) + 1                                        # strlen('_'+word)

    def A(i):                                             # nucleus bit of attr[i] (the '_'+word attr array)
        return (attr[i] & 2) if 0 <= i < len(attr) else 0

    def C(i):                                             # plain-word char at index i (0x20 past the end)
        return wb[i] if 0 <= i < len(wb) else 0x20
    pos = n - 3
    while pos > 0 and not A(pos):                          # walk 1: left to a nucleus
        pos -= 1
    if pos > 0:                                            # step off the nucleus
        pos -= 1
    # digraph adjust: ZD / ŽD / CH -> pos-=2 ; else a stop/fricative consonant -> pos-=1
    if ((C(pos) == 0x5a and C(pos - 1) == 0x44) or (C(pos) == 0xde and C(pos - 1) == 0x44)
            or (C(pos) == 0x48 and C(pos - 1) == 0x43)):
        if pos > 1:
            pos -= 2
    elif C(pos) in _STRCHR_BDG and pos > 0:
        pos -= 1
    p1 = pos
    while pos > 0 and not A(pos):                          # walk 2 (advances pos; the decision uses p1)
        pos -= 1
    # decision (asm 0x100063cb..0x1000645e): only reached when pos==0; then WALK unless the IA/IE check
    # (word[p1] in {A,E} AND word[p1-1] != 'I') -> FIXED. (word[p1] not in {A,E} -> WALK.)
    if pos != 0:
        return False
    if C(p1) in (0x41, 0x45) and C(p1 - 1) != 0x49:
        return False
    return True


def _noun_emit(out, ei, eflag, L, wb, cc, cls_list, b90d, b90e, b90f, cec, prio):
    """da-accent (mode lookup) + mode dispatch + walk for one matched stem; append (2,pos,type,prio).
    The engine's da-scan emits a candidate per matching column (not just the first) -- see _noun_modes."""
    for cls in cls_list:
        for mode, col, _da5 in _noun_modes(cls, b90d, ei):
            if mode == 2:
                # mode 2 (asm 0x1000640a): only class 0x1a(26) is eligible for the WALK; any other class is
                # ending-FIXED. Within class 26, the nucleus preprocessing decides WALK vs FIXED. (cont.32,
                # debugger-verified bit-exact on garsiai/gerai/retai/ramiai/tyliai/siaubiai.)
                m = 1 if (cls == 26 and _mode2_walks(wb, ei)) else 0
            else:
                m = mode
            if m == 0:                                      # ending-fixed
                pos1, typ = L - eflag[1], eflag[2]
            elif b90f == -1 or b90f == 0xff:                # type -1 (0xff): the walk yields the (-1,-1) marker
                pos1, typ = -1, -1                          # candidate (engine keeps it as a distinct result;
            else:                                           # the '*'-tail ret==2 / b9f=-1 feedback, e.g. -inė)
                res = dict(ending_idx=ei, b90e=b90e, b90f=b90f, cec=cec)   # mode 1 retraction walk
                pos1 = _noun_walk(wb, res, cc)
                if pos1 is None:
                    continue
                typ = b90f
            out.append((2, pos1, typ, prio))


def _noun_results(word):
    """Yield (valid=2, pos, type) noun records in engine order. Exact + '*'-tail + cluster/NE."""
    T = _noun_tables(); cc = tables()["cc"]
    wb = word.encode("cp1257", "replace")
    rev = wb[::-1]
    L = len(wb)
    AEIU = set(b"AEIU")
    CC, ZH, DD, TT = 0xc8, 0xde, ord("D"), ord("T")       # Č, Ž, D, T
    is_ne = L >= 2 and wb[0] == ord("N") and wb[1] == ord("E")
    out = []
    for ei, (estr, eflag) in enumerate(T["ends"]):
        if not estr:
            continue
        eb = estr.encode("cp1257", "replace")
        if len(eb) >= len(rev) or rev[:len(eb)] != eb:
            continue
        off = len(eb)
        if rev[off] in AEIU:                              # 0x101d91e4 gate
            continue
        base = rev[off:]
        # candidate variants = (rev_stem, prefix_flag). Cluster restore (asm 0x10004cd1/0x10004d66)
        # only when mainend.flag0==1; NE- prefix strip (0x10004e37) for words starting "NE".
        cands = [(base, 0)]
        if eflag[0] == 1:
            if base[0] == CC:
                cands.append((bytes([TT]) + base[1:], 0))      # Č -> T
            elif len(base) > 1 and base[0] == ZH and base[1] == DD:
                cands.append((base[1:], 0))                    # ŽD -> D (drop Ž)
        if is_ne:
            ne = base[:-2]                                      # drop trailing "EN" (=NE prefix)
            if ne:
                cands.append((ne, 1))
                if eflag[0] == 1 and ne[0] == CC:
                    cands.append((bytes([TT]) + ne[1:], 1))
        for rev_stem, pflag in cands:
            for midx, b in T["idx"].get(rev_stem, ()):         # exact lexicon match
                b90c, b90d = b[0], b[1]
                if pflag and b90c not in _NOUN_FILTER:         # b90c filter (0x10005227, flag!=0 only)
                    continue
                _noun_emit(out, ei, eflag, L, wb, cc,
                           [b90c] + _NOUN_EXPAND.get(b90c, []), b90d, b[2], b[3], 0, 0x1e)
            for pre, dg, b in _star_entries():                 # '*'-tail derived stems (sub_10001BB1)
                if not rev_stem.startswith(pre) or len(pre) >= len(rev_stem):
                    continue
                b90c, b90d = b[0], b[1]
                if pflag and b90c not in _NOUN_FILTER:
                    continue
                b9f_arg = b[3] - 256 if b[3] > 127 else b[3]
                g = dict(ce0=0, ce4=-1, ce8=0, cf4=0, cf8=0); accout = [0, 0, 0, 0]
                ret = _star_parse(dg, rev_stem, len(pre), len(pre), b9f_arg, accout, 0, g)
                if ret == 0:
                    continue
                cec = accout[0]
                if cec == 0:                                   # noun feedback (asm 0x10005316)
                    nb90e, nb90f = b[2], b9f_arg
                else:
                    nb90e = g["cf8"]; nb90f = -1 if ret == 2 else g["cf4"]
                # '*'-entry priority (asm 0x100027d4): star_pos + 0x14, or star_pos if digit-after-* == '4'
                star_prio = len(pre) if (dg[:1] == b"4") else len(pre) + 0x14
                _noun_emit(out, ei, eflag, L, wb, cc,
                           [b90c] + _NOUN_EXPAND.get(b90c, []), b90d, nb90e, nb90f, cec, star_prio)
    return out
