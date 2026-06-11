package lt.gintaras.tts.engine

// Faithful Kotlin port of lt_tts/accent.py — transcr4.dll lexical stress engine (kirčiavimas).
// See Python source for full RE map and algorithm description.
//
// Entry point: Accent.accent(word) -> Pair<Int,Int>? where first=1-based pos, second=type.
// type 1=circumflex (tvirtagalė), type 2=acute (tvirtapradė), 0=short-final.

import org.json.JSONArray
import org.json.JSONObject
import java.nio.charset.Charset

private val CP1257: Charset = Charset.forName("windows-1257")

// ---- cp1257 char helpers -----------------------------------------------------------------------

private fun cp(b: Int): Char = byteArrayOf(b.toByte()).toString(CP1257)[0]

// cp1257 char constants used by the candidate-augmentation stage.
private val CH_S  = cp(0x53)   // S
private val CH_Z  = cp(0x5a)   // Z
private val CH_SH = cp(0xd0)   // Š
private val CH_ZH = cp(0xde)   // Ž
private val CH_K  = 'K'
private val CH_G  = 'G'
private val CH_Y  = 'Y'
private val CH_UU = cp(0xdb)   // Ū
private val CH_C  = cp(0xc8)   // Č
private val CH_T  = 'T'
private val CH_D  = 'D'
private val CH_I  = 'I'
private val CH_U  = 'U'

private val S90bc: Set<Char> = setOf(cp(65), cp(192), cp(79), cp(85), cp(219), cp(216))  // A Ą O U Ū Ų
private val S90c4: Set<Char> = setOf(cp(65), cp(69), cp(85), cp(79))                      // A E U O
private val S90cc: Set<Char> = setOf(cp(65), cp(69), cp(79))                              // A E O

// ---- Data structures ---------------------------------------------------------------------------

// Verb tables
private data class AccentTables(
    val endings:  List<Pair<String, List<Int>>>,
    val prefixes: List<Pair<String?, List<Int>>>,
    val stems:    List<Triple<String?, List<Pair<String?, List<Int>>>, List<Int>>>,
    val cc:       Map<String, Set<Char>>
)

// Noun tables
private data class NounTables(
    val main: List<Pair<String?, List<Int>>>,
    val ends: List<Pair<String?, List<Int>>>,
    val bb0:  List<Int>,
    val c60:  List<Int>,
    val da:   List<List<Triple<Int, Int, Int>>>,
    val t17:  List<List<Int>>,
    val idx:  Map<String, List<Pair<Int, List<Int>>>>  // rev_stem(cp1257 string) -> [(midx, bytes8)]
)

private data class DllClasses(
    val starClasses: Map<String, Set<Int>>,
    val nounWalkCc:  Map<String, Set<Int>>
)

// ---- Lazy singletons ---------------------------------------------------------------------------

@Volatile private var _at: AccentTables? = null
@Volatile private var _nt: NounTables? = null
@Volatile private var _frn: Map<String, List<List<Int>>>? = null   // cp1257-encoded form string -> acc lists
@Volatile private var _dll: DllClasses? = null
@Volatile private var _mainStemb: List<ByteArray>? = null
@Volatile private var _verbStemb: List<ByteArray>? = null
@Volatile private var _pfxb: List<ByteArray>? = null
@Volatile private var _starEntries: List<Triple<ByteArray, ByteArray, List<Int>>>? = null

// ---- JSON helpers ------------------------------------------------------------------------------

private fun JSONArray.toIntList(): List<Int> = (0 until length()).map { getInt(it) }

// ---- tables() ----------------------------------------------------------------------------------

private fun tables(): AccentTables {
    _at?.let { return it }
    return synchronized(Accent) {
        _at ?: run {
            // accent_endings.json: [[rev_ending, [f0..f7]], ...]
            val endArr = Assets.jsonArray("accent_endings.json")
            val endings = (0 until endArr.length()).map { i ->
                val row = endArr.getJSONArray(i)
                val s = if (row.isNull(0)) "" else row.getString(0)
                val fl = row.getJSONArray(1).toIntList()
                s to fl
            }
            // accent_prefixes.json: [[prefix_or_null, [f0..f3]], ...]
            val pfxArr = Assets.jsonArray("accent_prefixes.json")
            val prefixes = (0 until pfxArr.length()).map { i ->
                val row = pfxArr.getJSONArray(i)
                val s = if (row.isNull(0)) null else row.getString(0)
                val fl = row.getJSONArray(1).toIntList()
                s to fl
            }
            // stem_lexicon.json: [[stem_or_null, [[field_or_null, acc4], [f,a4], [f,a4]], flag8], ...]
            val stemArr = Assets.jsonArray("stem_lexicon.json")
            val stems = (0 until stemArr.length()).map { i ->
                val row = stemArr.getJSONArray(i)
                val stem = if (row.isNull(0)) null else row.getString(0)
                val grpArr = row.getJSONArray(1)
                val groups = (0 until grpArr.length()).map { gi ->
                    val g = grpArr.getJSONArray(gi)
                    val field = if (g.isNull(0)) null else g.getString(0)
                    val acc = g.getJSONArray(1).toIntList()
                    field to acc
                }
                val flag = row.getJSONArray(2).toIntList()
                Triple(stem, groups, flag)
            }
            // accent_charclasses.json: { name: [cp1257_byte, ...], ... }
            val ccObj = Assets.json("accent_charclasses.json")
            val cc = mutableMapOf<String, Set<Char>>()
            for (key in ccObj.keys()) {
                val arr = ccObj.getJSONArray(key)
                val bytes = ByteArray(arr.length()) { arr.getInt(it).toByte() }
                cc[key] = bytes.toString(CP1257).toSet()
            }
            AccentTables(endings, prefixes, stems, cc).also { _at = it }
        }
    }
}

// ---- nounTables() ------------------------------------------------------------------------------

private fun nounTables(): NounTables {
    _nt?.let { return it }
    return synchronized(Accent) {
        _nt ?: run {
            // main_lexicon.json: [[rev_stem_or_null, [b0..b7]], ...]
            val mainArr = Assets.jsonArray("main_lexicon.json")
            val main = (0 until mainArr.length()).map { i ->
                val row = mainArr.getJSONArray(i)
                val s = if (row.isNull(0)) null else row.getString(0)
                val b = row.getJSONArray(1).toIntList()
                s to b
            }
            // main_endings.json: [[rev_ending_or_null, [f0..f3]], ...]
            val endsArr = Assets.jsonArray("main_endings.json")
            val ends = (0 until endsArr.length()).map { i ->
                val row = endsArr.getJSONArray(i)
                val s = if (row.isNull(0)) null else row.getString(0)
                val fl = row.getJSONArray(1).toIntList()
                s to fl
            }
            // noun_accent_tables.json: { bb0: [...], c60: [...], da: [...], t17: [...] }
            val nt = Assets.json("noun_accent_tables.json")
            val bb0 = nt.getJSONArray("bb0").toIntList()
            val c60 = nt.getJSONArray("c60").toIntList()
            // da: list of lists of [col, da5, eidx] triples; terminates with eidx==-1
            val daArr = nt.getJSONArray("da")
            val da = (0 until daArr.length()).map { i ->
                val rowArr = daArr.getJSONArray(i)
                (0 until rowArr.length()).map { j ->
                    val t = rowArr.getJSONArray(j)
                    Triple(t.getInt(0), t.getInt(1), t.getInt(2))
                }
            }
            // t17: list of lists of ints (valid classes per digit2)
            val t17Arr = nt.getJSONArray("t17")
            val t17 = (0 until t17Arr.length()).map { t17Arr.getJSONArray(it).toIntList() }
            // Build idx: rev_stem (as cp1257-round-tripped string key) -> [(midx, bytes8)]
            val idx = mutableMapOf<String, MutableList<Pair<Int, List<Int>>>>()
            for (i in main.indices) {
                val s = main[i].first ?: continue
                // key is the cp1257-encoded bytes round-tripped back as a Latin-1 key
                val key = s.toByteArray(CP1257).toString(Charsets.ISO_8859_1)
                idx.getOrPut(key) { mutableListOf() }.add(i to main[i].second)
            }
            NounTables(main, ends, bb0, c60, da, t17, idx).also { _nt = it }
        }
    }
}

