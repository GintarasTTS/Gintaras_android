package lt.gintaras.tts.engine

// Kotlin port of lt_tts/selection.py (frontend_free + build_tiling + helpers).
// The PSOLA functions (psola_render, xfade, synth) are NOT ported — the engine uses Backend.kt.

internal object Selection {

    // ---- constants ---------------------------------------------------------------
    private const val SR = 22050
    private const val K_DUR = 42.0
    private const val GRAIN_DUR = 330.0
    private const val LONG_MULT = 2.0
    private val CLOSURE_LEN = (0.010 * SR).toInt()  // ~220 samples

    // cp1257 special chars
    private val U_OG = "ų"   // ų (cp1257 0xf8) — stressed u-offglide
    private val A_OG = "ą"   // ą (cp1257 0xe0)

    // phoneme sets (as unit-alphabet chars)
    private val VOWELS  = setOf('a','e','i','o','u','ą','į','ę','ė','ų','ū')
    private val LONGV   = setOf('ą','į','ę','ė','ų','ū','o')
    private val SONOR   = setOf('l','m','n','r','j','v','w','N','L','M','R')
    private val STOPS   = setOf('p','b','t','d','k','g','P','B','T','D','K','G')
    private val GLIDES  = setOf('j','w')
    private val FRIC    = setOf('s','š','z','ž','f','h','c')

    private val FALLING_GLIDE = setOf('i','j','u',U_OG[0])

    // Token normalization maps
    private val LONG   = mapOf("aa" to "ą","ee" to "ė","ii" to "į","uu" to "ų","oo" to "o")
    private val AFFRIC = mapOf("ts" to "c","dz" to "dz")
    private val HUSH   = mapOf("S" to "š","Z" to "ž","tS" to "č","dZ" to "dž")
    private val VLONG2 = mapOf("ea" to "ę")
    private val GLIDE_MAP = mapOf("w" to "u","j" to "j")  // transcr4 glide -> unit-alpha
    private val LONG2SHORT = mapOf('ą' to 'a','ę' to 'e','į' to 'i','ų' to 'u','ė' to 'e')

    // Diphthong merging data
    private val GLIDE_SPELL = mapOf("j" to listOf("i","j"), "w" to listOf("u"))
    private val FIXED_DIPH = mapOf(Pair("a","j") to "aj", Pair("e","j") to "ei", Pair("a","w") to "au")

    // Phoneme keys that use double-dashed body
    private val CDBL_BODY = setOf("x", "dž", "dz")   // /x/ (ch), dž, dz bodies are "--cho"/"--džo"/"--dze":
                                               // dž bodies were recorded ONLY double-dashed, so a single dash
                                               // fell through to the palatal "o|" pipe and added an i-glide
                                               // (Džordana -> "Džiordana"). Engine-verified.
    private val CSPELL = mapOf("x" to "ch")  // /x/ -> "ch" orthographic

    // F0 model constants
    const val INTON_H  = 0.10; const val STRESS_H = 0.20
    const val F0_BASE  = 75.0; const val F0_REF   = 90.0
    const val F0_FLOOR = 75.0; const val F0_CEIL  = 130.5

    // ---- data classes ------------------------------------------------------------

    data class PhoneEntry(
        val phone: String,
        val dur: Int,
        val bps: List<Pair<Int, Int>>,
        val stressed: Boolean,
        val palatal: Boolean,
        // RAW transcr token (same pattern as `palatal`): norm() collapses the SHORT stressed 'O' and the
        // LONG 'oo'/'Oo'/'oO' to the same 'o', but the long-/o:/ a5 doubling needs that distinction for a
        // HIATUS o (ios i-oo-s doubles, chaosas a-O does not).
        val raw: String
    )

    data class TilingElem(
        val kind: String,
        val key: String?,
        val target: Double?,
        val vin: Boolean,
        val vout: Boolean,
        val f0: Double
    )

    data class TilingMeta(val pi: Int, val isVowel: Boolean, val stressed: Boolean)

    // ---- voice data accessor -----------------------------------------------------

    fun getVoiceData(): Voice.VoiceData = Voice.load()
    fun getUnitSet(): Set<String> = getVoiceData().units.keys.toSet()

