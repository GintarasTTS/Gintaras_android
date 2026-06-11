# -*- coding: utf-8 -*-
# Decode wtvlt1.dta = the GINTARAS voice (MFC CMapStringToOb of CMapItem).
#
# Architecture (reverse-engineered 2026-05-31): a SHARED PITCH-FRAME POOL.
#   * A pitch frame is DEFINED once as:  03 80 <i32 fid> <i32 flag> <i32 nbytes> <int16 PCM[nbytes/2]>
#     (fid >= 1).  Frames are shared, so most units only reference them.
#   * A UNIT (syllable/demisyllable, the map key, cp1257) is:
#        <CString key> 03 80  ff ff ff ff  <i32 0> <i32 0>  <i32 framecount>  <i32 0> <i32 0>
#        then framecount frame REFERENCES:  04 "<fid>" <i32 0> <i32 flag>
#     i.e. the unit's audio = concat(pool[fid] for each referenced fid, in order).
#   The unit's CMapItem tag (03 80) is followed by ff ff ff ff (-1); a frame def's
#   03 80 is followed by a positive fid -> that disambiguates the two uses of 03 80.
import re, struct, numpy as np, wave, os
from . import paths

TAG = b'\x03\x80'

def load():
    return open(paths.voice_path(), 'rb').read()

# ---------------------------------------------------------------- frame pool
# A genuine inline frame definition is:
#   03 80 <i32 fid> <i32 flag> <i32 nbytes> <padding> ff ff ff ff <int16 PCM[nbytes/2]>
# The PCM begins right after the ff ff ff ff sentinel (a few bytes of zero padding sit
# between nbytes and the sentinel).  Anchoring on the sentinel + using nbytes as the exact
# length rejects the many spurious 03 80 byte-matches that occur inside PCM data.
SENT = b'\xff\xff\xff\xff'

def _key_before(d, i):
    """MFC CString key serialised right before a frame tag at offset i: BYTE len + ASCII chars.
    Frame keys are short numeric strings ('0'..'6xxx'). Returns the key string or None."""
    for L in range(1, 8):
        if i - 1 - L < 0:
            break
        if d[i - 1 - L] == L and all(33 <= c < 127 for c in d[i - L:i]):
            return d[i - L:i].decode('latin1')
    return None

def build_frame_pool(d):
    """The voice is an MFC CMapStringToOb of (key, frame) pairs. Each frame object:
    03 80 | hdrfid(4) | flag(4) | nbytes(4) | gap(12) | PCM[nbytes]  (PCM at tag+26; gap = 8 zero
    bytes + ff ff ff ff [single pitch period] OR 00 00 00 00 [~100ms segment]).
    CRITICAL: units reference frames by the MAP KEY string that precedes each frame object — NOT
    the header fid (which is an unrelated internal counter). Pool is keyed by int(keystring)."""
    pool = {}
    n = len(d)
    for m in re.finditer(re.escape(TAG), d):
        i = m.start()
        p = m.end()                              # just past 03 80
        if p + 24 > n:
            continue
        hdrfid, flag, nbytes = struct.unpack('<3i', d[p:p+12])
        if not (0 <= hdrfid < 0x2000 and 0 < flag < 64 and 32 <= nbytes <= 0x20000 and nbytes % 2 == 0):
            continue
        gap = d[p+12:p+24]                        # 12-byte gap before PCM (strong validator)
        if gap[:8] != b'\x00' * 8 or gap[8:12] not in (SENT, b'\x00\x00\x00\x00'):
            continue
        key = _key_before(d, i)
        if key is None or not key.isdigit():
            continue
        raw = d[p+24:p+24 + nbytes]
        if len(raw) < nbytes:
            continue
        a = np.frombuffer(raw, dtype='<i2')
        if float(np.mean(np.abs(a))) >= 28000:   # reject only near-full-scale garbage
            continue
        fid = int(key)
        if fid not in pool:
            pool[fid] = a
    return pool

