# SPDX-License-Identifier: GPL-2.0-or-later
"""
Binary Domain MOMO (.mmo) motion file and MOMA (.mma) rig file.

MOMO: 'MOMO' + ff 01 00 00 + 8 zero bytes, then clip records:
  RAW0 <u32 len(incl hdr)>          -- one container per clip
      HED0 (payload 0x18): u16 id, u16 ver=3, u16 ?, u16 frames, ..., float speed
      HED1 (payload var): u8 0, u8 len, name\0, 9 tail bytes
      PRG2 (payload 0x18)
      TRK2 (payload var): u16 0, u16 4*node_count, then node_count*3+1 tracks
        separated by 0x7fff words. Each track is one key:
          {value, 0x0120, time}                      -- 'k'
          {value, 0x016e, 0x0120, time}              -- 'e' (3rd component)
          {value, 0x0162, h0,h1,h2,h3, 0x0120, time} -- 's' (bezier)
        track 0 is a special/global track, tracks 1+3n..3+3n = node n
        components 0..2 (10-bit-ish quantized values 0..1023).
  File ends with 20 zero bytes.

MOMA: 'MOMA' + ff010000 + 8 zeros, MAP1 container with:
  HED0 rig name, ADJ0, FCE0, RMN0 (u32 count + names {u8 type,u8 len,str}),
  NDM0, NOD3 (count, 4*count, flags), NDT0, MIR0 (mirror table u32s), OPE0.
"""
import struct

K_LINEAR = 0x0120
K_E = 0x016E
K_BEZIER = 0x0162
SEP = 0x7FFF


def _cstr(b):
    return b.split(b'\0')[0].decode('ascii', 'replace')


class Track:
    __slots__ = ('value', 'kind', 'time', 'handles', 'words')

    def __repr__(self):
        return '<Track %s v=%d t=%d>' % (self.kind, self.value, self.time)


class Clip:
    __slots__ = ('index', 'offset', 'length', 'id', 'frames', 'speed',
                 'name', 'tail', 'prg2', 'tracks', 'trk_off', 'trk_len',
                 'hed0_raw', 'header')

    def __repr__(self):
        return '<Clip %s>' % self.name


def _read_chunks(data, off, end):
    out = []
    while off + 8 <= end:
        m = data[off:off + 4]
        ln = struct.unpack_from('>I', data, off + 4)[0]
        if ln < 8 or off + ln > end:
            break
        out.append((m, off, ln))
        off += ln
    return out


def parse_momo(data):
    if data[:4] != b'MOMO':
        raise ValueError('not a MOMO file')
    clips = []
    off = 16
    ci = 0
    while off + 8 <= len(data):
        magic = data[off:off + 4]
        ln = struct.unpack_from('>I', data, off + 4)[0]
        if ln < 8 or off + ln > len(data):
            break
        if magic != b'RAW0':  # e.g. MTS0 header container - skip
            off += ln
            continue
        c = Clip()
        c.index = ci
        c.offset = off
        c.length = ln
        inner = {m: (o, l) for m, o, l in _read_chunks(data, off + 8, off + ln)}
        ho, hl = inner[b'HED0']
        c.hed0_raw = bytes(data[ho + 8: ho + hl])
        p = c.hed0_raw
        c.id = struct.unpack_from('>H', p, 0)[0]
        c.frames = struct.unpack_from('>H', p, 6)[0]
        c.speed = struct.unpack_from('>f', p, 12)[0]
        h1o, h1l = inner[b'HED1']
        payload = data[h1o + 8: h1o + h1l]
        nlen = payload[1]
        c.name = _cstr(payload[2:2 + nlen])
        c.tail = bytes(payload[2 + nlen:])
        po, pl = inner[b'PRG2']
        c.prg2 = bytes(data[po + 8: po + pl])
        to, tl = inner[b'TRK2']
        c.trk_off = to + 8
        c.trk_len = tl - 8  # payload only
        c.tracks, c.header = _parse_tracks(data, to + 8, tl - 8)
        clips.append(c)
        off += ln
        ci += 1
    return clips