    // ---- token normalization -----------------------------------------------------

    fun norm(tok: String): String {
        val t = tok.replace("'", "")
        if (t in HUSH) return HUSH[t]!!
        val low = t.lowercase()
        if (low in VLONG2) return VLONG2[low]!!
        if (low in GLIDE_MAP) return GLIDE_MAP[low]!!
        if (low in AFFRIC) return AFFRIC[low]!!
        if (low.length == 2 && low[0] == low[1]) return LONG[low] ?: low[0].toString()
        return low
    }

    fun isVowel(p: String) = p != "_" && p[0] in VOWELS
    fun isVoiced(p: String) = isVowel(p) || (p.isNotEmpty() && p[0] in SONOR)

    // ---- diphthong merging -------------------------------------------------------

    // merged record: (tok, dur, bps, head). `head` = the ORIGINAL nucleus-HEAD transcr token -- for a
    // merged pair the vowel token (the length-bearing 'oo' vs 'o' that the merged 'ou' spelling collapses);
    // for an unmerged token, the token itself. recsToFull uses `head` as the raw (6th) field.
    private data class MergedRec(
        val tok: String, val dur: Int, val bps: List<Pair<Int, Int>>, val head: String
    )

    private fun mergeDiphthongs(
        recs: List<Triple<String, Int, List<Pair<Int, Int>>>>,
        unitSet: Set<String>
    ): List<MergedRec> {
        val out = mutableListOf<MergedRec>()
        var i = 0
        while (i < recs.size) {
            val (tok, dur, bps) = recs[i]
            val v = norm(tok)
            val glideNext = (i + 2 >= recs.size) || recs[i + 2].first == "_" ||
                    !isVowel(norm(recs[i + 2].first))
            val glideStr = recs.getOrNull(i + 1)?.first?.lowercase()?.replace("'","") ?: ""
            if (tok != "_" && v.length == 1 && isVowel(v) && glideNext &&
                    glideStr in GLIDE_SPELL && i + 1 < recs.size) {
                val g = glideStr
                val vb = (LONG2SHORT[v[0]] ?: v[0]).toString()
                val fixed = FIXED_DIPH[Pair(vb, g)]
                var best: String? = null; var score = -1
                if (fixed != null && listOf(fixed, "-$fixed", "$fixed-").any { it in unitSet }) {
                    best = fixed; score = 99
                }
                for (sp in GLIDE_SPELL[g]!!) {
                    val nuc = vb + sp
                    val s = (if (("-$nuc") in unitSet) 10 else 0) +
                            (if (("$nuc-") in unitSet) 5 else 0) +
                            (if (nuc in unitSet) 1 else 0)
                    if (s > score) { best = nuc; score = s }
                }
                if (best != null && v != vb && best.startsWith(vb)) best = v + best.substring(vb.length)
                if (score > 0) {
                    val bs = best!!
                    val finalKey = if (tok.any { it.isUpperCase() }) bs[0].uppercaseChar() + bs.substring(1) else bs
                    out.add(MergedRec(finalKey, dur + recs[i + 1].second, bps, tok))   // head = vowel tok
                    i += 2; continue
                }
            }
            out.add(MergedRec(tok, dur, bps, tok)); i++
        }
        return out
    }

    private fun recsToFull(
        recs: List<Triple<String, Int, List<Pair<Int, Int>>>>,
        unitSet: Set<String>
    ): List<PhoneEntry> {
        return mergeDiphthongs(recs, unitSet).map { (tok, dur, bps, head) ->
            val stressed = tok != "_" && tok.replace("'","").any { it.isUpperCase() }
            val palatal = "'" in tok
            val low = tok.replace("'","").lowercase()
            val isdiph = low.length == 2 && low[0] != low[1] &&
                    low[0] in "aeiou" && low[1] in "iju"
            val phone = if (tok == "_") "_" else (if (isdiph) low else norm(tok))
            // raw (6th field) = `head`, the original nucleus-head token, so an `ou` diphthong's long 'oo'
            // vs short klounas 'o' survives the merge for the a5 doubling gate (see PlanBuilder.a5Eligible).
            PhoneEntry(phone, dur, bps, stressed, palatal, head)
        }
    }

