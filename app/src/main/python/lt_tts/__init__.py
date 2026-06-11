# -*- coding: utf-8 -*-
"""lt_tts — a complete, DLL-free Python text-to-speech engine for the Lithuanian Gintaras voice.

This package is a faithful, bit-for-bit re-implementation of the legacy RosaSOFT WinTalker / SAPI4 "Gintaras"
engine (originally the closed 32-bit Windows DLLs transcr4.dll + hlas.dll), reverse-engineered and ported to
pure Python.  Nothing here calls a DLL at run time; the only data is the Gintaras voice file
(``data/gintaras.dta``) plus the extracted lexicons and rule tables.

Public API
----------
    from lt_tts import Gintaras
    tts = Gintaras()
    wav_bytes = tts.synth("Labas rytas. Kaip jums sekasi?")     # 16-bit mono PCM @ 22050 Hz, WAV bytes
    tts.save("out.wav", "Turiu 2024 metus.")
    pcm = tts.synth_pcm("ačiū", rate=50, pitch=60)               # list[int] samples

Layers (each in its own module, so a maintainer can improve one without touching the rest):
    text       : input normalization   -> numbers.py (digits->words), spell.py (vowelless/letter spell-out)
    frontend   : text -> phonemes+prosody
                 transcribe.py (grapheme->phoneme + lexicon), accent.py (lexical stress), nucleus.py,
                 render.py (g2p rule table), duration.py (per-phone length), tonai.py (pitch breakpoints)
    voice      : voice.py            (decode data/gintaras.dta into the shared pitch-period grain pool)
    synth      : selection.py        (choose the demisyllable units = the native backbone)
                 planbuilder.py      (assemble the per-frame control Plan: units + a5 + s90/s94 + arm)
                 backend.py          (gen_synth: the bit-exact TD-PSOLA synthesis from the Plan)
                 dsp.py              (the low-level e6b0/ebc0/edc0 DSP primitives)
    orchestration:
                 speak.py            (clause splitting, multi-word prosody, question rise, capital-letter pitch)

See README.md for the architecture and how to add dictionaries / corrections safely.
"""
from .engine import Gintaras, SAMPLE_RATE

__all__ = ["Gintaras", "SAMPLE_RATE"]
__version__ = "1.0.0"
