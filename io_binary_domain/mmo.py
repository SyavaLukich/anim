# SPDX-License-Identifier: GPL-2.0-or-later
"""Binary Domain MOMO constant expression channels and MOMA face-rig names.

TRK2: u32 channel count; per node: scalar X, Y, Z, quaternion XYZW.
0120 stores {s16 base, u16 divisor, s16 numerator}: base/32 + numerator/divisor.
0162 stores 4 signed normalized shorts; 016e is an identity quaternion.
The Fey pattern corpus contains 95 static expression poses (184 channels each).
Other animated channel opcodes are rejected, not guessed. See README.
"""
import struct


def _cstr(b):
    return b.split(b'\0')[0].decode('ascii', 'replace')


class Track:
    __slots__ = ('value', 'kind', 'opcode', 'offset', 'words')

    def __repr__(self):
        return '<Track %s value=%r>' % (self.kind, self.value)


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
    """TRK2 constant channels, as loaded by BinaryDomain.exe at VA D8B000.

    0120: scalar, signed base/32 + signed numerator/unsigned denominator.
    0162: constant xyzw quaternion, four signed 16-bit normalized words.
    016e: identity quaternion, no payload. These are NOT key delimiters.
    """
    if ln < 4:
        raise ValueError('truncated TRK2 header')
    count = struct.unpack_from('>I', data, off)[0]
    p, end = off + 4, off + ln
    tracks = []
    for index in range(count):
        if p + 2 > end:
            raise ValueError('truncated MOMO channel %d' % index)
        opcode = struct.unpack_from('>H', data, p)[0]
        widths = {0x0120: 8, 0x0162: 10, 0x016e: 2}
        if opcode not in widths:
            raise ValueError('unsupported MOMO channel opcode 0x%04x at 0x%x; '
                             'only constant expression channels are supported'
                             % (opcode, p))
        width = widths[opcode]
        if p + width > end:
            raise ValueError('truncated MOMO channel at 0x%x' % p)
        t = Track()
        t.opcode, t.offset = opcode, p
        t.words = struct.unpack_from('>%dH' % (width // 2), data, p)
        if opcode == 0x0120:
            base, divisor, numerator = struct.unpack_from('>hHh', data, p + 2)
            if not divisor:
                raise ValueError('zero MOMO scalar divisor')
            t.kind = 'scalar'
            t.value = base / 32.0 + numerator / divisor
        else:
            t.kind = 'quaternion'
            if opcode == 0x016e:
                t.value = (0.0, 0.0, 0.0, 1.0)
            else:
                t.value = tuple(v / 32767.0 for v in
                                struct.unpack_from('>4h', data, p + 2))
        tracks.append(t)
        p += width
    if p != end:
        raise ValueError('unexpected %d trailing TRK2 bytes' % (end - p))
    return tracks, count


def pose_nodes(clip):
    """Return (position xyz, quaternion xyzw) per flat face-rig node."""
    if len(clip.tracks) % 4:
        raise ValueError('MOMO channel count is not divisible by four')
    nodes = []
    for i in range(0, len(clip.tracks), 4):
        group = clip.tracks[i:i + 4]
        if ([t.kind for t in group] !=
                ['scalar', 'scalar', 'scalar', 'quaternion']):
            raise ValueError('unexpected MOMO node channel layout')
        nodes.append((tuple(t.value for t in group[:3]), group[3].value))
    return nodes


def write_track_values(data, clip, values):
    """Patch decoded constant channels, preserving untouched words exactly.

    Identity quaternion channels cannot expand to a full quaternion in this
    fixed-layout patcher; such edits raise an explicit error.
    """
    import math
    if len(values) != len(clip.tracks):
        raise ValueError('expected %d MOMO channel values' % len(clip.tracks))
    patches = []
    for t, value in zip(clip.tracks, values):
        words = t.words
        if value != t.value:
            if t.kind == 'scalar':
                if not math.isfinite(value):
                    raise ValueError('non-finite MOMO scalar')
                base = math.floor(value * 32.0)
                divisor = words[2]
                numerator = round((value - base / 32.0) * divisor)
                if not (-32768 <= base <= 32767 and -32768 <= numerator <= 32767):
                    raise ValueError('MOMO scalar outside encoding range')
                words = (t.opcode, base & 0xffff, divisor, numerator & 0xffff)
            else:
                if t.opcode == 0x016e:
                    raise ValueError('identity quaternion channel cannot expand '
                                     'in a fixed-layout patch')
                if len(value) != 4 or not all(math.isfinite(v) for v in value):
                    raise ValueError('expected a finite xyzw quaternion')
                norm = math.sqrt(sum(v*v for v in value))
                if norm < 1e-12:
                    raise ValueError('zero quaternion')
                words = (t.opcode,) + tuple(
                    round(v / norm * 32767.0) & 0xffff for v in value)
        patches.append((t.offset, struct.pack('>%dH' % len(words), *words)))
    for off, payload in patches:
        if off < 0 or off + len(payload) > len(data):
            raise ValueError('MOMO patch exceeds destination size')
    for off, payload in patches:
        data[off:off + len(payload)] = payload
    return len(clip.tracks)


def parse_moma(data):
    """Return rig name, node names, parent indices and mirror table."""
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
    res = {'name': None, 'nodes': [], 'mirror': [], 'parents': []}
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
    if b'NOD3' in inner:
        o, ln = inner[b'NOD3']
        count, channels = struct.unpack_from('>2I', data, o)
        if count != len(res['nodes']) or channels != 4 * count:
            raise ValueError('unsupported MOMA node/channel declaration')
        parent_off = o + 8 + count * 4
        if parent_off + count * 2 > o + ln:
            raise ValueError('truncated MOMA parent table')
        res['parents'] = list(struct.unpack_from('>%dh' % count, data, parent_off))
        for i, parent in enumerate(res['parents']):
            if parent >= i or parent < -1:
                raise ValueError('unsupported cyclic/out-of-order MOMA hierarchy')
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
