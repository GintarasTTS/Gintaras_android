package lt.gintaras.tts.engine

// Kotlin port of lt_tts/planbuilder.py
// Generative (DLL-free) plan builder: unit selection + prosody -> Backend.Plan.

internal object PlanBuilder {

    private val PHASE2_TAIL = listOf(256, 256, 149)
    // question-rise s94/s90 target: final period 294-35=259 => F0 ~85 Hz == the engine's measured
    // question-end (statement ends flat 75 Hz). Reached EXACTLY via the per-frame reseed ramp below
    // (the old -46 was an IIR-undershoot compensation that still stalled at ~80 Hz).
    private const val Q_RISE_S90 = -35
    private const val Q_RISE_FRAMES = 16
    private const val A5_DMIN = 108
    private const val A5_LONGV_EXTRA = 10
    private const val SE8_SEC_LAG = 100

    private val A5_LONG_MONO = setOf("o")
    private val A5_AU_ONSET = setOf("ąu", "ąj", "ąū")

    // hlas vowel table bytes (cp1257)
    private val VOWEL_BYTES: Set<Int> = setOf(
        'a'.code, 'e'.code, 'i'.code, 'o'.code, 'u'.code,
        0xf8, 0xe0, 0xe1, 0xf3, 0xeb, 0xe6
    )
    // falling-diphthong 2nd element bytes: u ū i j
    private val DIPH_GLIDES: Set<Int> = setOf(0x75, 0xf8, 0x69, 0x6a)

    private val CP1257 = java.nio.charset.Charset.forName("windows-1257")
    private val CSPELL = mapOf("x" to "ch")
    private val STOPS = setOf('p','b','t','d','k','g','P','B','T','D','K','G')
    private val BURST_FID = mapOf("k" to 4788)

    // ---- unit-pad cache (Voice.parseUnitPads is expensive; call once) -----------------

    @Volatile private var cachedPads: Map<String, Pair<List<Int>, List<Int>>>? = null

    private fun loadPads(): Map<String, Pair<List<Int>, List<Int>>> {
        cachedPads?.let { return it }
        synchronized(this) {
            cachedPads?.let { return it }
            return Voice.parseUnitPads(Assets.bytes("gintaras.dta")).also { cachedPads = it }
        }
    }

    // ---- internal per-frame representation (before converting to Backend.Frame) -------

    private data class SFrame(
        val pcm: IntArray,
        val pi: Int?,
        val key: String?,
        val inStress: Boolean,
        var releasePoint: Boolean = false
    )

    // ---- helpers ----------------------------------------------------------------------

    private fun phoneLetters(p: String): Int =
        if (Selection.isVowel(p) && p.replace("'", "").length >= 2) 2 else 1

    private fun engstrSpell(p: String): String = CSPELL[p] ?: p

    // ---- select_frames ----------------------------------------------------------------

