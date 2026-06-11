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

    // Short-o loanwords (from shorto_data.py — unstressed 'oo' -> 'o')
    private val SHORT_O = setOf(
        "pozityvus","terminologija","kontaktas","kostiumas","prospektas","proletaras",
        "politikas","fonetika","kompozicija","konstanta","postulatas","mikroskopas",
        "kosmonautas","reformatorius","konsultacija","komunizmas","gubernatorius",
        "eksponatas","inovacija","kontrastas","meteorologija","monotonija","problematika",
        "koncentracija","rezoliucija","orkestras","konsolidacija","kooperacija",
        "psichologas","protagonistas","metropolis","komisija","konstitucija","produktas",
        "konstrukcija","senatorius","kompensacija","ideologija","propeleris","populiarus",
        "profesija","komplikacija","romantika","televizorius","geometrija","zoologija",
        "komitetas","prognozė","konkursas","etimologija","kontraktas","pomidoras",
        "direktorius","pornografija","kompiuteris","organizatorius","procesas","dotacija",
        "evoliucija","optimistas","prodiuseris","komunalinis","statorius","optimizmas",
        "chromosoma","moneta","momentas","teologija","projektorius","konkretus",
        "ekspozicija","rekomendacija","poliglotas","komandiruotė","provokacija",
        "monitorius","moderatorius","fotografija","poetas","terorizmas","monologas",
        "poligonas","hormonas","ikona","fotonas","oratorija","poezija","revoliucija",
        "donoras","ekologija","fotosintezė","poliklinika","komercija","demonstracija",
        "komentaras","holograma","konvencija","nostalgija","kotletas","vokalas",
        "simbolis","proporcija","navigatorius","astrologija","globalus","mozaika",
        "konfliktas","inspektorius","propozicija","prozodija","konkurencija",
        "formuluotė","procedūra","korektorius","fonoteka","transformacija",
        "psichologija","kontekstas","konsekvencija","notaras","strofa","progresas",
        "filologija","provizorius","dekompozicija","kompanija","operatorius",
        "demografija","kontrabanda","monarchija","lokomotyvas","detonacija",
        "monografija","intonacija","reorganizacija","improvizacija","memorandumas",
        "protokolas","projektas","diplomatas","modifikacija","mitologija",
        "impozantiškas","konservatorius","orbita","kompetencija","ekonomija",
        "kompleksas","komunikacija","konditeris","geologija","kontinentas","kongresas",
        "dekoracija","provincija","molekulė","dispozicija","monopolija","formatas",
        "politechnika","romanas","topografija","traktorius","proporcingas","prokuroras",
        "odisėja","obligacija","renovacija","ekonomistas","koncepcija","humoras",
        "morfologija","supozicija","mobilus","ortodoksas","diskoteka","honoraras",
        "laboratorija","ilona","kompromisas","korupcija","porcelianas","technologija",
        "koordinacija"
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

    /** x->ks, q->k (the engine's phonetic value for these non-LT letters) */
    private fun xqNormalize(word: String): String {
        if ('x' !in word && 'X' !in word && 'q' !in word && 'Q' !in word) return word
        return word.map { c -> when (c) {
            'x' -> "ks"; 'X' -> "Ks"; 'q' -> "k"; 'Q' -> "K"; else -> c.toString()
        }}.joinToString("")
    }

    /** Replace unstressed 'oo' with 'o' for known loanwords. */
    private fun shortenO(w: String, toks: List<String>): List<String> =
        if (w in SHORT_O) toks.map { if (it == "oo") "o" else it } else toks

    /**
     * Full token list with leading/trailing '_'. Two tiers:
     * 1) exact lexicon hit -> transcr4's own accented tokens (bit-exact);
     * 2) OOV -> Accent.accent() + Render.render() (the ported pipeline).
     * Falls back to a rule-based g2p only if the renderer errors.
     */
    fun transcribe(word: String): List<String> {
        // VOWELLESS word -> spell letter names as phonemes
        val sp = Spell.spellOut(word)
        if (sp != null) return sp
        val w2 = xqNormalize(word)
        val wl = w2.lowercase()
        // exact lexicon hit
        loadLex()[wl]?.let { return it }
        // accent + render (the ported DLL pipeline)
        return try {
            val stress = Accent.accent(w2)
            val toks = Render.render(w2.uppercase(), stress)
            if (toks != null && toks.isNotEmpty()) shortenO(wl, toks) else fallbackG2P(wl)
        } catch (_: Exception) {
            fallbackG2P(wl)
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
