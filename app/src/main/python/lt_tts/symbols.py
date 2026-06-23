# -*- coding: utf-8 -*-
"""Optional symbol reading: emoji, punctuation, Russian Cyrillic letters, and Latvian-unique letters.

Before the normal pipeline runs, the engine can substitute symbols with spoken Lithuanian text. Four editable
data files drive it, each individually switchable; a missing/disabled file leaves its symbols untouched, so the
core engine never depends on them.

    data/emoji.tsv     <emoji char(s)><TAB><spoken text>          (read_emoji)
    data/punct.tsv     <punctuation char><TAB><spoken text>       (read_punctuation)
    data/cyrillic.tsv  two blocks [names] / [sounds], per letter   (read_cyrillic)
    data/latvian.tsv   two blocks [names] / [sounds], per letter   (read_latvian; Latvian-UNIQUE letters only)

emoji.tsv and punct.tsv share the same flat `<char><TAB><text>` format and the same matcher; they are split
into two files only so the host can toggle them independently (an emoji is content, punctuation is what a
screen reader usually names itself). LETTER reading (cyrillic/latvian) has two modes, exactly like a Lithuanian
letter (ą = "a nosinė" when typed, a long /a:/ in a word):
  * a letter TYPED ON ITS OWN (a single isolated letter) -> its NAME      (б -> "bė",  я -> "ja")
  * a letter INSIDE a word                              -> its SOUND      (б -> "b",   я -> "ja")
The [names] / [sounds] blocks supply the two readings; CAPITALS are defined too (separate keys).

Normal Lithuanian text is never altered: emoji/Cyrillic trigger only on their own codepoints, and latvian.tsv
contains only letters that do not occur in Lithuanian. `expand` is a no-op when nothing matches.
"""
import os, re, unicodedata
from . import paths

READ_EMOJI = True
READ_CYRILLIC = True
READ_LATVIAN = True
READ_PUNCTUATION = False        # OFF by default: a screen reader (NVDA/TalkBack) expands punctuation into
                                # words ITSELF according to the user's punctuation-verbosity setting, so the
                                # engine must stay silent on punctuation (like every mainstream TTS) -- naming
                                # it here made quotes etc. ALWAYS spoken regardless of the SR setting. True
                                # names the punct.tsv marks (quotes/dashes/brackets) for hosts without their own
                                # punctuation processing.

# clause delimiters that drive pauses / the question contour -- never stripped
_DELIMS = set(u".,;:!?—")
# text-normalization symbols mainstream TTS engines DO read as part of plain text (50% -> "proc",
# §3, A&B, #1, 20°) -- kept spoken even with punctuation off (they live in emoji.tsv, not punct.tsv)
_PUNCT_KEEP = set(u"%‰§¶&#°")

# Decimal separator naming (espeak-style): a comma/period sitting DIRECTLY between two digit groups
# is a decimal mark and is SPOKEN, e.g. '2,5' -> '2 kablelis 5' -> "du kablelis penki". The fractional
# part is read as a whole number ('2,75' -> "du kablelis septyniasdešimt penki"). A run with two or more
# separators is a date / time / thousands group (2026.06.12, 21:20, 1,234,567), NOT a decimal -- it is
# left untouched so the normal punctuation step turns those inter-digit separators into word gaps.
_DECIMAL_RUN = re.compile(r"\d+(?:[.,:]\d+)+")
_DECIMAL_WORD = {u",": u"kablelis", u".": u"taškas"}

# Always-read symbols (espeak-style text normalization), independent of the punctuation-verbosity setting:
#   * a '-' directly before a digit, at the start of the text or after a space, is a MINUS sign:
#     '-15' -> 'minus 15', 'temperatūra -15 laipsnių' -> '... minus 15 ...'.
#   * '.', '*', '@' GLUED between two LETTERS (no spaces) are read by name so identifiers stay legible:
#     'lrt.lt' -> 'lrt taškas lt', 'a*b' -> 'a žvaigždutė b', 'vardas@host' -> 'vardas eta host'.
# A '-' BETWEEN letters is NOT read (Lithuanian hyphenated words like 'kažin-kas' read naturally); a '-'
# preceded by a digit ('2026-06-12') is not a minus either. Letters = Unicode letters (incl. ą č ę ...),
# so a separator between DIGITS never triggers the inter-letter rule (decimals/dates are handled above).
_MINUS_RE = re.compile(r"(?<![^\W_])-(?=\d)")        # '-' before a digit, NOT preceded by a letter/digit
_INLETTER_RE = re.compile(r"(?<=[^\W\d_])([.*@])(?=[^\W\d_])")
_INLETTER_WORD = {u".": u"taškas", u"*": u"žvaigždutė", u"@": u"eta"}

