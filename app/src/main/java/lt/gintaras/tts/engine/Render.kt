package lt.gintaras.tts.engine

import org.json.JSONArray

// Kotlin port of lt_tts/render.py
// Bit-for-bit port of transcr4.dll sub_1000aec0: the G2P + stress-casing renderer.

internal object Render {

    private val CP1257 = java.nio.charset.Charset.forName("windows-1257")

    // Renderer literal char-class sets (raw cp1257 byte values, from VAs 0x101db420/30/38/48)
    private val VOWELS: Set<Int> = setOf(0x41,0xc0,0x45,0xc6,0xcb,0x49,0x59,0xc1,0x4f,0x55,0xdb,0xd8)
    private val FRONT:  Set<Int> = setOf(0x45,0xc6,0xcb,0x49,0x59,0xc1)
    private val OBSTR:  Set<Int> = setOf(0x42,0x44,0x47,0x50,0x54,0x4b,0x53,0xd0,0x5a,0xde,0x43,0xc8)
    private val VOICED: Set<Int> = setOf(0x42,0x44,0x47,0x5a,0xde)

    private const val _J   = 0x4a  // 'J'
    private const val _NUL = 0

    // Special separator sequences (cp1257 encoded)
    private val INIT  = "\n".toByteArray(CP1257)    // _\n prefix
    private val SEP   = "\n".toByteArray(CP1257)
    private val TRAIL = "+\n".toByteArray(CP1257)
    private val FINAL = "_".toByteArray(CP1257)

    private data class Rule(
        val lc: IntArray, val mc: Int,
        val rs1: IntArray, val rs2: IntArray,
        val accmask: Int, val fl: Int, val b2: Int, val attr: Int,
        val out: IntArray, val adv: Int, val skip: Int
    )

    @Volatile private var rules: Array<Rule>? = null

    private fun loadRules(): Array<Rule> {
        rules?.let { return it }
        synchronized(this) {
            rules?.let { return it }
            val arr: JSONArray = Assets.jsonArray("render_rules.json")
            val out = Array(arr.length()) { i ->
                val r = arr.getJSONObject(i)
                Rule(
                    lc      = r.getString("lc").toByteArray(CP1257).map { it.toInt() and 0xff }.toIntArray(),
                    mc      = r.getInt("mc"),
                    rs1     = r.getString("rs1").toByteArray(CP1257).map { it.toInt() and 0xff }.toIntArray(),
                    rs2     = r.getString("rs2").toByteArray(CP1257).map { it.toInt() and 0xff }.toIntArray(),
                    accmask = r.getInt("accmask"),
                    fl      = r.getInt("fl"),
                    b2      = r.getInt("b2"),
                    attr    = r.getInt("attr"),
                    out     = r.getString("out").toByteArray(CP1257).map { it.toInt() and 0xff }.toIntArray(),
                    adv     = r.getInt("adv"),
                    skip    = r.getInt("skip")
                )
            }
            return out.also { rules = it }
        }
    }

    private fun accodeTrans(accode: IntArray, L: Int) {
        for (i in 1 until L) {
            val c = accode[i]
            if (c == 2) accode[i - 1] = 0x10
            accode[i] = when (c) {
                0xff -> 1
                0    -> 2
                1    -> 4
                else -> 8
            }
        }
    }

    private fun diphthongAttr(attr: IntArray, L: Int) {
        for (i in L - 2 downTo 0) {
            if (attr[i] and 2 != 0)
                attr[i + 1] += if (attr[i + 1] and 2 != 0) 4 else 3
        }
    }

    private fun buildB1(buf: IntArray, L: Int): IntArray {
        val B1 = IntArray(L)
        var cur = 2; var state = 1
        for (pos in L - 1 downTo 1) {
            val c = buf[pos]
            if (c in VOWELS && c != _NUL) { state = 1 }
            else if (state == 1) {
                val nxt = buf[pos + 1]
                cur = if ((nxt in FRONT && nxt != _NUL) || c == _J) 1 else 2
                state = 0
            }
            B1[pos] = cur
        }
        return B1
    }

    private fun buildB2(buf: IntArray, L: Int): IntArray {
        val B2 = IntArray(L)
        var value = 2; var state = 0
        for (pos in L - 1 downTo 1) {
            val c = buf[pos]
            if (c in OBSTR) {
                if (state == 1 && c != _NUL) { value = if (c in VOICED) 1 else 2; state = 0 }
            } else { state = 1 }
            B2[pos] = value
        }
        return B2
    }

