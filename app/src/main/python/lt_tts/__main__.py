# -*- coding: utf-8 -*-
"""Command-line interface:  python -m lt_tts [options] "text to speak"

    python -m lt_tts "Labas rytas. Kaip jums sekasi?"
    python -m lt_tts --rate 60 --pitch 55 -o out.wav "Turiu 2024 metus."
"""
import sys
from .engine import Gintaras


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    rate = pitch = None
    out = "lt_tts_out.wav"
    words = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--rate", "-r") and i + 1 < len(argv):
            rate = int(argv[i + 1]); i += 2
        elif a in ("--pitch", "-p") and i + 1 < len(argv):
            pitch = int(argv[i + 1]); i += 2
        elif a in ("--out", "-o") and i + 1 < len(argv):
            out = argv[i + 1]; i += 2
        else:
            words.append(a); i += 1
    text = " ".join(words) if words else "Labas. Aš esu Gintaras, lietuviškas balsas."
    tts = Gintaras(rate=rate, pitch=pitch)
    pcm = tts.synth_pcm(text)
    tts.save(out, text)
    print("text : %s" % text)
    print("out  : %s  (%.2f s, %d samples @ 22050 Hz)  [DLL-free]" % (out, len(pcm) / 22050.0, len(pcm)))


if __name__ == "__main__":
    main()