// ---- foreignLex() ------------------------------------------------------------------------------

private fun foreignLex(): Map<String, List<List<Int>>> {
    _frn?.let { return it }
    return synchronized(Accent) {
        _frn ?: run {
            val arr = Assets.jsonArray("foreign_lexicon.json")
            val map = mutableMapOf<String, MutableList<List<Int>>>()
            for (i in 0 until arr.length()) {
                val row = arr.getJSONArray(i)
                if (row.isNull(0)) continue
                val form = row.getString(0)
                val acc = row.getJSONArray(1).toIntList()
                // key = cp1257 bytes as ISO-8859-1 string (byte-identity key)
                val key = form.toByteArray(CP1257).toString(Charsets.ISO_8859_1)
                map.getOrPut(key) { mutableListOf() }.add(acc)
            }
            (map as Map<String, List<List<Int>>>).also { _frn = it }
        }
    }
}

// ---- dllClasses() ------------------------------------------------------------------------------

private fun dllClasses(): DllClasses {
    _dll?.let { return it }
    return synchronized(Accent) {
        _dll ?: run {
            val obj = Assets.json("accent_dllclasses.json")
            fun parseByteSetMap(o: JSONObject): Map<String, Set<Int>> {
                val m = mutableMapOf<String, Set<Int>>()
                for (k in o.keys()) {
                    val arr = o.getJSONArray(k)
                    m[k] = (0 until arr.length()).map { arr.getInt(it) }.toSet()
                }
                return m
            }
            val sc = parseByteSetMap(obj.getJSONObject("star_classes"))
            val nw = parseByteSetMap(obj.getJSONObject("noun_walk_cc"))
            DllClasses(sc, nw).also { _dll = it }
        }
    }
}

private fun starClasses(): Map<String, Set<Int>> = dllClasses().starClasses
private fun nounWalkCc(): Map<String, Set<Int>> = dllClasses().nounWalkCc

// ---- Stem byte-array caches --------------------------------------------------------------------

private fun mainStemb(): List<ByteArray> {
    _mainStemb?.let { return it }
    return synchronized(Accent) {
        _mainStemb ?: run {
            nounTables().main.map { (s, _) ->
                if (s == null) byteArrayOf() else s.toByteArray(CP1257)
            }.also { _mainStemb = it }
        }
    }
}

private fun verbStemb(): List<ByteArray> {
    _verbStemb?.let { return it }
    return synchronized(Accent) {
        _verbStemb ?: run {
            tables().stems.map { (s, _, _) ->
                if (s == null) byteArrayOf() else s.toByteArray(CP1257)
            }.also { _verbStemb = it }
        }
    }
}

private fun pfxb(): List<ByteArray> {
    _pfxb?.let { return it }
    return synchronized(Accent) {
        _pfxb ?: run {
            tables().prefixes.map { (p, _) ->
                if (p == null) byteArrayOf() else p.toByteArray(CP1257)
            }.also { _pfxb = it }
        }
    }
}

// ---- '*'-tail entries cache -------------------------------------------------------------------

private fun starEntries(): List<Triple<ByteArray, ByteArray, List<Int>>> {
    _starEntries?.let { return it }
    return synchronized(Accent) {
        _starEntries ?: run {
            val list = mutableListOf<Triple<ByteArray, ByteArray, List<Int>>>()
            for ((s, b) in nounTables().main) {
                if (s != null && '*' in s) {
                    val parts = s.split('*', limit = 2)
                    val pre = parts[0].toByteArray(CP1257)
                    val dg  = parts[1].toByteArray(CP1257)
                    list.add(Triple(pre, dg, b))
                }
            }
            list.also { _starEntries = it }
        }
    }
}

// ---- priority helper ---------------------------------------------------------------------------

private fun prio(idx: Int): Int {
    val sb = mainStemb()[idx]
    val sp = sb.indexOf(0x2a.toByte())
    if (sp < 0) return 0x1e
    val after = if (sp + 1 < sb.size) sb[sp + 1].toInt() and 0xff else 0
    return if (after == 0x34) sp else sp + 0x14
}

// ---- ByteArray helpers -------------------------------------------------------------------------

private fun ByteArray.indexOf(b: Byte): Int {
    for (i in indices) if (this[i] == b) return i
    return -1
}

private fun ByteArray.startsWith(prefix: ByteArray): Boolean {
    if (prefix.size > size) return false
    for (i in prefix.indices) if (this[i] != prefix[i]) return false
    return true
}

private fun cmpBytes(a: ByteArray, b: ByteArray): Int {
    val n = minOf(a.size, b.size)
    for (i in 0 until n) {
        val d = (a[i].toInt() and 0xff) - (b[i].toInt() and 0xff)
        if (d != 0) return if (d > 0) 1 else -1
    }
    return a.size.compareTo(b.size)
}

// ---- _bisect_a9a -------------------------------------------------------------------------------

/** Port of sub_10001A9A: 2-3 level strcmp bisect over main_lexicon[0..0xed2e]. */
private fun bisectA9a(key: ByteArray): Pair<Int, Int> {
    val stb = mainStemb()
    fun cmp(i: Int): Int = cmpBytes(stb[i], key)
    var lo = 0; var hi = 0xed2e
    val mid = (lo + hi) / 2
    val c = cmp(mid)
    when {
        c > 0 -> hi = mid
        c < 0 -> lo = mid
        else  -> {
            val m2 = (lo + mid) / 2
            if (cmp(m2) < 0) lo = m2
            val m3 = (hi + mid) / 2
            if (cmp(m3) > 0) hi = m3
        }
    }
    return lo to hi
}

// ---- _verb_bracket -----------------------------------------------------------------------------

