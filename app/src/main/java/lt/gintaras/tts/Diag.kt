package lt.gintaras.tts

import android.content.Context
import lt.gintaras.tts.engine.GintarasEngine
import lt.gintaras.tts.engine.Voice

// On-device self-test for diagnosing "silent / no sound" reports without adb/logcat. Runs the SAME
// load + synthesis path the TTS service uses, in this app process, and returns a human-readable
// report: the heap limit (to see if largeHeap is in effect), timings, sample count on success, or
// the exact Throwable + stack trace on failure (OutOfMemoryError is an Error -- the service's
// catch(Throwable) hides it as silence, so this surfaces it). NOT part of the parity harness.
object Diag {
    fun run(ctx: Context): String {
        val sb = StringBuilder()
        val rt = Runtime.getRuntime()
        sb.append("Gintaras diagnostika\n")
        sb.append("maxMemory (krūvos riba): ${rt.maxMemory() / 1048576} MB\n")
        sb.append("(jei < ~90 MB, largeHeap neveikia šiame įrenginyje)\n\n")
        return try {
            GintarasEngine.init(ctx.applicationContext)
            var t = System.currentTimeMillis()
            Voice.load()
            sb.append("Voice.load: OK (${System.currentTimeMillis() - t} ms)\n")
            t = System.currentTimeMillis()
            val pcm = GintarasEngine().synthPcm("Tai kalbos sintezės lietuvių pavyzdys")
            sb.append("synthPcm: OK, ${pcm.size} sample (${System.currentTimeMillis() - t} ms)\n")
            val nz = pcm.count { it != 0 }
            sb.append("ne-tylos sample: $nz\n")
            sb.append("naudota krūva: ${(rt.totalMemory() - rt.freeMemory()) / 1048576} MB\n\n")
            sb.append(if (pcm.size > 1000 && nz > 100) "VARIKLIS VEIKIA. Jei TTS vis tiek tyli — problema garso/kalbos parinkime, ne variklyje."
                     else "Variklis grąžino tuščią/tylų garsą — tai problema.")
            sb.toString()
        } catch (e: Throwable) {
            sb.append("KLAIDA: ${e.javaClass.name}: ${e.message}\n\n")
            for ((i, el) in e.stackTrace.withIndex()) { if (i >= 18) break; sb.append("  at $el\n") }
            var c = e.cause; var d = 0
            while (c != null && d < 4) { sb.append("\nPriežastis: ${c.javaClass.name}: ${c.message}\n"); c = c.cause; d++ }
            sb.toString()
        }
    }
}
