package lt.gintaras.tts.engine

import java.nio.charset.Charset

// Kotlin port of lt_tts/transcribe.py
// Full Lithuanian G2P transcription: lexicon lookup + accent + render.

internal object Transcribe {

    private val CP1257 = Charset.forName("windows-1257")

    // Vowel set for spell-out check
    private val VOWELS_SP = Spell.run {
        // reuse the same vowel check from Spell
        setOf('a','e','i','o','u','y','ą','ę','ė','į','ų','ū',
              'A','E','I','O','U','Y','Ą','Ę','Ė','Į','Ų','Ū')
    }

    // Short-o loanwords (shorto_data.py merged set, 202 words -- unstressed 'oo' -> 'o';
    // each verified against the engine: oo->o reproduces its tokens exactly)
    private val SHORT_O = setOf(
        "astrologija","beisbolas","chromosoma","dekompozicija","dekoracija","demografija","demonstracija",
        "detonacija","diplomatas","direktorius","diskoteka","dispozicija","dokumentas","donoras","dotacija",
        "ekologija","ekonomija","ekonomistas","eksponatas","ekspozicija","etimologija","evoliucija","favoritas",
        "filologija","fonetika","fonoteka","formatas","formuluotė","fotografija","fotonas","fotosintezė","futbolas",
        "geologija","geometrija","globalus","gubernatorius","holograma","honoraras","hormonas","horoskopas",
        "humoras","ideologija","ikona","ilona","impozantiškas","improvizacija","inovacija","inspektorius",
        "intonacija","izoliacija","izoliacijai","izoliaciją","izoliacijų","izoliatorius","izoliuota","izoliuotas",
        "kolekcija","kolonija","komandiruotė","kombinacija","komentaras","komercija","komisija","komitetas",
        "kompanija","kompensacija","kompetencija","kompiuteris","kompleksas","komplikacija","kompozicija",
        "kompromisas","komunalinis","komunikacija","komunizmas","koncentracija","koncepcija","koncernas",
        "konditeris","konfliktas","kongresas","konkretus","konkurencija","konkursas","konsekvencija",
        "konservatorius","konsolidacija","konspektas","konstanta","konstitucija","konstrukcija","konsultacija",
        "kontaktas","konteineris","kontekstas","kontinentas","kontrabanda","kontraktas","kontrastas","konvencija",
        "kooperacija","koordinacija","korektorius","korupcija","kosmonautas","kostiumas","kotletas","laboratorija",
        "lokalizacija","lokomotyvas","memorandumas","meteorologija","metropolis","mikroskopas","mitologija",
        "mobilizacija","mobilus","moderatorius","modernizacija","modifikacija","molekulė","momentas","monarchija",
        "moneta","monitorius","monografija","monologas","monopolija","monotonija","morfologija","motociklas",
        "mozaika","navigatorius","nostalgija","notaras","obligacija","odisėja","operatorius","optimistas",
        "optimizmas","oratorija","orbita","organizatorius","orkestras","ortodoksas","poetas","poezija","poliglotas",
        "poligonas","poliklinika","politechnika","politikas","pomidoras","populiarus","porcelianas","pornografija",
        "postulatas","pozityvus","problematika","procedūra","procesas","prodiuseris","produktas","profesija",
        "prognozė","progresas","projektas","projektorius","prokuroras","proletaras","propeleris","proporcija",
        "proporcingas","propozicija","prospektas","protagonistas","protokolas","provincija","provizorius",
        "provokacija","prozodija","psichologas","psichologija","reformatorius","rekomendacija","renovacija",
        "reorganizacija","revoliucija","rezoliucija","romanas","romantika","senatorius","simbolis","statorius",
        "strofa","supozicija","technologija","televizorius","teologija","terminologija","terorizmas","topografija",
        "traktorius","transformacija","trofėjus","vokalas","zoologija"
    )

    // Lexicon: lowercase word -> RAW token string (loaded from lt_lex.tsv). Stored unsplit and split
    // on demand by lexTokens() -- ~60k entries with several tokens each would otherwise hold hundreds
    // of thousands of boxed String + ArrayList objects (the 1.9 MB file ballooned to ~75 MB on the
    // Java heap, the main OOM contributor on low-heap devices). The raw value keeps one String per
    // entry; only the looked-up entry is split (a rare hit), so the result is byte-identical.
    @Volatile private var lex: Map<String, String>? = null
    private val WS = Regex("\\s+")

    private fun loadLex(): Map<String, String> {
        lex?.let { return it }
        synchronized(this) {
            lex?.let { return it }
            val map = HashMap<String, String>(1 shl 16)
            try {
                val lines = Assets.lines("lt_lex.tsv", CP1257)
                for (line in lines) {
                    val l = line.trimEnd('\r', '\n')
                    if ('\t' !in l) continue
                    val idx = l.indexOf('\t')
                    map[l.substring(0, idx)] = l.substring(idx + 1)
                }
            } catch (_: Exception) {}
            return map.also { lex = it }
        }
    }