    private fun selectFrames(word: String): List<SFrame> {
        val vd = Voice.load()
        val pool = vd.pool
        val units = vd.units

        val full = Selection.frontendFree(word)
        val (f0Fn, cum) = Selection.buildF0(full)

        // non-boundary phones with computed F0
        data class Inner(val phone: String, val dur: Int, val f0: Double, val stressed: Boolean, val palatal: Boolean)
        val inner = full.zip(cum).filter { (e, _) -> e.phone != "_" }.map { (e, span) ->
            val f0 = Selection.damp(f0Fn((span.first + span.second) / 2.0), e.stressed && Selection.isVowel(e.phone))
            Inner(e.phone, e.dur, f0, e.stressed, e.palatal)
        }

        val phones   = inner.map { it.phone }
        val durs     = inner.map { it.dur }
        val f0s      = inner.map { it.f0 }
        val stresses = inner.map { it.stressed }
        val palatals = inner.map { it.palatal }

        val (elems, meta) = Selection.buildTiling(phones, durs, f0s, stresses, units, palatals)

        // stressed vowel index
        val sv = phones.indices.firstOrNull { stresses[it] && Selection.isVowel(phones[it]) }
            ?: phones.indices.filter { Selection.isVowel(phones[it]) }.maxOrNull() ?: 0

        // stressed syllable end (onset-maximisation rule)
        var j = sv + 1
        while (j < phones.size && !Selection.isVowel(phones[j]) && phones[j] != "_") j++
        val ncons = j - (sv + 1)
        val syllEnd = if (j < phones.size && Selection.isVowel(phones[j]))
            sv + maxOf(0, ncons - 1) else j - 1

        // arm_char: floor(wl/2), skipping plain-'a' diphthong 2nd element chars
        val wl = phones.sumOf { phoneLetters(it) }
        val skipped = mutableSetOf<Int>()
        var cpOff = 0
        for (p in phones) {
            val lc = phoneLetters(p)
            if (lc == 2 && Selection.isVowel(p) && p.replace("'", "").take(1) == "a")
                skipped.add(cpOff + 1)
            cpOff += lc
        }
        var armChar = wl / 2
        while (armChar in skipped && armChar < wl - 1) armChar++

        var cumL = 0; var armPhone = phones.size - 1; var armOff = 0; var armFrac = 0.0
        for ((ii, p) in phones.withIndex()) {
            val lc = phoneLetters(p)
            if (cumL <= armChar && armChar < cumL + lc) {
                armPhone = ii; armOff = armChar - cumL; armFrac = armOff.toDouble() / lc; break
            }
            cumL += lc
        }

        // build frames from tiling elems
        val frames = mutableListOf<SFrame>()
        for ((ei, elem) in elems.withIndex()) {
            val kind = elem.kind; val key = elem.key
            val piVal = if (ei < meta.size) meta[ei].pi else phones.size - 1
            val inStress = piVal <= syllEnd
            if (kind == "sil" || key == null) continue
            val refs = units[key] ?: continue
            for (fid in refs) {
                val pcm = pool[fid] ?: continue
                frames.add(SFrame(pcm = pcm, pi = piVal, key = key, inStress = inStress))
            }
        }

        // unburstable stop: insert closure + burst frames (e.g. 'k' in "sveiki", or a WORD-INITIAL
        // 'k' in kitas/ki/kinija -- no 'ki-' onset, so it sits at phones[0] and the scan must start at 0;
        // the closure doubles as the leading silence + the burst = the 1704 samples dropped word-initially).
        // EVERY unburstable stop is restored, not just the first: skirkite (s-k-i-r-K-i-t-e) has TWO unburstable
        // k's -- restoring only the first dropped the second 'k' so it read "skir-ite". We re-scan `frames` each
        // iteration, so a later stop's frames insert AFTER an earlier one's (earlier frames carry pi < ii).
        for (ii in 0 until phones.size - 1) {
            val p = phones[ii]
            if (p.isNotEmpty() && p[0] in STOPS && ii + 1 < phones.size
                && Selection.isVowel(phones[ii + 1])
                && Selection.onsetUnit(p, phones[ii + 1][0], units) == null
                && p in BURST_FID
            ) {
                val closKey = "${p}a-"
                val clos = units[closKey]?.firstOrNull()
                val burstFid = BURST_FID[p]
                val ins = frames.indexOfFirst { it.pi != null && it.pi >= ii }
                    .let { if (it < 0) frames.size else it }
                val newFrames = mutableListOf<SFrame>()
                for (fid in listOfNotNull(clos, burstFid)) {
                    val pcm = pool[fid] ?: continue
                    // python labels BOTH inserted frames '_stop' (one (key,pi) unit -> one pause group)
                    newFrames.add(SFrame(pcm = pcm, pi = ii, key = "_stop", inStress = ii <= syllEnd))
                }
                frames.addAll(ins, newFrames)
            }
        }

        // mark the release frame. python's candidate list is ALL non-sil frames (closures included --
        // zcgagtp arms ON its 'ga-' closure); our frames list already dropped the sil elems, so every
        // index is a candidate. (The old `pcm.size <= 350` filter wrongly excluded closures.)
        val voiced = frames.indices.toList()
        val phFrames = voiced.filter { frames[it].pi == armPhone }
        val tgt: Int? = when {
            phFrames.isEmpty() ->
                voiced.firstOrNull { frames[it].pi != null && frames[it].pi!! >= armPhone }
                    ?: voiced.firstOrNull()
            armOff >= 1 -> {
                val keys = phFrames.map { frames[it].key }
                val split = (1 until keys.size).firstOrNull { keys[it] != keys[it - 1] }
                if (split != null) phFrames[split]
                else phFrames[minOf(phFrames.size - 1, Math.rint(armFrac * phFrames.size).toInt())]
            }
            armPhone > 0 && Selection.isVowel(phones.getOrElse(armPhone) { "" })
                && !Selection.isVowel(phones.getOrElse(armPhone - 1) { "_" })
                && phones.getOrElse(armPhone - 1) { "_" } != "_" -> {
                val onset = voiced.filter { frames[it].pi == armPhone - 1 }
                onset.firstOrNull() ?: phFrames.firstOrNull()
            }
            else -> phFrames.firstOrNull()
        }
        if (tgt != null) frames[tgt] = frames[tgt].copy(releasePoint = true)
        return frames
    }

