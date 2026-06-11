# -*- coding: utf-8 -*-
"""Optional symbol reading: emoji, Russian Cyrillic letters, and Latvian-unique letters.

Before the normal pipeline runs, the engine can substitute symbols with spoken Lithuanian text. Three editable
data files drive it, each individually switchable; a missing/disabled file leaves its symbols untouched, so the
core engine never depends on them.

    data/emoji.tsv     <emoji char(s)><TAB><spoken text>          (read_emoji)
    data/cyrillic.tsv  two blocks [names] / [sounds], per letter   (read_cyrillic)
    data/latvian.tsv   two blocks [names] / [sounds], per letter   (read_latvian; Latvian-UNIQUE letters only)

LETTER reading has two modes, exactly like a Lithuanian letter (ą = "a nosinė" when typed, a long /a:/ in a
word):
  * a letter TYPED ON ITS OWN (a single isolated letter) -> its NAME      (б -> "bė",  я -> "ja")
  * a letter INSIDE a word                              -> its SOUND      (б -> "b",   я -> "ja")
The [names] / [sounds] blocks supply the two readings; CAPITALS are defined too (separate keys).

Normal Lithuanian text is never altered: emoji/Cyrillic trigger only on their own codepoints, and latvian.tsv
contains only letters that do not occur in Lithuanian. `expand` is a no-op when nothing matches.
"""
import os, re
from . import paths

READ_EMOJI = True
READ_CYRILLIC = True
READ_LATVIAN = True

_EMOJI = None
_EMOJI_RE = None
_LETTERS = {}                   # filename -> {'names': {ch:txt}, 'sounds': {ch:txt}}  (None inside = absent)


# ---- loaders -------------------------------------------------------------------------------------------------
def _load_emoji():
    global _EMOJI, _EMOJI_RE
    if _EMOJI is None:
        _EMOJI = {}
        path = paths.data_path("emoji.tsv")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\r\n")
                    if line and not line.startswith("#") and "\t" in line:
                        k, v = line.split("\t", 1)
                        if k:
                            _EMOJI[k] = v.strip()
        # longest key first so multi-codepoint (ZWJ) emoji win over their parts
        _EMOJI_RE = (re.compile("|".join(re.escape(k) for k in sorted(_EMOJI, key=len, reverse=True)))
                     if _EMOJI else None)
    return _EMOJI, _EMOJI_RE


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


def expand(text, read_emoji=None, read_cyrillic=None, read_latvian=None):
    """Substitute emoji / Cyrillic / Latvian symbols with spoken Lithuanian text. Disabled features and missing
    files are skipped. A NO-OP (returns the input unchanged) when nothing matches, so normal Lithuanian text is
    never altered."""
    re_e = READ_EMOJI if read_emoji is None else read_emoji
    re_c = READ_CYRILLIC if read_cyrillic is None else read_cyrillic
    re_l = READ_LATVIAN if read_latvian is None else read_latvian
    changed = False

    if re_e:
        emap, ere = _load_emoji()
        if ere is not None and ere.search(text):
            text = ere.sub(lambda m: " " + emap[m.group(0)] + " ", text)
            changed = True

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
