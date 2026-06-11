package lt.gintaras.tts.engine

// Kotlin port of lt_tts/duration.py
// Bit-exact port of transcr4.dll `ilgiai` export (sub_10001090).

internal object Duration {

    // 93-entry duration table @0x1000d960 (name, f1=base/long, f2=floor/short)
    private val TABLE = arrayOf(
        Triple("_",100,33), Triple("+",0,0), Triple("i",68,25), Triple("e",70,26),
        Triple("a",68,25), Triple("o",70,26), Triple("u",66,24), Triple("I",75,28),
        Triple("E",79,29), Triple("A",78,29), Triple("O",89,33), Triple("U",75,28),
        Triple("ii",115,43), Triple("Ii",107,40), Triple("iI",119,44), Triple("ie",122,55),
        Triple("Ie",131,59), Triple("iE",135,61), Triple("ee",114,42), Triple("Ee",119,44),
        Triple("eE",124,46), Triple("ea",115,43), Triple("Ea",113,42), Triple("eA",118,44),
        Triple("aa",112,41), Triple("Aa",110,41), Triple("aA",116,43), Triple("oo",112,41),
        Triple("Oo",117,43), Triple("oO",123,46), Triple("uo",115,52), Triple("Uo",112,50),
        Triple("uO",121,54), Triple("uu",112,41), Triple("Uu",107,40), Triple("uU",117,43),
        Triple("p",92,83), Triple("p'",88,79), Triple("b",80,72), Triple("b'",77,69),
        Triple("t",87,78), Triple("t'",90,81), Triple("d",74,67), Triple("d'",71,64),
        Triple("k",93,84), Triple("k'",100,90), Triple("g",78,70), Triple("g'",76,68),
        Triple("ts",111,100), Triple("ts'",111,100), Triple("dz",96,86), Triple("dz'",97,87),
        Triple("tS",109,98), Triple("tS'",111,100), Triple("dZ",100,86), Triple("dZ'",92,79),
        Triple("s",108,97), Triple("s'",113,102), Triple("z",81,73), Triple("z'",85,77),
        Triple("S",105,95), Triple("S'",106,95), Triple("Z",75,68), Triple("Z'",80,72),
        Triple("x",105,95), Triple("x'",99,89), Triple("h",76,68), Triple("h'",76,68),
        Triple("f",98,88), Triple("f'",97,87), Triple("j'",72,27), Triple("j",77,28),
        Triple("J",86,32), Triple("v",72,65), Triple("v'",65,59), Triple("w",84,31),
        Triple("W",97,36), Triple("l",77,28), Triple("l'",74,27), Triple("L",112,41),
        Triple("L'",113,42), Triple("r",74,67), Triple("r'",68,61), Triple("R",92,83),
        Triple("R'",76,68), Triple("m",81,30), Triple("m'",76,28), Triple("M",122,45),
        Triple("M'",122,45), Triple("n",77,28), Triple("n'",77,28), Triple("N",135,50),
        Triple("N'",136,50)
    )

    private val IDX: Map<String, Int> = TABLE.mapIndexed { i, (nm, _, _) -> nm to i }.toMap()

    // strchr() character classes (first byte of a token's table name)
    // 0xeb=ė (cp1257) U+0117; 0xcb=Ė (cp1257) U+0116
    private val VOW  = setOf('a','e','i','o','u','A','E','I','O','U','ė','Ė')
    private val VOWB = VOW + setOf('_')
    private val SON  = setOf('b','d','g','z','Z','j','l','m','n','r','v','w','h')

    // x87 fmul constants
    private const val M_VV    = 1.25
    private const val M_pSON  = 1.22
    private const val M_pVOW  = 1.35
    private const val M_CC    = 0.87
    private const val M_CEND  = 1.26
    private const val M_VEND  = 1.31
    private const val M_VOPEN = 1.68
    private const val SP_POS  = 10.0
    private const val SP_NEG  = 5.0

    private fun firstChar(arr: IntArray, c: Int): Char {
        if (c < 0 || c >= arr.size) return '_'
        return TABLE[arr[c]].first[0]
    }

    private fun factor(arr: IntArray, c: Int): Double {
        var f = 1.0
        val cur  = firstChar(arr, c)
        val nx   = firstChar(arr, c + 1)
        val nx2  = firstChar(arr, c + 2)
        val nx3  = firstChar(arr, c + 3)
        val pv   = firstChar(arr, c - 1)
        val curV = cur in VOW
        // A: vowel + following vowel
        if (curV && nx in VOW) f *= M_VV
        // B: vowel preceded by sonorant / vowel
        if (curV && c > 0) {
            if (pv in SON) f *= M_pSON
            else if (pv in VOW) f *= M_pVOW
        }
        // C: vowel before >=2 consonant cluster
        if (curV && nx !in VOWB && nx2 !in VOWB) f *= M_CC
        // D: consonant inside a cluster
        if (cur !in VOWB) {
            if (c > 0 && pv !in VOWB) f *= M_CC
            else if (nx !in VOWB) f *= M_CC
        }
        // E: consonant at word end
        if (cur !in VOWB) {
            if (nx == '_' || (nx !in VOWB && nx2 == '_')) f *= M_CEND
        }
        // F: vowel in final closed syllable
        if (curV) {
            if ((nx !in VOWB && nx2 == '_') ||
                (nx !in VOWB && nx2 !in VOWB && nx3 == '_')) f *= M_VEND
        }
        // G: word-final open vowel
        if (curV && nx == '_') f *= M_VOPEN
        return f
    }

    private fun speedFactor(speed: Int): Double {
        val spd = speed.coerceIn(-10, 10)
        return if (spd > 0) (SP_POS - spd) / SP_POS else (SP_NEG - spd) / SP_NEG
    }

    /**
     * Returns list of (token, duration) reproducing transcr4 ilgiai.
     * `tokens` = KircTranskr phoneme token list (may include "+" boundary markers and "_" boundaries).
     * speed = -10..10, 0 = neutral.
     */
    fun ilgiai(tokens: List<String>, speed: Int = 0): List<Pair<String, Int>> {
        // split '+' markers off: arr = phoneme indices; mark[k]=true if '+' followed phoneme k
        val arr = mutableListOf<Int>()
        val mark = mutableListOf<Boolean>()
        for (t in tokens) {
            if (t == "+") {
                if (mark.isNotEmpty()) mark[mark.size - 1] = true
                continue
            }
            arr.add(IDX[t] ?: 0)  // unknown -> '_' (index 0)
            mark.add(false)
        }
        val sf = speedFactor(speed)
        val out = mutableListOf<Pair<String, Int>>()
        for (c in arr.indices) {
            val (nm, f1, f2) = TABLE[arr[c]]
            val dur = (factor(arr.toIntArray(), c) * sf * (f1 - f2) + 0.5 + f2).toInt()
            out.add(Pair(nm, dur))
            if (mark[c]) out.add(Pair("+", 0))
        }
        return out
    }
}