    // ---- a5 generation ----------------------------------------------------------------

    private fun a5LongDistribute(n: Int): List<Int> {
        val out = MutableList(n) { 0 }
        val s = n - 1; if (s <= 0) return out
        var prev = 0
        for (k in 0 until s) {
            val cur = kotlin.math.round((k + 1).toDouble() * A5_LONGV_EXTRA / s).toInt()
            out[1 + k] = cur - prev; prev = cur
        }
        return out
    }

    private fun a5Eligible(key: String, phone: String, D: Int?, prevPhone: String?, raw: String? = null): String? {
        val body = key.trimStart('-').trimEnd('-').replace("|", "")
        val isCoda = key.startsWith("-") || (!key.endsWith("-") && key.length <= 3)
        val isOnset = key.endsWith("-")
        val pl = phone.replace("'", "")
        if (isCoda && body.isNotEmpty() && body.last().toString() in A5_LONG_MONO
            && pl.length == 1 && pl in A5_LONG_MONO
        ) {
            // a HIATUS o (a vowel immediately before it) doubles only when the RAW transcr token is the
            // LONG doubled 'oo'/'Oo'/'oO' (ios i-oo-s: engine a5=[0,1x10], capture_prosody-verified); the
            // SHORT stressed 'O' (chaosas a-O) does NOT double. norm() collapses both to 'o', so the raw
            // token (PhoneEntry.raw) carries the distinction.
            if (prevPhone != null && Selection.isVowel(prevPhone)) {
                val rawLong = raw != null && raw.replace("'", "").lowercase() == "oo"
                if (!rawLong) return null
            }
            return if (D == null || D >= A5_DMIN) "o" else null
        }
        // the o-HEAD body of an `ou` loanword diphthong (sound/about/out/loud...). Selection renders `ou`
        // as a real long-o body `-Co` (this branch) + the `-ou` u-offglide, because the recorded `-ou` is
        // acoustically a steady `u` (the o is absent) -- so the long-/o:/ doubling lands on the `-Co` head
        // (body ends 'o'), gated on the LONG head (raw 'oo'); klounas's SHORT head 'o' stays single.
        if (isCoda && pl == "ou" && body.isNotEmpty() && body.last() == 'o') {
            if (raw != null && raw.replace("'", "").lowercase() == "oo") return "o"
        }
        if (isOnset && pl in A5_AU_ONSET) return "au"
        return null
    }

    private fun genA5List(word: String, gen: List<SFrame>? = null): List<Int> {
        val vd = Voice.load()
        val pool = vd.pool; val units = vd.units
        val pads = loadPads()
        val bit6 = pads.entries.filter { (_, v) -> v.second.any { it and 0x40 != 0 } }
            .map { it.key }.toSet()

        val frames = gen ?: selectFrames(word)
        val full = Selection.frontendFree(word).filter { it.phone != "_" }
        val phones = full.map { it.phone }
        val durs = full.map { it.dur }
        val raws = full.map { it.raw }   // raw transcr token ('oo' vs 'O' for the hiatus-o gate)

        val out = mutableListOf<Int>()
        var i = 0
        while (i < frames.size) {
            val key = frames[i].key ?: ""; val pi = frames[i].pi
            var jj = i
            while (jj < frames.size && frames[jj].key == key && frames[jj].pi == pi) jj++
            val n = jj - i
            val phone = if (pi != null && pi < phones.size) phones[pi] else "?"
            val D = if (pi != null && pi < durs.size) durs[pi] else null
            if (key in bit6) {
                val padEntry = pads[key]
                val refs = units[key] ?: emptyList()
                val kept = if (padEntry != null)
                    refs.indices.filter { refs[it] in pool }.map { padEntry.first.getOrElse(it) { 0 } }
                else emptyList()
                out.addAll((kept + List(n) { 0 }).take(n))
            } else {
                val prevPhone = if (pi != null && pi >= 1) phones.getOrNull(pi - 1) else null
                val raw = if (pi != null) raws.getOrNull(pi) else null
                val cls = if (n <= 14) a5Eligible(key, phone, D, prevPhone, raw) else null
                when (cls) {
                    "o"  -> out.addAll(a5LongDistribute(n))
                    "au" -> { out.add(0); repeat(n - 1) { out.add(1) } }
                    else -> repeat(n) { out.add(0) }
                }
            }
            i = jj
        }
        return out
    }

