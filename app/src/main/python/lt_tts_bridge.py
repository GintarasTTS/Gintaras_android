# -*- coding: utf-8 -*-
"""Android bridge: wraps lt_tts, returns raw PCM as bytes for Chaquopy/Kotlin.

Engine singleton is kept alive across calls so gintaras.dta and all lexicons are
loaded only once per process lifetime.
"""
import functools
import numpy as np
from lt_tts import Gintaras

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = Gintaras()
    return _engine


def _pack(pcm):
    """Convert a PCM sample list to little-endian int16 bytes via numpy (fast)."""
    return np.clip(np.asarray(pcm, dtype=np.int32), -32768, 32767).astype(np.int16).tobytes()


def warm_up():
    """Trigger all lazy loads (gintaras.dta, lexicons) before the first real call."""
    _get_engine().synth_pcm("a")


@functools.lru_cache(maxsize=512)
def _synth_clause_cached(clause, question, rate, pitch):
    """Synthesize one clause (cached by content). Returns bytes."""
    from lt_tts import planbuilder as PF, backend as GS
    r = None if rate == 50 else int(rate)
    p = None if pitch == 50 else int(pitch)
    plan = PF.build_plan_phrase(clause, question=bool(question))
    return _pack(list(GS.synthesize(plan, rate=r, pitch=p)))


def synth_clause(clause, question=False, rate=50, pitch=50):
    """Synthesize one clause -> PCM bytes (LRU-cached for TalkBack label repetition)."""
    _get_engine()  # ensure warm
    return _synth_clause_cached(str(clause), bool(question), int(rate), int(pitch))


def synthesize(text, rate=50, pitch=50):
    """Synthesize full text -> PCM bytes (used as fallback / for testing)."""
    eng = _get_engine()
    r = None if rate == 50 else int(rate)
    p = None if pitch == 50 else int(pitch)
    return _pack(eng.synth_pcm(text, rate=r, pitch=p))
