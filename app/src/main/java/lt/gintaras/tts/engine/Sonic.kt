package lt.gintaras.tts.engine

// Kotlin port of lt_tts/sonic.py: Sonic's SPEED path, byte-identical to sonic.c and to the Python reference.
//
// Sonic by Bill Cox (https://github.com/waywardgeek/sonic, commit b93885d), used under the Apache License 2.0 --
// a copy ships in the APK as assets/licenses/SONIC_LICENSE.txt; modified 2026 for Gintaras. Ported from the
// CURRENT sonic.c, deliberately NOT the official Sonic.java: that port predates sonic.c's timeError /
// inputPlayTime / samplePeriod logic and differs in 71-78% of samples at speeds between x1 and x2.
//
// Scope (what Boost uses): mono, pitch = rate = volume = 1, quality mode (no AMDF down-sampling), one stream per
// chunk: create -> speed -> ONE write -> flush -> read everything (changeSpeed).
//
// EXACTNESS: every C `float` expression is a Kotlin Float operation and every C `double` sub-expression a Kotlin
// Double operation, in the same order (the C type is noted beside each). JVM and ART floating point is strict
// IEEE-754 with no fused multiply-add, so the output is the same on the desktop JVM and on devices. The AMDF
// `diff * period` products use Long: the exact 64-bit form every Gintaras port uses instead of upstream's
// platform-dependent `unsigned long`.
internal object Sonic {

    private const val SONIC_MIN_PITCH = 65          // Hz -> maxPeriod = sampleRate / 65
    private const val SONIC_MAX_PITCH = 400         // Hz -> minPeriod = sampleRate / 400

    /** Change the speed of mono int16 [samples] by speedMilli / 1000 with pitch preserved, following the Gintaras
     *  contract exactly: one stream, speed = (float)speedMilli / 1000.0f, quality 1, ONE write, flush, read all. */
    fun changeSpeed(samples: IntArray, speedMilli: Int, sampleRate: Int = 22050): IntArray {
        require(speedMilli in 50..20000) { "speedMilli must be within 50..20000 (x0.05..x20), got $speedMilli" }
        val stream = Stream(sampleRate, speedMilli.toFloat() / 1000.0f)          // (float)speedMilli / 1000.0f
        stream.writeShort(samples)
        stream.flush()
        return stream.output()
    }

    private class Stream(sampleRate: Int, private val speed: Float) {
        private val minPeriod = sampleRate / SONIC_MAX_PITCH                       // int
        private val maxPeriod = sampleRate / SONIC_MIN_PITCH                       // int
        private val maxRequired = 2 * maxPeriod                                    // int
        private val samplePeriod: Float = (1.0 / sampleRate).toFloat()             // float = 1.0 / sampleRate (double)
        private val pitch = 1.0f
        private val rate = 1.0f
        private var inputPlayTime = 0.0f                                          // float
        private var timeError = 0.0f                                               // float
        private var numPitchSamples = 0                                            // stays 0: rate == 1
        private var prevPeriod = 0
        private var prevMinDiff = 0
        private var retMinDiff = 0
        private var retMaxDiff = 0

        private var inp = IntArray(maxRequired + (maxRequired shr 2))              // inputBuffer
        private var numInput = 0
        private var outBuf = IntArray(maxRequired + (maxRequired shr 2))           // outputBuffer
        private var numOutput = 0

        fun output(): IntArray = outBuf.copyOf(numOutput)

        private fun ensureInput(extra: Int) {
            if (numInput + extra > inp.size) inp = inp.copyOf(maxOf(numInput + extra, inp.size + (inp.size shr 1)))
        }

        private fun ensureOutput(extra: Int) {
            if (numOutput + extra > outBuf.size) outBuf = outBuf.copyOf(maxOf(numOutput + extra, outBuf.size + (outBuf.size shr 1)))
        }

        private fun appendOutput(src: IntArray, from: Int, count: Int) {
            ensureOutput(count)
            System.arraycopy(src, from, outBuf, numOutput, count)
            numOutput += count
        }