    // ---- engstr / armc / armRpos -------------------------------------------------------

    private fun genEngstrMap(word: String): Pair<ByteArray, List<Int>> {
        val full = Selection.frontendFree(word).filter { it.phone != "_" }
        val pre = mutableListOf<Byte>(0x20); val src = mutableListOf<Int>(-1)
        for ((pi, entry) in full.withIndex()) {
            for (b in engstrSpell(entry.phone).toByteArray(CP1257)) { pre.add(b); src.add(pi) }
            if (entry.palatal) { pre.add(0x27); src.add(-1) }
            pre.add(0x20); src.add(-1)
        }
        val n = pre.size; var i = 0
        while (i < n) {
            if (pre[i] == 0x27.toByte()) {
                pre[i] = 0x20
                val c2 = if (i + 2 < n) pre[i + 2].toInt() and 0xff else 0
                if (c2 in VOWEL_BYTES) {
                    var jj = i + 2
                    while (jj < n && pre[jj] != 0x20.toByte()) jj++
                    if (jj < n) pre[jj] = 0x7c
                } else pre[i] = 0x7c
            }
            i++
        }
        val engstr = mutableListOf<Byte>(); val pos2phone = mutableListOf<Int>()
        for (k in 0 until n) {
            if (pre[k] != 0x20.toByte()) {
                engstr.add(pre[k])
                pos2phone.add(if (pre[k] == 0x7c.toByte()) -1 else src[k])
            }
        }
        return Pair(engstr.toByteArray(), pos2phone)
    }

    private fun genArmc(word: String, engstr: ByteArray): Int {
        val n = engstr.size
        val pipes = (0 until n).filter { engstr[it] == 0x7c.toByte() }.toSet()
        val dskip = (1 until n).filter {
            engstr[it - 1] == 0x61.toByte() && (engstr[it].toInt() and 0xff) in DIPH_GLIDES
        }.toSet()
        val skip = pipes + dskip
        // s7c = strlen of the word the ENGINE would see: our i-hiatus reading feeds the engine-equivalent
        // DOUBLED word (ios is rendered as iios), so the arm midpoint must use the expanded length too.
        val seg = Transcribe.s7cWord(word).length / 2
        for (k in 0 until n) { if (k !in skip && k >= seg) return k }
        return n - 1
    }

    private fun genArmRpos(word: String, frames: List<Backend.Frame>, frameRpos: List<Int>): Int? {
        val (engstr, pos2phone) = genEngstrMap(word)
        val armc = genArmc(word, engstr)
        if (armc >= pos2phone.size) return null
        val ap = pos2phone[armc]; if (ap < 0) return null
        val vpos = engstr.indices.filter { pos2phone[it] == ap && engstr[it] != 0x7c.toByte() }
        val elemIdx = vpos.indexOf(armc).let { if (it < 0) 0 else it }
        val apIdx = frames.indices.filter { frames[it].pi == ap }
        val armFrame: Int? = if (elemIdx == 0 || apIdx.isEmpty()) {
            frames.indices.firstOrNull { frames[it].pi != null && frames[it].pi!! >= ap }
        } else {
            val blocks = mutableListOf(apIdx[0])
            for ((a, b) in apIdx.zip(apIdx.drop(1))) {
                if (frames[b].key != frames[a].key) blocks.add(b)
            }
            blocks.getOrNull(minOf(elemIdx, blocks.size - 1))
        }
        if (armFrame == null || armFrame == 0) return null
        return frameRpos[armFrame - 1] + SE8_SEC_LAG
    }

    // ---- threadN2 --------------------------------------------------------------------

    // set of all pool frame contents (the python poolset): n2 is threaded only toward an IN-POOL voiced unit
    @Volatile private var cachedPoolSet: Set<List<Int>>? = null

    private fun poolSet(): Set<List<Int>> {
        cachedPoolSet?.let { return it }
        synchronized(this) {
            cachedPoolSet?.let { return it }
            return Voice.load().pool.values.map { it.toList() }.toHashSet().also { cachedPoolSet = it }
        }
    }

