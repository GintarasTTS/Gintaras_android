# -*- coding: utf-8 -*-
"""Android bridge: wraps lt_tts.Gintaras, returns raw PCM as bytes for Chaquopy/Kotlin.

Engine singleton is kept alive across calls so gintaras.dta and all lexicons are
loaded only once per process lifetime.
"""
import struct
from lt_tts import Gintaras

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = Gintaras()
    return _engine


def warm_up():
    """Trigger all lazy loads (gintaras.dta, lexicons) before the first real call."""
    _get_engine().synth_pcm("a")


def synthesize(text, rate=50, pitch=50):
    """Synthesize `text` -> little-endian int16 PCM bytes at 22050 Hz mono.

    rate and pitch: engine knobs 0..100 (50 = natural, bit-exact).
    rate_thr(50)==150==THR and pitch_factor(50)==1.0, so 50 is identical to None.
    Returns bytes; Chaquopy auto-converts to Java byte[].
    """
    eng = _get_engine()
    r = None if rate == 50 else int(rate)
    p = None if pitch == 50 else int(pitch)
    pcm = eng.synth_pcm(text, rate=r, pitch=p)
    return struct.pack("<%dh" % len(pcm),
                       *[max(-32768, min(32767, int(v))) for v in pcm])
