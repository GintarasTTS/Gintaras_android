package lt.gintaras.tts.engine

import java.nio.charset.StandardCharsets

// Kotlin port of lt_tts/symbols.py
// Optional symbol reading: emoji, Cyrillic, Latvian-unique letters.

internal object Symbols {

    private var emojiMap: Map<String, String>? = null
    private var emojiRe: Regex? = null
    private val letterMaps = mutableMapOf<String, Map<String, Map<String, String>>?>()

    // Cyrillic block U+0400–U+04FF
    private val CYR_RE = Regex("[Ѐ-ӿ]+")

    // Latvian-unique letters (as in latvian.tsv)
    private val LAV_RE = Regex("[āĀēĒīĪōŌŗŖļĻņŅķĶģĢ]+")

    private fun loadEmoji(): Pair<Map<String, String>, Regex?> {
        emojiMap?.let { return Pair(it, emojiRe) }
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
        emojiMap = map; emojiRe = re
        return Pair(map, re)
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
               readLatvian: Boolean = true): String {
        var t = text
        var changed = false

        if (readEmoji) {
            val (emap, ere) = loadEmoji()
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
