package lt.gintaras.tts.engine

import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.File
import java.io.FileInputStream
import java.io.InputStreamReader
import java.nio.charset.Charset

// Desktop-JVM stand-in for the Android Assets object, used by the engine-parity harness
// (jvmtest/Harness.kt). Compiled INSTEAD of app/src/main/java/.../engine/Assets.kt -- same
// package, same API, but reads the data files straight from a directory on disk
// (app/src/main/assets/lt_tts/data).

internal object Assets {

    lateinit var dataDir: File

    fun open(name: String) = FileInputStream(File(dataDir, name))

    fun bytes(name: String): ByteArray = open(name).use { it.readBytes() }

    fun text(name: String, charset: Charset = Charsets.UTF_8): String =
        open(name).use { it.readBytes().toString(charset) }

    fun lines(name: String, charset: Charset = Charsets.UTF_8): List<String> =
        BufferedReader(InputStreamReader(open(name), charset)).readLines()

    fun json(name: String): JSONObject = JSONObject(text(name))

    fun jsonArray(name: String): JSONArray = JSONArray(text(name))
}