    private fun transduce(buf: IntArray, accode: IntArray, attr: IntArray, accbyte: IntArray,
                          L: Int, B1: IntArray, B2: IntArray, rules: Array<Rule>): String? {
        val out = ByteArray(16384 + 64)
        var outLen = 0
        // write INIT prefix: "_\n"
        val initBytes = "_\n".toByteArray(CP1257)
        for (b in initBytes) out[outLen++] = b

        var pos = 1; var ri = 0
        while (pos < L) {
            val r = rules[ri]
            var matched = false
            if (r.mc != buf[pos]) {
                ri += r.skip
            } else {
                val skip = if (pos + 1 < buf.size && buf[pos + 1] == _NUL) 2 else 1
                var ok = true
                if (ok && r.lc.isNotEmpty() && r.lc[0] != 0 && buf[pos - 1] !in r.lc) ok = false
                if (ok && r.rs1.isNotEmpty() && r.rs1[0] != 0 && buf[pos + skip] !in r.rs1) ok = false
                if (ok && r.rs2.isNotEmpty() && r.rs2[0] != 0 && buf[pos + skip + 1] !in r.rs2) ok = false
                if (ok && (r.accmask and accode[pos]) == 0) ok = false
                if (ok && ((r.fl and 3) and B1[pos]) == 0) ok = false
                if (ok && (r.b2 and B2[pos]) == 0) ok = false
                if (ok) {
                    val m = (r.attr and attr[pos]) and 7
                    if (m != (attr[pos] and 7) && m != r.attr) ok = false
                }
                if (ok && (r.fl and 4) != 0 && accbyte[pos] == 0) ok = false
                if (ok) {
                    pos += r.adv
                    if (outLen < 16384 - 8) {
                        for (b in r.out) out[outLen++] = b.toByte()
                        if (r.out.isNotEmpty() && r.out[0] != 0) {
                            for (b in SEP) out[outLen++] = b
                        }
                    } else return null
                    ri = 0; matched = true
                }  else ri++
            }
            if (!matched && ri > 0x9b) { ri = 0; pos++ }
            if (pos < buf.size && buf[pos] == _NUL) {
                if (pos > 0 && (attr[pos - 1] and 8) != 0) {
                    for (b in TRAIL) out[outLen++] = b
                    pos++
                }
            }
        }
        for (b in FINAL) out[outLen++] = b
        return out.copyOf(outLen).toString(CP1257)
    }

    /**
     * Transcribe one PradApdZod'd uppercase cp1257 word to the phoneme token list,
     * applying `stress` = (pos1, type) from Accent.accent() (or null for unstressed).
     * Returns the token list (whitespace-split).
     */
    fun render(wordUpper: String, stress: Pair<Int, Int>?): List<String>? {
        val W = wordUpper.uppercase()
        val core = ("_" + W).toByteArray(CP1257)
        val L = core.size
        val buf = IntArray(L + 4) { if (it < L) core[it].toInt() and 0xff else 0 }
        val rawAttr = Nucleus.mark(
            IntArray(L) { buf[it] },
            IntArray(L) { 1 }
        )
        val attr = IntArray(L + 4) { if (it < rawAttr.size) rawAttr[it] else 0 }
        val accode = IntArray(L + 4) { 0xff }
        if (stress != null) {
            val (pos1, typ) = stress
            if (pos1 in 1 until L) accode[pos1] = typ
        }
        accodeTrans(accode, L)
        diphthongAttr(attr, L)
        // PART1/PART2 walk EXACTLY the L-long core (python render.py: _build_B1(buf, L)); they peek
        // buf[pos+1] one past the end, which is why buf carries the zero padding. Passing L+4 here
        // walked into the pad and overran the buffer -- the bug that crashed render() for EVERY OOV
        // word (the engine then silently fell back to the crude no-stress g2p).
        val B1 = buildB1(buf, L)
        val B2 = buildB2(buf, L)
        // PART3 pad: '_' at buf[0], '_' '_' '\0' at buf[L], buf[L+1], buf[L+2]
        buf[0] = 0x5f; buf[L] = 0x5f; buf[L + 1] = 0x5f; buf[L + 2] = 0
        val accbyte = IntArray(L + 4)
        val s = transduce(buf, accode, attr, accbyte, L, B1, B2, loadRules())
        return s?.split(Regex("\\s+"))?.filter { it.isNotEmpty() }
    }
}