/** Port of sub_1000187F: two-phase bracket over 0x2276 forward verb stems. */
private fun verbBracket(cand: ByteArray): Pair<Int, Int> {
    val vstb = verbStemb()
    val c0 = if (cand.isNotEmpty()) cand[0].toInt() and 0xff else 0
    var lo = 0; var hi = 0x226f
    // phase 1: first-char bracket
    while (true) {
        val lo0 = lo; val hi0 = hi
        val mid = (lo + hi) shr 1
        val sc = if (vstb[mid].isNotEmpty()) vstb[mid][0].toInt() and 0xff else 0
        if (sc >= c0) hi = mid else lo = mid
        if (!(lo0 + 2 < lo || hi + 2 < hi0)) break
    }
    val saveLo = lo; hi = 0x226f
    // phase 2: strcmp refine
    while (true) {
        val lo0 = lo; val hi0 = hi
        val mid = (lo + hi) shr 1
        val r = cmpBytes(vstb[mid], cand)
        when {
            r > 0 -> hi = mid
            r < 0 -> lo = mid
            else  -> {
                val m2 = (lo0 + mid) shr 1
                if (cmpBytes(vstb[m2], cand) < 0) lo = m2
                val m3 = (hi0 + mid) shr 1
                if (cmpBytes(vstb[m3], cand) > 0) hi = m3
            }
        }
        if (!(lo0 + 2 < lo || hi + 2 < hi0)) break
    }
    return saveLo to hi
}

// ---- Foreign-walk byte sets --------------------------------------------------------------------

// cp1257 byte integers — vowels and related sets used in _foreign_walk
private val FV:     Set<Int> = setOf(65,192,69,198,203,73,89,193,79,85,219,216) // A Ą E Č Ė I Y Į O U Ū Ų
private val FVL:    Set<Int> = FV + setOf(76, 77, 78, 82)                        // + L M N R
private val FAEIUO: Set<Int> = setOf(65, 69, 73, 85, 79)
private val FLMNR:  Set<Int> = setOf(76, 77, 78, 82)
private val FUIO:   Set<Int> = setOf(85, 73, 79)
private val FAEOU:  Set<Int> = setOf(65, 69, 79, 85)

// ---- _VB_VOW (for _star_case123 PER counting) --------------------------------------------------

private val VB_VOW: Set<Int> = setOf(65,192,69,198,203,73,89,193,79,85,219,216)

// ---- _STRCHR_BDG (noun mode-2 digraph table) ---------------------------------------------------

private val STRCHR_BDG: Set<Int> = setOf(
    'B'.code,'D'.code,'G'.code,'K'.code,'P'.code,'T'.code,'C'.code,
    0xc8,  // Č
    'F'.code,'H'.code
)

// ---- Noun expand/filter tables -----------------------------------------------------------------

private val NOUN_EXPAND: Map<Int, List<Int>> = mapOf(
    0x10 to listOf(0x11, 0x1c, 0x1d),
    0x25 to listOf(0x26, 0x1c),
    0x12 to listOf(0x13, 0x1e, 0x1f),
    0x14 to listOf(0x15, 0x20, 0x1f),
    0x16 to listOf(0x17, 0x20, 0x1f),
    0x18 to listOf(0x19),
    0x27 to listOf(0x28),
    0x1a to listOf(0x1b, 0x21, 0x1f),
    0x29 to listOf(0x2a, 0x2b, 0x1d)
)
private val NOUN_FILTER: Set<Int> = setOf(0x10,0x12,0x14,0x16,0x18,0x1a,0x25,0x27,0x29)

private val PRIO_VERB = 30
private val PRIO_NOUN = 30

// ================================================================================================
internal object Accent {

    // ---- _candidates ---------------------------------------------------------------------------

    /**
     * Phase B+C+D: return accepted results as maps with keys:
     * prefix_idx, vidx, eidx, eflags, group, aug
     */
    private fun candidates(word: String): List<Map<String, Any>> {
        val T = tables()
        val endings = T.endings; val prefixes = T.prefixes; val stems = T.stems; val cc = T.cc
        val rev = word.reversed()
        val L = word.length
        val out = mutableListOf<Map<String, Any>>()
        for ((eidx, ep) in endings.withIndex()) {
            val (estr, flg) = ep
            if (estr.isEmpty() || !rev.startsWith(estr)) continue
            val elen = estr.length
            if (elen >= L) continue
            // hiatus check
            val cbar = rev[elen]
            if (cbar in (cc["AEIU_short"] ?: emptySet<Char>()) &&
                rev[elen - 1] in (cc["hiatus_b"] ?: emptySet<Char>())) continue
            val stemlen = L - elen
            var cand0 = word.substring(0, stemlen)
            val g = flg[0]
            val last = estr.last()   // 1st char of forward suffix
            // Č->T / DŽ->D restore
            if (last == CH_I && elen >= 2 && estr[estr.length - 2] in S90bc) {
                if (cand0.isNotEmpty() && cand0.last() == CH_C) {
                    cand0 = cand0.dropLast(1) + CH_T
                } else if (cand0.length >= 2 && cand0.last() == CH_ZH && cand0[cand0.length - 2] == CH_D) {
                    cand0 = cand0.dropLast(1)
                }
            }
            // array-A
            val arrayA: MutableList<Pair<String, Int>>
            if (last == CH_SH) {
                arrayA = mutableListOf(cand0 + CH_SH to 0, cand0 + CH_ZH to 0)
            } else if (last == CH_S) {
                arrayA = mutableListOf(cand0 to 0, cand0 + CH_S to 0, cand0 + CH_Z to 0)
                if (cand0.isNotEmpty() && cand0.last() == CH_I) {
                    if (cand0.length < 2 || cand0[cand0.length - 2] !in S90c4)
                        arrayA.add(cand0.dropLast(1) + CH_Y to 1)
                } else if (cand0.isNotEmpty() && cand0.last() == CH_U) {
                    if (cand0.length < 2 || cand0[cand0.length - 2] !in S90cc)
                        arrayA.add(cand0.dropLast(1) + CH_UU to 1)
                }
            } else if (last == CH_K) {
                arrayA = mutableListOf(cand0 to 0, cand0 + CH_K to 0, cand0 + CH_G to 0)
            } else {
                arrayA = mutableListOf(cand0 to 0)
            }
            // array-B + lexicon scan
            for ((cstem, aug) in arrayA) {
                val variants = mutableListOf<Triple<Int, String, Int>>()
                if (flg[1] == 0) {
                    for (pi in 1 until prefixes.size) {
                        val p = prefixes[pi].first ?: continue
                        if (flg[6] != 1 && p.length >= 2 && p.substring(0,2) == "TE" &&
                            !(p.length >= 3 && p.substring(0,3) == "TEB")) continue
                        if (p.length < cstem.length && cstem.startsWith(p))
                            variants.add(Triple(pi, cstem.substring(p.length), aug))
                    }
                }
                variants.add(Triple(0, cstem, aug))
                for ((pidx, stem, augf) in variants) {
                    for ((vidx, entry) in stems.withIndex()) {
                        val (vstem, groups, vflag) = entry
                        if (vstem == null || !stem.startsWith(vstem)) continue
                        val field = groups[g].first ?: ""
                        if (stem.substring(vstem.length) != field) continue
                        if ((vflag[0] and flg[2]) == 0) continue
                        if (augf != 0) {
                            if (vflag[3] != 0) continue
                            val g0 = groups[0].first ?: ""
                            val g1 = groups[1].first ?: ""
                            if (!((g0.getOrElse(0) {'?'} == CH_Y && g1.getOrElse(0) {'?'} == CH_I) ||
                                  (g0.getOrElse(0) {'?'} == CH_UU && g1.getOrElse(0) {'?'} == CH_U)))
                                continue
                        }
                        out.add(mapOf(
                            "prefix_idx" to pidx, "vidx" to vidx, "eidx" to eidx,
                            "eflags" to flg, "group" to g, "aug" to augf
                        ))
                    }
                }
            }
        }
        return out
    }

