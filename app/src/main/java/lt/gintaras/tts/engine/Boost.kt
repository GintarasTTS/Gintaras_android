package lt.gintaras.tts.engine

// Kotlin port of lt_tts/boost.py: the speed boost ABOVE engine rate 100 (Sonic time-stretch, quality mode).
//
// Why: the engine speeds up only by dropping whole voiced frames; ~72% of rate-100 audio is never shortened, so
// pushing its own gate past rate 100 clips words. Extra speed is added AFTER synthesis instead.
//
// boostMilli is an INTEGER permille 1000..2000 (x1.0..x2.0). 1000 leaves every chunk untouched, so the output
// stays byte-identical to the un-boosted engine. THE PER-CHUNK RULE (identical in every port -- Sonic's output
// depends on how audio is chunked into the stream):
//   clause body  -> one Sonic stream: speed (float)boostMilli / 1000.0f, quality 1, ONE write, flush, read all
//   pure silence -> no Sonic: n_out = n_in * 1000 / boostMilli
internal object Boost {

    const val BOOST_MIN = 1000              // x1.0 = off
    const val BOOST_MAX = 2000              // x2.0 = the chosen maximum

    // Requirement: plain engine rate 100 WITHOUT Sonic must stay reachable on the host slider.
    const val PERCENT_PLATEAU_END = 225     // 200..225 % = engine rate 100 with NO boost
    const val PERCENT_MAX = 600             // TalkBack's ceiling: SpeechRateAndPitchActor.RATE_MAXIMUM = 6.0

    /** null -> 1000 (off); anything else clamped to BOOST_MIN..BOOST_MAX. */
    fun clampBoost(boostMilli: Int?): Int = boostMilli?.coerceIn(BOOST_MIN, BOOST_MAX) ?: BOOST_MIN

    /** A clause-body chunk at [boostMilli] (already clamped). 1000 returns [pcm] itself, untouched. */
    fun stretchBody(pcm: IntArray, boostMilli: Int): IntArray =
        if (boostMilli <= BOOST_MIN || pcm.isEmpty()) pcm else Sonic.changeSpeed(pcm, boostMilli)

    /** A pure-silence chunk (lead / inter-clause pause / tail) at [boostMilli]. 1000 returns [pcm] itself. */
    fun shortenSilence(pcm: IntArray, boostMilli: Int): IntArray =
        if (boostMilli <= BOOST_MIN) pcm else IntArray((pcm.size.toLong() * 1000L / boostMilli).toInt())

    /** Android SynthesisRequest.getSpeechRate() (100 = normal) -> boostMilli (lt_tts boost.boost_for_percent).
     *  The engine rate keeps its mapping min(speechRate / 2, 100), which reaches 100 at 200 %. 200..225 % is a
     *  plateau at plain rate 100 with no boost -- wider than one TalkBack gesture (x1.1), so every gesture sequence
     *  lands on it; from the 100 % default the 8th gesture gives 214 %. Above 225 % the boost rises linearly to
     *  x2.0 at 600 %; higher values stay x2.0 (TalkBack multiplies its rate by the system default rate). */
    fun boostForPercent(ratePercent: Int): Int {
        if (ratePercent <= PERCENT_PLATEAU_END) return BOOST_MIN
        val b = BOOST_MIN + (ratePercent - PERCENT_PLATEAU_END).toLong() * (BOOST_MAX - BOOST_MIN) /
            (PERCENT_MAX - PERCENT_PLATEAU_END)
        return minOf(BOOST_MAX.toLong(), b).toInt()
    }
}
