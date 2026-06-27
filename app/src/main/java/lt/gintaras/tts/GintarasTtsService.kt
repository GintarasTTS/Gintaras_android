package lt.gintaras.tts

import android.media.AudioFormat
import android.speech.tts.SynthesisCallback
import android.speech.tts.SynthesisRequest
import android.speech.tts.TextToSpeech
import android.speech.tts.TextToSpeechService
import android.util.Log
import lt.gintaras.tts.engine.GintarasEngine
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

class GintarasTtsService : TextToSpeechService() {

    companion object {
        private const val TAG = "GintarasTTS"
        private const val SAMPLE_RATE = 22050
        private const val CHUNK_BYTES = 8192
        private const val WARMUP_TIMEOUT_SEC = 30L
    }

    private val warmupLatch = CountDownLatch(1)
    private lateinit var engine: GintarasEngine

    override fun onCreate() {
        super.onCreate()
        GintarasEngine.init(this)
        engine = GintarasEngine()
        val t = Thread {
            try {
                lt.gintaras.tts.engine.Voice.load()
                Log.i(TAG, "Engine warm-up complete")
            } catch (e: Throwable) {
                // Throwable (not just Exception): loading the lexicons/voice can throw
                // OutOfMemoryError on a low-heap device -- an Error would otherwise escape this
                // background thread and crash the whole process ("Gintaras TTS stopped").
                Log.w(TAG, "Engine warm-up failed", e)
            } finally {
                warmupLatch.countDown()
            }
        }
        t.name = "GintarasTTS-warmup"
        t.isDaemon = true
        t.priority = Thread.MAX_PRIORITY
        t.start()
    }

    override fun onIsLanguageAvailable(lang: String, country: String, variant: String): Int =
        if (lang == "lit") TextToSpeech.LANG_AVAILABLE else TextToSpeech.LANG_NOT_SUPPORTED

    override fun onGetLanguage(): Array<String> = arrayOf("lit", "LT", "")

    override fun onLoadLanguage(lang: String, country: String, variant: String): Int =
        onIsLanguageAvailable(lang, country, variant)

    override fun onStop() {}

    override fun onSynthesizeText(request: SynthesisRequest, callback: SynthesisCallback) {
        val text = request.charSequenceText?.toString()?.trim()
        if (text.isNullOrEmpty()) {
            callback.start(SAMPLE_RATE, AudioFormat.ENCODING_PCM_16BIT, 1)
            callback.done()
            return
        }

        // Block until warm-up completes so the first TalkBack label doesn't hit a cold start.
        warmupLatch.await(WARMUP_TIMEOUT_SEC, TimeUnit.SECONDS)

        val rate  = (request.speechRate / 2).coerceIn(0, 100)
        val pitch = (request.pitch       / 2).coerceIn(0, 100)

        if (callback.start(SAMPLE_RATE, AudioFormat.ENCODING_PCM_16BIT, 1) == TextToSpeech.ERROR) return

        try {
            // Split into clauses ONLY for first-audio latency (each clause is synthesized + streamed as a
            // unit so a long utterance starts playing early). The PAUSE between clauses is NOT invented here --
            // the delimiter is RE-ATTACHED to its clause so the ENGINE emits its own pause (the original
            // Gintaras pause model in Speak.kt: comma/dash short, . ; : ! ? long). Gintaras makes its own
            // pauses; the service must not add a second, mismatched one on top.
            for ((clause, delim) in splitClauses(text)) {
                val piece = clause + delim
                if (piece.isEmpty()) continue
                val samples = engine.synthPcm(piece, rate = rate, pitch = pitch)
                streamBytes(samplesToBytes(samples), callback) ?: run { callback.done(); return }
            }
        } catch (e: Throwable) {
            // Throwable (not just Exception): synthesis can hit OutOfMemoryError on a low-heap
            // device. Report the failure to the TTS framework instead of letting an Error crash
            // the process; the host then falls back gracefully rather than showing a crash dialog.
            Log.e(TAG, "Synthesis error for: $text", e)
            callback.error()
            return
        }

        callback.done()
    }

    private fun streamBytes(pcm: ByteArray, callback: SynthesisCallback): Unit? {
        var offset = 0
        while (offset < pcm.size) {
            val len = minOf(CHUNK_BYTES, pcm.size - offset)
            if (callback.audioAvailable(pcm, offset, len) == TextToSpeech.ERROR) return null
            offset += len
        }
        return Unit
    }

    private fun samplesToBytes(samples: IntArray): ByteArray {
        val buf = ByteBuffer.allocate(samples.size * 2).order(ByteOrder.LITTLE_ENDIAN)
        for (s in samples) buf.putShort(s.coerceIn(-32768, 32767).toShort())
        return buf.array()
    }

    private data class ClauseToken(val text: String, val delimiter: String)

    private fun splitClauses(text: String): List<ClauseToken> {
        // Split on punctuation into clause + delimiter, EXCEPT a delimiter sitting between two
        // digits (e.g. the colon in a time "21:20", or "8,5"). The engine's Symbols.expand turns
        // such an inter-digit delimiter into a plain word gap (short pause) when punctuation reading
        // is off -- so we must keep it inside the clause and let the engine handle it, instead of
        // splitting here and inserting our own long clause pause. This matches the NVDA/Python path,
        // which feeds the whole string straight to the engine.
        // Likewise a '.' glued BETWEEN TWO LETTERS ("lrt.lt", "jonas.lrt.lt") is not a sentence break:
        // the engine NAMES it ("taškas") in Symbols.expand BEFORE any clause split. If we split on it
        // here, the period lands at a clause edge (no letter on one side) and gets stripped to a pause
        // instead -- one "taškas" lost per glued period. Keep it in the clause and let the engine name it.
        val result = mutableListOf<ClauseToken>()
        val sb = StringBuilder()
        val n = text.length
        for (i in text.indices) {
            val c = text[i]
            if (c in ".,;:!?—") {
                val betweenDigits = i > 0 && text[i - 1].isDigit() &&
                                    i + 1 < n && text[i + 1].isDigit()
                val interLetterDot = c == '.' && i > 0 && text[i - 1].isLetter() &&
                                     i + 1 < n && text[i + 1].isLetter()
                if (betweenDigits || interLetterDot) {
                    sb.append(c)            // keep in clause -> engine names it / renders a word gap
                    continue
                }
                result.add(ClauseToken(sb.toString().trim(), c.toString()))
                sb.setLength(0)
                continue
            }
            sb.append(c)
        }
        if (sb.isNotEmpty()) result.add(ClauseToken(sb.toString().trim(), ""))
        return result.filter { it.text.isNotEmpty() || it.delimiter.isNotEmpty() }
    }
}
