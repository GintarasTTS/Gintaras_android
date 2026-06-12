package lt.gintaras.tts.engine

import java.nio.charset.StandardCharsets

// Kotlin port of lt_tts/symbols.py
// Optional symbol reading: emoji, Cyrillic, Latvian-unique letters.

internal object Symbols {

    private var emojiMap: Map<String, String>? = null
    private var emojiRe: Regex? = null
    private var emojiReNoPunct: Regex? = null   // emoji regex EXCLUDING punctuation-named entries (the
                                                // readPunctuation=false table: a kept delimiter like the
                                                // em dash must pause, not be named)
    private var emojiLoaded = false
    private val letterMaps = mutableMapOf<String, Map<String, Map<String, String>>?>()

    // clause delimiters that drive pauses / the question contour -- never stripped
    private val DELIMS = setOf(',', ';', ':', '.', '!', '?', '—')
    // text-normalization symbols mainstream TTS engines DO read as part of plain text (50% -> "proc",
    // §3, A&B, #1, 20°) -- kept spoken even with punctuation off
    private val PUNCT_KEEP = setOf('%', '‰', '§', '¶', '&', '#', '°')

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

    // A table key that is a single punctuation char -- the entries gated by readPunctuation
    // (real emoji and the PUNCT_KEEP normalization symbols are never gated).
    private fun isPunctKey(k: String): Boolean =
        k.length == 1 && k[0] !in PUNCT_KEEP && isPunctChar(k[0])

    /** Replace every Unicode punctuation char (category P*) and spacing accent (Sk) with a space --
     *  the 'skip punctuation' reading every other TTS does. Clause delimiters survive (they only time
     *  pauses, they are never spoken) and the PUNCT_KEEP normalization symbols survive. This also keeps
     *  stray ASCII quotes/brackets/hyphens from ever reaching the word pipeline. */
    private fun stripPunct(text: String): String {
        val sb = StringBuilder(text.length)
        for (ch in text) {
            sb.append(if (ch !in DELIMS && ch !in PUNCT_KEEP && isPunctChar(ch)) ' ' else ch)
        }
        return sb.toString()
    }

    // Cyrillic block U+0400–U+04FF
    private val CYR_RE = Regex("[Ѐ-ӿ]+")

    // Latvian-unique letters (as in latvian.tsv)
    private val LAV_RE = Regex("[āĀēĒīĪōŌŗŖļĻņŅķĶģĢ]+")

    private fun loadEmoji(): Triple<Map<String, String>, Regex?, Regex?> {
        if (emojiLoaded) return Triple(emojiMap ?: emptyMap(), emojiRe, emojiReNoPunct)
        val map = mutableMapOf<String, String>()
        try {
            val lines = Assets.lines("emoji.tsv")
            for (line in lines) {
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
        val nop = map.keys.filter { !isPunctKey(it) }
        val reNop = if (nop.isNotEmpty())
            Regex(nop.sortedByDescending { it.length }.joinToString("|") { Regex.escape(it) })
        else null
        emojiMap = map; emojiRe = re; emojiReNoPunct = reNop; emojiLoaded = true
        return Triple(map, re, reNop)
    }

    private fun loadLetters(filename: String): Map<String, Map<String, String>>? {
        if (letterMaps.containsKey(filename)) return letterMaps[filename]
        val names = mutableMapOf<String, String>()
        val sounds = mutableMapOf<String, String>()
        try {
            var cur = ""
            val lines = Assets.lines(filename)
            for (line in lines) {
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
        val chars = text.codePoints().toArray()
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

        // Punctuation is SKIPPED by default: a screen reader (TalkBack) expands punctuation into words
        // ITSELF according to the user's punctuation-verbosity setting, so the engine staying silent on
        // it is what makes that setting work (naming it here made quotes etc. ALWAYS spoken).
        if (!readPunctuation) {
            val t2 = stripPunct(t)
            if (t2 != t) { t = t2; changed = true }
        }

        if (readEmoji) {
            val (emap, ereFull, ereNop) = loadEmoji()
            val ere = if (readPunctuation) ereFull else ereNop
            if (ere != null && ere.containsMatchIn(t)) {
                t = ere.replace(t) { " ${emap[it.value]} " }
                changed = true
            }
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
