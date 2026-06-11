package lt.gintaras.tts.engine

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.charset.Charset

// Bit-for-bit Kotlin port of lt_tts/voice.py
// Decodes gintaras.dta (MFC CMapStringToOb of CMapItem) into a shared pitch-frame pool.

internal object Voice {

    private val TAG_BYTES = byteArrayOf(0x03.toByte(), 0x80.toByte())
    private val SENT = byteArrayOf(0xff.toByte(), 0xff.toByte(), 0xff.toByte(), 0xff.toByte())
    private val ZERO8 = ByteArray(8)

    // ---- frame pool -----------------------------------------------------------------------

    private fun keyBefore(d: ByteArray, i: Int): String? {
        for (L in 1..7) {
            val start = i - 1 - L
            if (start < 0) break
            if (d[start].toInt() and 0xff == L) {
                val slice = d.sliceArray(start + 1 until i)
                if (slice.all { b -> val c = b.toInt() and 0xff; c in 33..126 })
                    return slice.toString(Charsets.ISO_8859_1)
            }
        }
        return null
    }

    fun buildFramePool(d: ByteArray): Map<Int, IntArray> {
        val pool = mutableMapOf<Int, IntArray>()
        val n = d.size
        var i = 0
        while (i < n - 1) {
            // find next 0x03 0x80
            if (d[i] != 0x03.toByte() || d[i + 1] != 0x80.toByte()) { i++; continue }
            val p = i + 2
            if (p + 24 > n) { i++; continue }
            val bb = ByteBuffer.wrap(d, p, 12).order(ByteOrder.LITTLE_ENDIAN)
            val hdrfid = bb.int; val flag = bb.int; val nbytes = bb.int
            if (!(hdrfid in 0..0x1fff && flag in 1..63 && nbytes in 32..0x20000 && nbytes % 2 == 0)) { i++; continue }
            val gap = d.sliceArray(p + 12 until p + 24)
            if (!gap.sliceArray(0..7).contentEquals(ZERO8)) { i++; continue }
            val gapSent = gap.sliceArray(8..11)
            if (!gapSent.contentEquals(SENT) && !gapSent.contentEquals(ByteArray(4))) { i++; continue }
            val key = keyBefore(d, i); if (key == null) { i++; continue }
            if (!key.all { it.isDigit() }) { i++; continue }
            val pcmOff = p + 24
            if (pcmOff + nbytes > n) { i++; continue }
            // mean abs check (reject full-scale garbage)
            val bb2 = ByteBuffer.wrap(d, pcmOff, nbytes).order(ByteOrder.LITTLE_ENDIAN)
            val samps = IntArray(nbytes / 2) { bb2.short.toInt() }
            val meanAbs = samps.sumOf { Math.abs(it).toLong() }.toDouble() / samps.size
            if (meanAbs >= 28000.0) { i++; continue }
            val fid = key.toInt()
            if (fid !in pool) pool[fid] = samps
            i += 2
        }
        return pool
    }

    // ---- units ----------------------------------------------------------------------------

    private fun looksLabel(b: ByteArray): Boolean {
        if (b.isEmpty()) return false
        var hasAlpha = false
        for (byte in b) {
            val c = byte.toInt() and 0xff
            if (c < 0x20) return false
            if (c < 0x80 && !(c.toChar().isLetter() || c.toChar() in "'%-|$^")) return false
            if ((c < 0x80 && c.toChar().isLetter()) || c >= 0x80) hasAlpha = true
        }
        return hasAlpha
    }