        // sonicWriteShortToStream
        fun writeShort(samples: IntArray?) {
            if (samples != null && samples.isNotEmpty()) {      // addShortSamplesToInputBuffer returns early on 0 ...
                ensureInput(samples.size)
                System.arraycopy(samples, 0, inp, numInput, samples.size)
                numInput += samples.size
                updateNumInputSamples(samples.size)
            }
            processStreamInput()                                  // ... but processStreamInput always runs
        }

        private fun updateNumInputSamples(numSamples: Int) {
            val speed = this.speed / pitch                                             // float
            inputPlayTime += numSamples.toFloat() * samplePeriod / speed               // float
        }

        private fun removeInputSamples(position: Int) {
            val remaining = numInput - position
            if (remaining > 0) System.arraycopy(inp, position, inp, 0, remaining)
            inputPlayTime = inputPlayTime * remaining.toFloat() / numInput.toFloat()  // float
            numInput = remaining
        }

        // sonicFlushStream
        fun flush() {
            val remaining = numInput
            val speed = this.speed / pitch                                             // float
            val rate = this.rate * pitch                                               // float
            val expected = numOutput +
                ((remaining.toFloat() / speed + numPitchSamples.toFloat()) / rate + 0.5f).toInt()   // float -> int
            ensureInput(2 * maxRequired)
            java.util.Arrays.fill(inp, numInput, numInput + 2 * maxRequired, 0)
            numInput += 2 * maxRequired              // padding silence: inputPlayTime deliberately NOT updated
            writeShort(null)                         // sonicWriteShortToStream(stream, NULL, 0)
            if (numOutput > expected) numOutput = expected
            numInput = 0
            inputPlayTime = 0.0f
            timeError = 0.0f
            numPitchSamples = 0
        }

        // processStreamInput
        private fun processStreamInput() {
            if (numInput == 0) return
            val localSpeed = numInput.toFloat() * samplePeriod / inputPlayTime         // float; x / 0 -> Infinity
            if (localSpeed.toDouble() > 1.00001 || localSpeed.toDouble() < 0.99999) {  // float vs double literal
                changeSpeed(localSpeed)
            } else {                                                                   // copyInputToOutput
                appendOutput(inp, 0, numInput)
                removeInputSamples(numInput)
            }
            // rate == 1 and volume == 1: adjustRate() and scaleSamples() never run
        }

        // changeSpeed
        private fun changeSpeed(speed: Float) {
            val numSamples = numInput
            if (numSamples < maxRequired) return
            var position = 0
            do {
                if ((speed > 1.0f && speed < 2.0f && timeError < 0.0f) ||
                    (speed < 1.0f && speed > 0.5f && timeError > 0.0f)) {
                    position += copyUnmodifiedSamples(speed, position)
                } else {
                    val period = findPitchPeriod(position)
                    val newSamples: Int
                    if (speed.toDouble() > 1.0) {
                        newSamples = skipPitchPeriod(position, speed, period)
                        position += period + newSamples
                        if (speed.toDouble() < 2.0) {
                            // timeError += newSamples * samplePeriod - (period + newSamples) * inputPlayTime / numInputSamples
                            timeError += newSamples.toFloat() * samplePeriod -
                                (period + newSamples).toFloat() * inputPlayTime / numSamples.toFloat()    // float
                        }
                    } else {
                        newSamples = insertPitchPeriod(position, speed, period)
                        position += newSamples
                        if (speed.toDouble() > 0.5) {
                            // timeError += (period + newSamples) * samplePeriod - newSamples * inputPlayTime / numInputSamples
                            timeError += (period + newSamples).toFloat() * samplePeriod -
                                newSamples.toFloat() * inputPlayTime / numSamples.toFloat()               // float
                        }
                    }
                    if (newSamples == 0) return      // upstream returns WITHOUT removing consumed input
                }
            } while (position + maxRequired <= numSamples)
            removeInputSamples(position)
        }

