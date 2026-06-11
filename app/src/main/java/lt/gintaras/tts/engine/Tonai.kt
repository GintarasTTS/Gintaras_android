package lt.gintaras.tts.engine

import kotlin.math.*

// Kotlin port of lt_tts/tonai.py
// Faithful port of transcr4.dll's `tonai`/`tonai1` exports (F0 / pitch contour).

internal object Tonai {

    // Constants (all dumped from DLL)
    private const val BASE_HZ  = 90.0
    private const val SEMI     = 1.03
    private const val MS       = 1000.0
    private const val T_OFF    = 0.017
    private const val GRID     = 0.01
    private const val PLO_W    = 0.08
    private const val PI_DLL   = 3.1415   // the DLL's pi
    private const val H_INTON  = 0.1
    private const val H_STRESS = 0.2
    private const val H_PLOS   = 0.13
    private const val TOL      = 2.0
    private const val HALF     = 0.5

    private val OBS  = setOf('b','d','g','k','p','t','s','S','z','Z','f','x','h','_')
    private val VOW_L = setOf('a','e','i','o','u')
    private val VOW_U = setOf('A','E','I','O','U')
    private val SON_U = setOf('L','M','N','R','J','W')

    // _ftol: MSVCRT _ftol truncate toward zero
    private fun ftol(x: Double): Int = x.toInt()

    // raised-cosine window on [-1,1]
    private fun win(x: Double): Double {
        if (x < -1.0 || x > 1.0) return 0.0
        return (cos(PI_DLL * x) + 1.0) / 2.0
    }

    // onset pulse 9u*e^{-3u} (x>=0)
    private fun pulse(x: Double): Double {
        if (x < 0.0) return 0.0
        return 9.0 * x * exp(-3.0 * x)
    }

    // phrase pitch range from last-word dur
    private fun range_(d: Double, sem: Double): Double {
        val t = if (sem > 0) 1.0 - sem / 20.0 else 1.0 - sem / 10.0
        return when {
            t * 0.32 > d -> d * 0.625
            t * 0.64 > d -> t / 5.0
            else         -> d * 0.3125
        }
    }

    // '.' final fall
    private fun finalPeriod(R: Double, T1: Double, t: Double): Double {
        if (R + T1 > t) return 0.0
        return 26.0 / ((t - T1 - R) / 0.15 + 1.0) - 26.0
    }

    // '?' final rise
    private fun finalQuest(R: Double, T1: Double, t: Double): Double {
        if (R > T1) return 0.0
        if (T1 + R > t) return 0.0
        val a = -T1 / 0.03
        val b = (26.0 / ((t - T1 - R) / 0.15 + 1.0) - 26.0) * 1.75
        return a - b
    }

    private data class Parsed(
        val name: List<String>,
        val dur: List<Int>,
        val mark: List<String>,
        val lastPlus: Int
    )

    private fun parse(stream: List<Pair<String, Int>>): Parsed {
        val name = mutableListOf<String>()
        val dur  = mutableListOf<Int>()
        val mark = mutableListOf<String>()
        var lastPlus = 0
        for ((tok, d) in stream) {
            if (tok == "+") {
                if (mark.isNotEmpty()) mark[mark.size - 1] = "+"
                lastPlus = name.size
            } else {
                name.add(tok); dur.add(d); mark.add("-")
            }
        }
        return Parsed(name, dur, mark, lastPlus)
    }

    private data class Targets(
        val A: List<Pair<Double, Double>>,  // plosive: (center, height)
        val B: List<Triple<Double, Double, Double>>,  // intonation: (center, width, height)
        val total: Double
    )

    private fun buildTargets(name: List<String>, dur: List<Int>): Targets {
        val n = name.size
        val A = mutableListOf<Pair<Double, Double>>()
        val B = mutableListOf<Triple<Double, Double, Double>>()
        var accum = 0.0
        for (i in 0 until n) {
            val c0 = name[i][0]
            val c1 = if (name[i].length > 1) name[i][1] else ' '
            val d = dur[i] / MS
            val pv = if (i > 0) name[i - 1][0] else '_'
            val nx = if (i + 1 < n) name[i + 1][0] else '_'
            // plosive perturbation
            if (c0 in "bdg" && c1 != 'z' && c1 != 'Z') {
                A.add(Pair(d + accum + T_OFF, H_PLOS / 2.0))
            } else if (c0 in "kpt" && c1 != 's' && c1 != 'S') {
                A.add(Pair(d + accum + T_OFF, H_PLOS))
            }
            // intonation / stress bump
            val alpha = (c0 in VOW_L && c1.code != ' '.code && c1 in VOW_U) || (c0 in SON_U)
            if (alpha) {
                val (cm, wm) = when {
                    pv in OBS && nx in OBS -> Pair(0.5, 2.0)
                    pv !in OBS && nx in OBS -> Pair(0.66, 1.66)
                    else -> Pair(1.0, 2.0)
                }
                B.add(Triple(d * cm + accum, d * wm, H_STRESS))
            } else {
                var emit = false
                if (c0 in VOW_U && c1.code != ' '.code && c1 in VOW_L) emit = true
                else if (c0 in VOW_U && c1.code == ' '.code && nx !in SON_U) emit = true
                if (emit) {
                    val (cm, wm) = when {
                        pv in OBS && nx in OBS -> Pair(0.75, 1.75)
                        pv !in OBS && nx in OBS -> Pair(0.75, 1.75)
                        pv in OBS && nx !in OBS -> Pair(1.0, 2.0)
                        else -> Pair(1.0, 2.0)
                    }
                    B.add(Triple(d * cm + accum, d * wm, H_STRESS))
                }
            }
            accum += d
        }
        return Targets(A, B, accum)
    }