    /** Token list for an exact lexicon word, or null. Splits the raw value lazily -- identical to the
     *  old eager `split(\s+).filter{notEmpty}` but only for the single entry actually looked up. */
    private fun lexTokens(word: String): List<String>? =
        loadLex()[word]?.split(WS)?.filter { it.isNotEmpty() }

    /** x->ks, q->k, w->v (the engine's phonetic value for these non-LT letters; w per the
     *  ruleslt.rul `Dw v` digraph rule: windows -> vindovs). A STANDALONE letter is spelled by
     *  NAME (x='iks', q='kū', w='dviguba vė') -- the spell-mode path runs first. */
    private fun xqNormalize(word: String): String {
        if (word.none { it in "xXqQwW" }) return word
        return word.map { c -> when (c) {
            'x' -> "ks"; 'X' -> "Ks"; 'q' -> "k"; 'Q' -> "K"
            'w' -> "v"; 'W' -> "V"; else -> c.toString()
        }}.joinToString("")
    }

    // o/u-family vowels after word-initial i- (user-scoped: only io-/iu- starts; ie- stays native)
    private val I_HIATUS = setOf('o', 'ō', 'u', 'ū', 'ų')

    /** Word-INITIAL io/iu ("ios", "iOS", "Iowa"): transcr4 treats the i as a bare palatalization mark
     *  and DELETES it (ios -> 'oo s'). Fix (user-tuned): read the i as a FULL SEPARATE vowel, "i os" --
     *  NOT a j glide and NOT palatalized -- by DOUBLING the i ("ios" -> "iios"): the first i survives
     *  as the vowel, the second is consumed by the engine's own palatalization rule -> 'i oo s'.
     *  Applied ONLY on the OOV path (after the lexicon misses) so no lexicon word can change. */
    private fun iHiatus(word: String): String =
        if (word.length >= 2 && word[0] in "iI" && word[1].lowercaseChar() in I_HIATUS)
            word[0] + "i" + word.substring(1)
        else word

    /** Mid-word foreign "iou" (the English -ious family: previous/serious/various/obvious/curious):
     *  transcr4 deletes the i (palatalization mark) -> "prevous". No native LT word has the letter run
     *  "iou" (the "ou" diphthong is loanword-only), so keep the i by DOUBLING it ("iou" -> "iiou").
     *  OOV-only, like iHiatus. */
    private fun iouHiatus(word: String): String {
        val lower = word.lowercase()
        if (!lower.contains("iou")) return word
        val sb = StringBuilder(word.length + 2)
        var i = 0
        while (i < word.length) {
            if (i + 2 < word.length && lower[i] == 'i' && lower[i + 1] == 'o' && lower[i + 2] == 'u') {
                sb.append(word[i]); sb.append('i')   // keep the i (case-preserved) + the doubled palatalization mark
                i += 1
            } else {
                sb.append(word[i]); i += 1
            }
        }
        return sb.toString()
    }

    // kloun* paradigm (the loanword "klounas" = clown, and its declensions): the engine gives the `ou`
    // STEM a SHORT o (k-l-o-w-..., not k-l-oo-w-...), unlike every other `ou` loanword (sound/out/loud/
    // foulas... which are LONG and double their /o:/). Verified token-exact vs transcr4 for the whole
    // paradigm. shortenO shortens these ONLY in the `ou` stem (the `oo` immediately before `w`), so a long
    // ending stays long (klouno = k-l-o-w-n-OO keeps its genitive -o).
    private val SHORT_OU = setOf(
        "klounas", "klouno", "klounui", "klouną", "klounu", "kloune",
        "klounai", "klounų", "klounams", "klounus", "klounais", "klounuose"
    )

    /** Replace unstressed 'oo' with 'o' for known loanwords; the kloun* set shortens only the `ou`-stem oo. */
    private fun shortenO(w: String, toks: List<String>): List<String> = when {
        w in SHORT_O  -> toks.map { if (it == "oo") "o" else it }
        w in SHORT_OU -> toks.mapIndexed { i, t ->
            if (t == "oo" && i + 1 < toks.size && toks[i + 1] == "w") "o" else t
        }
        else -> toks
    }

    /** The word whose strlen the engine's s7c would see: the i-hiatus doubling feeds the engine-equivalent
     *  word (ios is rendered as iios), so the se8-arm midpoint must use the expanded length too -- gated
     *  exactly like transcribe() (OOV only: a lexicon word never doubles). */
    fun s7cWord(word: String): String =
        if (loadLex()[word.lowercase()] != null) word   // lexicon word never doubles
        else iouHiatus(iHiatus(word))                    // mirror transcribe()'s OOV expansion (ios->iios, iou->iiou)

