# -*- coding: utf-8 -*-
"""Public engine API for lt_tts: the `Gintaras` class wrapping the full text->WAV pipeline.

This is the single entry point a host (SAPI5 bridge, Android service, NVDA add-on, CLI) should use. All the
heavy lifting lives in the layered modules (see the package docstring); this file only exposes a clean,
stable interface and bundles the output as WAV bytes.
"""
import struct
from . import speak

SAMPLE_RATE = 22050             # the Gintaras voice is 16-bit mono PCM at 22050 Hz


def _wav_bytes(pcm, sample_rate=SAMPLE_RATE):
    """Wrap a list of int16 samples in a minimal canonical WAV container -> bytes."""
    data = struct.pack("<%dh" % len(pcm), *[max(-32768, min(32767, int(v))) for v in pcm])
    n = len(data)
    hdr = b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    hdr += b"data" + struct.pack("<I", n)
    return hdr + data


class Gintaras:
    """The Lithuanian Gintaras TTS voice.

    Parameters (host knobs, all 0..100 with 50 = neutral; None = engine-default):
        rate    : speaking rate.  50 = the engine's natural speed; >50 faster, <50 slower.
        pitch   : voice pitch.    50 = natural; >50 higher, <50 lower.  (Pitch is a fresh implementation --
                  the original DLL never wired it -- so it is not bit-exact, unlike rate.)
        capital_pitch : when reading isolated letters / abbreviations, raise UPPERCASE letters to a high pitch
                  so capitals are audibly distinguished from lowercase (common screen-reader practice).

    Any parameter may also be overridden per call to synth()/synth_pcm()/save().
    """

    def __init__(self, rate=None, pitch=None, capital_pitch=True,
                 read_emoji=True, read_cyrillic=True, read_latvian=True, read_punctuation=False):
        self.rate = rate
        self.pitch = pitch
        self.capital_pitch = capital_pitch
        self.read_emoji = read_emoji            # speak emoji via data/emoji.tsv (skipped if file absent)
        self.read_cyrillic = read_cyrillic      # speak Russian Cyrillic via data/cyrillic.tsv
        self.read_latvian = read_latvian        # speak Latvian-unique letters via data/latvian.tsv
        self.read_punctuation = read_punctuation  # name punctuation (quotes/brackets/...). DEFAULT OFF: a
                                                # screen reader names punctuation itself per ITS setting --
                                                # the engine skipping it is what makes that setting work.

    def synth_pcm(self, text, rate=None, pitch=None, capital_pitch=None,
                  read_emoji=None, read_cyrillic=None, read_latvian=None, read_punctuation=None):
        """Synthesize `text` -> a list[int] of int16 PCM samples at 22050 Hz."""
        r = self.rate if rate is None else rate
        p = self.pitch if pitch is None else pitch
        cp = self.capital_pitch if capital_pitch is None else capital_pitch
        re_ = self.read_emoji if read_emoji is None else read_emoji
        rc = self.read_cyrillic if read_cyrillic is None else read_cyrillic
        rl = self.read_latvian if read_latvian is None else read_latvian
        rp = self.read_punctuation if read_punctuation is None else read_punctuation
        return speak.synth_text(text, rate=r, pitch=p, capital_pitch=cp,
                                read_emoji=re_, read_cyrillic=rc, read_latvian=rl,
                                read_punctuation=rp)

    def synth(self, text, **kw):
        """Synthesize `text` -> WAV file bytes (16-bit mono @ 22050 Hz)."""
        return _wav_bytes(self.synth_pcm(text, **kw))

    def save(self, path, text, **kw):
        """Synthesize `text` and write it to `path` as a WAV file."""
        with open(path, "wb") as f:
            f.write(self.synth(text, **kw))
        return path
