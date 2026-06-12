# -*- coding: utf-8 -*-
# Port of transcr4.dll's PradApdZod number-to-words expansion (sub_10009E4C). The engine uses its OWN numeral
# words (NOT ruleslt.rul -- e.g. 'keturiasdešimt'/'tūkstančiai', not the file's 'keturesdešimt'/'tūkstančei'),
# so the atoms are EXTRACTED from PradApdZod (lt_number_data.py, built by _build_number_table.py) and combined
# with the positional algorithm below. Verified byte-exact vs PradApdZod (_probe_number). DLL-free at runtime.
#
# A number is split into 3-digit GROUPS (units / thousands / millions / billions, base power b=0/3/6/9). Within
# a group (hundreds h, tens t, units u): emit hundreds, then either the teen (t==1 -> 10..19) or tens+units. For
# a higher group (b>0) the scale word rides on the units position: u=1 alone -> bare 'tūkstantis' (vienas
# dropped: 1000=tūkstantis), u=1 after tens/hundreds -> 'vienas tūkstantis' (21000=...vienas tūkstantis),
# u=2..9 -> 'du tūkstančiai'..., u=0 but group non-zero -> genitive 'tūkstančių', teen -> 'vienuolika tūkstančių'.
from . import number_data as _D

ONES = _D.ONES; TEENS = _D.TEENS; TENS = _D.TENS; HUNDREDS = _D.HUNDREDS; SCALES = _D.SCALES


def _group_words(h, t, u, b):
    out = []
    if h == 0 and t == 0 and u == 0:
        return out                                       # all-zero group -> nothing (the N000 case)
    if h > 0:
        out.append(HUNDREDS[h])
    sc = SCALES.get(b)
    if t == 1:                                           # teen 10..19
        out.append(TEENS[u] + ((" " + sc["gen"]) if sc else ""))
    else:
        if t >= 2:
            out.append(TENS[t])
        if u > 0:
            if not sc:                                   # units group (b==0): plain count word
                out.append(ONES[u])
            elif u == 1:
                out.append(sc["one_bare"] if (t == 0 and h == 0) else sc["one_full"])
            else:
                out.append(sc["few"][u])
        elif sc and (h > 0 or t >= 2):                   # units 0 but group non-zero -> genitive scale word
            out.append(sc["gen"])
    return out


def to_words(n):
    """Integer / digit string -> the engine's exact Lithuanian numeral word string (lowercase, space-joined).
    LEADING ZEROS are spoken, one 'nulis' each, exactly like PradApdZod (transcr_cli-verified:
    '01'->nulis vienas, '007'->nulis nulis septyni, '0023'->nulis nulis dvidešimt trys, '00'->nulis nulis);
    zeros INSIDE the number are positional as before ('100'->šimtas, '10'->dešimt)."""
    s = str(n).strip().lstrip("+")
    if not s.isdigit():
        return None
    nz = s.lstrip("0")
    pre = [ONES[0]] * (len(s) - len(nz))                 # one 'nulis' per leading zero
    if not nz:                                           # all zeros: '0'->nulis, '00'->nulis nulis, ...
        return " ".join(pre)
    digits = [int(c) for c in nz]
    ng = (len(digits) + 2) // 3
    digits = [0] * (ng * 3 - len(digits)) + digits       # left-pad to whole 3-digit groups
    words = pre
    for gi in range(ng):
        h, t, u = digits[gi * 3:gi * 3 + 3]
        b = 3 * (ng - 1 - gi)
        words += _group_words(h, t, u, b)
    return " ".join(words)


import re as _re


def expand_text(text):
    """Replace every run of digits in `text` with its Lithuanian numeral words (PradApdZod-exact), like the
    engine's PradApdZod preprocessor. Non-digit text is untouched. E.g. 'Turiu 21 obuolį' -> 'Turiu dvidešimt
    vienas obuolį'."""
    return _re.sub(r"\d+", lambda m: to_words(m.group()) or m.group(), text)
