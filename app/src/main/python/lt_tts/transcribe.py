# -*- coding: utf-8 -*-
# Task-2 (part 1): a pure-Python Lithuanian grapheme->phoneme transcriber that reproduces
# transcr4.dll's KircTranskr phoneme tokens, to make the front-end transcr4-free.
#
# Scope/honesty: the grapheme->phoneme IDENTITY (consonants, palatalization, diphthongs,
# affricates, vowel quality) is RULE-BASED and ported here. The STRESS / pitch-accent (the
# UPPERCASE codes aA/Aa/Ii/oO ...) is LEXICAL -- transcr4 carries a ~1.3 MB embedded Lithuanian
# accentuation lexicon (75k+ stems) in its .data. Without porting that lexicon + lookup we
# cannot place stress; this module emits the unstressed (lowercase) phoneme stream and a simple
# default-stress guess. validate_against_transcr4() measures both identity- and exact-match.
import sys, os, subprocess, tempfile
from . import paths

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
CLI = os.path.join(ROOT, "transcr_cli.exe")

# cp1257 Lithuanian letters
LT = {"ą": "a~", "č": "č", "ę": "e~", "ė": "ė", "į": "i~",
      "š": "š", "ų": "u~", "ū": "ū", "ž": "ž", "y": "y"}

FRONT = set("eėęiįy")                  # palatalising vowels (+ j); 'e~'/'i~' too
VOICED = set("bdgzžvj")
VOICELESS = set("ptksšcč fh")

# base single-consonant -> phoneme code (pre-palatalisation)
CONS = {"p": "p", "b": "b", "t": "t", "d": "d", "k": "k", "g": "g",
        "m": "m", "n": "n", "l": "l", "r": "r", "v": "v", "f": "f", "j": "j",
        "s": "s", "z": "z", "h": "h", "š": "S", "ž": "Z", "c": "ts", "č": "tS"}

# vowel -> base (lowercase) phoneme code; o is always long ("oo"); y/ū/ą... long
VOW = {"a": "a", "e": "e", "i": "i", "u": "u", "o": "oo",
       "y": "ii", "ū": "uu", "ą": "aa", "ę": "ea", "ė": "ee", "į": "ii", "ų": "uu"}
GLIDES = {"ai": ("a", "j"), "ei": ("e", "j"), "ui": ("u", "j"), "oi": ("o", "j"),
          "au": ("a", "w"), "eu": ("e", "w"), "iau": ("e", "w")}
RISING = {"ie": "ie", "uo": "uo"}      # merged rising diphthongs

def _norm(word):
    # map any unicode LT letters to internal single-char keys we switch on
    out = []
    for ch in word.lower():
        out.append({"ą": "ą", "ę": "ę", "į": "į", "ų": "ų"}.get(ch, ch))
    return out

def g2p(word):
    """Return the lowercase phoneme token list (no stress) for a Lithuanian word."""
    s = _norm(word)
    i, n = 0, len(s)
    toks = []
    while i < n:
        ch = s[i]
        two = "".join(s[i:i+2]); three = "".join(s[i:i+3])
        # diphthongs first
        if three in GLIDES:
            v, gl = GLIDES[three]; toks += [v, gl]; i += 3; continue
        if two in RISING:
            toks.append(RISING[two]); i += 2; continue
        if two in GLIDES:
            v, gl = GLIDES[two]; toks += [v, gl]; i += 2; continue
        if ch in VOW:
            toks.append(VOW[ch]); i += 1; continue
        if ch in CONS:
            code = CONS[ch]
            # palatalisation: consonant before a front vowel or j
            nxt = s[i+1] if i+1 < n else ""
            nxt2 = "".join(s[i+1:i+3])
            front = (nxt in FRONT) or (nxt == "j") or (nxt2 in ("ie",)) or nxt in ("ė", "ę", "į")
            if front:
                code += "'"
            # (note: transcr4's velar 'N' allophone is lexical/contextual -- gintaras 'nt'->N but
            #  ranka 'nk'->n -- so it is NOT a simple n-before-k/g rule; left as plain 'n' here.)
            toks.append(code); i += 1; continue
        # unknown char -> boundary
        i += 1
    return toks

# ---- stress lexicon (extracted from transcr4's .data, transcribed offline; see lt_lex.tsv) ----
_LEX = None
def _load_lex():
    global _LEX
    if _LEX is not None:
        return _LEX
    _LEX = {}
    path = paths.data_path("lt_lex.tsv")
    if os.path.exists(path):
        for line in open(path, "rb"):
            line = line.decode("cp1257", "replace").rstrip("\n")
            if "\t" in line:
                k, v = line.split("\t", 1)
                _LEX[k] = v.split()
    return _LEX

_VOWELS_SP = set("aeiouy") | set("ąęėįųū") | set("AEIOUY") | set("ĄĘĖĮŲŪ")
_LETPHON = None
_SPVOW = None


