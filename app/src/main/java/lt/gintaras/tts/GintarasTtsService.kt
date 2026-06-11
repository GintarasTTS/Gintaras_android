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

class GintarasTtsService : TextToSpeechService() {

    companion object {
        private const val TAG = "GintarasTTS"
        private const val SAMPLE_RATE = 22050
        private const val CHUNK_BYTES = 8192
    }

    private lateinit var engine: GintarasEngine

    override fun onCreate() {
        super.onCreate()
        GintarasEngine.init(this)
        engine = GintarasEngine()
        // Pre-warm: load voice data + lexicons on a background thread
        Thread {
            try {
                lt.gintaras.tts.engine.Voice.load()
                Log.i(TAG, "Engine warm-up complete")
            } catch (e: Exception) {
                Log.w(TAG, "Engine warm-up failed", e)
            }
        }.also { it.name = "GintarasTTS-warmup"; it.isDaemon = true }.start()
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

        // Android rate/pitch: 100 = normal. Engine scale: 50 = natural (0-100).
        val rate  = (request.speechRate / 2).coerceIn(0, 100)
        val pitch = (request.pitch       / 2).coerceIn(0, 100)

        if (callback.start(SAMPLE_RATE, AudioFormat.ENCODING_PCM_16BIT, 1) == TextToSpeech.ERROR) {
            Log.e(TAG, "callback.start() returned ERROR")
            return
        }

        try {
            val samples = engine.synthPcm(text, rate = rate, pitch = pitch)
            val pcm = samplesToBytes(samples)
            var offset = 0
            while (offset < pcm.size) {
                val len = minOf(CHUNK_BYTES, pcm.size - offset)
                if (callback.audioAvailable(pcm, offset, len) == TextToSpeech.ERROR) {
                    Log.e(TAG, "audioAvailable() error at offset $offset")
                    break
                }
                offset += len
            }
        } catch (e: Exception) {
            Log.e(TAG, "Synthesis error for: $text", e)
            callback.error()
            return
        }

        callback.done()
    }

    private fun samplesToBytes(samples: IntArray): ByteArray {
        val buf = ByteBuffer.allocate(samples.size * 2).order(ByteOrder.LITTLE_ENDIAN)
        for (s in samples) buf.putShort(s.coerceIn(-32768, 32767).toShort())
        return buf.array()
    }
}