_MAPS = {}                      # filename -> (dict, compiled-regex|None); cached per file
_LETTERS = {}                   # filename -> {'names': {ch:txt}, 'sounds': {ch:txt}}  (None inside = absent)


# ---- loaders -------------------------------------------------------------------------------------------------
def _load_map(filename):
    """Load a flat `<char(s)><TAB><spoken text>` table (emoji.tsv / punct.tsv) into a {key: text} dict and a
    combined regex (longest key first, so a multi-codepoint ZWJ emoji wins over its parts). Cached; a missing
    file yields an empty map and a None regex (the feature becomes a no-op)."""
    if filename not in _MAPS:
        table = {}
        path = paths.data_path(filename)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\r\n")
                    if line and not line.startswith("#") and "\t" in line:
                        k, v = line.split("\t", 1)
                        if k:
                            table[k] = v.strip()
        rx = (re.compile("|".join(re.escape(k) for k in sorted(table, key=len, reverse=True)))
              if table else None)
        _MAPS[filename] = (table, rx)
    return _MAPS[filename]


def _load_letters(filename):
    """Parse a two-block [names]/[sounds] letter file -> {'names':{...},'sounds':{...}}; {} if absent."""
    if filename in _LETTERS:
        return _LETTERS[filename]
    path = paths.data_path(filename)
    blocks = {"names": {}, "sounds": {}}
    if os.path.exists(path):
        cur = None
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line or line.startswith("#"):
                    continue
                if line == "[names]":
                    cur = "names"; continue
                if line == "[sounds]":
                    cur = "sounds"; continue
                if cur and "\t" in line:
                    k, v = line.split("\t", 1)
                    if k:
                        blocks[cur][k] = v.strip()
    else:
        blocks = {}
    _LETTERS[filename] = blocks
    return blocks


# ---- substitution helpers ------------------------------------------------------------------------------------
def _sub_map(text, filename):
    """Replace every key of `filename`'s table found in `text` with its spoken text (space-padded so it reads as
    a separate token). Returns (new_text, changed). A no-op when the table is empty or nothing matches."""
    table, rx = _load_map(filename)
    if rx is None or not rx.search(text):
        return text, False
    return rx.sub(lambda m: " " + table[m.group(0)] + " ", text), True


def _strip_punct(text):
    """Replace every Unicode punctuation char (category P*) and spacing accent (Sk: ` ´ ^ ¨) with a space --
    the 'skip punctuation' reading every other TTS does. Clause delimiters survive (they only time pauses,
    they are never spoken) and the _PUNCT_KEEP normalization symbols survive. This also keeps stray ASCII
    quotes/brackets/hyphens from ever reaching the word pipeline, where they used to garble the token.
    A delimiter BETWEEN TWO DIGITS (21:20, 8.5, 2026.06.12) becomes a word gap, not a clause pause: the
    engine NAMES it there (dvitaškis/taškas/kablelis), so with naming off only the word boundary remains --
    a 0.45s clause pause inside a clock time read as a long break."""
    out = []
    n = len(text)
    for i, ch in enumerate(text):
        if ch in _DELIMS:
            digit_ctx = (i > 0 and text[i - 1].isdigit()
                         and i + 1 < n and text[i + 1].isdigit())
            out.append(" " if digit_ctx else ch)
            continue
        if ch in _PUNCT_KEEP:
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        out.append(" " if (cat[0] == "P" or cat == "Sk") else ch)
    return "".join(out)


def _read_decimals(text):
    """Name a LONE decimal separator that sits directly between two digit groups, espeak-style:
    '2,5' -> '2 kablelis 5', '2.5' -> '2 taškas 5'. Runs through this BEFORE the punctuation step, so the
    decimal is spoken even with punctuation reading off (it is number formatting, not prose punctuation).
    Only a SINGLE separator counts: a chain of two or more (2026.06.12, 21:20, 1,234,567) is a date / time /
    thousands group and is left for the normal inter-digit-gap handling. A trailing sentence period ('2,5.')
    is not part of the digit run, so it stays a clause delimiter. '2, 3' (separator + space) never matches."""
    def repl(m):
        run = m.group(0)
        seps = re.findall(r"[.,:]", run)
        if len(seps) == 1 and seps[0] in _DECIMAL_WORD:
            i = run.index(seps[0])
            return run[:i] + u" " + _DECIMAL_WORD[seps[0]] + u" " + run[i + 1:]
        return run
    return _DECIMAL_RUN.sub(repl, text)


