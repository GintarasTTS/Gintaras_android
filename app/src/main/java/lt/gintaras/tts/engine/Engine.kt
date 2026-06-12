package lt.gintaras.tts.engine

import android.content.Context

// Public API for the Gintaras TTS engine.
// Rate / pitch: 0..100, 50 = neutral. null = engine default.

class GintarasEngine(
    private val rate: Int? = null,
    private val pitch: Int? = null,
    private val capitalPitch: Boolean = true,
    private val readEmoji: Boolean = true,
    private val readCyrillic: Boolean = true,
    private val readLatvian: Boolean = true,
    // OFF by default: the screen reader (TalkBack) names punctuation itself according to the user's
    // punctuation-verbosity setting; the engine skipping it is what makes that setting work.
    private val readPunctuation: Boolean = false
) {
    companion object {
        const val SAMPLE_RATE = 22050

        fun init(context: Context) = Assets.init(context)
    }

    fun synthPcm(
        text: String,
        rate: Int? = null,
        pitch: Int? = null,
        capitalPitch: Boolean? = null,
        readEmoji: Boolean? = null,
        readCyrillic: Boolean? = null,
        readLatvian: Boolean? = null,
        readPunctuation: Boolean? = null
    ): IntArray = Speak.synthText(
        text,
        rate         = rate ?: this.rate,
        pitch        = pitch ?: this.pitch,
        capitalPitch = capitalPitch ?: this.capitalPitch,
        readEmoji    = readEmoji ?: this.readEmoji,
        readCyrillic = readCyrillic ?: this.readCyrillic,
        readLatvian  = readLatvian ?: this.readLatvian,
        readPunctuation = readPunctuation ?: this.readPunctuation
    )
}
