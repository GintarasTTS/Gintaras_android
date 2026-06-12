package lt.gintaras.tts.engine

// Bit-for-bit Kotlin port of lt_tts/backend.py
// Epoch-placement synthesis core (the hlas sub_1000E410 / sub_1000FDA0 laws).

internal object Backend {

    private const val THR = 150
    private const val BASE_P = 22050 / 100   // = 220 added to e4 per voiced E410 pass

    // ---- pitch / rate control (SAPI4-exact; see lt_tts/backend.py for the full disassembly notes) ----
    // hlas SetRate(S): ebp = trunc(200000/S); a1 = trunc(trunc(150*ebp/200)/10).  S=100 -> a1=150 (the
    // bit-exact baseline), S=450 -> 33, S=800 -> 18.  NVDA's SAPI4 driver mapped its 0..100 slider
    // linearly over the engine-reported [100..800]: wpm = round(100 + 7r).
    // hlas SetPitch(H): P_DC = trunc(22050/H), H in [75..130] Hz; slider p -> Hz = round(75 + 0.55p).
    // The synthesis period is clamp(s94 + P_DC, 169, 294) (sub_1000FC80) -- P_DC shifts the whole F0
    // base while the s90/s94/se8 contour offsets stay untouched. No resampling, no period scaling.
    private const val PDC_NAT = 294           // P_DC at the 75 Hz sentinel = every bit-exact capture's setting
    private const val PDC_DEF_PERIOD = 220    // trunc(22050 / 100 Hz): the s94 seed anchor

    // Python round() is round-half-to-even; Math.rint matches it exactly.
    private fun pyRound(x: Double): Int = Math.rint(x).toInt()

    fun rateThr(r: Int?): Int {
        if (r == null) return THR
        val wpm = pyRound(100 + 7.0 * r)
        val ebp = 200000 / wpm
        return maxOf(1, (150 * ebp / 200) / 10)
    }

    fun pitchPdc(p: Int?): Int {
        if (p == null) return PDC_NAT
        var hz = pyRound(75 + 0.55 * p)
        if (hz < 75) hz = 75
        if (hz > 130) hz = 130
        return 22050 / hz
    }

    // The utterance-initial s94: fda0's reseed (s94 = P_DC - 220) followed by its own IIR step toward
    // the base s90=-20. pdc=294 -> 68 (the value every natural capture showed).
    fun pitchS94Seed(pdc: Int): Int = iir(pdc - PDC_DEF_PERIOD, -20)

    // ---- integer IIR + period clamping --------------------------------------------------

    private fun iir(s94: Int, s90: Int): Int {
        val x = s90 - s94
        return s94 + (if (x > 0) (x shr 4) + 1 else (x shr 4))
    }

    private fun period(s94: Int, pdc: Int): Int {
        val v = s94 + pdc
        return when { v < 169 -> 169; v > 294 -> 294; else -> v }
    }

    private fun trunc(a: Int, b: Int): Int = a / b  // truncates toward zero in Kotlin

    // ---- se8 ramp constants (sub_1000FDA0) ----------------------------------------------
    private const val SE8_SD2 = 20
    private const val SE8_HI  = 160
    private const val SE8_BASE = 20

    // ---- Plan ---------------------------------------------------------------------------

    data class Frame(
        val pcm: IntArray,
        val s90: Int,
        val s94: Int,
        val pause: Boolean,
        val reseed: Boolean,
        val a5: Int = 0,
        val release: Boolean = false,
        val n2: IntArray? = null,
        val ngrain: Int? = null,
        val wordStart: Boolean = false,
        val se8Base: Int = SE8_BASE,
        val silence: Boolean = false,   // inter-word gap (ring-flush marker; the phase2 builder never sets it)
        val pi: Int? = null,
        val key: String? = null,
    )

    class Plan(val frames: List<Frame>, val tailSilence: List<Int>) {
        var se8Ramp: Boolean = frames.any { it.release }
        var releaseRpos: Int? = null
        var releaseRposList: List<Int?>? = null
        var capThr: Int? = null
    }

    // ---- synthesize ---------------------------------------------------------------------