    private fun threadN2(frames: MutableList<Backend.Frame>) {
        val n = frames.size
        val ps = poolSet()
        for (i in 0 until n) {
            val nb = if (i + 1 < n) frames[i + 1] else null
            val n2 = if (nb != null && !nb.pause && nb.pcm.toList() in ps) nb.pcm else null
            frames[i] = frames[i].copy(n2 = n2)
        }
    }

    // diagnostic trace for the jvmtest parity harness: one "key pi pcmlen release" line per frame
    fun debugFrames(word: String): List<String> =
        selectFrames(word).map { "${it.key} ${it.pi} ${it.pcm.size}${if (it.releasePoint) " REL" else ""}" }

    // ---- buildPlanPhase2 -------------------------------------------------------------

    fun buildPlanPhase2(word: String, final: Boolean = true, question: Boolean = false,
                        rate: Int? = null, pitch: Int? = null): Backend.Plan {
        // rate/pitch (NVDA sliders, null = bit-exact sentinels) shape the TIMING-dependent parts of the
        // plan (the s94 seed + the no-arm contour pass) and must match what synthesize() is called with.
        val pdc = Backend.pitchPdc(pitch)
        val s94Init = Backend.pitchS94Seed(pdc)   // 68 at pitch=null (pdc=294) -> bit-exact path unchanged
        val gen = selectFrames(word)
        val a5gen = genA5List(word, gen)

        // pause flag: first grain of a (key,pi) unit > 350 samples => whole unit is verbatim closure
        val pause = BooleanArray(gen.size)
        var fi = 0
        while (fi < gen.size) {
            val k0 = gen[fi].key; val p0 = gen[fi].pi
            var fj = fi
            while (fj < gen.size && gen[fj].key == k0 && gen[fj].pi == p0) fj++
            val pv = gen[fi].pcm.size > 350
            for (k in fi until fj) pause[k] = pv
            fi = fj
        }

        val mframes = mutableListOf<Backend.Frame>()
        for ((gi, g) in gen.withIndex()) {
            val a5v = a5gen.getOrElse(gi) { 0 }
            val s90 = if (pause[gi]) 0 else -20
            mframes.add(Backend.Frame(
                pcm = g.pcm, s90 = s90, s94 = s94Init,
                pause = pause[gi], reseed = false,
                a5 = a5v, release = g.releasePoint,
                pi = g.pi, key = g.key
            ))
        }
        if (mframes.isNotEmpty()) mframes[0] = mframes[0].copy(s94 = s94Init)
        threadN2(mframes)

        val tail = PHASE2_TAIL

        if (question) {
            for (i in mframes.indices) mframes[i] = mframes[i].copy(release = false)
            val vidx = mframes.indices.filter { !mframes[it].pause }
            val m = minOf(Q_RISE_FRAMES, vidx.size)
            // The ramp RESEEDS s94 per frame (not just s90): the IIR alone moves ~2/epoch and the trailing
            // frames carry only a few epochs -- without the reseed the rise stalled near ~80 Hz at natural
            // rate and vanished at fast rates. Reseed is rate-independent.
            for ((r, k) in vidx.takeLast(m).withIndex()) {
                val frac = (r + 1).toDouble() / m
                val v = Math.rint(-20 + (Q_RISE_S90 - (-20)) * frac).toInt()
                mframes[k] = mframes[k].copy(s90 = v, s94 = v, reseed = true)
            }
            return Backend.Plan(mframes.toList(), tail)
        }

        if (!final) {
            for (i in mframes.indices) mframes[i] = mframes[i].copy(release = false)
            return Backend.Plan(mframes.toList(), tail)
        }

        // no-arm pass to collect per-frame cumulative output positions. It MUST run at the TARGET
        // rate/pitch: releaseRpos is an absolute OUTPUT-sample position and a fast THR compresses the
        // output (a natural-schedule arm misfires at fast rates). rate=null keeps the bit-exact path.
        val framesList = mframes.toList()
        val plan0 = Backend.Plan(framesList, tail)
        val frRpos = mutableListOf<Int>()
        Backend.synthesize(plan0, rate = rate, pitch = pitch, frameRpos = frRpos)
        val rr = genArmRpos(word, framesList, frRpos)
        if (rr != null) {
            val stripped = framesList.map { it.copy(release = false) }
            val plan = Backend.Plan(stripped, tail)
            plan.se8Ramp = true
            plan.releaseRpos = rr
            return plan
        }
        return plan0
    }