    // ---- _mode ---------------------------------------------------------------------------------

    /**
     * Dispatch on ending flag[5] -> Pair(mode {0,1,2}, forced_type: Int or -1 for None).
     * Returns (mode, forcedType) where forcedType==-1 means "None"/not forced.
     */
    private fun mode(res: Map<String, Any>): Pair<Int, Int> {
        val T = tables(); val stems = T.stems
        @Suppress("UNCHECKED_CAST")
        val eflags = res["eflags"] as List<Int>
        val f5 = eflags[5]
        val vidx = res["vidx"] as Int
        val (stem, groups, vflag) = stems[vidx]
        val g0a0 = groups[0].second[0]; val g0a1 = groups[0].second[1]
        val g1a0 = groups[1].second[0]; val g1a1 = groups[1].second[1]
        val g2a0 = groups[2].second[0]; val g2a1 = groups[2].second[1]
        val e08c = vflag[0]; val e08d = vflag[1]; val e08e = vflag[2]; val e08f = vflag[3]
        val g0field = groups[0].first ?: ""; val g2field = groups[2].first ?: ""
        val hasPfx = (res["prefix_idx"] as Int) != 0
        // TRUE retraction prefix (flag[0]==1) -> always mode 2
        if (hasPfx && T.prefixes[res["prefix_idx"] as Int].second[0] == 1) return 2 to -1
        if (f5 == 0) {
            if (hasPfx && e08d != 0) return 2 to -1
            if (g0a0 == 1 && g0a1 != 1) return 0 to -1
            return 1 to -1
        }
        if (f5 == 1) {
            if (!hasPfx) return 1 to -1
            return if (e08d != 0) 2 to -1 else 1 to -1
        }
        if (f5 == 2) return if (e08e == 0) 0 to -1 else 1 to -1
        if (f5 == 3) return 1 to -1
        if (f5 == 4) return if (e08d != 0) 0 to -1 else 1 to -1
        if (f5 == 5) return if (e08e == 0 && (e08c and 0x1f) != 0) 0 to -1 else 1 to -1
        if (f5 == 6) {
            if (e08e != 0) return 1 to -1
            if ((e08c and 0x1f) == 0) return 1 to -1
            if (g0a1 == 0) return 0 to -1
            if (g0a1 != 2) return 1 to -1
            val cc = (stem ?: "") + g0field
            val cc4 = cc.takeLast(4)
            val p0 = cc4.getOrElse(0) { '\u0000' }; val p1 = cc4.getOrElse(1) { '\u0000' }; val p2 = cc4.getOrElse(2) { '\u0000' }
            if (p1 != 'I') {
                if (p2 == 'A' || p2 == 'E') return 0 to -1
            }
            if (p0 == 'I') return 1 to -1
            if (p1 == 'A' || p1 == 'E') {
                return if (p2 in "IULMNR") 1 to -1 else 0 to -1
            }
            return 1 to -1
        }
        if (f5 == 7) {
            if (hasPfx && (e08c and 0xa) != 0 && g1a1 != 1) return 2 to -1
            if (g1a0 == 1 && g1a1 != 1) return 0 to -1
            return 1 to -1
        }
        if (f5 == 8) {
            if (hasPfx && (e08c and 0xa) != 0 && g1a1 != 1) return 2 to -1
            return 1 to -1
        }
        if (f5 == 9) return 1 to -1
        if (f5 == 10) return 1 to -1
        if (f5 == 11) return if (e08f == 0) 0 to -1 else 1 to -1
        if (f5 == 12) return if (e08f == 0 && g2a1 != 1) 0 to -1 else 1 to -1
        if (f5 == 13) {
            if (e08f != 0) return 1 to -1
            if (g2a1 == 1) return 1 to -1
            if (hasPfx) return 2 to -1
            if (g2a1 != 0) return 1 to -1
            val ft = if ('A' in g2field || 'E' in g2field || 'A' in (stem ?: "") || 'E' in (stem ?: "")) 2 else -1
            return 1 to ft
        }
        if (f5 == 15) {
            if (e08f != 0) return 1 to -1
            if (g2a1 == 1) return 1 to -1
            if (hasPfx) return 2 to -1
            return 0 to -1
        }
        if (f5 == 14) {
            if ((res["aug"] as? Int ?: 0) != 0) return 1 to 0
            if (g2a0 == 1 && g2a1 == 1) return 1 to 2
            return 1 to -1
        }
        return 1 to -1
    }

    // ---- _mode1 --------------------------------------------------------------------------------

    /**
     * MODE 1 retraction walk. Returns (pos1, type) or null. forcedType==-1 means not forced.
     */
    private fun mode1(word: String, res: Map<String, Any>, forcedType: Int = -1): Pair<Int, Int>? {
        val T = tables(); val cc = T.cc; val stems = T.stems
        val eidx = res["eidx"] as Int; val g = res["group"] as Int; val vidx = res["vidx"] as Int
        val estr = T.endings[eidx].first
        val (_, groups, vflag) = stems[vidx]
        val acc = groups[g].second
        var typ = if (forcedType != -1) forcedType else acc[1]
        val L = word.length
        val nuc = Nucleus.kirchNucleus(word)
        // e090 stem-diphthong marks
        val e090 = vflag[4]
        val prefixLen = (T.prefixes[res["prefix_idx"] as Int].first ?: "").length
        var bitcnt = 0
        val vowelsD8 = cc["vowels_d8"] ?: emptySet()
        val vowelsE8 = cc["vowels_e8"] ?: emptySet()
        for (pos2 in prefixLen until L - 1 - estr.length) {
            if (word[pos2] in vowelsD8 && word[pos2 + 1] in vowelsE8) {
                if ((1 shl bitcnt) and e090 != 0) {
                    val k = pos2 + 1
                    if (k in nuc.indices) nuc[k] = (nuc[k] or 2) and 0xe
                }
                bitcnt++
            }
        }
        fun WA(k: Int) = if (k in nuc.indices && (nuc[k] and 2) != 0) 1 else 0
        val vowelsF8 = cc["vowels_f8"] ?: emptySet()
        var pos = L - estr.length
        if (pos < L && word[pos] !in vowelsF8) {
            pos++
            if (pos >= L) pos++
        }
        val syl = acc[0]
        var cnt = 0
        while (cnt < syl) {
            if (pos > 0) pos--
            while (true) {
                if (WA(pos) != 0) break
                if (pos > 0) pos-- else break
            }
            cnt++
        }
        if (pos > 0) pos--
        if (typ == 0) {
            val vset108 = cc["vset_108"] ?: emptySet()
            while (pos > 0 && word[pos] !in vset108) pos--
        } else if (typ == 2) {
            val vsonor118 = cc["vsonor_118"] ?: emptySet()
            val aeiuo = cc["AEIUO"] ?: emptySet()
            val lmnr = cc["LMNR"] ?: emptySet()
            while (pos > 0 && word[pos] !in vsonor118) pos--
            if (pos > 0 && word[pos - 1] !in aeiuo && word[pos] in lmnr) pos--
        } else {  // typ == 1
            val vset13c = cc["vset_13c"] ?: emptySet()
            val uio = cc["UIO"] ?: emptySet()
            val aeou = cc["AEOU"] ?: emptySet()
            while (pos > 0 && word[pos] !in vset13c) pos--
            val go = (word[pos] in uio && pos > 0 && word[pos - 1] in aeou) ||
                     (word[pos] == 'E' && pos > 0 && word[pos - 1] == 'I')
            if (go && WA(pos) == 0 && pos > 0) pos--
        }
        return (pos + 1) to typ
    }

