package lt.gintaras.tts.engine

import org.json.JSONObject

// Kotlin port of lt_tts/nucleus.py
// Bit-exact port of transcr4.dll sub_10009310 — the syllable-nucleus marker.

internal object Nucleus {

    private data class CC(
        val VS: Set<Int>, val V: Set<Int>,
        val SON: Set<Int>, val OBS: Set<Int>,
        val SIB_S: Set<Int>, val SIB_Z: Set<Int>,
        val SUBSTR: List<ByteArray>
    )

    private var cc: CC? = null

    private val CP1257 = java.nio.charset.Charset.forName("windows-1257")

    private fun loadCC(): CC {
        cc?.let { return it }
        val json = Assets.json("nucleus_data.json")
        fun strToByteSet(s: String) = s.toByteArray(CP1257).map { it.toInt() and 0xff }.toSet()
        val VS    = strToByteSet(json.getString("VS"))
        val V     = strToByteSet(json.getString("V"))
        val SON   = strToByteSet(json.getString("SON"))
        val OBS   = strToByteSet(json.getString("OBS"))
        val SIB_S = strToByteSet(json.getString("SIB_S"))
        val SIB_Z = strToByteSet(json.getString("SIB_Z"))
        val arr   = json.getJSONArray("SUBSTR")
        val SUBSTR = (0 until arr.length()).map { arr.getString(it).toByteArray(CP1257) }
        return CC(VS, V, SON, OBS, SIB_S, SIB_Z, SUBSTR).also { cc = it }
    }

    // constants (cp1257 byte values)
    private const val SP = 0x20; private const val Z = 0x5a
    private const val D  = 0x44; private const val H = 0x48
    private const val C  = 0x43; private const val ZH = 0xde

    /**
     * Core sub_10009310: w = word byte list (read-only), a = attr byte list (mutated).
     * Both IntArrays hold unsigned byte values 0..255.
     */
    fun mark(w: IntArray, a: IntArray): IntArray {
        val cc = loadCC()
        var i = w.size - 1
        if (a[i] and 1 != 0) a[i] += 1
        while (i > 0) {
            // A1: skip trailing non-VS
            while (true) {
                if (w[i] in cc.VS) break
                if (i <= 0) break
                i--
            }
            // A2: skip the vowel cluster
            while (true) {
                if (w[i] !in cc.V) break
                if (i <= 0) break
                i--
            }
            // B: sonorant coda
            if (i > 0 && (a[i] and 2) == 0 && w[i] in cc.SON) i--
            // C: ZD / ŽD / HC cluster
            var didCluster = false
            if (i > 1 && (a[i] and 2) == 0) {
                val c2 = w[i]; val p = w[i - 1]
                if ((c2 == Z && p == D) || (c2 == ZH && p == D) || (c2 == H && p == C)) {
                    i -= 2; didCluster = true
                }
            }
            // D: obstruent coda
            if (!didCluster) {
                if (i > 0 && (a[i] and 2) == 0 && w[i] in cc.OBS) i--
            }
            // E: sibilant coda
            if (i > 0 && (a[i] and 2) == 0) {
                val c2 = w[i]
                when {
                    c2 in cc.SIB_S -> i--
                    c2 in cc.SIB_Z -> if (w[i - 1] != D) i--
                }
            }
            // mark the nucleus
            if (a[i] and 1 != 0) a[i] += 1
            if (w[i] == SP) {
                if (i > 0) i--
                if (a[i] and 1 != 0) a[i] += 1
            }
        }
        // substring (diphthong) table pass
        val wb = w.map { it.toByte() }.toByteArray()
        for (s in cc.SUBSTR) {
            val idx = indexOfSlice(wb, s)
            if (idx >= 0 && (a[idx] and 1) != 0) a[idx] += 1
        }
        return a
    }

    private fun indexOfSlice(haystack: ByteArray, needle: ByteArray): Int {
        if (needle.isEmpty()) return 0
        outer@ for (i in 0..haystack.size - needle.size) {
            for (j in needle.indices) if (haystack[i + j] != needle[j]) continue@outer
            return i
        }
        return -1
    }

    /**
     * Nucleus attr as KircTranskr produces it:
     * word buffer = '_' + token.uppercase() + ' ', attr init = all 1s, then mark().
     * Returns the attr int array; matcher reads (attr[pos] & 2) where pos is matcher word position.
     */
    fun kirchNucleus(token: String): IntArray {
        val buf = ("_" + token.uppercase() + " ").toByteArray(CP1257)
        val w = IntArray(buf.size) { buf[it].toInt() and 0xff }
        val a = IntArray(buf.size) { 1 }
        return mark(w, a)
    }
}
