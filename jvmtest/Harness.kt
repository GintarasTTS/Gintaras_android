package lt.gintaras.tts.engine

import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest

// Engine-parity harness: renders every case through the KOTLIN engine on a desktop JVM and writes
// one result line per case: "<md5-of-int16le-pcm> <sample-count>". Compared verbatim against the
// golden file produced by jvmtest/gen_golden.py from the bundled PYTHON reference engine
// (app/src/main/python/lt_tts) -- the port is correct only if every line is IDENTICAL (the Python
// engine itself is validated bit-exact against the original hlas/transcr4 DLLs).
//
// usage: Harness <dataDir> <cases.tsv> <out.tsv>
//   cases.tsv: one case per line, TAB-separated:  text \t rate(int|-) \t pitch(int|-)

fun main(args: Array<String>) {
    Assets.dataDir = File(args[0])
    if (args[1] == "--debug") {
        // diagnostic mode: print the front-end + selection trace for each word on the cmdline
        val out = java.io.PrintStream(System.out, true, "UTF-8")
        for (w in args.drop(2)) {
            out.println("== $w")
            out.println("tokens: " + Transcribe.transcribe(w).joinToString(" "))
            try {
                val st = Accent.accent(w)
                out.println("accent: $st")
                out.println("render: " + Render.render(w.uppercase(), st))
            } catch (e: Exception) {
                out.println("OOV-PIPELINE THREW:")
                e.printStackTrace(out)
            }
            for (s in PlanBuilder.debugFrames(w)) out.println("  $s")
        }
        return
    }
    val out = StringBuilder()
    for (raw in File(args[1]).readLines()) {
        if (raw.isBlank()) continue
        val f = raw.split("\t")
        val text = f[0]
        val rate = f.getOrNull(1)?.takeIf { it != "-" }?.toInt()
        val pitch = f.getOrNull(2)?.takeIf { it != "-" }?.toInt()
        val res = try {
            val pcm = Speak.synthText(text, rate = rate, pitch = pitch)
            val bb = ByteBuffer.allocate(pcm.size * 2).order(ByteOrder.LITTLE_ENDIAN)
            for (s in pcm) bb.putShort(s.toShort())
            val md5 = MessageDigest.getInstance("MD5").digest(bb.array())
                .joinToString("") { "%02x".format(it) }
            "$md5 ${pcm.size}"
        } catch (e: Exception) {
            "ERROR ${e.javaClass.simpleName}: ${e.message}"
        }
        out.append(res).append('\n')
        System.err.println("done: $text rate=$rate pitch=$pitch -> $res")
    }
    File(args[2]).writeText(out.toString())
}
