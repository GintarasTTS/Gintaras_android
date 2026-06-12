# -*- coding: utf-8 -*-
# Golden-vector generator for the engine-parity harness: renders jvmtest/cases.tsv through the
# bundled PYTHON reference engine (app/src/main/python/lt_tts, validated bit-exact against the
# original hlas/transcr4 DLLs) and writes one "<md5-of-int16le-pcm> <sample-count>" line per case
# into jvmtest/golden.tsv. The Kotlin harness (Harness.kt) must reproduce every line IDENTICALLY.
import hashlib
import io
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "src", "main", "python"))
from lt_tts import speak  # noqa: E402


def main():
    cases = os.path.join(HERE, "cases.tsv")
    golden = os.path.join(HERE, "golden.tsv")
    out = []
    for raw in io.open(cases, encoding="utf-8").read().splitlines():
        if not raw.strip():
            continue
        f = raw.split("\t")
        text = f[0]
        rate = None if len(f) < 2 or f[1] == "-" else int(f[1])
        pitch = None if len(f) < 3 or f[2] == "-" else int(f[2])
        try:
            pcm = speak.synth_text(text, rate=rate, pitch=pitch)
            blob = struct.pack("<%dh" % len(pcm), *pcm)
            res = "%s %d" % (hashlib.md5(blob).hexdigest(), len(pcm))
        except Exception as e:  # pragma: no cover
            res = "ERROR %s: %s" % (type(e).__name__, e)
        out.append(res)
        sys.stderr.write("done: %r rate=%r pitch=%r -> %s\n" % (text, rate, pitch, res))
    io.open(golden, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
    print("wrote", golden)


if __name__ == "__main__":
    main()