    // ---- _verb_results -------------------------------------------------------------------------

    private fun verbResults(word: String, L: Int, T: AccentTables): List<IntArray> {
        val out = mutableListOf<IntArray>()
        for (res in candidates(word)) {
            val (m, forcedType) = mode(res)
            @Suppress("UNCHECKED_CAST")
            val eflags = res["eflags"] as List<Int>
            if (m == 0) {
                val f3 = eflags[3]; val f4 = eflags[4]
                if (f3 == 0xff) continue
                out.add(intArrayOf(0, L - f3, f4, PRIO_VERB))
            } else if (m == 2) {
                val pf = T.prefixes[res["prefix_idx"] as Int].second
                out.add(intArrayOf(0, pf[1] + 1, pf[2], PRIO_VERB))
            } else {
                val (pos1, typ) = mode1(word, res, forcedType) ?: continue
                out.add(intArrayOf(0, pos1, typ, PRIO_VERB))
            }
        }
        return out
    }

    // ---- _foreign_walk -------------------------------------------------------------------------

    /**
     * Foreign-lexicon retraction walk. wb = word as cp1257 bytes. Returns 1-based stress pos.
     */
    private fun foreignWalk(wb: ByteArray, a0: Int, a1: Int, a2: Int): Int {
        val L = wb.size
        val nuc = Nucleus.kirchNucleus(wb.toString(CP1257))
        fun WA(k: Int) = if (k in nuc.indices && (nuc[k] and 2) != 0) 1 else 0
        // diphthong-nucleus marks
        var bit = 0
        for (pos in 0 until L - 1) {
            val b0 = wb[pos].toInt() and 0xff
            val b1 = wb[pos + 1].toInt() and 0xff
            if (b0 in FV && b1 in FV) {
                if ((1 shl bit) and a2 != 0) {
                    val k = pos + 1
                    if (k in nuc.indices) nuc[k] = (nuc[k] or 2) and 0xe
                }
                bit++
            }
        }
        var pos = L
        var cnt = 0
        while (cnt < a0) {
            if (pos > 0) pos--
            while (WA(pos) == 0 && pos > 0) pos--
            cnt++
        }
        if (pos > 0) pos--
        if (a1 == 0) {
            while (pos > 0 && (wb[pos].toInt() and 0xff) !in FV) pos--
        } else if (a1 == 2) {
            while (pos > 0 && (wb[pos].toInt() and 0xff) !in FVL) pos--
            if (pos > 0 && (wb[pos - 1].toInt() and 0xff) !in FAEIUO &&
                           (wb[pos].toInt()     and 0xff)  in FLMNR) pos--
        } else {  // a1 == 1
            while (pos > 0 && (wb[pos].toInt() and 0xff) !in FV) pos--
            val b = wb[pos].toInt() and 0xff
            val bPrev = if (pos > 0) wb[pos - 1].toInt() and 0xff else -1
            val go = (b in FUIO && pos > 0 && bPrev in FAEOU) ||
                     (pos > 0 && b == 0x45 && bPrev == 0x49)   // E after I
            if (go && WA(pos - 1) == 0 && pos > 0) pos--
        }
        return pos + 1
    }

    // ---- _foreign_results ----------------------------------------------------------------------

    private fun foreignResults(word: String): List<IntArray> {
        val wb = word.toByteArray(CP1257)
        val key = wb.toString(Charsets.ISO_8859_1)
        val out = mutableListOf<IntArray>()
        for (acc in foreignLex()[key] ?: emptyList()) {
            val a0 = acc[0]; val a1 = acc[1]; val a2 = acc[2]
            val pos1 = foreignWalk(wb, a0, a1, a2)
            out.add(intArrayOf(1, pos1, a1, 40))
        }
        return out
    }

    // ---- _noun_walk ----------------------------------------------------------------------------

    /**
     * Noun MODE-1 retraction walk. Returns 1-based pos or null.
     * res keys used: ending_idx, b90e, b90f, cec.
     */
    private fun nounWalk(wb: ByteArray, res: Map<String, Int>, cc: Map<String, Set<Char>>): Int? {
        val T = nounTables()
        val L = wb.size
        val ei = res["ending_idx"]!!
        val estr = T.ends[ei].first ?: ""
        val elen = estr.length
        val nuc = Nucleus.kirchNucleus(wb.toString(CP1257))
        fun WA(k: Int) = if (k in nuc.indices && (nuc[k] and 2) != 0) 1 else 0
        val NW = nounWalkCc()
        var pos = L - elen + 1 - (res["cec"] ?: 0)
        val syl = res["b90e"]!!
        repeat(syl) {
            if (pos > 0) pos--
            while (WA(pos) == 0 && pos > 0) pos--
        }
        if (pos > 0) pos--
        val b90f = res["b90f"]!!
        if (b90f == 0xff || b90f == -1) return null
        if (b90f == 0) {
            while (pos > 0 && (wb[pos].toInt() and 0xff) !in NW["V"]!!) pos--
        } else if (b90f == 2) {
            while (pos > 0 && (wb[pos].toInt() and 0xff) !in NW["VL"]!!) pos--
            if (pos > 0 && (wb[pos - 1].toInt() and 0xff) !in NW["AEIUO"]!! &&
                           (wb[pos].toInt()     and 0xff)  in NW["LMNR"]!!) pos--
        } else {  // b90f == 1
            while (pos > 0 && (wb[pos].toInt() and 0xff) !in NW["V1"]!!) pos--
            val bPos  = wb[pos].toInt() and 0xff
            val bPrev = if (pos > 0) wb[pos - 1].toInt() and 0xff else -1
            val go = (bPos in NW["UIO"]!! && pos > 0 && bPrev in NW["AEOU"]!!) ||
                     (pos > 0 && bPos == 0x45 && bPrev == 0x49)
            if (go && WA(pos) == 0 && pos > 0) pos--
        }
        return pos + 1
    }

    // ---- _noun_modes ---------------------------------------------------------------------------