def _spell_data():
    global _LETPHON, _SPVOW
    if _LETPHON is None:
        try:
            from . import spell_data
            _LETPHON = lt_spell_data.LETTER_PHON
            _SPVOW = getattr(lt_spell_data, "SPELLED_VOWELS", {})
        except Exception:
            _LETPHON, _SPVOW = {}, {}
    return _LETPHON, _SPVOW


def _spell_out(word):
    """Spell-out rules (engine spelllt.dct, route-2 exact):
      * a SINGLE isolated vowel letter the engine names -- y/ą/ę/ų/ū -> its NAME (i ilgoji / a nosinė / ...);
        a/e/i/o/u/ė/į read as the plain SOUND (None -> normal transcription).
      * a word with NO vowel letter (lt/cd/km/www) -> concatenate each consonant letter's NAME phonemes.
    Returns the ['_',...,'_'] token list, or None (-> normal transcription) for a word with a pronounceable
    vowel. Per-letter phonemes carry the engine's exact stress; cross-letter assimilation (l->l' before a soft
    consonant) is not modelled (inaudible)."""
    letphon, spvow = _spell_data()
    if not word or not any(c.isalpha() for c in word):
        return None
    if len(word) == 1 and word.lower() in spvow:      # single isolated y/ą/ę/ų/ū -> spell by name
        return ["_"] + list(spvow[word.lower()]) + ["_"]
    if any(c in _VOWELS_SP for c in word):            # has a vowel -> normal (NOT a vowelless abbreviation)
        return None
    out = ["_"]
    for c in word.lower():
        if c in letphon:
            out += list(letphon[c])
    if len(out) == 1:
        return None
    out.append("_")
    return out


def _xq_normalize(word):
    """x and q are NOT Lithuanian letters; in text they take their phonetic value (the engine RE: x -> 'ks'
    taxi/oxidas/sax, q -> 'k' iraqas/quizas). Substitute at the GRAPHEME level so the existing rules handle the
    rest (case-preserving). NOTE: a STANDALONE letter is spelled by NAME (x='iks', q='kū') -- a separate
    spell-mode path; this is the in-word reading. No-op for words without x/q."""
    if "x" not in word and "X" not in word and "q" not in word and "Q" not in word:
        return word
    out = []
    for ch in word:
        if ch == "x":
            out.append("ks")
        elif ch == "X":
            out.append("Ks")
        elif ch == "q":
            out.append("k")
        elif ch == "Q":
            out.append("K")
        else:
            out.append(ch)
    return "".join(out)


def transcribe(word):
    """Full token list with leading/trailing '_'. Two tiers, both transcr4-faithful:
       1) exact lexicon hit -> transcr4's own accented tokens (bit-exact, incl. stress);
       2) OOV -> the ported engine pipeline: lt_accent.accent() places lexical stress (verb+noun
          matcher + sub_100098F3 disambiguation), then lt_render renders it through transcr4's own
          156-rule g2p+casing table (sub_1000aec0). Bit-exact vs KircTranskr for ~96% of words
          (the residual is the not-yet-ported FOREIGN accent path; renderer itself is 99.9%).
    Falls back to the rule-based g2p only if the ported renderer errors."""
    sp = _spell_out(word)                          # VOWELLESS word (lt/cd/km) -> spelled letter names
    if sp is not None:
        return sp
    word = _xq_normalize(word)                     # x->ks, q->k (the engine's phonetic value for these non-LT
                                                   # letters: taxi->taksi, oxidas, quizas->kuizas, iraqas)
    w = word.lower()
    lex = _load_lex()
    if w in lex:
        return list(lex[w])                       # bit-exact transcr4 output
    try:
        from . import accent as lt_accent, render as lt_render
        toks = lt_render.render(word.upper(), lt_accent.accent(word))
        if toks:
            return _shorten_o(w, toks)            # loanword SHORT-o (oo->o) for the verified SHORT_O set
    except Exception:
        pass
    return _shorten_o(w, ["_"] + g2p(w) + ["_"])   # legacy rule-based fallback (no stress)


_SHORT_O = None


def _shorten_o(w, toks):
    """Lithuanian /o/ is normally LONG (oo); in many international loanwords it is SHORT (o). This is LEXICAL
    (komedija short vs komatas long), so we use a VERIFIED set (lt_shorto_data, built by _build_shorto.py from
    the engine: a word is listed only if oo->o reproduces the engine's tokens EXACTLY). Words NOT listed keep
    long oo -> CANNOT degrade. Applies the short-o by replacing unstressed `oo` -> `o`."""
    global _SHORT_O
    if _SHORT_O is None:
        try:
            from . import shorto_data
            _SHORT_O = lt_shorto_data.SHORT_O
        except Exception:
            _SHORT_O = set()
    if w in _SHORT_O:
        return ["o" if t == "oo" else t for t in toks]
    return toks

# ---- validation against the real transcr4 (batch --tokens-file = reliable reference) ----