    fun synthesize(plan: Plan, rate: Int? = null, pitch: Int? = null,
                   frameRpos: MutableList<Int>? = null): IntArray {
        val frames = plan.frames
        val thr = if (rate == null) (plan.capThr ?: THR) else rateThr(rate)
        val pdc = pitchPdc(pitch)
        val releaseRposInit = plan.releaseRpos
        val armList = plan.releaseRposList
        var armI = 0
        val ramp = plan.se8Ramp || releaseRposInit != null || armList != null

        var e4 = 0; var f8 = 0; var prev = 0; var rpos = 0
        var release: Int? = releaseRposInit
        var se8 = 0; var sf0 = 0
        var s94 = if (frames.isNotEmpty()) frames[0].s94 else 0
        val grains = mutableListOf<IntArray>()
        val flushIdx = mutableSetOf<Int>()
        var prevSilence = false
        var grainTotal = 0                      // running sum of grain lengths (for frameRpos)

        fun pper(s: Int): Int = period(s, pdc)  // pdc=294 (pitch=null) => the old fixed clamp => bit-exact

        val se8Sec = 50 * thr / 75              // [+0xec] = base_dur(50)*a1/75: the rate-scaled chase step
                                                // (thr=150 -> 100 = the validated natural constant)

        fun se8Burst() {
            // fda0 tail loop: se8 += sd2 while rpos > sf0 + se8Sec, clamped to [0, hi]; armed only
            // after the release node (sd2=20, hi=160), before it sd2=0 so se8 holds 0 (=> s90=-20).
            val sd2 = if (release != null && rpos >= release!!) SE8_SD2 else 0
            val hi = if (sd2 != 0) SE8_HI else 0
            var c = 0
            while (rpos > sf0 + se8Sec && c < 50) {
                se8 = when {
                    se8 + sd2 > hi -> SE8_HI
                    se8 + sd2 < 0  -> 0
                    else           -> se8 + sd2
                }
                sf0 += se8Sec; c++
            }
        }

        for (fr in frames) {
            val base = fr.se8Base
            if (ramp && fr.wordStart) {
                se8 = 0; sf0 = rpos; release = null
                e4 = 0; f8 = 0                  // fresh per-word epoch clock (phrase declination)
                if (armList != null) armI++
            }
            if (armList != null) {
                val arm = if (armI < armList.size) armList[armI] else null
                if (release == null && arm != null && rpos >= arm) release = arm
            } else if (ramp && release == null && fr.release) {
                release = rpos
            }
            if (prevSilence && !fr.silence) flushIdx.add(grains.size)
            prevSilence = fr.silence

            if (fr.pause) {
                grains.add(fr.pcm.copyOf())
                grainTotal += fr.pcm.size
                prev = fr.pcm.last()
                rpos += fr.pcm.size
                // each a6==0 pause frame still triggers ONE fda0 call => ONE s94 IIR + ONE se8 burst
                if (ramp) {
                    val s90e = (se8 shr 3) - base
                    s94 = iir(s94, s90e)
                    se8Burst()
                }
                frameRpos?.add(grainTotal)
                continue
            }
            if (fr.reseed) s94 = fr.s94
            val s90 = fr.s90
            val nxt: IntArray? = if ((fr.n2?.isNotEmpty()) == true) fr.n2 else null

            var s94l = s94
            val pers = mutableListOf<Int>()
            val ng = if (rate == null) (fr.ngrain?.takeIf { it > 0 }) else null

            if (ng != null) {
                // GROUND-TRUTH epoch count (capture plans only; phase2 plans leave ngrain null)
                e4 += BASE_P * (fr.a5 + 1)
                repeat(ng) {
                    val per = pper(s94l)
                    pers.add(per)
                    val s90e = if (ramp) (se8 shr 3) - base else s90
                    f8 += per; rpos += per
                    if (ramp) se8Burst()
                    s94l = iir(s94l, s90e)
                }
            } else {
                // LITERAL sub_1000E410 epoch loop. Engine quirks (see backend.py): the continue-gate
                // after emitting an epoch sees the JUST-EMITTED period (pre-IIR refetch); the next epoch
                // emits that same fetch (the period lags one IIR step: P0,P1,P1,P2,...); a pass whose
                // first gate fails yields NO epoch (no force-one for voiced frames).
                var per = pper(s94l)            // frame-initial fetch (s94 from the previous frame)
                var cnt = 0
                for (pass in 0..fr.a5) {
                    e4 += BASE_P
                    if (trunc((f8 + per) * 100, e4) > thr) continue   // no epoch this pass
                    var first = true
                    while (cnt < 12) {
                        pers.add(per)
                        val s90e = if (ramp) (se8 shr 3) - base else s90
                        f8 += per; rpos += per; cnt++
                        val cand = trunc((f8 + per) * 100, e4)        // gate with the just-emitted period
                        if (ramp) se8Burst()
                        if (first) {            // pass-initial epoch: refetch AFTER its fda0 -> post-IIR
                            s94l = iir(s94l, s90e)
                            per = pper(s94l)
                            first = false
                        } else {                // inner epochs: refetch PRECEDES the fda0 -> one-step lag
                            val nper = pper(s94l)
                            s94l = iir(s94l, s90e)
                            per = nper
                        }
                        if (cand >= thr) break
                    }
                }
            }

            val total = pers.size
            for ((i, tgt) in pers.withIndex()) {
                val out: IntArray = if (total > 1 && nxt != null) {
                    val p1 = Dsp.e6b0(fr.pcm, tgt, prev)
                    val p2 = Dsp.e6b0(nxt, tgt, prev)
                    Dsp.ebc0(p1, p1.size, p2, p2.size, total, total - i, 1)
                } else {
                    Dsp.e6b0(fr.pcm, tgt, prev)
                }
                grains.add(out)
                grainTotal += out.size
                prev = out.last()
            }
            s94 = s94l
            frameRpos?.add(grainTotal)
        }

        if (rate == null) {
            for (n in plan.tailSilence) grains.add(IntArray(n))   // natural path: verbatim (bit-exact)
        } else {
            // EXACT engine law: the utterance-final silence flush = trunc(a1/5) ms.
            val tot = 22050 * (thr / 5) / 1000
            if (tot > 0) grains.add(IntArray(tot))
        }
        return Dsp.renderFromGrains(grains, flushIdx)
    }
}
