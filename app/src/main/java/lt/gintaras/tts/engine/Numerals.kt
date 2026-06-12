package lt.gintaras.tts.engine

// Kotlin port of lt_tts/numerals.py + number_data.py
// Number-to-Lithuanian-words expansion (PradApdZod-exact).

internal object Numerals {

    private val ONES = mapOf(
        0 to "nulis", 1 to "vienas", 2 to "du", 3 to "trys", 4 to "keturi",
        5 to "penki", 6 to "šeši", 7 to "septyni", 8 to "aštuoni", 9 to "devyni"
    )
    private val TEENS = mapOf(
        0 to "dešimt", 1 to "vienuolika", 2 to "dvylika", 3 to "trylika",
        4 to "keturiolika", 5 to "penkiolika", 6 to "šešiolika",
        7 to "septyniolika", 8 to "aštuoniolika", 9 to "devyniolika"
    )
    private val TENS = mapOf(
        2 to "dvidešimt", 3 to "trisdešimt", 4 to "keturiasdešimt",
        5 to "penkiasdešimt", 6 to "šešiasdešimt", 7 to "septyniasdešimt",
        8 to "aštuoniasdešimt", 9 to "devyniasdešimt"
    )
    private val HUNDREDS = mapOf(
        1 to "šimtas", 2 to "du šimtai", 3 to "trys šimtai", 4 to "keturi šimtai",
        5 to "penki šimtai", 6 to "šeši šimtai", 7 to "septyni šimtai",
        8 to "aštuoni šimtai", 9 to "devyni šimtai"
    )

    private data class Scale(
        val oneBare: String, val oneFull: String,
        val few: Map<Int, String>, val gen: String
    )

    private val SCALES = mapOf(
        3 to Scale("tūkstantis", "vienas tūkstantis",
            mapOf(2 to "du tūkstančiai", 3 to "trys tūkstančiai", 4 to "keturi tūkstančiai",
                5 to "penki tūkstančiai", 6 to "šeši tūkstančiai", 7 to "septyni tūkstančiai",
                8 to "aštuoni tūkstančiai", 9 to "devyni tūkstančiai"), "tūkstančių"),
        6 to Scale("milijonas", "vienas milijonas",
            mapOf(2 to "du milijonai", 3 to "trys milijonai", 4 to "keturi milijonai",
                5 to "penki milijonai", 6 to "šeši milijonai", 7 to "septyni milijonai",
                8 to "aštuoni milijonai", 9 to "devyni milijonai"), "milijonų"),
        9 to Scale("milijardas", "vienas milijardas",
            mapOf(2 to "du milijardai", 3 to "trys milijardai", 4 to "keturi milijardai",
                5 to "penki milijardai", 6 to "šeši milijardai", 7 to "septyni milijardai",
                8 to "aštuoni milijardai", 9 to "devyni milijardai"), "milijardų")
    )

    private fun groupWords(h: Int, t: Int, u: Int, b: Int): List<String> {
        val out = mutableListOf<String>()
        if (h == 0 && t == 0 && u == 0) return out
        if (h > 0) out.add(HUNDREDS[h]!!)
        val sc = SCALES[b]
        if (t == 1) {
            out.add(TEENS[u]!! + if (sc != null) " ${sc.gen}" else "")
        } else {
            if (t >= 2) out.add(TENS[t]!!)
            if (u > 0) {
                if (sc == null) {
                    out.add(ONES[u]!!)
                } else if (u == 1) {
                    out.add(if (t == 0 && h == 0) sc.oneBare else sc.oneFull)
                } else {
                    out.add(sc.few[u]!!)
                }
            } else if (sc != null && (h > 0 || t >= 2)) {
                out.add(sc.gen)
            }
        }
        return out
    }

    fun toWords(n: String): String? {
        val s = n.trim().trimStart('+')
        if (s.isEmpty() || !s.all { it.isDigit() }) return null
        // LEADING ZEROS are spoken, one 'nulis' each, exactly like PradApdZod ('01' -> nulis vienas,
        // '0023' -> nulis nulis dvidešimt trys, '00' -> nulis nulis); zeros INSIDE the number stay
        // positional as before ('100' -> šimtas, '10' -> dešimt).
        val nz = s.trimStart('0')
        val pre = List(s.length - nz.length) { ONES[0]!! }
        if (nz.isEmpty()) return pre.joinToString(" ")
        val digits = nz.map { it - '0' }
        val ng = (digits.size + 2) / 3
        val padded = IntArray(ng * 3 - digits.size) { 0 }.toMutableList() + digits
        val words = pre.toMutableList()
        for (gi in 0 until ng) {
            val h = padded[gi * 3]; val t = padded[gi * 3 + 1]; val u = padded[gi * 3 + 2]
            val b = 3 * (ng - 1 - gi)
            words += groupWords(h, t, u, b)
        }
        return words.joinToString(" ")
    }

    private val DIGIT_RE = Regex("""\d+""")

    fun expandText(text: String): String =
        DIGIT_RE.replace(text) { toWords(it.value) ?: it.value }
}
