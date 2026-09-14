# -*- coding: utf-8 -*-
"""Sonic speed-up, pure Python -- the reference implementation behind the Gintaras speed boost (boost.py).

A faithful port of the SPEED path of sonic.c from the Sonic library by Bill Cox
(Copyright 2010 Bill Cox; https://github.com/waywardgeek/sonic, commit b93885d), used under the Apache License 2.0
(a copy ships as lt_tts/SONIC_LICENSE.txt); modified 2026 for Gintaras. Only what the boost uses is ported:
mono int16, pitch = rate = volume = 1.0, quality mode (sonicSetQuality(stream, 1): no AMDF down-sampling), and
the one-stream-per-chunk usage  create -> setSpeed -> ONE write -> flush -> read everything  (change_speed()).

EXACTNESS. The output is byte-identical to sonic.c compiled with plain IEEE-754 float/double semantics (MSVC x86 /
x64 with SSE2; clang/gcc with -ffp-contract=off). Every C `float` operation is evaluated on exact rationals and
rounded ONCE to binary32 (ties-to-even); every C `double` sub-expression uses Python floats (binary64). The C
type of each expression is noted beside it -- keep those notes when porting to Kotlin / Rust / C++.

ONE DELIBERATE DEVIATION from upstream: findPitchPeriodInRange's `diff * period` products are exact (64-bit).
Upstream declares them `unsigned long`, which is 32 bits on Windows (LLP64) but 64 bits on Linux / macOS / iOS
(LP64), so stock Sonic can choose different pitch periods per platform on loud input. Every Gintaras port uses the
exact form; the vendored C copy is patched the same way. _WRAP32 = True reproduces a stock MSVC build (tests only).
"""
import math
from fractions import Fraction
from operator import sub

SONIC_MIN_PITCH = 65          # Hz (sonic.h) -> maxPeriod = sampleRate // 65
SONIC_MAX_PITCH = 400         # Hz (sonic.h) -> minPeriod = sampleRate // 400

_WRAP32 = False               # test hook: emulate 32-bit `unsigned long` AMDF products (stock MSVC sonic.c)
_MASK32 = 0xFFFFFFFF


# ---- C float (binary32) and int arithmetic ---------------------------------------------------------------------
def _f32(x):
    """Nearest binary32 (ties-to-even) of an exact number -- int, Fraction or finite binary64 float -- returned as
    a Python float holding that binary32 value exactly. inf / nan floats pass through unchanged."""
    if isinstance(x, float):
        if x != x or x in (math.inf, -math.inf):
            return x
        x = Fraction(x)
    elif not isinstance(x, Fraction):
        x = Fraction(x)
    if not x:
        return 0.0
    neg = x < 0
    if neg:
        x = -x
    n, d = x.numerator, x.denominator
    e = n.bit_length() - d.bit_length() - 23
    while True:                                   # find e with 2^23 <= x / 2^e < 2^24
        if e >= 0:
            num, den = n, d << e
        else:
            num, den = n << -e, d
        if num < den << 23:
            e -= 1
        elif num >= den << 24:
            e += 1
        else:
            break
    q, r = divmod(num, den)
    if 2 * r > den or (2 * r == den and q & 1):   # round half to even
        q += 1
        if q == 1 << 24:
            q >>= 1
            e += 1
    if not -149 <= e <= 104:
        raise OverflowError("outside the normal binary32 range: %r" % x)
    v = math.ldexp(q, e)
    return -v if neg else v


def _finite(a, b):
    return a == a and b == b and a not in (math.inf, -math.inf) and b not in (math.inf, -math.inf)


def _fadd(a, b):                                  # float + float
    return _f32(Fraction(a) + Fraction(b)) if _finite(a, b) else a + b


def _fsub(a, b):                                  # float - float
    return _f32(Fraction(a) - Fraction(b)) if _finite(a, b) else a - b


def _fmul(a, b):                                  # float * float
    return _f32(Fraction(a) * Fraction(b)) if _finite(a, b) else a * b


def _fdiv(a, b):                                  # float / float (IEEE: x/0 -> +-inf, 0/0 -> nan)
    if b == 0:
        if a == 0 or a != a:
            return math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)
    return _f32(Fraction(a) / Fraction(b)) if _finite(a, b) else a / b