    private fun nounModes(b90c: Int, b90d: Int, endingIdx: Int): List<Triple<Int, Int, Int>> {
        val T = nounTables()
        if (b90c !in 0 until 44 || b90d !in 1..4) return emptyList()
        val out = mutableListOf<Triple<Int, Int, Int>>()
        val c60row = T.bb0[(b90d - 1) * 44 + b90c]
        for ((col, da5, eidx) in T.da[b90c]) {
            if (eidx == -1) break
            if (eidx == endingIdx) {
                out.add(Triple(T.c60[c60row * 0x0e + col], col, da5))
            }
        }
        return out
    }

    // ---- _mode2_walks --------------------------------------------------------------------------

    /**
     * Noun mode-2 preprocessing + decision. Returns true if mode 2 resolves to MODE-1 WALK.
     */
    private fun mode2Walks(word: ByteArray, ei: Int): Boolean {
        val attr = Nucleus.kirchNucleus(word.toString(CP1257))
        val wb = word
        val n = wb.size + 1  // strlen('_'+word)
        fun A(i: Int) = if (i in attr.indices) attr[i] and 2 else 0
        fun C(i: Int) = if (i in wb.indices) wb[i].toInt() and 0xff else 0x20
        var pos = n - 3
        while (pos > 0 && A(pos) == 0) pos--
        if (pos > 0) pos--
        // digraph adjust
        val cp  = C(pos); val cpPrev = C(pos - 1)
        if ((cp == 0x5a && cpPrev == 0x44) || (cp == 0xde && cpPrev == 0x44) ||
            (cp == 0x48 && cpPrev == 0x43)) {
            if (pos > 1) pos -= 2
        } else if (cp in STRCHR_BDG && pos > 0) {
            pos--
        }
        val p1 = pos
        while (pos > 0 && A(pos) == 0) pos--
        // decision
        if (pos != 0) return false
        val cp1 = C(p1); val cp1Prev = C(p1 - 1)
        if ((cp1 == 0x41 || cp1 == 0x45) && cp1Prev != 0x49) return false
        return true
    }

    // ---- _noun_emit ----------------------------------------------------------------------------

    private fun nounEmit(
        out: MutableList<IntArray>, ei: Int, eflag: List<Int>, L: Int,
        wb: ByteArray, cc: Map<String, Set<Char>>,
        clsList: List<Int>, b90d: Int, b90e: Int, b90f: Int, cec: Int, prio: Int
    ) {
        for (cls in clsList) {
            for ((mode, _, _) in nounModes(cls, b90d, ei)) {
                val m = if (mode == 2) {
                    if (cls == 26 && mode2Walks(wb, ei)) 1 else 0
                } else mode
                val pos1: Int; val typ: Int
                if (m == 0) {
                    pos1 = L - eflag[1]; typ = eflag[2]
                } else if (b90f == -1 || b90f == 0xff) {
                    pos1 = -1; typ = -1
                } else {
                    val res = mapOf("ending_idx" to ei, "b90e" to b90e, "b90f" to b90f, "cec" to cec)
                    pos1 = nounWalk(wb, res, cc) ?: continue
                    typ = b90f
                }
                out.add(intArrayOf(2, pos1, typ, prio))
            }
        }
    }

    // ---- _star_case123 -------------------------------------------------------------------------

    /**
     * asm 0x10002219 shared case-1/2/3 handler. digitstr, remaining = cp1257 bytes.
     * Returns acc_state (0/1/2) and writes accout[depth].
     */
    private fun starCase123(
        digitstr: ByteArray, remaining: ByteArray, charIdx: Int,
        b90fArg: Int, accout: IntArray, depth: Int, g: IntArray
    ): Int {
        // g layout: [ce0, ce4, ce8, cf4, cf8]  indices: 0=ce0, 1=ce4, 2=ce8, 3=cf4, 4=cf8
        val stems = tables().stems; val vstb = verbStemb(); val pfx = pfxb()
        val dcat = (digitstr[0].toInt() and 0xff) - 0x31   // 0,1,2
        val fwd = remaining.reversedArray()
        val pfxmax = if (digitstr.size > 1 && digitstr[1].toInt() == 0x31) 1 else 252
        val lacc = intArrayOf(accout[depth], accout[depth])
        var accState = 0
        for (pi in 0 until pfxmax) {
            var pmatch = 0; var j = 0
            if (pi > 0) {
                if (pi >= pfx.size) break
                val p = pfx[pi]
                while (j < fwd.size && j < p.size && fwd[j] == p[j]) j++
                if (j >= p.size && j < fwd.size) pmatch = 1
            }
            if (pi != 0 && pmatch == 0) continue
            val cand = fwd.copyOfRange(j, fwd.size)
            val (lo, hi) = verbBracket(cand)
            var vidx = lo
            while (vidx < 0x2276) {
                val s = vstb[vidx]
                val sg = stems[vidx].second; val vf = stems[vidx].third
                val field = (sg[dcat].first ?: "").toByteArray(CP1257)
                val expected = s + field
                if (!expected.contentEquals(cand)) {
                    vidx = if (vidx == hi) 0x2270 else vidx + 1; continue
                }
                val e08c = vf[0]; val e08e = vf[2]; val e08f = vf[3]
                val e090 = vf[4]; val e091 = vf[5]
                val gacc0 = sg[dcat].second[0]; val gacc1 = sg[dcat].second[1]
                val d1 = if (digitstr.size > 1) digitstr[1].toInt() and 0xff else 0
                val ok = (d1 == 0x30 && pmatch == 1) || (d1 == 0x31 && pmatch == 0) || (d1 == 0x32)
                if (!ok) { vidx = if (vidx == hi) 0x2270 else vidx + 1; continue }
                val d2 = if (digitstr.size > 2) digitstr[2].toInt() and 0xff else 0
                if (d2 != 0x30) {
                    val m = when {
                        dcat == 0 -> (d2 == 0x31 && e08e == 0) || (d2 == 0x32 && e08e == 1)
                        dcat == 1 -> (d2 == 0x31 && (e08f == 0 || (e08c and 0x40) != 0)) || (d2 == 0x32 && e08f == 1)
                        dcat == 2 -> (d2 == 0x31 && e08f == 0) || (d2 == 0x32 && e08f == 1)
                        else      -> false
                    }
                    if (!m) { vidx = if (vidx == hi) 0x2270 else vidx + 1; continue }
                }
                val d3 = if (digitstr.size > 3) digitstr[3].toInt() and 0xff else 0
                if (d3 != 0x30) {
                    val pass = when (d3) {
                        0x31 -> (e08c and 0x3f) != 0
                        0x32 -> (e08c and 0x40) != 0
                        else -> false
                    }
                    if (!pass) { vidx = if (vidx == hi) 0x2270 else vidx + 1; continue }
                }
                // ACCEPT
                if (g[1] == -1) { g[1] = e090; g[0] = e091 }  // ce4, ce0
                var pmatchCur = pmatch
                if (pmatchCur == 1) {
                    pmatchCur = if (pfx[pi].contains(byteArrayOf('P'.code.toByte(),'E'.code.toByte(),'R'.code.toByte()))) 1 else 0
                }
                if (b90fArg != -1 && pmatchCur == 0) { accout[depth] = 0; return 1 }
                if (accState == 0) {
                    if (lacc[accState] == 0) {
                        if (pmatchCur == 0) {
                            g[4] = gacc0; g[3] = gacc1  // cf8, cf4
                        } else {
                            // PER-prefix syllable count
                            var found = -1
                            outer@ for (fi in 0 until fwd.size - 2) {
                                if (fwd[fi] == 0x50.toByte() && fwd[fi+1] == 0x45.toByte() && fwd[fi+2] == 0x52.toByte()) {
                                    found = fi; break@outer
                                }
                            }
                            var fwdIdx = if (found >= 0) found + 3 else fwd.size
                            var cnt2 = 1
                            while (fwdIdx < fwd.size) {
                                while (fwdIdx < fwd.size && (fwd[fwdIdx].toInt() and 0xff) in VB_VOW) fwdIdx++
                                while (fwdIdx < fwd.size && (fwd[fwdIdx].toInt() and 0xff) !in VB_VOW) fwdIdx++
                                cnt2++
                            }
                            g[4] = cnt2; g[3] = 1  // cf8, cf4
                        }
                    }
                    lacc[accState] += charIdx; accState = 1
                } else if (accState == 1) {
                    if (pmatchCur == 0) {
                        if (!(g[4] == gacc0 && g[3] == gacc1)) return 2
                    }
                }
                vidx = if (vidx == hi) 0x2270 else vidx + 1
            }
        }
        accout[depth] = lacc[0]; return accState
    }

