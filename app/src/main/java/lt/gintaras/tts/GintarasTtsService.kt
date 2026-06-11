package lt.gintaras.tts

import android.media.AudioFormat
import android.speech.tts.SynthesisCallback
import android.speech.tts.SynthesisRequest
import android.speech.tts.TextToSpeech
import android.speech.tts.TextToSpeechService
import android.util.Log
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

class GintarasTtsService : TextToSpeechService() {

    companion object {
        private const val TAG = "GintarasTTS"
        private const val SAMPLE_RATE = 22050
        private const val CHUNK_BYTES = 8192
        private const val WARMUP_TIMEOUT_SEC = 30L

        // Pause durations (ms) mirroring speak.py PAUSE table
        private val PAUSE_MS = mapOf(
            ',' to 200, ';' to 280, ':' to 280,
            '.' to 360, '!' to 360, '?' to 360, '—' to 280
        )
    }

    private val warmupLatch = CountDownLatch(1)

    @Volatile private var bridge: PyObject? = null

    override fun onCreate() {
        super.onCreate()
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }
        val t = Thread {
            try {
                val b = Python.getInstance().getModule("lt_tts_bridge")
                b.callAttr("warm_up")
                bridge = b
                Log.i(TAG, "Engine warm-up complete")
            } catch (e: Exception) {
                Log.w(TAG, "Engine warm-up failed", e)
                bridge = try {
                    Python.getInstance().getModule("lt_tts_bridge")
                } catch (_: Exception) { null }
            } finally {
                warmupLatch.countDown()
            }
        }
        t.name = "GintarasTTS-warmup"
        t.isDaemon = true
        t.priority = Thread.MAX_PRIORITY
        t.start()
    }

    // ---- Android TTS service contract --------------------------------------------------

    override fun onIsLanguageAvailable(lang: String, country: String, variant: String): Int =
        if (lang == "lit") TextToSpeech.LANG_AVAILABLE else TextToSpeech.LANG_NOT_SUPPORTED

    override fun onGetLanguage(): Array<String> = arrayOf("lit", "LT", "")

    override fun onLoadLanguage(lang: String, country: String, variant: String): Int =
        onIsLanguageAvailable(lang, country, variant)

    override fun onStop() {}

    // ---- synthesis ---------------------------------------------------------------------

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

        val b = bridge ?: run { callback.error(); return }

        try {
            for ((clause, delim) in splitClauses(text)) {
                if (clause.isNotEmpty()) {
                    val pcm: ByteArray = b.callAttr(
                        "synth_clause", clause, delim == "?", rate, pitch
                    ).toJava(ByteArray::class.java)
                    streamBytes(pcm, callback) ?: run { callback.done(); return }
                }
                if (delim.isNotEmpty()) {
                    val silMs = PAUSE_MS[delim[0]] ?: 120
                    val sil = ByteArray(SAMPLE_RATE * silMs / 1000 * 2) // 16-bit silence
                    streamBytes(sil, callback) ?: run { callback.done(); return }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Synthesis error for: $text", e)
            callback.error()
            return
        }

        callback.done()
    }

    // ---- helpers -----------------------------------------------------------------------

    /** Returns null (and stops) on ERROR, or Unit on success. */
    private fun streamBytes(pcm: ByteArray, callback: SynthesisCallback): Unit? {
        var offset = 0
        while (offset < pcm.size) {
            val len = minOf(CHUNK_BYTES, pcm.size - offset)
            if (callback.audioAvailable(pcm, offset, len) == TextToSpeech.ERROR) return null
            offset += len
        }
        return Unit
    }

    private data class ClauseToken(val text: String, val delimiter: String)

    private fun splitClauses(text: String): List<ClauseToken> {
        val delimRegex = Regex("[.,;:!?—]")
        val parts = delimRegex.split(text)
        val delims = delimRegex.findAll(text).map { it.value }.toList()
        return parts.indices.map { i ->
            ClauseToken(parts[i].trim(), if (i < delims.size) delims[i] else "")
        }.filter { it.text.isNotEmpty() || it.delimiter.isNotEmpty() }
    }
}
