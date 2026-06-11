# -*- coding: utf-8 -*-
# Bit-for-bit port of transcr4.dll's grapheme->phoneme + stress-casing renderer sub_1000aec0
# (the tail of KircTranskr). This is a DATA-DRIVEN transducer: a 156-record rule table
# (@0x101d9fd8, stride 0x1c, extracted verbatim to render_rules.json) interpreted over the
# uppercase word plus four per-char context arrays. Replaces the rule-based lt_transcribe.g2p
# faithfully (engine table, not a reimplementation).
#
# Inputs the orchestrator (KircTranskr body @0x1000a95d) hands the renderer, and how we build them:
#   word   buffer  = '_' + PradApdZod(word) + ' '   (the matcher token, padded; PART3 re-pads '_')
#   accode array   = per-char stress code, from the applied (pos,type) via the orchestrator's
#                    0x1000ad48 transform: none->1, short(0)->2, circumflex(1)->4, acute(2)->8 and
#                    the char BEFORE an acute -> 0x10 (long first element of an acute diphthong).
#   attr   array   = sub_10009310 nucleus marks (nucleus.kirc_nucleus) + the 0x1000acbe diphthong
#                    pass (attr[i+1] += 3 or 4 when attr[i]&2).
#   accbyte array  = result[0]+0x46 (always 0 in practice) -> all zeros.
# Record fields (0x1c bytes): +0 lc(left-context set), +4 mc(match char), +8 rs1 / +0xc rs2
# (right-context sets at pos+skip / pos+skip+1), +0x10 accmask (AND accode[pos]), +0x11 fl
# (&3 -> B1 palatalisation gate, &4 -> require accbyte[pos]), +0x12 b2 (AND B2 voicing gate),
# +0x13 attr (masked-equality vs attr[pos]&7), +0x14 out(phonemes to emit), +0x18 adv(pos delta),
# +0x19 skip(rule-index advance on a char mismatch).
import os, json
from . import paths

HERE = os.path.dirname(os.path.abspath(__file__))

# renderer literal char-class sets, raw cp1257 bytes (VAs 0x101db420/30/38/48). Defined by byte
# value to avoid unicode<->cp1257 round-trip ambiguity (e.g. 0xc0='Ą', 0xc6='Č'... in cp1257).
_VOWELS = {0x41, 0xc0, 0x45, 0xc6, 0xcb, 0x49, 0x59, 0xc1, 0x4f, 0x55, 0xdb, 0xd8}   # 0x101db420
_FRONT  = {0x45, 0xc6, 0xcb, 0x49, 0x59, 0xc1}                                       # 0x101db430
_OBSTR  = {0x42, 0x44, 0x47, 0x50, 0x54, 0x4b, 0x53, 0xd0, 0x5a, 0xde, 0x43, 0xc8}   # 0x101db438
_VOICED = {0x42, 0x44, 0x47, 0x5a, 0xde}                                             # 0x101db448
_INIT, _SEP, _TRAIL, _FINAL = "_\n", "\n", "+\n", "_"   # 0x101db41c/450/454/458
_J, _NUL = ord("J"), 0

_RULES = None
def _rules():
    global _RULES
    if _RULES is None:
        raw = json.load(open(paths.data_path("render_rules.json"), encoding="utf-8"))
        _RULES = []
        for r in raw:
            _RULES.append(dict(
                lc=r["lc"].encode("cp1257", "replace"),
                mc=r["mc"],
                rs1=r["rs1"].encode("cp1257", "replace"),
                rs2=r["rs2"].encode("cp1257", "replace"),
                accmask=r["accmask"], fl=r["fl"], b2=r["b2"], attr=r["attr"],
                out=r["out"].encode("cp1257", "replace"),
                adv=r["adv"], skip=r["skip"]))
    return _RULES


def _accode_transform(accode, L):
    """Orchestrator 0x1000ad48 forward pass: map the raw type array (init 0xff=-1, with type at the
    stressed pos) to the per-char stress codes the rule table matches on."""
    for i in range(1, L):
        c = accode[i]
        if c == 2:                       # acute -> the preceding char is the long first element
            accode[i - 1] = 0x10
        if c == 0xff:                    # -1 (no accent)
            r = 1
        elif c == 0:                     # short (type 0)
            r = 2
        else:                            # type 1 -> 4 (circumflex), type 2 -> 8 (acute)
            r = 4 if c == 1 else 8
        accode[i] = r
    return accode


def _diphthong_attr(attr, L):
    """Orchestrator 0x1000acbe backward pass: bump the char AFTER a nucleus (diphthong 2nd element)."""
    for i in range(L - 2, -1, -1):
        if attr[i] & 2:
            attr[i + 1] += 4 if (attr[i + 1] & 2) else 3
    return attr


def _build_B1(buf, L):
    """Renderer PART1 (0x1000aef6): palatalisation context. B1[pos]=1 (soft) if the consonant run
    leads into a front vowel or J, else 2 (hard); carried back across vowels."""
    B1 = [0] * L
    cur, state = 2, 1
    for pos in range(L - 1, 0, -1):
        c = buf[pos]
        if c in _VOWELS and c != _NUL:
            state = 1
        elif state == 1:
            nxt = buf[pos + 1]
            if (nxt in _FRONT and nxt != _NUL) or c == _J:
                cur = 1
            else:
                cur = 2
            state = 0
        B1[pos] = cur
    return B1


