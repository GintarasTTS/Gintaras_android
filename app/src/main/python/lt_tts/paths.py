# -*- coding: utf-8 -*-
"""Central data-file locator for the lt_tts package.

Every module loads its data (the voice, lexicons, rule tables) through `data_path(name)` so the
package is relocatable: the data lives in `lt_tts/data/` next to the code and is found regardless of
the caller's working directory. To ship the engine you only need this package directory.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "data")

# The Gintaras voice (a single SAPI4/WinTalker `wtvlt1.dta`, renamed for clarity). All synthesis pulls its
# pitch-period grains from this one file -- there is no other audio data.
VOICE_FILE = "gintaras.dta"


def data_path(name):
    """Absolute path to a data file shipped inside the package (e.g. data_path('lt_lex.tsv'))."""
    return os.path.join(DATA_DIR, name)


def voice_path():
    """Absolute path to the Gintaras voice file (gintaras.dta)."""
    return os.path.join(DATA_DIR, VOICE_FILE)