    fun parseUnits(d: ByteArray): Map<String, List<Int>> {
        val units = mutableMapOf<String, List<Int>>()
        val n = d.size
        val tagFF = byteArrayOf(0x03, 0x80.toByte(), 0xff.toByte(), 0xff.toByte(), 0xff.toByte(), 0xff.toByte())
        var i = 0
        while (i < n - tagFF.size) {
            // find tag + 0xffffffff
            if (!d.sliceArray(i until i + tagFF.size).contentEquals(tagFF)) { i++; continue }
            val t = i
            var key: String? = null
            for (L in 1..12) {
                val s = t - 1 - L
                if (s < 0) break
                val lb = d[s].toInt() and 0xff
                if (lb == L) {
                    val slice = d.sliceArray(s + 1 until s + 1 + L)
                    if (looksLabel(slice)) {
                        key = slice.toString(Charset.forName("windows-1257")); break
                    }
                }
            }
            if (key == null) { i++; continue }
            val p = i + tagFF.size
            if (p + 20 > n) { i++; continue }
            val bb = ByteBuffer.wrap(d, p, 20).order(ByteOrder.LITTLE_ENDIAN)
            bb.int; bb.int  // i0, i1
            val cnt = bb.int; bb.int; bb.int  // i3, i4
            if (cnt <= 0 || cnt >= 4000) { i++; continue }
            var q = p + 20
            val refs = mutableListOf<Int>()
            var ok = true
            repeat(cnt) {
                if (q >= n || d[q].toInt() and 0xff == 0 || d[q].toInt() and 0xff > 9) { ok = false; return@repeat }
                val L = d[q].toInt() and 0xff
                if (q + 1 + L > n) { ok = false; return@repeat }
                val sid = d.sliceArray(q + 1 until q + 1 + L)
                if (!sid.all { it in 0x30..0x39 }) { ok = false; return@repeat }
                refs.add(sid.toString(Charsets.US_ASCII).toInt())
                q += 1 + L + 8
            }
            if (ok && refs.isNotEmpty()) {
                if (key !in units || refs.size > units[key]!!.size) units[key] = refs
            }
            i++
        }
        return units
    }

    fun parseUnitPads(d: ByteArray): Map<String, Pair<List<Int>, List<Int>>> {
        val out = mutableMapOf<String, Pair<List<Int>, List<Int>>>()
        val n = d.size
        val tagFF = byteArrayOf(0x03, 0x80.toByte(), 0xff.toByte(), 0xff.toByte(), 0xff.toByte(), 0xff.toByte())
        var i = 0
        while (i < n - tagFF.size) {
            if (!d.sliceArray(i until i + tagFF.size).contentEquals(tagFF)) { i++; continue }
            val t = i
            var key: String? = null
            for (L in 1..12) {
                val s = t - 1 - L
                if (s < 0) break
                val lb = d[s].toInt() and 0xff
                if (lb == L) {
                    val slice = d.sliceArray(s + 1 until s + 1 + L)
                    if (looksLabel(slice)) { key = slice.toString(Charset.forName("windows-1257")); break }
                }
            }
            if (key == null) { i++; continue }
            val p = i + tagFF.size
            if (p + 20 > n) { i++; continue }
            val bb = ByteBuffer.wrap(d, p, 20).order(ByteOrder.LITTLE_ENDIAN)
            bb.int; bb.int; val cnt = bb.int; bb.int; bb.int
            if (cnt <= 0 || cnt >= 4000) { i++; continue }
            var q = p + 20
            val pads = mutableListOf<Int>(); val flags = mutableListOf<Int>()
            var ok = true
            repeat(cnt) {
                if (q >= n || d[q].toInt() and 0xff == 0 || d[q].toInt() and 0xff > 9) { ok = false; return@repeat }
                val L = d[q].toInt() and 0xff
                if (q + 1 + L + 8 > n) { ok = false; return@repeat }
                val bb2 = ByteBuffer.wrap(d, q + 1 + L, 8).order(ByteOrder.LITTLE_ENDIAN)
                pads.add(bb2.int); flags.add(bb2.int)
                q += 1 + L + 8
            }
            if (ok && pads.isNotEmpty()) {
                if (key !in out || pads.size > out[key]!!.first.size) out[key] = Pair(pads, flags)
            }
            i++
        }
        return out
    }

    // ---- lazy-loaded voice data -----------------------------------------------------------

    data class VoiceData(val pool: Map<Int, IntArray>, val units: Map<String, List<Int>>)

    @Volatile private var cached: VoiceData? = null

    fun load(): VoiceData {
        cached?.let { return it }
        synchronized(this) {
            cached?.let { return it }
            val d = Assets.bytes("gintaras.dta")
            val pool = buildFramePool(d)
            val units = parseUnits(d)
            return VoiceData(pool, units).also { cached = it }
        }
    }
}
