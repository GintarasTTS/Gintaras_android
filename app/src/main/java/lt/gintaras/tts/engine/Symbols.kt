package lt.gintaras.tts.engine

import java.util.regex.Pattern

// Kotlin port of lt_tts/symbols.py
// Optional symbol reading: emoji, punctuation, Cyrillic, Latvian-unique letters.
// punct.tsv is the SINGLE symbol-name table -- the decimal, inter-letter and isolated-symbol rules all look
// names up there (the code holds only the RULE, never the spoken word). readPunctuation does NOT name prose:
// punctuation in running text is always left to the screen reader; it only decides whether a LONE symbol
// (typed / deleted / navigated to) is spoken by name. emoji.tsv shares the same flat `<char><TAB><text>` format.

internal object Symbols {

    private val maps = mutableMapOf<String, Pair<Map<String, String>, Regex?>>()  // filename -> (map, regex)
    private val letterMaps = mutableMapOf<String, Map<String, Map<String, String>>?>()

    // clause delimiters that drive pauses / the question contour -- never stripped
    private val DELIMS = setOf(',', ';', ':', '.', '!', '?', '—')
    // text-normalization symbols mainstream TTS engines DO read as part of plain text (50% -> "proc",
    // §3, A&B, #1, 20°) -- kept spoken even with punctuation off (they live in emoji.tsv, not punct.tsv)
    private val PUNCT_KEEP = setOf('%', '‰', '§', '¶', '&', '#', '°')

    // Decimal separator naming (espeak-style): a comma/period DIRECTLY between two digit groups is a decimal
    // mark and is SPOKEN, '2,5' -> '2 kablelis 5' -> "du kablelis penki" (fraction read as a whole number).
    // A run with two or more separators is a date/time/thousands group (2026.06.12, 21:20, 1,234,567), NOT a
    // decimal -- left untouched so the normal punctuation step turns those inter-digit separators into gaps.
    private val DECIMAL_RUN = Regex("\\d+(?:[.,:]\\d+)+")
    // WHICH inter-digit separators are decimal marks (the RULE; the spoken NAME comes from punct.tsv via name()).
    private val DECIMAL_SEPS = setOf(',', '.')

    // Always-read symbols (espeak-style), independent of the punctuation setting (number/identifier
    // formatting, not prose punctuation): a '-' before a digit and not preceded by a letter/digit is a
    // MINUS ('-15' -> 'minus 15'); a '.'/'*'/'@' glued between two LETTERS is read by name ('lrt.lt' ->
    // 'lrt taškas lt'). A '-' between letters is NOT read (Lithuanian hyphenated words read naturally),
    // and a '-' after a digit ('2026-06-12') is not a minus. UNICODE_CHARACTER_CLASS so \W/\d treat
    // Lithuanian letters (ą č ę ...) as letters, matching Python's re defaults.
    private val MINUS_RE = Pattern.compile("(?<![^\\W_])-(?=\\d)", Pattern.UNICODE_CHARACTER_CLASS).toRegex()
    // '.'/'*'/'@' glued between two letters are named (the RULE is this char class; the NAME comes from punct.tsv).
    private val INLETTER_RE = Pattern.compile("(?<=[^\\W\\d_])([.*@])(?=[^\\W\\d_])", Pattern.UNICODE_CHARACTER_CLASS).toRegex()

    /** Spoken Lithuanian name for a single symbol char, from the ONE table punct.tsv; "" if not listed. The
     *  decimal / inter-letter / isolated rules all draw names from here -- no rule hardcodes a spoken word. */
    private fun name(ch: Char): String = loadMap("punct.tsv").first[ch.toString()] ?: ""

    /** Name every symbol in an ALL-SYMBOL input (no letters/digits) from punct.tsv: a lone '.' -> "taškas",
     *  a run "->" -> each char named, space-joined. Unknown chars kept; whitespace dropped. Used only when
     *  readPunctuation is on -- prose (anything with a letter or digit) never reaches here. */
    private fun nameIsolated(text: String): String {
        val map = loadMap("punct.tsv").first
        return text.filter { !it.isWhitespace() }
            .map { ch -> map[ch.toString()] ?: ch.toString() }
            .filter { it.isNotEmpty() }
            .joinToString(" ")
    }

    /** Speak a leading-minus before a digit and a '.'/'*'/'@' glued between two letters. Runs BEFORE the
     *  punctuation step so these are spoken even with punctuation reading off. */
    private fun readSymbols(text: String): String {
        var t = MINUS_RE.replace(text, "minus ")
        t = INLETTER_RE.replace(t) { m -> val nm = name(m.groupValues[1][0]); if (nm.isNotEmpty()) " $nm " else m.value }
        return t
    }