    private fun bumps(t: Double, A: List<Pair<Double,Double>>, B: List<Triple<Double,Double,Double>>): Double {
        var s = 0.0
        for ((j, b) in B.withIndex()) {
            val (c, w, h) = b
            val hh = if (j == B.size - 1) h * HALF else h
            if (w != 0.0) s += win((t - c) / w) * hh
        }
        for ((j, a) in A.withIndex()) {
            val (c, h) = a
            val hh = if (j == A.size - 1) h * HALF else h
            s += win((t - c) / PLO_W) * hh
        }
        return s
    }

    /**
     * Returns list of (token, dur, listOf(Pair(pos%,f0))) reproducing transcr4 tonai.
     * stream = ilgiai output list (tok, dur).
     * semitone = pitch shift (clamped ±24).
     * punct = ending punctuation codepoint (ord('.'), ord('?'), or 0).
     */
    fun tonai(stream: List<Pair<String, Int>>, semitone: Int = 0, punct: Int = 0):
            List<Triple<String, Int, List<Pair<Int, Int>>>> {
        val (name, dur, mark, lastPlus) = parse(stream)
        val n = name.size
        if (n == 0) return emptyList()
        val sem = semitone.coerceIn(-24, 24).toDouble()
        val base = BASE_HZ * SEMI.pow(sem)
        val logbase = ln(base)
        val (A, B, total) = buildTargets(name, dur)
        val t0 = dur[0] / MS
        val T1 = (0 until lastPlus).sumOf { dur[it] / MS }
        val T2 = (lastPlus until n - 1).sumOf { dur[it] / MS }
        val declScale = if (T1 + T2 > 1.0) (T1 + T2) * H_INTON else H_INTON
        val R = range_(T2, sem)

        val fin: (Double) -> Double = when (punct) {
            '.'.code -> { t -> finalPeriod(R, T1, t) }
            '?'.code -> { t -> finalQuest(R, T1, t) }
            else     -> { t -> finalPeriod(R, T1, t) / 2.0 }
        }

        // evaluate F0 on 10ms grid
        val gt = mutableListOf<Double>()
        val gf = mutableListOf<Double>()
        var t = 0.0; var k = 0
        while (t < total && k < 10000) {
            val lf = logbase + bumps(t, A, B)
            val v = exp(lf + pulse(t - t0) * declScale) + fin(t)
            gt.add(t); gf.add(v)
            t += GRID; k++
        }
        val K = gt.size

        // resample per phoneme with polyline decimation (tol 2 Hz)
        val out = mutableListOf<Triple<String, Int, List<Pair<Int, Int>>>>()
        var accum = 0.0
        var gi = 0
        var g = 2
        for (i in 0 until n) {
            val pts = mutableListOf<Pair<Int, Int>>()
            if (i == 0 && K > 0) pts.add(Pair(0, ftol(gf[0] + HALF)))
            val d = dur[i] / MS
            val phonEnd = d + accum
            while (g < K) {
                if (gt[g] >= phonEnd) break
                val denom = gt[g] - gt[gi]
                var flag = 1
                if (denom != 0.0) {
                    for (m in gi + 1 until g) {
                        val interp = (gf[g] - gf[gi]) * (gt[m] - gt[gi]) / denom + gf[gi] - gf[m]
                        if (abs(interp) >= TOL) { flag = 0; break }
                    }
                }
                if (flag == 0) {
                    val pos = if (gt[g] < accum) 0 else ftol((gt[g] - accum) / d * 100.0 + HALF)
                    pts.add(Pair(pos, ftol(gf[g] + HALF)))
                    gi = g - 1
                }
                g++
            }
            // phrase-end point on last phoneme
            if (g >= K && K >= 2) {
                val pEnd = d + accum
                val denom = gt[K - 1] - gt[K - 2]
                if (denom != 0.0) {
                    val f0e = (gf[K - 1] - gf[K - 2]) * (pEnd - gt[K - 2]) / denom + gf[K - 2]
                    pts.add(Pair(100, ftol(f0e + HALF)))
                }
            }
            accum += d
            out.add(Triple(name[i], dur[i], pts))
            if (mark[i] == "+") out.add(Triple("+", 0, emptyList()))
        }
        return out
    }
}
