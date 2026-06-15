package lt.gintaras.tts.engine

// Kotlin port of lt_tts/speak.py
// Multi-word / number synthesizer.

internal object Speak {

    private const val SR = 22050
    // Engine-measured silences (tts_cli, natural rate, gap between 'labas' and 'rytas'): word gap 660
    // samples, comma 1318, . ; : ! ? 10579, em dash 11240; NO extra lead/tail beyond the render's own
    // thr/5-ms tail. Each clause render already ENDS with that 660-sample engine tail, so PAUSE holds only
    // the REMAINDER (1318-660=658 ~ 0.03s; 10579-660=9919 ~ 0.45s; 11240-660=10580 ~ 0.48s). The old
    // 0.04/0.22 lead/tail and 0.20-0.36 pauses padded every screen-reader chunk and read audibly slower
    // than the original SAPI4 voice.
    private const val LEAD = 0.0
    private const val TAIL = 0.0
    private const val SPELL_GAP = 0.05

    private val PAUSE = mapOf(
        ',' to 0.03, ';' to 0.45, ':' to 0.45,
        '.' to 0.45, '!' to 0.45, '?' to 0.45, '—' to 0.48
    )

    private fun sil(sec: Double): IntArray = IntArray((sec * SR).toInt())

    private fun isLetterToken(word: String): Boolean {
        if (word.isEmpty()) return false
        if (word.length == 1 && word[0].isLetter()) return true
        return try { Spell.spellOut(word) != null } catch (_: Exception) { false }
    }

    fun synthText(
        text: String,
        rate: Int? = null,
        pitch: Int? = null,
        capitalPitch: Boolean = true,
        readEmoji: Boolean? = null,
        readCyrillic: Boolean? = null,
        readLatvian: Boolean? = null,
        readPunctuation: Boolean? = null
    ): IntArray {
        val expanded = Symbols.expand(text,
            readEmoji = readEmoji ?: true,
            readCyrillic = readCyrillic ?: true,
            readLatvian = readLatvian ?: true,
            readPunctuation = readPunctuation ?: false   // skip punctuation: the screen reader's own
                                                         // punctuation setting decides whether it's named
        )
        val numbered = Numerals.expandText(expanded)

        // Synthetic pause scale: the engine scales ALL its silences with the rate (duration ~ a1), so
        // our own LEAD/TAIL/clause pauses must follow -- a fast rate with fixed 0.2-0.36s pauses is what
        // made fast reading feel slower than the original SAPI4 voice. rate=null -> 1.0 (unchanged).
        val pf = if (rate == null) 1.0 else Backend.rateThr(rate) / 150.0

        val out = mutableListOf<Int>()
        out.addAll(sil(LEAD * pf).toList())

        // split on punctuation, preserving delimiters
        val parts = mutableListOf<String>()
        val delims = mutableListOf<Char>()
        var last = 0
        for ((i, c) in numbered.withIndex()) {
            if (c in PAUSE) {
                parts.add(numbered.substring(last, i))
                delims.add(c)
                last = i + 1
            }
        }
        parts.add(numbered.substring(last))
        delims.add(' ')

        for ((clauseRaw, delim) in parts.zip(delims)) {
            val clause = clauseRaw.trim()
            if (clause.isNotEmpty()) {
                val toks = clause.split(Regex("\\s+")).filter { it.isNotEmpty() }
                if (capitalPitch && toks.isNotEmpty() && toks.all { isLetterToken(it) }) {
                    // spell mode: render each letter discretely. Case is NOT pitch-distinguished -- the screen
                    // reader has its own capital-letter setting, so a capital renders at the SAME pitch.
                    for (t in toks) {
                        val lp = pitch
                        try {
                            val pcm = Backend.synthesize(
                                PlanBuilder.buildPlanPhase2(t, rate = rate, pitch = lp),
                                rate = rate, pitch = lp
                            )
                            out.addAll(pcm.toList())
                            out.addAll(sil(SPELL_GAP * pf).toList())
                        } catch (_: Exception) {}
                    }
                } else {
                    val question = delim == '?'
                    try {
                        val pcm = Backend.synthesize(
                            PlanBuilder.buildPlanPhrase(clause, question = question,
                                                        rate = rate, pitch = pitch),
                            rate = rate, pitch = pitch
                        )
                        out.addAll(pcm.toList())
                    } catch (_: Exception) {
                        // ONE unbuildable word must not silence the whole line (the old per-clause
                        // swallow made any failing word eat its entire sentence): retry word by word,
                        // dropping only the word(s) that actually fail.
                        for (t in toks) {
                            try {
                                val pcm = Backend.synthesize(
                                    PlanBuilder.buildPlanPhase2(t, rate = rate, pitch = pitch),
                                    rate = rate, pitch = pitch
                                )
                                out.addAll(pcm.toList())
                                out.addAll(sil(0.02 * pf).toList())
                            } catch (_: Exception) {}
                        }
                    }
                }
            }
            if (delim != ' ') {
                out.addAll(sil((PAUSE[delim] ?: 0.12) * pf).toList())
            }
        }

        out.addAll(sil(TAIL * pf).toList())
        return out.toIntArray()
    }
}