    /** Name a LONE decimal separator between two digit groups: '2,5' -> '2 kablelis 5', '2.5' -> '2 taškas 5'.
     *  Runs BEFORE the punctuation step so the decimal is spoken even with punctuation reading off (number
     *  formatting, not prose punctuation). Only a SINGLE separator counts -- a chain (2026.06.12, 21:20,
     *  1,234,567) is a date/time/thousands group, left for the normal inter-digit-gap handling. A trailing
     *  sentence period ('2,5.') is not part of the digit run; '2, 3' (separator + space) never matches. */
    private fun readDecimals(text: String): String {
        return DECIMAL_RUN.replace(text) { m ->
            val run = m.value
            val seps = run.filter { it == '.' || it == ',' || it == ':' }
            val w = if (seps.length == 1 && seps[0] in DECIMAL_SEPS) name(seps[0]).ifEmpty { null } else null
            if (w != null) {
                val i = run.indexOf(seps[0])
                run.substring(0, i) + " " + w + " " + run.substring(i + 1)
            } else run
        }
    }

    private fun isPunctChar(ch: Char): Boolean {
        val t = Character.getType(ch)
        return t == Character.DASH_PUNCTUATION.toInt() ||
               t == Character.START_PUNCTUATION.toInt() ||
               t == Character.END_PUNCTUATION.toInt() ||
               t == Character.CONNECTOR_PUNCTUATION.toInt() ||
               t == Character.OTHER_PUNCTUATION.toInt() ||
               t == Character.INITIAL_QUOTE_PUNCTUATION.toInt() ||
               t == Character.FINAL_QUOTE_PUNCTUATION.toInt() ||
               t == Character.MODIFIER_SYMBOL.toInt()      // Sk: ` ´ ^ ¨ spacing accents
    }

    /** Replace every Unicode punctuation char (category P*) and spacing accent (Sk) with a space --
     *  the 'skip punctuation' reading every other TTS does. Clause delimiters survive (they only time
     *  pauses, they are never spoken) and the PUNCT_KEEP normalization symbols survive. This also keeps
     *  stray ASCII quotes/brackets/hyphens from ever reaching the word pipeline.
     *  A delimiter BETWEEN TWO DIGITS (21:20, 8.5, 2026.06.12) becomes a word gap, not a clause pause:
     *  the engine NAMES it there (dvitaškis/taškas/kablelis), so with naming off only the word boundary
     *  remains -- a 0.45s clause pause inside a clock time read as a long break. */
    private fun stripPunct(text: String): String {
        val sb = StringBuilder(text.length)
        for ((i, ch) in text.withIndex()) {
            val out = when {
                ch in DELIMS -> {
                    val digitCtx = i > 0 && text[i - 1].isDigit() &&
                                   i + 1 < text.length && text[i + 1].isDigit()
                    if (digitCtx) ' ' else ch
                }
                ch in PUNCT_KEEP -> ch
                isPunctChar(ch) -> ' '
                else -> ch
            }
            sb.append(out)
        }
        return sb.toString()
    }

    // Cyrillic block U+0400–U+04FF
    private val CYR_RE = Regex("[Ѐ-ӿ]+")

    // Latvian-unique letters (as in latvian.tsv)
    private val LAV_RE = Regex("[āĀēĒīĪōŌŗŖļĻņŅķĶģĢ]+")

    /** Load a flat `<char(s)><TAB><spoken text>` table (emoji.tsv / punct.tsv) into a {key: text} map and a
     *  combined regex (longest key first, so a multi-codepoint ZWJ emoji wins over its parts). Cached; a
     *  missing/unreadable file yields an empty map and a null regex (the feature becomes a no-op). */
    private fun loadMap(filename: String): Pair<Map<String, String>, Regex?> {
        maps[filename]?.let { return it }
        val map = mutableMapOf<String, String>()
        try {
            for (line in Assets.lines(filename)) {
                val l = line.trimEnd('\r', '\n')
                if (l.isEmpty() || l.startsWith("#") || '\t' !in l) continue
                val idx = l.indexOf('\t')
                val k = l.substring(0, idx)
                val v = l.substring(idx + 1).trim()
                if (k.isNotEmpty()) map[k] = v
            }
        } catch (_: Exception) {}
        val re = if (map.isNotEmpty())
            Regex(map.keys.sortedByDescending { it.length }.joinToString("|") { Regex.escape(it) })
        else null
        val result = Pair(map.toMap(), re)
        maps[filename] = result
        return result
    }

    /** Replace every key of `filename`'s table found in `text` with its spoken text (space-padded). */
    private fun subMap(text: String, filename: String): Pair<String, Boolean> {
        val (map, re) = loadMap(filename)
        if (re == null || !re.containsMatchIn(text)) return Pair(text, false)
        return Pair(re.replace(text) { " ${map[it.value]} " }, true)
    }

