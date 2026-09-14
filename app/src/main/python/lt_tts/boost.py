# -*- coding: utf-8 -*-
"""Speed boost above engine rate 100: time-stretch the FINISHED audio with Sonic in quality mode.

Why (measured 2026-09-14, see _speed_boost_research/): the engine speeds up only by dropping whole voiced
frames; ~72% of rate-100 audio (consonants, bursts, pauses) is never shortened, so pushing the engine's own
gate past rate 100 clips words. Listening tests chose engine rate 100 + Sonic x2.0 quality as the maximum.

boost_milli is an INTEGER permille, BOOST_MIN..BOOST_MAX (x1.0..x2.0), passed alongside rate/pitch. 1000 leaves
every chunk untouched, so all existing output stays byte-identical.

THE PER-CHUNK RULE -- every port must follow it exactly, because Sonic's output depends on how audio is chunked:
  * clause-body chunk  -> one Sonic stream: speed (float)boost_milli / 1000.0f, quality 1, ONE write, flush,
                          read everything  (sonic.change_speed)
  * pure-silence chunk -> no Sonic: n_out = n_in * 1000 // boost_milli
"""
from . import sonic

BOOST_MIN = 1000              # x1.0 = off
BOOST_MAX = 2000              # x2.0 = the chosen maximum


def clamp_boost(boost_milli):
    """None -> 1000 (off); anything else -> an int clamped to BOOST_MIN..BOOST_MAX."""
    if boost_milli is None:
        return BOOST_MIN
    return max(BOOST_MIN, min(BOOST_MAX, int(boost_milli)))


def stretch_body(pcm, boost_milli):
    """A clause-body chunk at `boost_milli` (already clamped). 1000 returns `pcm` itself, untouched."""
    if boost_milli <= BOOST_MIN or not pcm:
        return pcm
    return sonic.change_speed(pcm, boost_milli)


def shorten_silence(pcm, boost_milli):
    """A pure-silence chunk (lead / inter-clause pause / tail) at `boost_milli`. 1000 returns `pcm` itself."""
    if boost_milli <= BOOST_MIN:
        return pcm
    return [0] * (len(pcm) * 1000 // boost_milli)


# ---- host slider -> (engine rate, boost_milli) ----------------------------------------------------------------
# REQUIREMENT (user, 2026-09-14): plain engine rate 100 WITHOUT Sonic must stay reachable at some slider position,
# so people used to Gintaras at its old maximum keep it. Sonic only starts ABOVE that position.

PERCENT_PLATEAU_END = 225     # 200..225 % = engine rate 100 with NO boost
PERCENT_MAX = 600             # TalkBack's ceiling: SpeechRateAndPitchActor.RATE_MAXIMUM = 6.0 (10 %..600 %)


def boost_for_percent(rate_percent):
    """Android SynthesisRequest.getSpeechRate() or iOS SSML rate % (100 = normal) -> boost_milli.

    The engine rate keeps its existing host mapping (Android min(p / 2, 100)), which reaches 100 at 200 %.
    200..225 % is a plateau at rate 100 with no boost. It is wider than one TalkBack gesture (each multiplies by
    1.1; 225 / 200 = 1.125), so every gesture sequence lands on it at least once -- from the 100 % default the
    8th gesture gives 214 %. Above 225 % the boost rises linearly to x2.0 at 600 %; anything higher stays x2.0
    (TalkBack multiplies its own rate by the system default rate, so values above 600 can arrive)."""
    p = int(rate_percent)
    if p <= PERCENT_PLATEAU_END:
        return BOOST_MIN
    return min(BOOST_MAX, BOOST_MIN + (p - PERCENT_PLATEAU_END) * (BOOST_MAX - BOOST_MIN)
               // (PERCENT_MAX - PERCENT_PLATEAU_END))


def sapi_rate_to_engine(sapi_rate):
    """Windows SAPI integer rate (host rate + XML adjustment; -10..+10, NVDA sends (percent - 50) // 5) ->
    (engine rate 0..100, boost_milli). One slider, no extra option:

        -10 .. 0   NVDA  0 .. 50 %   engine 0 .. 50 in steps of 5 (unchanged)
         +1 .. +5  NVDA 55 .. 75 %   engine 60 .. 100 in steps of 10; +5 = plain rate 100, no boost
         +6 .. +10 NVDA 80 .. 100 %  engine 100 + boost x1.2 .. x2.0"""
    r = max(-10, min(10, int(sapi_rate)))
    if r <= 0:
        return 50 + 5 * r, BOOST_MIN
    if r <= 5:
        return 50 + 10 * r, BOOST_MIN
    return 100, BOOST_MIN + 200 * (r - 5)