    /**
     * Full token list with leading/trailing '_'. Two tiers:
     * 1) exact lexicon hit -> transcr4's own accented tokens (bit-exact);
     * 2) OOV -> Accent.accent() + Render.render() (the ported pipeline).
     * Falls back to a rule-based g2p only if the renderer errors.
     */
    // Letters the engine speaks: ASCII a-z + the Lithuanian-specific letters (both cases). Anything else is a
    // foreign letter and is DROPPED (silent) by dropForeign, so e.g. ß/ı/ſ aren't voiced via uppercase()'s
    // ß->"SS"/ı->"I" expansion. Cyrillic/Latvian are converted to names in Symbols.expand before transcribe.
    private val RECOGNIZED_LETTERS =
        ("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" + "ąčęėįšųūž" + "ĄČĘĖĮŠŲŪŽ").toSet()

    private fun dropForeign(word: String): String {
        if (word.all { !it.isLetter() || it in RECOGNIZED_LETTERS }) return word   // fast path: bit-exact
        return word.filter { !it.isLetter() || it in RECOGNIZED_LETTERS }
    }

    fun transcribe(word: String): List<String> {
        val w = dropForeign(word)                  // non-Lithuanian/non-ASCII letters -> silent
        // VOWELLESS word -> spell letter names as phonemes (engine lexicon hit preferred)
        val sp = Spell.spellOut(w) { lexTokens(it) }
        if (sp != null) return sp
        var w2 = xqNormalize(w)
        var wl = w2.lowercase()
        // exact lexicon hit
        lexTokens(wl)?.let { return it }
        w2 = iHiatus(w2)                       // OOV word-initial i+vowel: ios -> iios (see iHiatus)
        w2 = iouHiatus(w2)                      // OOV mid-word "iou": previous/serious keep the i (see iouHiatus)
        wl = w2.lowercase()
        lexTokens(wl)?.let { return it }       // the doubled form may itself be a lexicon word
        // accent + render (the ported DLL pipeline)
        return try {
            val stress = Accent.accent(w2)
            val toks = Render.render(w2.uppercase(), stress)
            if (toks != null && toks.isNotEmpty()) shortenO(wl, toks)
            else shortenO(wl, fallbackG2P(wl))
        } catch (_: Exception) {
            shortenO(wl, fallbackG2P(wl))
        }
    }

    // ---- simple rule-based g2p fallback (no stress) -----------------------------------

    private val VOWMAP = mapOf(
        'a' to "a", 'e' to "e", 'i' to "i", 'u' to "u", 'o' to "oo",
        'y' to "ii", 'ū' to "uu", 'ą' to "aa", 'ę' to "ea", 'ė' to "ee",
        'į' to "ii", 'ų' to "uu"
    )
    private val CONSMAP = mapOf(
        'p' to "p", 'b' to "b", 't' to "t", 'd' to "d", 'k' to "k", 'g' to "g",
        'm' to "m", 'n' to "n", 'l' to "l", 'r' to "r", 'v' to "v", 'f' to "f",
        'j' to "j", 's' to "s", 'z' to "z", 'h' to "h", 'š' to "S", 'ž' to "Z",
        'c' to "ts", 'č' to "tS"
    )
    private val GLIDES = mapOf(
        "ai" to Pair("a","j"), "ei" to Pair("e","j"), "ui" to Pair("u","j"),
        "oi" to Pair("o","j"), "au" to Pair("a","w"), "eu" to Pair("e","w"),
        "iau" to Pair("e","w")
    )
    private val RISING = mapOf("ie" to "ie", "uo" to "uo")
    private val FRONT = setOf('e','ė','ę','i','į','y')

    private fun fallbackG2P(word: String): List<String> {
        val s = word.toCharArray()
        val n = s.size
        val toks = mutableListOf<String>("_")
        var i = 0
        while (i < n) {
            val two = if (i + 1 < n) "${s[i]}${s[i+1]}" else ""
            val three = if (i + 2 < n) "${s[i]}${s[i+1]}${s[i+2]}" else ""
            val g3 = GLIDES[three]; if (g3 != null) { toks.add(g3.first); toks.add(g3.second); i+=3; continue }
            val r2 = RISING[two]; if (r2 != null) { toks.add(r2); i+=2; continue }
            val g2 = GLIDES[two]; if (g2 != null) { toks.add(g2.first); toks.add(g2.second); i+=2; continue }
            val vm = VOWMAP[s[i]]; if (vm != null) { toks.add(vm); i++; continue }
            val cm = CONSMAP[s[i]]
            if (cm != null) {
                val nxt = if (i + 1 < n) s[i+1] else ' '
                val code = if (nxt in FRONT || nxt == 'j') "$cm'" else cm
                toks.add(code); i++; continue
            }
            i++
        }
        toks.add("_")
        return toks
    }
}
