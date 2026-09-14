# -*- coding: utf-8 -*-
"""Command-line interface:  python -m lt_tts [options] "text to speak"

    python -m lt_tts "Labas rytas. Kaip jums sekasi?"
    python -m lt_tts --rate 60 --pitch 55 -o out.wav "Turiu 2024 metus."
    python -m lt_tts --rate 100 --boost 2.0 "Greitas skaitymas."   # speed boost x1.0..x2.0 (Sonic)
"""
import sys
from .engine import Gintaras
from . import boost as BO


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    rate = pitch = boost = None
    out = "lt_tts_out.wav"
    words = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--rate", "-r") and i + 1 < len(argv):
            rate = int(argv[i + 1]); i += 2
        elif a in ("--pitch", "-p") and i + 1 < len(argv):
            pitch = int(argv[i + 1]); i += 2
        elif a in ("--boost", "-b") and i + 1 < len(argv):
            boost = int(round(float(argv[i + 1]) * 1000)); i += 2   # factor 1.0..2.0 -> permille
        elif a in ("--out", "-o") and i + 1 < len(argv):
            out = argv[i + 1]; i += 2
        else:
            words.append(a); i += 1
    text = " ".join(words) if words else "Labas. Aš esu Gintaras, lietuviškas balsas."
    tts = Gintaras(rate=rate, pitch=pitch, boost_milli=boost)
    pcm = tts.synth_pcm(text)
    tts.save(out, text)
    print("text : %s" % text)
    print("out  : %s  (%.2f s, %d samples @ 22050 Hz)  [DLL-free]" % (out, len(pcm) / 22050.0, len(pcm)))
    if boost is not None:
        bm = BO.clamp_boost(boost)
        print("boost: %d permille (x%.3f, Sonic quality mode)" % (bm, bm / 1000.0))


if __name__ == "__main__":
    main()
