# -*- coding: utf-8 -*-
# Spell-out for VOWELLESS words (the engine's spelllt.dct behavior): a "word" containing NO vowel letter
# (a e i o u y ą ę ė į ų ū) is read LETTER-BY-LETTER using each letter's Lithuanian NAME (lt -> "el tė",
# cd -> "cė dė", km -> "ka em", www -> "dviguba vė ..."). A word with at least one vowel is read normally.
# The letter names come straight from the engine's spelllt.dct; the names are then transcribed by the normal
# pipeline (exactly as the engine does -- the spell dict substitutes the names as TEXT, then KircTranskr runs).
import os, re
from . import paths

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
# cp1257 vowel letters (lowercase) -- ą=0xe0 ę=0xea ė=0xeb į=0xe1 ų=0xf8 ū=0xfb ... use the unicode chars
_VOWELS = set("aeiouy") | set("ąęėįųū") | set("AEIOUY") | set("ĄĘĖĮŲŪ")
_SPELL = None


def _load():
    global _SPELL
    if _SPELL is not None:
        return _SPELL
    _SPELL = {}
    path = paths.data_path("spelllt.dct")
    try:
        for line in open(path, "rb").read().decode("cp1257", "replace").splitlines():
            line = line.rstrip("\r\n")
            if not line or " " not in line:
                continue
            letter, name = line.split(" ", 1)
            if len(letter) == 1:
                _SPELL[letter.lower()] = name.strip()
    except Exception:
        _SPELL = {}
    return _SPELL


def _has_vowel(word):
    return any(c in _VOWELS for c in word)


def is_spellable(word):
    """True if `word` is an ALPHABETIC token with NO vowel letter (-> spell it letter-by-letter)."""
    if not word or _has_vowel(word):
        return False
    return any(c.isalpha() for c in word)


def spell_word(word):
    """Return the spelled-out NAME text for a vowelless word (e.g. 'lt' -> 'el tė'), or the word unchanged if
    it has a vowel. Letters with no spell entry are dropped (matches the engine ignoring punctuation)."""
    if not is_spellable(word):
        return word
    sp = _load()
    names = [sp[c.lower()] for c in word if c.lower() in sp]
    return " ".join(names) if names else word


_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)      # runs of letters (no digits/punct)


def expand_text(text):
    """Replace every VOWELLESS alphabetic token in `text` with its spelled-out letter names (engine spelllt.dct
    behavior). Tokens with a vowel, digits, and punctuation are untouched. Apply BEFORE number expansion is
    fine (they touch disjoint tokens)."""
    return _WORD_RE.sub(lambda m: spell_word(m.group()), text)


if __name__ == "__main__":
    import sys
    for w in sys.argv[1:] or ["lt", "cd", "km", "st", "www", "html", "pdf", "cda", "labas"]:
        print("%-8s -> %s" % (w, expand_text(w)))