# ---- the stream -----------------------------------------------------------------------------------------------
class _Stream:
    """sonicStreamStruct reduced to the speed path (mono, pitch = rate = volume = 1, quality = 1)."""

    def __init__(self, sample_rate, speed):
        self.min_period = sample_rate // SONIC_MAX_PITCH          # int minPeriod
        self.max_period = sample_rate // SONIC_MIN_PITCH          # int maxPeriod
        self.max_required = 2 * self.max_period                   # int maxRequired
        self.sample_period = _f32(1.0 / sample_rate)              # float samplePeriod = 1.0 / sampleRate (double)
        self.speed = speed                                        # float
        self.pitch = 1.0                                          # float
        self.rate = 1.0                                           # float
        self.input_play_time = 0.0                                # float
        self.time_error = 0.0                                     # float
        self.num_pitch_samples = 0                                # int (stays 0: rate == 1)
        self.prev_period = 0                                      # int
        self.prev_min_diff = 0                                    # int
        self.inp = []                                             # inputBuffer[0:numInputSamples]
        self.out = []                                             # outputBuffer[0:numOutputSamples]

    # sonicWriteShortToStream
    def write_short(self, samples):
        if samples:                               # addShortSamplesToInputBuffer returns early for 0 samples ...
            self.inp.extend(samples)
            self._update_num_input_samples(len(samples))
        self._process_stream_input()              # ... but processStreamInput always runs

    def _update_num_input_samples(self, num_samples):
        speed = _fdiv(self.speed, self.pitch)                                         # float
        # inputPlayTime += numSamples * samplePeriod / speed                            (float)
        self.input_play_time = _fadd(self.input_play_time,
                                     _fdiv(_fmul(_f32(num_samples), self.sample_period), speed))

    def _remove_input_samples(self, position):
        num_input = len(self.inp)
        remaining = num_input - position
        self.inp = self.inp[position:] if remaining > 0 else []
        # inputPlayTime = (inputPlayTime * remainingSamples) / numInputSamples          (float)
        self.input_play_time = _fdiv(_fmul(self.input_play_time, _f32(remaining)), _f32(num_input))

    # sonicFlushStream
    def flush(self):
        remaining = len(self.inp)
        speed = _fdiv(self.speed, self.pitch)                                         # float
        rate = _fmul(self.rate, self.pitch)                                           # float
        # expectedOutputSamples = numOutputSamples
        #                         + (int)((remainingSamples / speed + numPitchSamples) / rate + 0.5f)  (float)
        x = _fadd(_fdiv(_fadd(_fdiv(_f32(remaining), speed), _f32(self.num_pitch_samples)), rate), 0.5)
        expected = len(self.out) + int(x)
        # pad with silence: numInputSamples grows, inputPlayTime deliberately does NOT
        self.inp.extend([0] * (2 * self.max_required))
        self.write_short(None)                    # sonicWriteShortToStream(stream, NULL, 0)
        if len(self.out) > expected:              # throw away what the padding silence generated
            del self.out[expected:]
        self.inp = []
        self.input_play_time = 0.0
        self.time_error = 0.0
        self.num_pitch_samples = 0

    # processStreamInput
    def _process_stream_input(self):
        num_input = len(self.inp)
        if num_input == 0:
            return
        # localSpeed = numInputSamples * samplePeriod / inputPlayTime                   (float; x/0 -> inf)
        local_speed = _fdiv(_fmul(_f32(num_input), self.sample_period), self.input_play_time)
        if local_speed > 1.00001 or local_speed < 0.99999:                           # float vs double literal
            self._change_speed(local_speed)
        else:                                                                         # copyInputToOutput
            self.out.extend(self.inp)
            self._remove_input_samples(num_input)
        # rate == 1 and volume == 1: adjustRate() and scaleSamples() never run

    # changeSpeed
    def _change_speed(self, speed):
        num_samples = len(self.inp)
        max_required = self.max_required
        if num_samples < max_required:
            return
        position = 0
        while True:
            if ((speed > 1.0 and speed < 2.0 and self.time_error < 0.0) or
                    (speed < 1.0 and speed > 0.5 and self.time_error > 0.0)):
                new_samples = self._copy_unmodified_samples(speed, position)
                position += new_samples
            else:
                period = self._find_pitch_period(position)
                if speed > 1.0:
                    new_samples = self._skip_pitch_period(position, speed, period)
                    position += period + new_samples
                    if speed < 2.0:
                        # timeError += newSamples * samplePeriod
                        #              - (period + newSamples) * inputPlayTime / numInputSamples  (float)
                        a = _fmul(_f32(new_samples), self.sample_period)
                        b = _fdiv(_fmul(_f32(period + new_samples), self.input_play_time), _f32(num_samples))
                        self.time_error = _fadd(self.time_error, _fsub(a, b))
                else:
                    new_samples = self._insert_pitch_period(position, speed, period)
                    position += new_samples
                    if speed > 0.5:
                        # timeError += (period + newSamples) * samplePeriod
                        #              - newSamples * inputPlayTime / numInputSamples             (float)
                        a = _fmul(_f32(period + new_samples), self.sample_period)
                        b = _fdiv(_fmul(_f32(new_samples), self.input_play_time), _f32(num_samples))
                        self.time_error = _fadd(self.time_error, _fsub(a, b))
                if new_samples == 0:
                    return                        # upstream returns here WITHOUT removing consumed input
            if position + max_required > num_samples:
                break
        self._remove_input_samples(position)

    # copyUnmodifiedSamples
    def _copy_unmodified_samples(self, speed, position):
        available = len(self.inp) - position
        speed_m1 = speed - 1.0                                                        # (speed - 1.0): double
        # inputToCopyFloat = 1 - timeError * speed / (samplePeriod * (speed - 1.0))
        #   timeError * speed: float;  samplePeriod * (speed - 1.0), the quotient and 1 - q: double -> float
        to_copy = _f32(1.0 - _fmul(self.time_error, speed) / (self.sample_period * speed_m1))
        new_samples = available if to_copy > available else int(to_copy)
        self.out.extend(self.inp[position:position + new_samples])                   # copyToOutput
        # timeError += newSamples * samplePeriod * (speed - 1.0) / speed
        #   newSamples * samplePeriod: float;  * (speed - 1.0), / speed and the +=: double -> float
        self.time_error = _f32(self.time_error + _fmul(_f32(new_samples), self.sample_period) * speed_m1 / speed)
        return new_samples

    # findPitchPeriod (quality mode, mono: skip == 1, one full-range search)
    def _find_pitch_period(self, position):
        period, min_diff, max_diff = self._find_pitch_period_in_range(position, self.min_period, self.max_period)
        ret = self.prev_period if self._prev_period_better(min_diff, max_diff, True) else period
        self.prev_min_diff = min_diff
        self.prev_period = period
        return ret

    def _find_pitch_period_in_range(self, pos, min_period, max_period):
        x = self.inp
        best_period, worst_period = 0, 255
        min_diff, max_diff = 1, 0                                                     # unsigned long
        wrap = _WRAP32
        for period in range(min_period, max_period + 1):
            # diff = sum over i < period of |s[i] - s[i + period]|   (each term fits unsigned short)
            diff = sum(map(abs, map(sub, x[pos:pos + period], x[pos + period:pos + 2 * period])))
            if wrap:
                if best_period == 0 or ((diff * best_period) & _MASK32) < ((min_diff * period) & _MASK32):
                    min_diff, best_period = diff, period
                if ((diff * worst_period) & _MASK32) > ((max_diff * period) & _MASK32):
                    max_diff, worst_period = diff, period
            else:
                if best_period == 0 or diff * best_period < min_diff * period:
                    min_diff, best_period = diff, period
                if diff * worst_period > max_diff * period:
                    max_diff, worst_period = diff, period
        return best_period, min_diff // best_period, max_diff // worst_period

    # prevPeriodBetter
    def _prev_period_better(self, min_diff, max_diff, prefer_new_period):
        if min_diff == 0 or self.prev_period == 0:
            return False
        if prefer_new_period:
            if max_diff > min_diff * 3:
                return False                      # got a reasonable match this period
            if min_diff * 2 <= self.prev_min_diff * 3:
                return False                      # mismatch not that much greater this period
        elif min_diff <= self.prev_min_diff:
            return False
        return True

    # overlapAdd (no SONIC_USE_SIN): out = (down * (n - t) + up * t) / n, C integer division
    def _overlap_add(self, num_samples, ramp_down, ramp_up):
        x = self.inp
        o = self.out
        for t in range(num_samples):
            v = x[ramp_down + t] * (num_samples - t) + x[ramp_up + t] * t
            o.append(v // num_samples if v >= 0 else -(-v // num_samples))

    # skipPitchPeriod
    def _skip_pitch_period(self, position, speed, period):
        if speed >= 2.0:
            # newSamples = period / (speed - 1.0f)   (float), assigned to long: truncation
            new_samples = int(_fdiv(_f32(period), _fsub(speed, 1.0)))
        else:
            new_samples = period
        self._overlap_add(new_samples, position, position + period)
        return new_samples

    # insertPitchPeriod (speed < 1; unused by the boost, kept for a complete speed path)
    def _insert_pitch_period(self, position, speed, period):
        if speed <= 0.5:
            # newSamples = period * speed / (1.0f - speed)   (float), assigned to long: truncation
            new_samples = int(_fdiv(_fmul(_f32(period), speed), _fsub(1.0, speed)))
        else:
            new_samples = period
        self.out.extend(self.inp[position:position + period])
        self._overlap_add(new_samples, position + period, position)
        return new_samples


def change_speed(samples, speed_milli, sample_rate=22050):
    """Change the speed of mono int16 `samples` by speed_milli / 1000 with pitch preserved, following the Gintaras
    contract exactly: one stream, speed = (float)speed_milli / 1000.0f, quality 1, ONE write of everything,
    flush, read everything. Returns a new list of int16 samples."""
    speed_milli = int(speed_milli)
    if not 50 <= speed_milli <= 20000:
        raise ValueError("speed_milli must be within 50..20000 (x0.05..x20), got %d" % speed_milli)
    stream = _Stream(sample_rate, _fdiv(_f32(speed_milli), 1000.0))   # (float)speed_milli / 1000.0f
    stream.write_short([int(v) for v in samples])
    stream.flush()
    return stream.out