def _read_symbols(text):
    """Speak a leading-minus before a digit ('-15' -> 'minus 15') and a '.'/'*'/'@' glued between two letters
    ('lrt.lt' -> 'lrt taškas lt'). Runs BEFORE the punctuation step, so these are spoken even with punctuation
    reading off. See _MINUS_RE / _INLETTER_RE for the exact (espeak-style) contexts."""
    text = _MINUS_RE.sub(u"minus ", text)
    text = _INLETTER_RE.sub(lambda m: u" " + _INLETTER_WORD[m.group(1)] + u" ", text)
    return text


# ---- letter substitution (the isolated=NAME / in-word=SOUND rule) --------------------------------------------
def _sub_letters(text, blocks, charclass_re):
    """Replace every letter in `blocks` found in `text`: an ISOLATED single letter -> its NAME (space-padded so
    it is read as a separate spoken token); a letter adjacent to other letters -> its SOUND (joined in place, so
    the word reads). Returns (new_text, changed)."""
    names, sounds = blocks["names"], blocks["sounds"]
    changed = False
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if charclass_re.match(ch):
            prev_alpha = i > 0 and text[i - 1].isalpha()
            next_alpha = i + 1 < n and text[i + 1].isalpha()
            isolated = not prev_alpha and not next_alpha
            if isolated:
                out.append(" " + names.get(ch, names.get(ch.lower(), "")) + " ")
            else:                                # inside a word -> the sound (case-insensitive)
                out.append(sounds.get(ch, sounds.get(ch.lower(), "")))
            changed = True
        else:
            out.append(ch)
        i += 1
    return "".join(out), changed


_CYR_RE = re.compile(r"[Ѐ-ӿ]")              # Cyrillic block
_LAV_RE = re.compile(r"[āĀēĒīĪōŌŗŖļĻņŅķĶģĢ]")   # the Latvian-unique letters in latvian.tsv


def expand(text, read_emoji=None, read_cyrillic=None, read_latvian=None, read_punctuation=None):
    """Substitute emoji / punctuation / Cyrillic / Latvian symbols with spoken Lithuanian text. Disabled
    features and missing files are skipped. A NO-OP (returns the input unchanged) when nothing matches, so
    normal Lithuanian text is never altered.

    Punctuation runs FIRST and on the ORIGINAL text: read_punctuation=True names the punct.tsv marks; the
    default (False) strips punctuation BEFORE the emoji pass, so stray quotes/brackets never reach the word
    pipeline and a screen reader's own punctuation setting decides whether the user hears them. Naming punct
    before emoji also keeps the quote chars INSIDE an emoji's name (🅰 -> 'mygtukas „A“') literal, never
    re-named -- matching the old single-pass behavior."""
    re_e = READ_EMOJI if read_emoji is None else read_emoji
    re_c = READ_CYRILLIC if read_cyrillic is None else read_cyrillic
    re_l = READ_LATVIAN if read_latvian is None else read_latvian
    re_p = READ_PUNCTUATION if read_punctuation is None else read_punctuation
    changed = False

    # Decimal numbers first: a comma/period directly between two digit groups is a decimal mark and must be
    # SPOKEN ('2,5' -> 'du kablelis penki'), like espeak -- regardless of the punctuation-verbosity setting.
    t2 = _read_decimals(text)
    if t2 != text:
        text = t2
        changed = True

    # Minus sign before a digit, and '.'/'*'/'@' glued between letters -> spoken (espeak-style), also
    # independent of the punctuation setting (number/identifier formatting, not prose punctuation).
    t2 = _read_symbols(text)
    if t2 != text:
        text = t2
        changed = True

    if re_p:
        text, c = _sub_map(text, "punct.tsv")     # name quotes/dashes/brackets/...
        changed = changed or c
    else:
        t2 = _strip_punct(text)                   # skip punctuation (the screen reader names it itself)
        if t2 != text:
            text = t2
            changed = True

    if re_e:
        text, c = _sub_map(text, "emoji.tsv")
        changed = changed or c

    if re_c and _CYR_RE.search(text):
        blocks = _load_letters("cyrillic.tsv")
        if blocks:
            text, c = _sub_letters(text, blocks, _CYR_RE)
            changed = changed or c

    if re_l and _LAV_RE.search(text):
        blocks = _load_letters("latvian.tsv")
        if blocks:
            text, c = _sub_letters(text, blocks, _LAV_RE)
            changed = changed or c

    return re.sub(r"\s+", " ", text).strip() if changed else text
