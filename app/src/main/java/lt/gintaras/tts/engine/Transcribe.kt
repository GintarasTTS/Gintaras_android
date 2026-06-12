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
        "astrologija","chromosoma","dekompozicija","dekoracija","demografija","demonstracija",
        "detonacija","diplomatas","direktorius","diskoteka","dispozicija","donoras","dotacija",
        "ekologija","ekonomija","ekonomistas","eksponatas","ekspozicija","etimologija","evoliucija",
        "filologija","fonetika","fonoteka","formatas","formuluotė","fotografija","fotonas",
        "fotosintezė","geologija","geometrija","globalus","gubernatorius","holograma","honoraras",
        "hormonas","horoskopas","humoras","ideologija","ikona","ilona","impozantiškas",
        "improvizacija","inovacija","inspektorius","intonacija","izoliacija","izoliacijai",
        "izoliaciją","izoliacijų","izoliatorius","izoliuota","izoliuotas","kolekcija","kolonija",
        "komandiruotė","kombinacija","komentaras","komercija","komisija","komitetas","kompanija",
        "kompensacija","kompetencija","kompiuteris","kompleksas","komplikacija","kompozicija",
        "kompromisas","komunalinis","komunikacija","komunizmas","koncentracija","koncepcija",
        "koncernas","konditeris","konfliktas","kongresas","konkretus","konkurencija","konkursas",
        "konsekvencija","konservatorius","konsolidacija","konspektas","konstanta","konstitucija",
        "konstrukcija","konsultacija","kontaktas","konteineris","kontekstas","kontinentas",
        "kontrabanda","kontraktas","kontrastas","konvencija","kooperacija","koordinacija",
        "korektorius","korupcija","kosmonautas","kostiumas","kotletas","laboratorija",
        "lokalizacija","lokomotyvas","memorandumas","meteorologija","metropolis","mikroskopas",
        "mitologija","mobilizacija","mobilus","moderatorius","modernizacija","modifikacija",
        "molekulė","momentas","monarchija","moneta","monitorius","monografija","monologas",
        "monopolija","monotonija","morfologija","motociklas","mozaika","navigatorius","nostalgija",
        "notaras","obligacija","odisėja","operatorius","optimistas","optimizmas","oratorija",
        "orbita","organizatorius","orkestras","ortodoksas","poetas","poezija","poliglotas",
        "poligonas","poliklinika","politechnika","politikas","pomidoras","populiarus","porcelianas",
        "pornografija","postulatas","pozityvus","problematika","procedūra","procesas","prodiuseris",
        "produktas","profesija","prognozė","progresas","projektas","projektorius","prokuroras",
        "proletaras","propeleris","proporcija","proporcingas","propozicija","prospektas",
        "protagonistas","protokolas","provincija","provizorius","provokacija","prozodija",
        "psichologas","psichologija","reformatorius","rekomendacija","renovacija","reorganizacija",
        "revoliucija","rezoliucija","romanas","romantika","senatorius","simbolis","statorius",
        "strofa","supozicija","technologija","televizorius","teologija","terminologija",
        "terorizmas","topografija","traktorius","transformacija","vokalas","zoologija"
    )

    // Lexicon: lowercase word -> token list (loaded from lt_lex.tsv)
    @Volatile private var lex: Map<String, List<String>>? = null

    private fun loadLex(): Map<String, List<String>> {
        lex?.let { return it }
        synchronized(this) {
            lex?.let { return it }
            val map = mutableMapOf<String, List<String>>()
            try {
                val lines = Assets.lines("lt_lex.tsv", CP1257)
                for (line in lines) {
                    val l = line.trimEnd('\r', '\n')
                    if ('\t' !in l) continue
                    val idx = l.indexOf('\t')
                    val k = l.substring(0, idx)
                    val v = l.substring(idx + 1).split(Regex("\\s+")).filter { it.isNotEmpty() }
                    map[k] = v
                }
            } catch (_: Exception) {}
            return map.also { lex = it }
        }
    }

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

    /** Replace unstressed 'oo' with 'o' for known loanwords. */
    private fun shortenO(w: String, toks: List<String>): List<String> =
        if (w in SHORT_O) toks.map { if (it == "oo") "o" else it } else toks

    /** The word whose strlen the engine's s7c would see: the i-hiatus doubling feeds the engine-equivalent
     *  word (ios is rendered as iios), so the se8-arm midpoint must use the expanded length too -- gated
     *  exactly like transcribe() (OOV only: a lexicon word never doubles). */
    fun s7cWord(word: String): String =
        if (word.length >= 2 && word[0] in "iI" && loadLex()[word.lowercase()] == null)
            iHiatus(word)
        else word

    /**
     * Full token list with leading/trailing '_'. Two tiers:
     * 1) exact lexicon hit -> transcr4's own accented tokens (bit-exact);
     * 2) OOV -> Accent.accent() + Render.render() (the ported pipeline).
     * Falls back to a rule-based g2p only if the renderer errors.
     */
    fun transcribe(word: String): List<String> {
        // VOWELLESS word -> spell letter names as phonemes (engine lexicon hit preferred)
        val sp = Spell.spellOut(word) { loadLex()[it] }
        if (sp != null) return sp
        var w2 = xqNormalize(word)
        var wl = w2.lowercase()
        // exact lexicon hit
        loadLex()[wl]?.let { return it }
        w2 = iHiatus(w2)                       // OOV word-initial i+vowel: ios -> iios (see iHiatus)
        wl = w2.lowercase()
        loadLex()[wl]?.let { return it }       // the doubled form may itself be a lexicon word
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