    // ---- F0 model ----------------------------------------------------------------

    fun damp(f0Raw: Double, stressed: Boolean = false): Double {
        val h = INTON_H + if (stressed) STRESS_H else 0.0
        val f0 = F0_BASE + (f0Raw - F0_REF) * h
        return f0.coerceIn(F0_FLOOR, F0_CEIL)
    }

    /** Piecewise-linear interpolation (numpy.interp equivalent, with endpoint clamping). */
    private fun interp(tt: Double, xs: DoubleArray, ys: DoubleArray): Double {
        if (xs.isEmpty()) return 90.0
        if (tt <= xs[0]) return ys[0]
        if (tt >= xs[xs.size - 1]) return ys[ys.size - 1]
        var lo = 0; var hi = xs.size - 1
        while (hi - lo > 1) {
            val mid = (lo + hi) / 2
            if (xs[mid] <= tt) lo = mid else hi = mid
        }
        val t = (tt - xs[lo]) / (xs[hi] - xs[lo])
        return ys[lo] + t * (ys[hi] - ys[lo])
    }

    /** Build F0 interpolation function. Returns (f0fn, cumulative time spans). */
    fun buildF0(full: List<PhoneEntry>): Pair<(Double) -> Double, List<Pair<Double, Double>>> {
        val cum = mutableListOf<Pair<Double, Double>>()
        var t = 0.0
        for (e in full) { cum.add(Pair(t, t + e.dur)); t += e.dur }
        val xsList = mutableListOf<Double>(); val ysList = mutableListOf<Double>()
        for ((span, entry) in cum.zip(full)) {
            val (t0, _) = span
            for ((pos, f0) in entry.bps) {
                xsList.add(t0 + pos / 100.0 * entry.dur); ysList.add(f0.toDouble())
            }
        }
        if (xsList.isEmpty()) { xsList.add(0.0); xsList.add(t); ysList.add(90.0); ysList.add(90.0) }
        // sort by x
        val order = xsList.indices.sortedBy { xsList[it] }
        val xs = DoubleArray(order.size) { xsList[order[it]] }
        val ys = DoubleArray(order.size) { ysList[order[it]] }
        return Pair({ tt: Double -> interp(tt, xs, ys) }, cum)
    }

    // ---- frontend_free -----------------------------------------------------------

    fun frontendFree(text: String): List<PhoneEntry> {
        val voiceData = getVoiceData()
        val unitSet = voiceData.units.keys.toSet()
        val words = text.split(Regex("\\s+")).filter { it.isNotEmpty() }
        val toks = mutableListOf("_")
        for ((wi, w) in words.withIndex()) {
            val phones = Transcribe.transcribe(w).filter { it != "_" }
            if (phones.isEmpty()) continue
            if (toks.size > 1) toks.add("+")
            toks.addAll(phones)
        }
        toks.add("_")
        val durPairs = Duration.ilgiai(toks)
        val tonaiOut = Tonai.tonai(durPairs)
        val recs = tonaiOut.map { (tok, dur, bps) -> Triple(tok, dur, bps) }
        return recsToFull(recs, unitSet)
    }

    // ---- unit lookup helpers -----------------------------------------------------

    private fun cs(c: String) = CSPELL[c] ?: c

    private fun first(cands: List<String>, units: Map<String, List<Int>>): String? =
        cands.firstOrNull { it in units }

    fun onsetUnit(c: String, v1: Char, units: Map<String, List<Int>>): String? {
        val cc = cs(c)
        return first(listOf("$cc$v1-", "$cc$v1$v1-"), units)
    }

    fun bodyUnit(c: String, v: String, pipe: Boolean, units: Map<String, List<Int>>): String? {
        val cc = cs(c)
        val dash = if (c in CDBL_BODY) "--" else "-"
        val cands = mutableListOf<String>()
        if (pipe && v.length == 1 && (v[0] in "iuo" || v == U_OG))
            cands.add("$v|")
        cands.addAll(listOf("$dash$cc$v", "-$v", "$v|", v))
        return first(cands, units)
    }

