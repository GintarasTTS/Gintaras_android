# Engine parity harness (Kotlin vs Python reference)

The Kotlin engine (`app/src/main/java/lt/gintaras/tts/engine/`) is a 1:1 port of the Python
reference engine bundled at `app/src/main/python/lt_tts/` — and that Python engine is itself
validated **bit-exact** against the original WinTalker `hlas.dll`/`transcr4.dll`. The port is
correct only if the Kotlin PCM output is **byte-identical** to the Python output for every case.

- `cases.tsv` — the test cases: `text <TAB> rate(int|-) <TAB> pitch(int|-)` (covers lexicon +
  OOV + nonsense words, diacritics, digits/leading zeros, spell-mode letters, phrases, the `?`
  question contour, and rate/pitch slider values).
- `gen_golden.py` — renders every case through the Python reference engine → `golden.tsv`
  (`md5-of-int16le-pcm sample-count` per case).
- `JvmAssets.kt` — desktop-JVM stand-in for the Android `Assets` object (reads
  `app/src/main/assets/lt_tts/data` from disk). Compiled **instead of** the app's `Assets.kt`.
- `Harness.kt` — renders the same cases through the Kotlin engine → the same line format.
  `--debug <words…>` prints the front-end token + selection-frame trace for divergence hunting
  (compare with `py_debug.py`).

Run (any OS; needs JDK 17, kotlinc 1.9.x, org.json jar, python3):

```sh
python3 jvmtest/gen_golden.py
FILES=$(ls app/src/main/java/lt/gintaras/tts/engine/*.kt | grep -v -e 'Assets.kt' -e 'Engine.kt')
kotlinc $FILES jvmtest/JvmAssets.kt jvmtest/Harness.kt -cp json.jar -include-runtime -d harness.jar
java -cp harness.jar:json.jar lt.gintaras.tts.engine.HarnessKt \
  app/src/main/assets/lt_tts/data jvmtest/cases.tsv jvmtest/kotlin_out.tsv
diff jvmtest/golden.tsv jvmtest/kotlin_out.tsv   # must be empty
```

CI runs this automatically (`engine-parity` job in `.github/workflows/build.yml`).