# ---------------------------------------------------------------- units
def looks_label(b):
    if not b:
        return False
    for c in b:
        if c < 0x20:
            return False
        if c < 0x80 and not (chr(c).isalpha() or chr(c) in "'%-|$.^"):
            return False
    return any((c < 0x80 and chr(c).isalpha()) or c >= 0x80 for c in b)

UNIT_HDR = re.compile(re.escape(TAG) + rb'\xff\xff\xff\xff')  # tag + (-1)

def parse_units(d):
    """key -> [fid,...] reference list, in order."""
    units = {}
    for m in UNIT_HDR.finditer(d):
        t = m.start()
        # key is the CString immediately before the tag
        key = None
        for L in range(1, 13):
            s = t - 1 - L
            if s < 0:
                break
            if d[s] == L and looks_label(d[s+1:s+1+L]):
                key = d[s+1:s+1+L].decode('cp1257', 'replace')
                break
        if key is None:
            continue
        p = m.end()                              # after ff ff ff ff
        i0, i1, cnt, i3, i4 = struct.unpack('<5i', d[p:p+20])
        p += 20
        if not (0 < cnt < 4000):
            continue
        refs = []
        ok = True
        for _ in range(cnt):
            if p >= len(d) or d[p] == 0 or d[p] > 9:
                ok = False; break
            L = d[p]; sid = d[p+1:p+1+L]
            if not sid.isdigit():
                ok = False; break
            refs.append(int(sid))
            p += 1 + L + 8                        # id string + i32 pad + i32 flag
        if ok and refs:
            if key not in units or len(refs) > len(units[key]):
                units[key] = refs
    return units

def parse_unit_pads(d):
    """key -> ([pad,...], [flag,...]) per frame reference. ADDITIVE companion to parse_units (does NOT alter
    it): each reference is `<len> "<fid>" <i32 pad> <i32 flag>`. The PAD field stores the engine's per-grain
    a5 (extra-epoch / 'double budget' count) DIRECTLY for the 12 'irregular' demisyllables whose grains carry
    the 0x40 flag bit (the long-vowel nuclei i|/ąą/juo|/u|j/gą|/-dą/-ną/lu|-- + word-final -ka/-rai). For all
    other ~609 units pad is 0 (a5 there is the regular stress-driven rule). FLAG bits: 0x01 voiced, 0x40
    a5-stored-in-pad, 0x80 last-grain-of-unit. RE'd 2026-06-10 (cont.39); see plan-frontend-wiring memory."""
    out = {}
    for m in UNIT_HDR.finditer(d):
        t = m.start()
        key = None
        for L in range(1, 13):
            s = t - 1 - L
            if s < 0:
                break
            if d[s] == L and looks_label(d[s+1:s+1+L]):
                key = d[s+1:s+1+L].decode('cp1257', 'replace')
                break
        if key is None:
            continue
        p = m.end()
        _i0, _i1, cnt, _i3, _i4 = struct.unpack('<5i', d[p:p+20])
        p += 20
        if not (0 < cnt < 4000):
            continue
        pads, flags = [], []
        ok = True
        for _ in range(cnt):
            if p >= len(d) or d[p] == 0 or d[p] > 9:
                ok = False; break
            L = d[p]; sid = d[p+1:p+1+L]
            if not sid.isdigit():
                ok = False; break
            pad, flag = struct.unpack('<2i', d[p+1+L:p+1+L+8])
            pads.append(pad); flags.append(flag)
            p += 1 + L + 8
        if ok and pads and (key not in out or len(pads) > len(out[key][0])):
            out[key] = (pads, flags)
    return out


def synth_unit(pool, refs):
    segs = [pool[f] for f in refs if f in pool]
    if not segs:
        return np.array([], dtype='<i2'), 0
    return np.concatenate(segs), sum(1 for f in refs if f in pool)

def save_wav(path, sig, sr=22050):
    wv = wave.open(path, 'wb'); wv.setnchannels(1); wv.setsampwidth(2); wv.setframerate(sr)
    wv.writeframes(np.asarray(sig, dtype='<i2').tobytes()); wv.close()