    private fun loadLetters(filename: String): Map<String, Map<String, String>>? {
        if (letterMaps.containsKey(filename)) return letterMaps[filename]
        val names = mutableMapOf<String, String>()
        val sounds = mutableMapOf<String, String>()
        try {
            var cur = ""
            for (line in Assets.lines(filename)) {
                val l = line.trimEnd('\r', '\n')
                if (l.isEmpty() || l.startsWith("#")) continue
                when (l) {
                    "[names]"  -> { cur = "names"; continue }
                    "[sounds]" -> { cur = "sounds"; continue }
                }
                if (cur.isEmpty() || '\t' !in l) continue
                val idx = l.indexOf('\t')
                val k = l.substring(0, idx)
                val v = l.substring(idx + 1).trim()
                if (k.isEmpty()) continue
                if (cur == "names") names[k] = v else sounds[k] = v
            }
        } catch (_: Exception) {
            letterMaps[filename] = null; return null
        }
        val result = mapOf("names" to names.toMap(), "sounds" to sounds.toMap())
        letterMaps[filename] = result
        return result
    }

    private fun subLetters(text: String, blocks: Map<String, Map<String, String>>, pattern: Regex): Pair<String, Boolean> {
        val names = blocks["names"] ?: emptyMap()
        val sounds = blocks["sounds"] ?: emptyMap()
        val out = StringBuilder()
        var changed = false
        val strs = text.map { it.toString() }
        val n = strs.size
        var i = 0
        while (i < n) {
            val ch = strs[i]
            if (pattern.matches(ch)) {
                val prevAlpha = i > 0 && strs[i - 1][0].isLetter()
                val nextAlpha = i + 1 < n && strs[i + 1][0].isLetter()
                val isolated = !prevAlpha && !nextAlpha
                if (isolated) {
                    val name = names[ch] ?: names[ch.lowercase()] ?: ""
                    out.append(" ").append(name).append(" ")
                } else {
                    val sound = sounds[ch] ?: sounds[ch.lowercase()] ?: ""
                    out.append(sound)
                }
                changed = true
            } else {
                out.append(ch)
            }
            i++
        }
        return Pair(out.toString(), changed)
    }

    fun expand(text: String,
               readEmoji: Boolean = true,
               readCyrillic: Boolean = true,
               readLatvian: Boolean = true,
               readPunctuation: Boolean = false): String {
        var t = text
        var changed = false

        // Decimal numbers first: a comma/period directly between two digit groups is a decimal mark and must
        // be SPOKEN ('2,5' -> 'du kablelis penki'), like espeak -- regardless of the punctuation setting.
        val td = readDecimals(t)
        if (td != t) { t = td; changed = true }

        // Minus before a digit, and '.'/'*'/'@' glued between letters -> spoken (espeak-style).
        val ts = readSymbols(t)
        if (ts != t) { t = ts; changed = true }

        // Punctuation policy: PROSE punctuation is ALWAYS left to the screen reader -- the engine never names
        // '.'/','/etc. inside running text, it only strips residual marks. readPunctuation governs ONLY an
        // ISOLATED symbol -- a lone mark with no letter and no digit (typed / deleted / navigated to): on ->
        // say its name from punct.tsv; off -> leave it to the host too (strip). Matches lt_tts symbols.py.
        val trimmed = t.trim()
        val isolated = trimmed.isNotEmpty() && trimmed.none { it.isLetter() || it.isDigit() }
        if (readPunctuation && isolated) {
            val named = nameIsolated(t)
            if (named.isNotEmpty() && named != t) { t = named; changed = true }
        } else {
            val t2 = stripPunct(t)
            if (t2 != t) { t = t2; changed = true }
        }

        if (readEmoji) {
            val (nt, c) = subMap(t, "emoji.tsv")
            t = nt; changed = changed || c
        }

        if (readCyrillic && CYR_RE.containsMatchIn(t)) {
            val blocks = loadLetters("cyrillic.tsv")
            if (blocks != null) {
                val (nt, c) = subLetters(t, blocks, Regex("[Ѐ-ӿ]"))
                t = nt; changed = changed || c
            }
        }

        if (readLatvian && LAV_RE.containsMatchIn(t)) {
            val blocks = loadLetters("latvian.tsv")
            if (blocks != null) {
                val (nt, c) = subLetters(t, blocks, Regex("[āĀēĒīĪōŌŗŖļĻņŅķĶģĢ]"))
                t = nt; changed = changed || c
            }
        }

        return if (changed) t.trim().replace(Regex("\\s+"), " ") else t
    }
}
