# -*- coding: utf-8 -*-
# Multi-word / number synthesizer using the BIT-EXACT gen_synth back-end (DLL-free). Renders each CLAUSE (text
# between punctuation) as ONE continuous build_plan_phase2 stream -- the engine's per-epoch se8 contour carries
# CONTINUOUSLY across the whole clause (one declination, the phrase-final fall at the end), NOT a fall per word
# (which sounded like separate utterances). Words inside a clause join flush (no inter-word silence, as the
# engine does). Punctuation -> a pause. Digits -> lt_number words. say.py's flat-per-phone TD-PSOLA is retired.
import os, sys, struct, wave, re
from . import planbuilder as PF
from . import backend as GS
from . import numerals as LN
from . import symbols as SY

SR = 22050
LEAD, TAIL = 0.04, 0.22
PAUSE = {",": 0.20, ";": 0.28, ":": 0.28, ".": 0.36, "!": 0.36, "?": 0.36, "—": 0.28}


def _sil(sec):
    return [0] * int(sec * SR)


CAPITAL_PITCH = 100              # raise an UPPERCASE spelled letter to the highest pitch (typing/letter-by-
                                # letter distinction of capital vs lowercase -- common screen-reader practice).
_SPELL_GAP = 0.05               # gap between discretely-spelled letters (s)


def _is_letter_token(word):
    """True if `word` is a LETTER being typed/spelled: a SINGLE alphabetic character (a/A/x/ą...), OR a vowelless
    abbreviation (cd/lt/www). NOT a normal multi-letter word (it has a vowel). Used to detect 'spell mode' (every
    token a letter) so capitals can be pitch-raised and each letter rendered discretely."""
    if not word:
        return False
    if len(word) == 1 and word.isalpha():
        return True
    try:
        from . import transcribe
        return lt_transcribe._spell_out(word) is not None      # vowelless abbreviation (cd/lt/www)
    except Exception:
        return False


def synth_text(text, rate=None, pitch=None, capital_pitch=True,
               read_emoji=None, read_cyrillic=None, read_latvian=None):
    text = SY.expand(text, read_emoji=read_emoji, read_cyrillic=read_cyrillic,
                     read_latvian=read_latvian)         # emoji / Cyrillic / Latvian -> spoken Lithuanian
    text = LN.expand_text(text)                                  # digits -> numeral words
    out = list(_sil(LEAD))
    # split into (clause, following-delimiter) pairs so a clause ending in '?' gets the QUESTION RISE contour
    parts = re.split(r"([.,;:!?—])", text)
    for k in range(0, len(parts), 2):
        clause = parts[k].strip()
        delim = parts[k + 1] if k + 1 < len(parts) else ""
        if clause:
            toks = clause.split()
            # SPELL MODE: every token is a spelled letter/abbreviation (typing letter-by-letter). Render each
            # DISCRETELY, and raise an UPPERCASE token to CAPITAL_PITCH so capitals are audibly distinguished
            # from lowercase. Normal prose (any non-spelled token) falls through to the continuous phrase path
            # -> a sentence-initial capital (Vilnius) is NOT raised, only true letter-spelling.
            if capital_pitch and toks and all(_is_letter_token(t) for t in toks):
                for t in toks:
                    lp = CAPITAL_PITCH if t.isupper() else pitch
                    try:
                        out += list(GS.synthesize(PF.build_plan_phase2(t), rate=rate, pitch=lp))
                        out += _sil(_SPELL_GAP)
                    except Exception:
                        pass
            else:
                question = (delim == "?")                        # terminal yes/no-question rise on '?'
                try:
                    # PER-WORD se8 contour (cont.48): each word its own fall, phrase-final word base 6.
                    out += list(GS.synthesize(PF.build_plan_phrase(clause, question=question),
                                              rate=rate, pitch=pitch))
                except Exception:
                    pass
        if delim:
            out += _sil(PAUSE.get(delim, 0.12))
    out += _sil(TAIL)
    return out
