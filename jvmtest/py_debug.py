# -*- coding: utf-8 -*-
# Python-side counterpart of `Harness --debug`: front-end + selection trace per word.
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "src", "main", "python"))
from lt_tts import planbuilder as PF, transcribe as LT  # noqa: E402

out = io.open(os.path.join(HERE, "py_debug.txt"), "w", encoding="utf-8", newline="\n")
for w in sys.argv[1:]:
    out.write("== %s\n" % w)
    out.write("tokens: %s\n" % " ".join(LT.transcribe(w)))
    for g in PF.select_frames(w):
        if g.get('sil'):
            continue
        out.write("  %s %s %d%s\n" % (g.get('key'), g.get('pi'), len(g['pcm']),
                                      " REL" if g.get('release_pt') else ""))
out.close()
print("written")