def _build_B2(buf, L):
    """Renderer PART2 (0x1000afee): regressive voicing context. B2[pos]=1 if the obstruent cluster
    ends voiced, else 2; reset by any non-obstruent."""
    B2 = [0] * L
    val, state = 2, 0
    for pos in range(L - 1, 0, -1):
        c = buf[pos]
        if c in _OBSTR:
            if state == 1 and c != _NUL:
                val = 1 if c in _VOICED else 2
                state = 0
        else:
            state = 1
        B2[pos] = val
    return B2


def _transduce(buf, accode, attr, accbyte, L, outsize=16384):
    """Renderer PART4 (0x1000b0d1): the rule-table FST. Returns the phoneme string (\\n-separated)."""
    rules = _rules()
    out = bytearray(_INIT.encode("cp1257"))
    pos, ri = 1, 0
    SEP = _SEP.encode("cp1257"); TRAIL = _TRAIL.encode("cp1257"); FINAL = _FINAL.encode("cp1257")
    while pos < L:
        r = rules[ri]
        matched = False
        if r["mc"] != buf[pos]:
            ri += r["skip"]
        else:
            skip = 2 if buf[pos + 1] == _NUL else 1
            ok = True
            if r["lc"] and r["lc"][0] != 0:
                if buf[pos - 1] not in r["lc"]:
                    ok = False
            if ok and r["rs1"] and r["rs1"][0] != 0 and buf[pos + skip] not in r["rs1"]:
                ok = False
            if ok and r["rs2"] and r["rs2"][0] != 0 and buf[pos + skip + 1] not in r["rs2"]:
                ok = False
            if ok and (r["accmask"] & accode[pos]) == 0:
                ok = False
            if ok and ((r["fl"] & 3) & B1[pos]) == 0:
                ok = False
            if ok and (r["b2"] & B2[pos]) == 0:
                ok = False
            if ok:                                    # attr masked-equality (0x1000b30c)
                m = (r["attr"] & attr[pos]) & 7
                if m != (attr[pos] & 7) and m != r["attr"]:
                    ok = False
            if ok and (r["fl"] & 4) and accbyte[pos] == 0:
                ok = False
            if ok:
                pos += r["adv"]
                if len(out) < outsize - 8:
                    out += r["out"]
                    if r["out"] and r["out"][0] != 0:
                        out += SEP
                else:
                    return None                        # overflow (asm returns -1)
                ri = 0
                matched = True
            else:
                ri += 1
        if not matched and ri > 0x9b:
            ri = 0
            pos += 1
        if pos < len(buf) and buf[pos] == _NUL:
            if pos > 0 and (attr[pos - 1] & 8):
                out += TRAIL
                pos += 1
    out += FINAL
    return out.decode("cp1257", "replace")


# B1/B2 are module-level so _transduce can read them (they are per-call; set in render()).
B1 = B2 = None

def render(word_upper, stress):
    """Transcribe one PradApdZod'd uppercase cp1257 word to the engine's \\n-separated phoneme
    string, applying `stress` = (pos1, type) from lt_accent.accent() (or None for unstressed).
    Returns the token list (whitespace-split)."""
    global B1, B2
    from . import nucleus
    W = word_upper.upper()
    # Renderer buffer is '_'+W (NO trailing space): the B2 voicing pass must reach the word-final
    # obstruent with state=0 so it devoices (g->k, d->t, ž->š ...). The nucleus marks of the word
    # body are identical with/without the trailing space (the space only adds a trailing slot).
    core = ("_" + W).encode("cp1257", "replace")
    L = len(core)
    attr = nucleus._mark(list(core), [1] * L)          # sub_10009310 on '_'+W
    PAD = 4
    buf = list(core) + [0] * PAD                        # buf[L..]=0 for B1/B2
    attr = list(attr) + [0] * PAD
    accode = [0xff] * (L + PAD)
    if stress is not None:
        pos1, typ = stress
        if 1 <= pos1 < L:
            accode[pos1] = typ
    _accode_transform(accode, L)
    _diphthong_attr(attr, L)
    B1 = _build_B1(buf, L)                              # PART1/PART2 see buf[L]=0 (terminator)
    B2 = _build_B2(buf, L)
    buf[0] = 0x5f                                       # PART3 pad (0x1000b0ae): '_' ... '_' '_' \0
    buf[L] = 0x5f; buf[L + 1] = 0x5f; buf[L + 2] = 0
    accbyte = [0] * (L + PAD)
    s = _transduce(buf, accode, attr, accbyte, L)
    return s.split() if s is not None else None


if __name__ == "__main__":
    import sys, lt_accent
    words = sys.argv[1:] or ["namas", "vakaras", "baltas", "medis", "auksas", "laukas", "ranka"]
    for w in words:
        st = lt_accent.accent(w)
        print("%-12s stress=%-10s -> %s" % (w, str(st), " ".join(render(w, st) or ["<none>"])))
