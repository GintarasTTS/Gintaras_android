package lt.gintaras.tts.engine

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.nio.charset.Charset

// Helpers to load data files from Android assets (lt_tts/data/).

internal object Assets {

    private const val PREFIX = "lt_tts/data"

    private lateinit var ctx: Context

    fun init(context: Context) { ctx = context.applicationContext }

    fun open(name: String) = ctx.assets.open("$PREFIX/$name")

    fun bytes(name: String): ByteArray = open(name).use { it.readBytes() }

    fun text(name: String, charset: Charset = Charsets.UTF_8): String =
        open(name).use { it.readBytes().toString(charset) }

    fun lines(name: String, charset: Charset = Charsets.UTF_8): List<String> =
        BufferedReader(InputStreamReader(open(name), charset)).readLines()

    fun json(name: String): JSONObject = JSONObject(text(name))

    fun jsonArray(name: String): JSONArray = JSONArray(text(name))
}
