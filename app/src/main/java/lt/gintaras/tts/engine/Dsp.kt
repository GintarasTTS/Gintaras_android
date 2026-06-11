package lt.gintaras.tts.engine

// Bit-for-bit Kotlin port of lt_tts/dsp.py
// Pure integer DSP — no floating-point, no external deps.

internal object Dsp {

    private const val I16_MIN = -0x8000
    private const val I16_MAX = 0x7fff

    // C integer division truncating toward zero (x86 idiv semantics).
    // In Kotlin, Int '/' already truncates toward zero — this is a 1:1 mapping.
    fun cdiv(a: Int, b: Int): Int = a / b

    // (a+b)/2 truncating toward zero  (add;cdq;sub eax,edx;sar eax,1)
    fun trunc2(a: Int, b: Int): Int {
        val s = a + b
        return if (s < 0) -((-s) shr 1) else (s shr 1)
    }

    fun clamp16(x: Int): Int = when {
        x > I16_MAX -> I16_MAX
        x < I16_MIN -> I16_MIN
        else -> x
    }

    // ---- sub_1000EBC0 : period cross-fade / resampler -------------------------------------
    fun ebc0(src1: IntArray, len1: Int, src2: IntArray, len2: Int, a6: Int, a7: Int, mode: Int): IntArray {
        if (a6 == 0) return IntArray(0)
        if (mode == 0) return src1.copyOf(len1)
        val w1 = a7
        val w2 = a6 - a7
        val outlen = cdiv(w1 * len1 + w2 * len2, a6)
        if (outlen <= 0) return IntArray(0)
        val mid = cdiv(outlen, 2)
        val c1 = cdiv(len1, 2) - mid
        val c2 = cdiv(len2, 2) - mid
        return IntArray(outlen) { idx ->
            var i1 = c1 + idx
            if (i1 >= len1) i1 = len1 - 1
            if (i1 < 0) i1 = 0
            var i2 = c2 + idx
            if (i2 >= len2) i2 = len2 - 1
            if (i2 < 0) i2 = 0
            clamp16(cdiv(src1[i1] * w1, a6) + cdiv(src2[i2] * w2, a6))
        }
    }

    // ---- sub_1000EAD0 : join declick (multi-pass moving average) -------------------------
    fun ead0(buf: IntArray, fwdpos: Int, joinpos: Int) {
        fun fwd(lo: Int, count: Int) {
            for (k in 0 until count) {
                val j = lo + k
                buf[j] = trunc2(buf[j - 1], buf[j + 1])
            }
        }
        var edx = fwdpos - 5; var eax = fwdpos + 1
        if (edx < eax) fwd(edx, eax - edx)
        edx = fwdpos - 10; eax = fwdpos + 2
        if (edx < eax) fwd(edx, eax - edx)
        edx = fwdpos - 15; eax = fwdpos + 4
        if (edx < eax) fwd(edx, eax - edx)

        buf[joinpos] = 0

        fun bwd(hi: Int, count: Int) {
            for (k in 0 until count) {
                val j = hi - k
                buf[j] = trunc2(buf[j - 1], buf[j + 1])
            }
        }
        var ecx = joinpos - 1; var ea = joinpos - 10
        if (ecx > ea) bwd(ecx, ecx - ea)
        ecx = joinpos - 1; ea = joinpos - 5
        if (ecx > ea) bwd(ecx, ecx - ea)
    }

    // ---- sub_1000E6B0 mirror branch : native pool period -> output grain ------------------
    fun e6b0Mirror(native: IntArray, target: Int, prevLast: Int): IntArray {
        val nc = native.size
        if (nc >= target) return native.copyOf()  // verbatim (nc==target pause grains AND nc>target)
        val out = native.toMutableList()
        val D = prevLast - out[0]
        if (D != 0) {
            val slope = cdiv(D, target)
            if (D >= 0) {
                val step = slope + 0x64; var esi = D; var k = 0
                while (esi >= 0 && k < out.size) { out[k] = clamp16(out[k] + esi); esi -= step; k++ }
            } else {
                val step = slope - 0x64; var esi = D; var k = 0
                while (esi < 0 && k < out.size) { out[k] = clamp16(out[k] + esi); esi -= step; k++ }
            }
        }
        while (out.size < target) out.add(out[nc - 1])  // flat-pad
        // seam declick
        val seam = nc - 1
        for ((half, hiOff) in listOf(Pair(5, 1), Pair(10, 2), Pair(15, 4))) {
            if (seam > half && seam + hiOff < target) {
                for (j in seam - half until seam + hiOff) {
                    out[j] = trunc2(out[j - 1], out[j + 1])
                }
            }
        }
        return out.subList(0, target).toIntArray()
    }

    // ---- sub_1000E6B0 full path (includes verbatim+detrend for nc>=target) ---------------
    fun e6b0(native: IntArray, target: Int, prevLast: Int): IntArray {
        val nc = native.size
        if (nc < target) return e6b0Mirror(native, target, prevLast)
        // nc >= target: memcpy full native, block-A end-detrend, block-B start-detrend
        val o = native.toMutableList()
        val X = target
        if (nc > X) {
            val D = o[nc - 1] - o[X - 1]
            if (D != 0) {
                val step = cdiv(D, X) + (if (D < 0) -0x64 else 0x64)
                var esi = D; var k = X - 1
                while ((if (D < 0) esi < 0 else esi >= 0) && k >= 0) {
                    o[k] = clamp16(o[k] + esi); esi -= step; k--
                }
            }
        }
        val D2 = prevLast - o[0]
        if (D2 != 0) {
            val step = cdiv(D2, X) + (if (D2 < 0) -0x64 else 0x64)
            var esi = D2; var k = 0
            while ((if (D2 < 0) esi < 0 else esi >= 0) && k < nc) {
                o[k] = clamp16(o[k] + esi); esi -= step; k++
            }
        }
        return o.subList(0, X).toIntArray()
    }

    // ---- render_from_grains : EDC0 ring write + EAD0 declick -----------------------------
    private const val BLOCKP = 22144
    private const val KEEP = 0x400
    private const val THRESH = BLOCKP + KEEP

    fun renderFromGrains(grains: List<IntArray>, flushIdx: Set<Int> = emptySet()): IntArray {
        val total = grains.sumOf { it.size }
        val buf = IntArray(total + 64)
        var pos = 0
        var b320 = 0
        for ((gi, g) in grains.withIndex()) {
            if (gi in flushIdx) b320 = 0
            var ebx = b320
            for (s in g) {
                buf[pos++] = s
                b320++
                if (b320 == THRESH) { b320 = KEEP; ebx -= BLOCKP }
            }
            if (ebx > 0x1e) ead0(buf, pos - g.size, pos)
        }
        return buf.copyOf(total)
    }
}