    private fun usePipe(phones: List<String>, j: Int, v: String, prevSoft: Boolean = true): Boolean {
        if (v.length != 1) return false
        return when (v) {
            "i" -> true
            "u" -> prevSoft
            "o" -> prevSoft
            else -> false
        }
    }

    // prevSoft = the preceding consonant is palatalized: a u-offglide (`ui`) after a SOFT consonant takes
    // the `u|j` PIPE-glide that carries the palatal coloring (kurjeriui/vyriui r'uj, broliui/arkliui l'uj
    // -> u|j) vs the dashed `-uj` after a hard one (puikus p-uj). `u|j` is the ONLY recorded pipe-glide, so
    // every other offglide (aj/ej/oj) falls through. Mirrors the u|/ū| palatalization rule.
    fun glideUnit(v1: String, g: String, stressed: Boolean, units: Map<String, List<Int>>,
                  prevSoft: Boolean = false, onset: String? = null): String? {
        val base = (LONG2SHORT[v1[0]] ?: v1[0]).toString()
        val cands: List<String> = if (g in listOf("u","w",U_OG)) {
            if (stressed) listOf("$base$U_OG","-$base$U_OG","${base}u","-${base}u")
            else listOf("${base}u","-${base}u","$base$U_OG","-$base$U_OG")
        } else {
            val base0 = if (v1 == A_OG) {
                // an `l` ONSET + stressed-long `ą`(A_OG) + i-glide takes the recorded `-ai` coda (laisvė/
                // laiškas/laimė -> `là- -là -ai`); every other onset keeps the `-àj` split, and l+plain-a is
                // unstressed (v1 != A_OG) so it keeps `aj`. Gate: l-onset AND A_OG only (engine-verified).
                val c0 = listOf("-${v1}j","${v1}j","${base}j","-${base}j")
                if (onset == "l" || onset == "L") listOf("-${base}i","${base}i") + c0 else c0
            } else
                listOf("${base}j","-${base}j","-${v1}j","${base}i","-${base}i")
            if (prevSoft) listOf("$base|j") + base0 else base0
        }
        return first(cands, units)
    }

    fun diphUnits(v1v2: String, units: Map<String, List<Int>>): Pair<String?, String?> {
        val s = (LONG2SHORT[v1v2[0]] ?: v1v2[0]).toString() + v1v2.substring(1)
        val on = if ("$s-" in units) "$s-" else null
        val bod = first(if (on != null) listOf("-$s",s) else listOf(s,"-$s"), units)
        return Pair(on, bod)
    }

    fun initVowel(v: String, units: Map<String, List<Int>>): String? =
        first(listOf(v,"-$v","$v-"), units)

    private fun codaUnit(v: String, c: String, units: Map<String, List<Int>>): String? {
        val cands = mutableListOf("-$v$c")
        if (v == "u") cands.add("-o$c")
        cands.addAll(listOf("-$c",c,"$c-"))
        return first(cands, units)
    }

    private fun standaloneUnit(c: String, units: Map<String, List<Int>>): String? {
        val cc = cs(c)
        var u = first(listOf(cc,"$cc-","-$cc"), units)
        if (u == null && cc == "j") {
            // The voice has NO standalone-j recording (j exists only inside diphthong offglides and Cv
            // onsets), and the ENGINE simply SKIPS a cluster/word-final bare j ("asj"/"jtas": bit-exact
            // with NOTHING rendered for the j). For unknown letter-strings every letter must stay audible
            // (a screen-reader user must hear ALL of a nonsense token), so render the bare j as its
            // vocalic value: a short i-glide. Real words never hit this (j is always next to a vowel).
            u = first(listOf("i", "i|"), units)
        }
        return u
    }

    private fun isLongV(v: String, key: String? = null): Boolean {
        val dblLong = setOf("o","ó")  // 'o' and 'ó' (cp1257 0xf3)
        return (v.length == 1 && v in dblLong) || (key != null && key.endsWith("|"))
    }

    private fun grainTarget(key: String?, units: Map<String, List<Int>>, long: Boolean, legacy: Double): Double {
        val n = if (key != null) (units[key]?.size ?: 0) else 0
        return n * GRAIN_DUR * (if (long) LONG_MULT else 1.0)
    }

