package lt.gintaras.tts.engine

// Bit-for-bit Kotlin port of lt_tts/backend.py
// TD-PSOLA synthesis engine.

internal object Backend {

    private const val THR = 150
    private const val BASE_P = 22050 / 100  // = 220
    private val DMIN = 100; private val DMAX = 800; private val DBASE = 200
    private const val PMID = 232.0

    // ---- rate / pitch control -----------------------------------------------------------

    fun rateThr(r: Int?): Int {
        if (r == null) return THR
        val ratePeriod = when {
            r <= 50 -> DMIN + (DBASE - DMIN) * r / 50.0
            else    -> DBASE + (DMAX - DBASE) * (r - 50) / 50.0
        }
        return (30000.0 / ratePeriod).roundToInt()
    }

    fun pitchFactor(p: Int?): Double {
        if (p == null) return 1.0
        return (294.55 - 1.25 * p) / PMID
    }

    private fun Double.roundToInt(): Int = if (this >= 0) (this + 0.5).toInt() else -((-this + 0.5).toInt())

    // ---- integer IIR + period clamping --------------------------------------------------

    private fun iir(s94: Int, s90: Int): Int {
        val x = s90 - s94
        return s94 + (if (x > 0) (x shr 4) + 1 else (x shr 4))
    }

    private fun period(s94: Int): Int {
        val v = s94 + 294
        return when { v < 169 -> 169; v > 294 -> 294; else -> v }
    }

    private fun trunc(a: Int, b: Int): Int = a / b  // truncates toward zero in Kotlin

    // ---- se8 ramp constants (sub_1000FDA0) ----------------------------------------------
    private const val SE8_SD2 = 20
    private const val SE8_SEC = 100
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
        val thr = plan.capThr?.let { if (rate == null) it else rateThr(rate) } ?: rateThr(rate)
        val pfac = pitchFactor(pitch)
        val releaseRpos = plan.releaseRpos
        val armList = plan.releaseRposList
        var armI = 0
        val ramp = plan.se8Ramp || releaseRpos != null || armList != null

        var e4 = 0; var f8 = 0; var prev = 0; var rpos = 0
        var release: Int? = releaseRpos
        var se8 = 0; var sf0 = 0
        var s94 = if (frames.isNotEmpty()) frames[0].s94 else 0
        val grains = mutableListOf<IntArray>()
        val flushIdx = mutableSetOf<Int>()
        var prevSilence = false

        val ppfac = pfac  // PSOLA pitch off by default → period scaling
        fun pper(s: Int): Int = maxOf(60, (period(s) * ppfac).roundToInt())

        fun se8Burst() {
            val sd2 = if (release != null && rpos >= release!!) SE8_SD2 else 0
            val hi = if (sd2 != 0) SE8_HI else 0
            var c = 0
            while (rpos > sf0 + SE8_SEC && c < 50) {
                se8 = when {
                    se8 + sd2 > hi -> SE8_HI
                    se8 + sd2 < 0  -> 0
                    else           -> se8 + sd2
                }
                sf0 += SE8_SEC; c++
            }
        }

        for ((k, fr) in frames.withIndex()) {
            val base = fr.se8Base
            if (ramp && fr.wordStart) {
                se8 = 0; sf0 = rpos; release = null
                e4 = 0; f8 = 0
                if (armList != null) armI++
            }
            if (armList != null) {
                val arm = if (armI < armList.size) armList[armI] else null
                if (release == null && arm != null && rpos >= arm) release = arm
            } else if (ramp && release == null && fr.release) {
                release = rpos
            }
            if (prevSilence && !fr.pause) flushIdx.add(grains.size)
            prevSilence = fr.pause

            if (fr.pause) {
                grains.add(fr.pcm.copyOf())
                prev = fr.pcm.last()
                rpos += fr.pcm.size
                if (ramp) {
                    val s90e = (se8 shr 3) - base
                    s94 = iir(s94, s90e)
                    se8Burst()
                }
                frameRpos?.add(grains.sumOf { it.size })
                continue
            }
            if (fr.reseed) s94 = fr.s94
            val s90 = fr.s90
            val nxt: IntArray? = if ((fr.n2?.isNotEmpty()) == true) fr.n2 else null

            var s94l = s94
            val pers = mutableListOf<Int>()
            val ng = if (rate == null) fr.ngrain else null

            if (ng != null) {
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
                for (pass in 0..fr.a5) {
                    e4 += BASE_P
                    var per = pper(s94l)
                    var cnt = 0
                    while (cnt < 12) {
                        val cand = trunc((f8 + per) * 100, e4)
                        val gate = if (cnt == 0) cand <= thr else cand < thr
                        if (!gate) break
                        pers.add(per)
                        val s90e = if (ramp) (se8 shr 3) - base else s90
                        f8 += per; rpos += per; cnt++
                        if (ramp) se8Burst()
                        s94l = iir(s94l, s90e)
                        per = pper(s94l)
                    }
                    if (cnt == 0 && thr >= THR) pers.add(pper(s94l))
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
                prev = out.last()
            }
            s94 = s94l
            frameRpos?.add(grains.sumOf { it.size })
        }

        for (n in plan.tailSilence) grains.add(IntArray(n))
        return Dsp.renderFromGrains(grains, flushIdx)
    }
}
