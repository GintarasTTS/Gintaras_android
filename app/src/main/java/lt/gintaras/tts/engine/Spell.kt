package lt.gintaras.tts.engine

// Kotlin port of lt_tts/spell.py + spell_data.py
// Phoneme spell-out for vowelless words (lt/cd/km/www).

internal object Spell {

    // cp1257 vowel set (checked against the word to decide spell-mode)
    private val VOWELS_SP = setOf(
        'a','e','i','o','u','y',
        'ą','ę','ė','į','ų','ū',
        'A','E','I','O','U','Y',
        'Ą','Ę','Ė','Į','Ų','Ū'
    )

    // spell_data.LETTER_PHON — consonant letter -> phoneme token list
    private val LETTER_PHON: Map<Char, List<String>> = mapOf(
        'b' to listOf("b'", "eE"),
        'c' to listOf("ts'", "eE"),
        'd' to listOf("d'", "eE"),
        'f' to listOf("E", "f"),
        'g' to listOf("g'", "eE"),
        'h' to listOf("h", "aA"),
        'j' to listOf("j'", "O", "t"),
        'k' to listOf("k", "aA"),
        'l' to listOf("E", "L"),
        'm' to listOf("E", "M"),
        'n' to listOf("E", "N"),
        'p' to listOf("p'", "eE"),
        'q' to listOf("k", "uU"),
        'r' to listOf("E", "R"),
        's' to listOf("E", "s"),
        't' to listOf("t'", "eE"),
        'v' to listOf("v'", "eE"),
        'w' to listOf("d", "a", "b'", "l'", "v'", "ee"),
        'x' to listOf("i", "k", "s"),
        'z' to listOf("z'", "eE"),
        'č' to listOf("tS'", "eE"),
        'š' to listOf("E", "S"),
        'ž' to listOf("Z'", "eE")
    )

    // spell_data.SPELLED_VOWELS — isolated special vowel -> phoneme token list
    private val SPELLED_VOWELS: Map<Char, List<String>> = mapOf(
        'y'  to listOf("ii", "i", "l", "g", "Oo", "j'", "i"),
        'ą'  to listOf("aa", "n", "oO", "s'", "i", "n'", "ee"),
        'ę'  to listOf("ea", "n", "oO", "s'", "i", "n'", "ee"),
        'ų'  to listOf("uu", "n", "oO", "s'", "i", "n'", "ee"),
        'ū'  to listOf("uu", "i", "l", "g", "Oo", "j'", "i")
    )

    fun isSpellable(word: String): Boolean {
        if (word.isEmpty()) return false
        if (word.any { it in VOWELS_SP }) return false
        return word.any { it.isLetter() }
    }

    /** Returns phoneme token list (with "_" boundaries) for a spelletable word, or null.
     *  lexLookup: optional lexicon probe -- a VOWELLESS word with an engine lexicon entry uses the
     *  engine's own spelled output (bit-exact, incl. the cross-letter assimilation the letter-phoneme
     *  concat can't model). */
    fun spellOut(word: String, lexLookup: ((String) -> List<String>?)? = null): List<String>? {
        if (word.isEmpty() || !word.any { it.isLetter() }) return null
        // single isolated y/ą/ę/ų/ū -> spell by name
        if (word.length == 1) {
            val lc = word[0].lowercaseChar()
            val sv = SPELLED_VOWELS[lc]
            if (sv != null) return listOf("_") + sv + listOf("_")
        }
        // word with a vowel -> normal transcription
        if (word.any { it in VOWELS_SP }) return null
        lexLookup?.invoke(word.lowercase())?.let { return it.toList() }
        // vowelless -> concatenate each consonant's phoneme tokens
        val out = mutableListOf("_")
        for (c in word.lowercase()) {
            val ph = LETTER_PHON[c]
            if (ph != null) out.addAll(ph)
        }
        if (out.size == 1) return null  // no known phonemes
        out.add("_")
        return out
    }
}
