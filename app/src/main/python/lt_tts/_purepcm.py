# -*- coding: utf-8 -*-
"""Tiny pure-Python helpers so the runtime engine needs NO numpy (important for hosts that can't ship a
compiled numpy: NVDA's 32-bit Python, Android/Chaquopy, iOS). These reproduce the exact float results the
numpy versions produced, so synthesis stays bit-for-bit identical.

The heavy DSP (dsp.py) and the default synthesis (backend.py) are already pure-Python list code; only the
voice-frame decode and the F0 break-point interpolation used numpy, and both are covered here.
"""
import array


def decode_pcm(raw):
    """Decode a little-endian int16 byte string into a list of Python ints. Replaces np.frombuffer(raw,'<i2').
    array('h') is native little-endian on every platform this engine targets (x86/ARM)."""
    a = array.array("h")
    a.frombytes(raw)
    return a            # an array('h') indexes/iterates/len()s like a list of ints; cheaper than a list


def mean_abs(samples):
    """Mean of |sample| -- the build_frame_pool garbage check (was np.mean(np.abs(a)))."""
    n = len(samples)
    if n == 0:
        return 0.0
    return sum(abs(int(x)) for x in samples) / n


def argsort_xy(xs, ys):
    """Sort (xs, ys) by ascending xs (stable), returning new lists. Replaces the np.argsort + fancy-index."""
    if not xs:
        return [], []
    paired = sorted(range(len(xs)), key=lambda i: xs[i])
    return [xs[i] for i in paired], [ys[i] for i in paired]


def interp(tt, xs, ys):
    """Linear interpolation matching numpy.interp for a SORTED-ascending xs: clamp to the ends, otherwise
    linearly interpolate between the bracketing break-points. Returns a float."""
    n = len(xs)
    if n == 0:
        return 0.0
    if tt <= xs[0]:
        return float(ys[0])
    if tt >= xs[-1]:
        return float(ys[-1])
    # binary search for the bracket [xs[lo], xs[hi]]
    lo, hi = 0, n - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= tt:
            lo = mid
        else:
            hi = mid
    x0, x1 = xs[lo], xs[hi]
    if x1 == x0:
        return float(ys[lo])
    f = (tt - x0) / (x1 - x0)
    return float(ys[lo] + f * (ys[hi] - ys[lo]))