    // ---- _star_parse ---------------------------------------------------------------------------

    /**
     * Bit-exact port of sub_10001BB1 case-0 (+case-4). Returns acc_state (0/1/2).
     * g: IntArray[5] = [ce0, ce4, ce8, cf4, cf8]
     */
    private fun starParse(
        digitstr: ByteArray, ctx: ByteArray, start: Int, charIdx: Int,
        b90fArg: Int, accout: IntArray, depth: Int, g: IntArray
    ): Int {
        val T = nounTables(); val stb = mainStemb(); val t17 = T.t17; val CC = starClasses()
        if (digitstr.isEmpty()) return 0
        val d0 = (digitstr[0].toInt() and 0xff) - 0x30
        if (d0 > 4) return 0
        // local copy + Č/ŽD restore
        var local = ctx.copyOfRange(start, ctx.size)
        if (start >= 1 && (ctx[start - 1].toInt() and 0xff) == 0x49 &&
            start >= 2 && (ctx[start - 2].toInt() and 0xff) in (CC["c9034"] ?: emptySet())) {
            if (local.isNotEmpty() && (local[0].toInt() and 0xff) == 0xc8) {
                local = byteArrayOf(0x54.toByte()) + local.copyOfRange(1, local.size)
            } else if (local.size > 1 && (local[0].toInt() and 0xff) == 0xde && (local[1].toInt() and 0xff) == 0x44) {
                local = local.copyOfRange(1, local.size)
            }
        }
        if (d0 == 4) return if (local.isNotEmpty()) 1 else 0
        if (d0 != 0) return starCase123(digitstr, local, charIdx, b90fArg, accout, depth, g)
        val digit2 = (digitstr[1].toInt() and 0xff) - 0x30
        val validCls = if (digit2 in t17.indices) t17[digit2] else listOf(-1)
        val lacc = intArrayOf(accout[depth], accout[depth])
        var accState = 0
        var maxprio = 0
        val (lo, hi) = bisectA9a(local)
        var idx = lo
        while (idx < 0xed6f) {
            val s = stb[idx]; val b = T.main[idx].second
            var j = 0
            while (j < s.size && j < local.size && s[j] == local[j]) j++
            var m = 0
            if (digitstr.size > 4 && (digitstr[4].toInt() and 0xff) == 0x31) {
                if (s.size >= 2 && (s[0].toInt() and 0xff) in setOf(0x4b, 0x4a) &&
                    (s[1].toInt() and 0xff) == 0x49 && b[2] > 1) {
                    while ((m + 2) < s.size && m < local.size && s[m + 2] == local[m] && s[m + 2] != 0.toByte()) m++
                }
            }
            var recRet = 0
            var accept = false
            if (j >= s.size) {
                if (j >= local.size) accept = true
            } else if ((s[j].toInt() and 0xff) == 0x2a) {  // nested '*'
                if (maxprio <= prio(idx)) {
                    recRet = starParse(s.copyOfRange(j + 1, s.size), local, j, j, b[3], lacc, accState, g)
                    if (recRet != 0) accept = true
                }
            }
            if (!accept) {
                val haveMore = (m + 2 < s.size && s[m + 2] != 0.toByte()) || (m < local.size && local[m] != 0.toByte())
                if (haveMore) { idx = if (idx == hi) 0xed2f else idx + 1; continue }
                accept = true
            }
            // ACCEPT
            val b90c = b[0]; val b90d = b[1]; val b90e = b[2]; val b90f = b[3]
            val b910 = b[4]; val b911 = b[5]
            if (maxprio < prio(idx)) maxprio = prio(idx)
            if (b90c !in validCls.filter { it != -1 }) { idx = if (idx == hi) 0xed2f else idx + 1; continue }
            val c2 = if (digitstr.size > 2) digitstr[2].toInt() and 0xff else 0
            if (c2 == 0x31) {
                if (b90d !in listOf(1, 2)) { idx = if (idx == hi) 0xed2f else idx + 1; continue }
            } else if (c2 == 0x32) {
                if (b90d !in listOf(3, 4)) { idx = if (idx == hi) 0xed2f else idx + 1; continue }
            } else if (c2 != 0x30) {
                idx = if (idx == hi) 0xed2f else idx + 1; continue
            }
            val c3 = if (digitstr.size > 3) digitstr[3].toInt() and 0xff else 0
            if (c3 != 0x30) {
                var p = 0
                val c903c = CC["c903c"] ?: emptySet(); val c904c = CC["c904c"] ?: emptySet()
                val c905c = CC["c905c"] ?: emptySet()
                while (p < local.size && (local[p].toInt() and 0xff) !in c903c) p++
                while (p < local.size && (local[p].toInt() and 0xff) in c904c) p++
                while (p < local.size && (local[p].toInt() and 0xff) !in c905c) p++
                val ended = p >= local.size
                if (c3 == 0x31) {
                    if (!ended) { idx = if (idx == hi) 0xed2f else idx + 1; continue }
                } else if (c3 == 0x32) {
                    if (ended) { idx = if (idx == hi) 0xed2f else idx + 1; continue }
                }
            }
            // globals
            if (g[1] == -1) { g[1] = b910; g[0] = b911 }  // ce4, ce0
            if (b90fArg != -1) { accout[depth] = 0; return 1 }
            if (recRet == 2) return 2
            if (maxprio > prio(idx)) { idx = if (idx == hi) 0xed2f else idx + 1; continue }
            // accumulate
            val se = (m + 2 >= s.size || s[m + 2] == 0.toByte()) && (m >= local.size || local[m] == 0.toByte())
            if (accState == 0) {
                val ce8 = if (se) 1 else 0
                if (lacc[accState] == 0) {
                    g[4] = b90e - ce8; g[3] = b90f; g[2] = ce8  // cf8, cf4, ce8
                }
                lacc[accState] += charIdx
                accState = 1
            } else if (accState == 1) {
                val ce8 = if (se) 1 else 0
                if (b90f == -1) { idx = if (idx == hi) 0xed2f else idx + 1; continue }
                if (!(g[4] == b90e - ce8 && g[3] == b90f)) return 2
            }
            idx = if (idx == hi) 0xed2f else idx + 1
        }
        accout[depth] = lacc[0]; return accState
    }