    private fun onsetLen(on: String?, f0: Double, units: Map<String, List<Int>>): Double {
        if (on == null || on !in units) return 0.0
        return units[on]!!.size * SR / f0.coerceAtLeast(60.0)
    }

    private fun kv(v: String) = if (v.length == 2) K_DUR else if (v[0] in LONGV) 42.0 else K_DUR

    // ---- build_tiling ------------------------------------------------------------

    /**
     * Walk phones left-to-right, emitting onset/body/coda demisyllable keys.
     * Returns (elems, meta) where meta is parallel to elems tagging each phone index.
     */
    fun buildTiling(
        phones: List<String>, durs: List<Int>, f0s: List<Double>,
        stresses: List<Boolean>, units: Map<String, List<Int>>,
        palatals: List<Boolean>? = null
    ): Pair<List<TilingElem>, List<TilingMeta>> {
        val n = phones.size
        val elems = mutableListOf<TilingElem>()
        val meta  = mutableListOf<TilingMeta>()

        fun tag(pi: Int) {
            while (meta.size < elems.size)
                meta.add(TilingMeta(pi, isVowel(phones[pi]), stresses[pi]))
        }
        fun emit(e: TilingElem) = elems.add(e)

        var i = 0
        var prevPipe = false
        var prevBare = false   // was the previous vowel a BARE body (no consonant onset)?

        while (i < n) {
            val p = phones[i]; val dur = durs[i]; val f0 = f0s[i]
            val prev = if (i > 0) phones[i - 1] else null
            val nxt  = if (i + 1 < n) phones[i + 1] else null

            if (isVowel(p)) {
                prevPipe = false
                prevBare = true   // bare vowel body -> a following obstruent coda backs off
                val wiFalling = p.length == 2 && p[1] in FALLING_GLIDE &&
                        !(p[1] in listOf('u','w',U_OG[0]) && p[0] in LONGV)
                when {
                    wiFalling -> {
                        val gl = glideUnit(p[0].toString(), p[1].toString(), stresses[i], units)
                        val ivk = if (p[1] in listOf('u','w',U_OG[0])) null else initVowel(p[0].toString(), units)
                        val nd = onsetLen(gl, f0, units)
                        if (ivk != null) emit(TilingElem("body", ivk,
                            grainTarget(ivk, units, isLongV(p[0].toString(), ivk), dur * K_DUR - nd),
                            true, true, f0))
                        if (gl != null) emit(TilingElem("dip", gl, null, true, true, f0))
                    }
                    p == "ou" -> {
                        // word-initial `ou` (out): o-HEAD long-o body + `-ou` u-offglide (see the C+`ou` branch).
                        val ohead = initVowel("o", units)
                        if (ohead != null) emit(TilingElem("body", ohead,
                            grainTarget(ohead, units, true, dur * K_DUR), true, true, f0))
                        val off = first(listOf("-ou", "ou"), units)
                        if (off != null) emit(TilingElem("dip", off, null, true, true, f0))
                    }
                    p.length == 2 -> {
                        val (on, bod) = diphUnits(p, units)
                        if (on != null) emit(TilingElem("dip", on, null, true, true, f0))
                        if (bod != null) emit(TilingElem("body", bod,
                            grainTarget(bod, units, isLongV(p, bod), dur * K_DUR - onsetLen(on, f0, units)),
                            true, true, f0))
                    }
                    else -> {
                        val bod = initVowel(p, units)
                        if (bod != null) emit(TilingElem("body", bod,
                            grainTarget(bod, units, isLongV(p, bod), dur * K_DUR),
                            true, true, f0))
                    }
                }
                tag(i); i++
            } else if (p != "_") {
                val c = p
                if (nxt != null && isVowel(nxt)) {
                    // ONSET consonant + vowel pair
                    val v = nxt; val vst = stresses[i + 1]; val vdur = durs[i + 1]; val vf0 = f0s[i + 1]
                    val v1 = if (v.length == 2) v[0] else v[0]
                    // SHORT-i combined onset `Ci|--` (ti|--): engine-verified 2026-06-12 -- the engine uses
                    // ti|-- for EVERY t+i syllable regardless of position (tikras/tinas/tilo/optika non-final
                    // AND eiti/naktis/dantis final), so there is NO last-vowel gate; lo|--/lu|-- are NEVER
                    // used (stalu/galu/metalo/salos play the plain onset+body pair). Still gated on the unit
                    // existing (only ti|-- recorded for i), so brolis li|-- absent -> li- backs off as before.
                    // Cv|-- combined SOFT-syllable unit: t+i (ti|--) AND a SOFT consonant + u/o (lu|--/lo|--,
                    // liutas l'u / liokajus l'o); hard stalu/galu/metalo play the plain pair. Engine-verified.
                    val softC = palatals?.getOrNull(i) ?: false
                    // long-ū/ų (U_OG) after a SOFT consonant takes the SAME Cu|-- combo as short u (soft-l +
                    // long-ū: liūtas/brolių/gilių/arklių -> lu|--). The combo KEY uses the short-u letter; the
                    // body stays the `ū|` pipe (comboUu is short-u-only). Gated on softC AND the unit existing.
                    val comboV = if (v == U_OG) "u" else v
                    val combo = if (v.length == 1 && (v == "i" || ((v == "u" || v == "o" || v == U_OG) && softC)))
                        "${c}${comboV}|--" else null
                    val comboUu = combo != null && combo in units && v == "u"   // only u -> dashed body
                    val diphCombo = if (v.length == 2 && "${c}${v}|" in units) "${c}${v}|" else null
                    // SOFT-GLIDE `e|ų` for an ACUTE "iau"/"jau" (front-end splits it into a stressed front-vowel
                    // `e` + a stressed u-offglide as TWO phones). After a SOFT consonant the engine renders this
                    // palatalized falling diphthong as ONE pipe body `e|ų` (j -> the j-specific `-je|ų`):
                    // kriaušė/siaubas/džiaugsmas -> Ce- + e|ų; jausmas -> je- + -je|ų. Consumes the u (i += 3).
                    val euBody = if (c in listOf("j","J") && "-je|$U_OG" in units) "-je|$U_OG"
                                 else if ("e|$U_OG" in units) "e|$U_OG" else null
                    val euSoft = softC && v == "e" && vst && i + 2 < n &&
                            phones[i + 2] == "u" && stresses[i + 2] && euBody != null
                    val on = when {
                        diphCombo != null -> null
                        combo != null && combo in units -> combo
                        else -> onsetUnit(c, v[0], units)
                    }
                    val falling = v.length == 2 && v[1] in FALLING_GLIDE &&
                            !(v[1] in listOf('u','w',U_OG[0]) && v[0] in LONGV)
                    val chain = mutableListOf<String>()
                    val dipkeys = mutableSetOf<String>()
                    var pbod: String? = null
                    when {
                        euSoft -> { chain.add(euBody!!); pbod = euBody }   // soft "iau"/"jau": Ce- onset + e|ų body
                        falling && v == "ei" -> {
                            val (dion, dbod) = diphUnits(v, units)
                            if (dion != null) { chain.add(dion); dipkeys.add(dion) }
                            if (dbod != null) { chain.add(dbod); pbod = dbod }
                        }
                        falling -> {
                            // SOFT onset + `ui` -> the `u|j` pipe-glide (kurjeriui/vyriui/broliui).
                            val gl = glideUnit(v[0].toString(), v[1].toString(), vst, units,
                                               prevSoft = (palatals != null && palatals[i]), onset = c)
                            if (gl != null && gl.startsWith("-")) {
                                val nbody = bodyUnit(c, v[0].toString(), false, units)
                                if (nbody != null) { chain.add(nbody); pbod = nbody }
                                chain.add(gl); dipkeys.add(gl)
                            } else if (gl != null) {
                                chain.add(gl); dipkeys.add(gl)
                            }
                        }
                        diphCombo != null -> { chain.add(diphCombo); pbod = diphCombo }
                        v.length == 2 && v == "ou" -> {
                            // `ou` loanword diphthong: the recorded `-ou` is acoustically a steady `u`, so give
                            // the o-HEAD a real long-o body `-Co` (carries the a5 doubling) + the `-ou` offglide.
                            val ohead = bodyUnit(c, "o", false, units)
                            if (ohead != null) { chain.add(ohead); pbod = ohead }
                            val off = first(listOf("-ou", "ou"), units)
                            if (off != null) { chain.add(off); dipkeys.add(off) }
                        }
                        v.length == 2 -> {
                            // A SOFT (palatalized) consonant + uo takes the `uo|` PIPE body alone (NO native
                            // uo- onset, NO -uo dashed) -- palatalization carried by the pipe, like u|/ū|.
                            // Engine Lookup-verified: diuoti/važiuoti/kiuoti/liuoti -> Cu- + uo|; a HARD C +
                            // uo (duona/tuo) keeps Cu- + uo- + -uo. Without this the soft consonant was read
                            // HARD ("važiuoti" -> "važuoti"). Gated on the pipe existing (only `uo|`; `ie|`
                            // absent -> soft+ie stays the onset+ie-+-ie dashed pair: siela/ties).
                            val softPipe = if (palatals != null && palatals[i] && "$v|" in units) "$v|" else null
                            if (softPipe != null) {
                                chain.add(softPipe); pbod = softPipe
                            } else {
                                val (dion, dbod) = diphUnits(v, units)
                                if (dion != null) { chain.add(dion); dipkeys.add(dion) }
                                if (dbod != null) { chain.add(dbod); pbod = dbod }
                            }
                        }
                        else -> {
                            // A LONG-ū/ų after a SOFT consonant takes the pipe body `ū|` in ANY position
                            // (word-final ačiū/svečių AND medial siųsti/žiūri); hard stays dashed (sūnų/vyrų).
                            // The old word-final/near-final gate dropped palatalization on a medial long-ū
                            // (siųsti read "sųsti", žiūri "žūri"). Mirrors the short-u rule. Engine-verified.
                            // After a lu|-- combo (comboUu) the body is the DASHED -lu; i/o keep i|/o| pipe.
                            val softLongU = v == U_OG && palatals != null && palatals[i]
                            val prevSoft = palatals?.getOrNull(i) ?: true
                            val up = if (comboUu) false else (usePipe(phones, i + 1, v, prevSoft) || softLongU)
                            val bod = bodyUnit(c, v, up, units)
                            if (bod != null) { chain.add(bod); pbod = bod }
                        }
                    }
                    // A Cv|-- combo (comboUu) leaves a DASHED -lu body, but the combo already closed the
                    // soft syllable, so a following coda is STANDALONE like after a pipe (brolius/kelius
                    // /arklius -> ...lu|-- -lu + bare `s`, NOT -os). Engine-Lookup verified.
                    prevPipe = (pbod != null && pbod.endsWith("|")) || comboUu
                    prevBare = false   // dashed/pipe body -> following coda stays standalone
                    val ic = dur; val iv = vdur + (if (euSoft) durs[i + 2] else 0)   // eu_soft glide spans e+u
                    val natDips = chain.sumOf { k ->
                        if (k in dipkeys || k != pbod) onsetLen(k, vf0, units) else 0.0
                    }
                    if (on != null && c.isNotEmpty() && c[0] in SONOR) {
                        emit(TilingElem("dip", on, (ic * K_DUR).takeIf { it >= 0 }, true, true, vf0))
                    } else {
                        if (on != null && c.isNotEmpty() && c[0] in STOPS && prev != null && isVowel(prev))
                            emit(TilingElem("sil", null, CLOSURE_LEN.toDouble(), false, false, f0))
                        if (on != null) emit(TilingElem("dip", on, null, true, true, vf0))
                        // stop/fricative plays native -> subtract onset len
                    }
                    tag(i)
                    val vlong = isLongV(v, pbod)
                    for (k in chain) {
                        if (k in dipkeys || k != pbod)
                            emit(TilingElem("dip", k, null, true, true, vf0))
                        else
                            emit(TilingElem("body", k,
                                grainTarget(k, units, vlong, iv * K_DUR - natDips - onsetLen(on, vf0, units)),
                                true, true, vf0))
                    }
                    tag(i + 1); i += if (euSoft) 3 else 2   // eu_soft consumed the following u-offglide too
                } else {
                    // leftover consonant (coda or standalone)
                    val pv = prev?.let { if (it.length == 2) it[1].toString() else it } ?: ""
                    val nxtUnburst = nxt != null && nxt.isNotEmpty() && nxt[0] in STOPS && i + 2 < n &&
                            isVowel(phones[i + 2]) && onsetUnit(nxt, phones[i + 2][0], units) == null
                    // n-coda after mid-front e/ę is STANDALONE before ANY consonant + word-final (vandens/žentas/
                    // šiandien/kentėti); after back a/ą/u/i it KEEPS the -Vn coda (dantis/rankos/bankas -an/-ąn).
                    // Positive on pv == e/ę, independent of the next consonant (engine-Lookup verified 2026-06-20).
                    val nFront = c in "nN" && (pv == "e" || pv == "ę")
                    // r-coda after mid-front e/ę is STANDALONE before ANY consonant + word-final (gerti/verkti/
                    // verslas/mergaitė); after back a/ą/o/u it KEEPS the -Vr coda (vartai/darbas/turtas/sportas).
                    // i has no -ir unit so it falls back standalone on its own. Positive on e/ę, like nFront.
                    val rFront = c in "rR" && (pv == "e" || pv == "ę")
                    val nxtSoft = palatals?.getOrNull(i + 1) ?: false
                    val lPipe = c in "lL" && "l|" in units && nxt != null && nxt != "" &&
                            nxt != "_" && !isVowel(nxt) && nxtSoft
                    if (lPipe) {
                        emit(TilingElem("dip", "l|", (dur * K_DUR).takeIf { it >= 0 }, true, true, f0))
                        tag(i); i++; prevPipe = true; continue
                    }
                    val codaCands = if (prev != null && isVowel(prev) && !prevPipe && !nxtUnburst && !nFront && !rFront) {
                        val cl = mutableListOf("-$pv$c")
                        if (pv.isNotEmpty() && pv[0] in LONG2SHORT) cl.add("-${LONG2SHORT[pv[0]]}$c")
                        if (pv == "u") {
                            cl.add("-o$c")
                            // u rounds to `o` before most obstruents, but where the voice recorded NO `-oc` it
                            // falls back to the `-ac` a-form: `d` has ONLY `-ad` (no -od), so u/au + d -> `-ad`
                            // (audra àu-d, futbolas u-d). Fires for both the bare `au` diphthong and the dashed
                            // `-Cu` body -- both excluded by the generic `-ac` gate below.
                            cl.add("-a$c")
                        }
                        // GENERIC `-aC` for an OBSTRUENT coda after a BARE vowel body (engine-verified grid
                        // 2026-06-12: word-initial i/e/y + k/p/t/f/z all back off to the a-coda; xkcd's
                        // i-k = '-ak'). THREE gates, each engine-confirmed: SONORANT codas never back off
                        // (imta/ilka/irgi = bare m/l/r); after a DASHED `-Cv` body the consonant stays
                        // standalone (wjak '-je'+k, mxyzptlk '-sy'+s); after a pipe body likewise.
                        // generic -aC only after a bare MONOPHTHONG (ikta -ak), not a bare DIPHTHONG (aitvaras -> t)
                        if (c.isNotEmpty() && c[0] !in SONOR && prevBare && prev.length == 1) cl.add("-a$c")
                        cl
                    } else emptyList()
                    val fullCoda = first(codaCands, units)
                    if (fullCoda != null) {
                        emit(TilingElem("dip", fullCoda, null, isVoiced(c), isVoiced(c), f0))
                    } else {
                        val tgt = if (c.isNotEmpty() && c[0] in SONOR) (dur * K_DUR).takeIf { it >= 0 } else null
                        emit(TilingElem("dip", standaloneUnit(c, units), tgt, isVoiced(c), isVoiced(c), f0))
                    }
                    tag(i); i++
                }
            } else { i++ }
        }
        return Pair(elems, meta)
    }
}