        // copyUnmodifiedSamples
        private fun copyUnmodifiedSamples(speed: Float, position: Int): Int {
            val available = numInput - position
            val speedM1 = speed.toDouble() - 1.0                                       // (speed - 1.0): double
            // inputToCopyFloat = 1 - timeError * speed / (samplePeriod * (speed - 1.0))
            //   timeError * speed: float;  the divisor, quotient and 1 - q: double -> float
            val inputToCopyFloat = (1.0 - (timeError * speed).toDouble() / (samplePeriod.toDouble() * speedM1)).toFloat()
            val newSamples = if (inputToCopyFloat > available.toFloat()) available else inputToCopyFloat.toInt()
            appendOutput(inp, position, newSamples)                                    // copyToOutput
            // timeError += newSamples * samplePeriod * (speed - 1.0) / speed
            //   newSamples * samplePeriod: float;  * (speed - 1.0), / speed and the +=: double -> float
            timeError = (timeError.toDouble() +
                (newSamples.toFloat() * samplePeriod).toDouble() * speedM1 / speed.toDouble()).toFloat()
            return newSamples
        }

        // findPitchPeriod (quality mode, mono: one full-range search)
        private fun findPitchPeriod(position: Int): Int {
            val period = findPitchPeriodInRange(position, minPeriod, maxPeriod)
            val minDiff = retMinDiff
            val maxDiff = retMaxDiff
            val ret = if (prevPeriodBetter(minDiff, maxDiff, true)) prevPeriod else period
            prevMinDiff = minDiff
            prevPeriod = period
            return ret
        }

        private fun findPitchPeriodInRange(pos: Int, minP: Int, maxP: Int): Int {
            var bestPeriod = 0
            var worstPeriod = 255
            var minDiff = 1L
            var maxDiff = 0L
            for (period in minP..maxP) {
                var diff = 0L
                for (i in 0 until period) {
                    val s = inp[pos + i]
                    val p = inp[pos + period + i]
                    diff += if (s >= p) (s - p) else (p - s)
                }
                if (bestPeriod == 0 || diff * bestPeriod < minDiff * period) {
                    minDiff = diff
                    bestPeriod = period
                }
                if (diff * worstPeriod > maxDiff * period) {
                    maxDiff = diff
                    worstPeriod = period
                }
            }
            retMinDiff = (minDiff / bestPeriod).toInt()
            retMaxDiff = (maxDiff / worstPeriod).toInt()
            return bestPeriod
        }

        // prevPeriodBetter
        private fun prevPeriodBetter(minDiff: Int, maxDiff: Int, preferNewPeriod: Boolean): Boolean {
            if (minDiff == 0 || prevPeriod == 0) return false
            if (preferNewPeriod) {
                if (maxDiff > minDiff * 3) return false             // got a reasonable match this period
                if (minDiff * 2 <= prevMinDiff * 3) return false    // mismatch not that much greater this period
            } else if (minDiff <= prevMinDiff) {
                return false
            }
            return true
        }

        // overlapAdd (no SONIC_USE_SIN): out = (down * (n - t) + up * t) / n, integer division truncating toward zero
        private fun overlapAdd(numSamples: Int, rampDown: Int, rampUp: Int) {
            ensureOutput(numSamples)
            for (t in 0 until numSamples) {
                outBuf[numOutput++] = (inp[rampDown + t] * (numSamples - t) + inp[rampUp + t] * t) / numSamples
            }
        }

        // skipPitchPeriod
        private fun skipPitchPeriod(position: Int, speed: Float, period: Int): Int {
            val newSamples = if (speed >= 2.0f) {
                (period.toFloat() / (speed - 1.0f)).toInt()        // float, assigned to long: truncation
            } else {
                period
            }
            overlapAdd(newSamples, position, position + period)
            return newSamples
        }

        // insertPitchPeriod (speed < 1; unused by the boost, kept for a complete speed path)
        private fun insertPitchPeriod(position: Int, speed: Float, period: Int): Int {
            val newSamples = if (speed <= 0.5f) {
                (period.toFloat() * speed / (1.0f - speed)).toInt() // float, assigned to long: truncation
            } else {
                period
            }
            appendOutput(inp, position, period)
            overlapAdd(newSamples, position + period, position)
            return newSamples
        }
    }
}