    // ---- buildPlanPhrase -------------------------------------------------------------

    private fun se8WordBase(wi: Int): Int = if (wi == 0) 20 else 10 - 4 * wi

    fun buildPlanPhrase(text: String, question: Boolean = false,
                        rate: Int? = null, pitch: Int? = null): Backend.Plan {
        val words = text.split(Regex("\\s+")).filter { it.isNotEmpty() }
        if (words.size <= 1) return buildPlanPhase2(text, question = question, rate = rate, pitch = pitch)

        // ENGINE WORD GAP (every boundary): the original hlas.dll inserts its `+` word boundary -- a short
        // silence = the per-word trailing tail (~662 smp natural) -- between EVERY pair of words, not just
        // vowel|vowel. Re-measured on the engine (2026-06-19): "labas rytas" (s|r, consonant|consonant) shows a
        // 662-sample gap, identical to a vowel|vowel boundary. Earlier gated to vowel|vowel only (byte-identical
        // consonant phrases + stop "a alfa" -> "ąlfa" merging); the user asked to restore the engine's real
        // behavior, so it now fires at every boundary. The gap carries silence=true so Backend starts a fresh
        // ring block after it (no declick bleed across the gap -- the mechanism that made vowel|vowel safe).
        fun edges(w: String): Pair<Boolean, Boolean> {
            val ph = Selection.frontendFree(w).map { it.phone }.filter { it != "_" }
            return if (ph.isEmpty()) Pair(false, false)
                   else Pair(Selection.isVowel(ph.first()), Selection.isVowel(ph.last()))
        }
        val edges = words.map { edges(it) }
        val thr = if (rate != null) Backend.rateThr(rate) else 150
        val gapLen = 22050 * (thr / 5) / 1000          // = the engine word gap (661 at the natural rate)
        val gapS94 = Backend.pitchS94Seed(Backend.pitchPdc(pitch))

        data class WordSpan(val start: Int, val end: Int, val word: String, val wp: Backend.Plan)
        val allFrames = mutableListOf<Backend.Frame>()
        val wordSpans = mutableListOf<WordSpan>()

        for ((wi, w) in words.withIndex()) {
            val last = wi == words.size - 1
            val wp = buildPlanPhase2(w, question = question && last, rate = rate, pitch = pitch)
            val start = allFrames.size
            for ((fi, fr) in wp.frames.withIndex()) {
                var f = fr.copy(release = false)
                if (fi == 0 && wi > 0) f = f.copy(wordStart = true)
                if (!(question && last) && wi > 0) f = f.copy(se8Base = se8WordBase(wi))
                allFrames.add(f)
            }
            wordSpans.add(WordSpan(start, allFrames.size, w, wp))
            // engine word gap at EVERY word boundary (NOT part of any span); edges kept for clarity
            if (!last && gapLen > 0) {
                allFrames.add(Backend.Frame(pcm = IntArray(gapLen), s90 = 0, s94 = gapS94, pause = true,
                    reseed = false, a5 = 0, silence = true, pi = null, key = "_wgap"))
            }
        }

        val plan = Backend.Plan(allFrames.toList(), PHASE2_TAIL)
        plan.se8Ramp = true

        // in-phrase no-arm pass to get absolute per-frame cumulative output positions AT THE TARGET
        // RATE+PITCH (arm samples must live on the actual epoch schedule)
        val frRpos = mutableListOf<Int>()
        Backend.synthesize(plan, rate = rate, pitch = pitch, frameRpos = frRpos)

        val armList = mutableListOf<Int?>()
        for ((start, end, w, wp) in wordSpans) {
            if (question && end == allFrames.size) { armList.add(null); continue }
            val wf = allFrames.subList(start, end).toList()
            val wr = if (end <= frRpos.size) frRpos.subList(start, end).toList() else null
            var rr: Int? = null
            if (wr != null) rr = genArmRpos(w, wf, wr)
            if (rr == null) {
                val baseRpos = if (start > 0) frRpos.getOrElse(start - 1) { 0 } else 0
                val srr = wp.releaseRpos
                rr = if (srr != null) baseRpos + srr else null
            }
            armList.add(rr)
        }
        plan.releaseRposList = armList
        return plan
    }
}