    // ---- _noun_results -------------------------------------------------------------------------

    private fun nounResults(word: String): List<IntArray> {
        val T = nounTables(); val cc = tables().cc
        val wb = word.toByteArray(CP1257)
        val rev = wb.reversedArray()
        val L = wb.size
        val AEIU = setOf(0x41, 0x45, 0x49, 0x55)
        val CC_BYTE = 0xc8; val ZH_BYTE = 0xde; val DD_BYTE = 0x44; val TT_BYTE = 0x54
        val isNe = L >= 2 && (wb[0].toInt() and 0xff) == 0x4e && (wb[1].toInt() and 0xff) == 0x45
        val out = mutableListOf<IntArray>()
        for ((ei, ep) in T.ends.withIndex()) {
            val (estr, eflag) = ep
            if (estr == null || estr.isEmpty()) continue
            val eb = estr.toByteArray(CP1257)
            if (eb.size >= rev.size || !rev.startsWith(eb)) continue
            val off = eb.size
            if ((rev[off].toInt() and 0xff) in AEIU) continue
            val base = rev.copyOfRange(off, rev.size)
            // candidate variants
            val cands = mutableListOf<Pair<ByteArray, Int>>()
            cands.add(base to 0)
            if (eflag[0] == 1) {
                val b0 = base[0].toInt() and 0xff
                if (b0 == CC_BYTE) {
                    cands.add((byteArrayOf(TT_BYTE.toByte()) + base.copyOfRange(1, base.size)) to 0)
                } else if (base.size > 1 && b0 == ZH_BYTE && (base[1].toInt() and 0xff) == DD_BYTE) {
                    cands.add(base.copyOfRange(1, base.size) to 0)
                }
            }
            if (isNe) {
                val ne = base.copyOfRange(0, base.size - 2)
                if (ne.isNotEmpty()) {
                    cands.add(ne to 1)
                    if (eflag[0] == 1 && (ne[0].toInt() and 0xff) == CC_BYTE) {
                        cands.add((byteArrayOf(TT_BYTE.toByte()) + ne.copyOfRange(1, ne.size)) to 1)
                    }
                }
            }
            for ((revStem, pflag) in cands) {
                val stemKey = revStem.toString(Charsets.ISO_8859_1)
                // exact lexicon match
                for ((_, b) in T.idx[stemKey] ?: emptyList()) {
                    val b90c = b[0]; val b90d = b[1]
                    if (pflag != 0 && b90c !in NOUN_FILTER) continue
                    nounEmit(out, ei, eflag, L, wb, cc,
                        listOf(b90c) + (NOUN_EXPAND[b90c] ?: emptyList()),
                        b90d, b[2], b[3], 0, 0x1e)
                }
                // '*'-tail derived stems
                for ((pre, dg, b) in starEntries()) {
                    if (!revStem.startsWith(pre) || pre.size >= revStem.size) continue
                    val b90c = b[0]; val b90d = b[1]
                    if (pflag != 0 && b90c !in NOUN_FILTER) continue
                    val b9fArg = if (b[3] > 127) b[3] - 256 else b[3]
                    val g = intArrayOf(0, -1, 0, 0, 0)  // [ce0, ce4, ce8, cf4, cf8]
                    val accout = IntArray(4)
                    val ret = starParse(dg, revStem, pre.size, pre.size, b9fArg, accout, 0, g)
                    if (ret == 0) continue
                    val cec = accout[0]
                    val nb90e: Int; val nb90f: Int
                    if (cec == 0) {
                        nb90e = b[2]; nb90f = b9fArg
                    } else {
                        nb90e = g[4]; nb90f = if (ret == 2) -1 else g[3]  // cf8, cf4
                    }
                    val starPrioVal = if (dg.isNotEmpty() && (dg[0].toInt() and 0xff) == 0x34) pre.size else pre.size + 0x14
                    nounEmit(out, ei, eflag, L, wb, cc,
                        listOf(b90c) + (NOUN_EXPAND[b90c] ?: emptyList()),
                        b90d, nb90e, nb90f, cec, starPrioVal)
                }
            }
        }
        return out
    }

    // ---- _disambig98F3 -------------------------------------------------------------------------

    /**
     * Full port of sub_100098F3 (PHASE1 + PHASE2). Returns distinct-accentuation count.
     * Each result is IntArray(4) = [valid, pos, type, prio].
     */
    private fun disambig98F3(results: List<IntArray>): Int {
        if (results.isEmpty()) return 0
        val p0 = results[0][1]; val t0 = results[0][2]
        fun same(pc: Int, tc: Int): Boolean {
            if (pc == p0 && (tc == t0 || tc + t0 == 3)) return true
            if (p0 == pc - 1 && t0 == 1 && tc == 2) return true
            if (p0 == pc + 1 && t0 == 2 && tc == 1) return true
            return false
        }
        var n = 0; var i = 0
        // PHASE 1
        while (i < results.size) {
            val v = results[i][0]
            if (v != 0 && v != 1) break
            if (n == 0) n = 1
            else if (!same(results[i][1], results[i][2])) n++
            i++
        }
        // PHASE 2
        var maxprio = if (n == 0) 0 else 0x14
        while (i < results.size) {
            val prio2 = results[i][3]
            if (maxprio <= prio2) {
                if (n == 0) { n = 1; maxprio = prio2 }
                else if (!same(results[i][1], results[i][2])) { n++; maxprio = prio2 }
            }
            i++
        }
        return n
    }

    // ---- Public entry point --------------------------------------------------------------------

    /**
     * Return (pos1, type) or null for an uppercase Lithuanian word.
     * pos1 is 1-based; type 1=circumflex 2=acute 0=short-final.
     */
    fun accent(word: String): Pair<Int, Int>? {
        val T = tables()
        val w = word.uppercase()
        val L = w.length
        val results = mutableListOf<IntArray>()
        results.addAll(verbResults(w, L, T))
        results.addAll(foreignResults(w))
        results.addAll(nounResults(w))
        if (results.isEmpty()) return null
        if (disambig98F3(results) == 1) return results[0][1] to results[0][2]
        return null
    }
}

// ---- ByteArray extension helpers ---------------------------------------------------------------

private operator fun ByteArray.plus(other: ByteArray): ByteArray {
    val result = ByteArray(size + other.size)
    copyInto(result, 0)
    other.copyInto(result, size)
    return result
}

private fun ByteArray.contains(sub: ByteArray): Boolean {
    if (sub.isEmpty()) return true
    outer@ for (i in 0..size - sub.size) {
        for (j in sub.indices) if (this[i + j] != sub[j]) continue@outer
        return true
    }
    return false
}