def _parse_tracks(data, off, ln):
    n = ln // 2
    u = struct.unpack_from('>%dH' % n, data, off)
    header = (u[0], u[1])
    # marker-driven tokenizer: the second word decides the segment width, so
    # a 0x7fff handle word inside a bezier track is not mistaken for a
    # separator. Track encodings:
    #   k: {value, 0120, time}  - or {0120, time} when value == 0
    #   e: {value, 016e, 0120, time} (the last one in a file may drop the
    #      trailing {0120, time})
    #   s: {value, 0162, h0,h1,h2,h3, 0120, time}
    tracks = []
    i = 2
    while i < n:
        if u[i] == SEP:
            i += 1
            continue
        tr = Track()
        tr.handles = None
        w0 = u[i]
        w1 = u[i + 1] if i + 1 < n else None
        nxt = u[i + 2] if i + 2 < n else SEP
        if w1 == K_LINEAR and not (w0 == K_LINEAR and nxt == SEP):
            tr.kind = 'k'
            tr.value = w0
            tr.time = nxt
            tr.words = tuple(u[i:i + 3])
            i += 3
        elif w1 == K_E:
            tr.kind = 'e'
            tr.value = w0
            if i + 3 < n:
                tr.time = u[i + 3]
                tr.words = tuple(u[i:i + 4])
                i += 4
            else:
                tr.time = 0  # last track in file can be truncated
                tr.words = tuple(u[i:i + 2])
                i += 2
        elif w1 == K_BEZIER:
            tr.kind = 's'
            tr.value = w0
            tr.handles = tuple(u[i + 2:i + 6])
            tr.time = u[i + 7] if i + 7 < n else 0
            tr.words = tuple(u[i:i + 8])
            i += 8
        elif w0 == K_LINEAR:
            tr.kind = 'k'
            tr.value = 0  # value word omitted
            tr.time = w1 or 0
            tr.words = tuple(u[i:i + 2])
            i += 2
        else:
            raise ValueError('unknown track marker at word %d: %04x %04x'
                             % (i, w0, w1 if w1 is not None else 0))
        tracks.append(tr)
    return tracks, header


def write_track_values(data, clip, values):
    """Patch the value word of each track in-place (values: list of ints).

    The value is word 0 of every group, except the first (special) group
    whose layout is {0120, time} - it carries no value word.
    """
    off = clip.trk_off
    n = clip.trk_len // 2
    u = list(struct.unpack_from('>%dH' % n, data, off))
    k = 0
    i = 2
    while i < n:
        w0 = u[i]
        if w0 == SEP:
            i += 1
            continue
        w1 = u[i + 1] if i + 1 < n else None
        nxt = u[i + 2] if i + 2 < n else SEP
        if w1 == K_LINEAR and not (w0 == K_LINEAR and nxt == SEP):
            u[i] = max(0, min(0xFFFF, int(values[k])))
            i += 3
        elif w1 == K_E:
            u[i] = max(0, min(0xFFFF, int(values[k])))
            i += 4 if i + 3 < n else 2
        elif w1 == K_BEZIER:
            u[i] = max(0, min(0xFFFF, int(values[k])))
            i += 8
        elif w0 == K_LINEAR:
            i += 2  # value 0 encoded implicitly - no value word
        else:
            raise ValueError('unknown track marker at word %d' % i)
        k += 1
    struct.pack_into('>%dH' % n, data, off, *u)
    return k


def parse_moma(data):
    """Return dict with rig name, node names, mirror table."""
    if data[:4] != b'MOMA':
        raise ValueError('not a MOMA file')
    # top container
    chunks = _read_chunks(data, 0x10, len(data))
    inner = {}
    for m, o, l in chunks:
        sub = _read_chunks(data, o + 8, o + l)
        for m2, o2, l2 in sub:
            inner[m2] = (o2 + 8, l2 - 8)
        inner.setdefault(m, (o + 8, l - 8))
    res = {'name': None, 'nodes': [], 'mirror': []}
    if b'HED0' in inner:
        o, l = inner[b'HED0']
        res['name'] = _cstr(data[o + 2:o + l])
    if b'RMN0' in inner:
        o, l = inner[b'RMN0']
        cnt = struct.unpack_from('>I', data, o)[0]
        p = o + 4
        for _ in range(cnt):
            nlen = data[p + 1]
            res['nodes'].append(_cstr(data[p + 2:p + 2 + nlen]))
            p += 2 + nlen
            if (2 + nlen) % 2:
                p += 1
    if b'MIR0' in inner:
        o, l = inner[b'MIR0']
        cnt = struct.unpack_from('>I', data, o)[0]
        vals = struct.unpack_from('>%dI' % (cnt + 1), data, o)
        # stored as u32 with value in high half (value<<16 pattern)
        mir = []
        for v in vals[1:]:
            mir.append(v >> 16 if v > 0xFFFF else v)
        res['mirror'] = mir
    return res
