# SPDX-License-Identifier: GPL-2.0-or-later
"""Binary Domain MOMO constant expression channels and MOMA face-rig names.

TRK2: u32 channel count; face node: scalar X, Y, Z, quaternion XYZW.
0120 stores {s16 base, u16 divisor, s16 numerator}: base/32 + numerator/divisor.
0162 stores 4 signed normalized shorts; 016e is an identity quaternion.
The 00-prefixed variants are supported too; 002e is a zero scalar.
The Fey pattern corpus contains 95 static expression poses (184 channels each).
Dense, sparse, segmented-run and Euler rotation curves are supported.
Other animated channel opcodes are rejected, not guessed. See README.
"""
import struct


def _cstr(b):
    return b.split(b'\0')[0].decode('ascii', 'replace')


class Track:
    __slots__ = ('value', 'kind', 'opcode', 'offset', 'words', 'times', 'samples')

    def __init__(self):
        self.times = self.samples = None

    def __repr__(self):
        return '<Track %s value=%r>' % (self.kind, self.value)


class Clip:
    __slots__ = ('index', 'offset', 'length', 'id', 'frames', 'speed',
                 'name', 'tail', 'prg2', 'tracks', 'trk_off', 'trk_len',
                 'hed0_raw', 'header', 'fps')

    def __repr__(self):
        return '<Clip %s>' % self.name


def _read_chunks(data, off, end):
    out = []
    while off + 8 <= end:
        m = bytes(data[off:off + 4])
        ln = struct.unpack_from('>I', data, off + 4)[0]
        if ln < 8 or off + ln > end:
            break
        out.append((m, off, ln))
        off += ln
    return out


def momo_rig_name(data):
    """Read the declared MOMA name, independent of clip names/channel count."""
    if data[:4] != b'MOMO':
        raise ValueError('not a MOMO file')
    for magic, off, length in _read_chunks(data, 16, len(data)):
        if magic != b'MTS0':
            continue
        p, end = off+8, off+length
        names = []
        for _ in range(2):
            if p+2 > end:
                raise ValueError('truncated MTS0 name')
            n = struct.unpack_from('>H', data, p)[0]
            p += 2
            if not n or p+n > end:
                raise ValueError('invalid MTS0 name length')
            names.append(_cstr(data[p:p+n]))
            p += n+n%2
        return names[1]
    return None


def parse_momo(data, decode_tracks=True):
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
        c.frames = struct.unpack_from('>H', p, 4)[0]
        c.fps = struct.unpack_from('>H', p, 6)[0]
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
        if c.trk_len < 4:
            raise ValueError('truncated TRK2 header')
        c.header = struct.unpack_from('>I', data, c.trk_off)[0]
        c.tracks = None
        if decode_tracks:
            decode_clip(data, c)
        clips.append(c)
        off += ln
        ci += 1
    return clips


def decode_clip(data, clip):
    """Decode one indexed clip on demand, without decoding the whole library."""
    clip.tracks, clip.header = _parse_tracks(data, clip.trk_off, clip.trk_len)
    return clip


def _quat_dense_lerp(a, b, t):
    """Game's normalized cubic-adjusted lerp (fast SLERP approximation).

    Constants are initialized by EXE f86780/f867a0; using exact SLERP here
    subtly disagrees with dense/half-rate channels at integer Blender frames.
    """
    import math
    dot = sum(x*y for x, y in zip(a, b))
    k = -.39936140179634094*dot + .5069268941879272
    f = t + k*t*(2*t*t-3*t+1)
    sign = -1 if dot < 0 else 1
    q = tuple(x + f*(sign*y-x) for x, y in zip(a, b))
    norm = math.sqrt(sum(v*v for v in q))
    if norm < 1e-8:
        raise ValueError('zero curve quaternion')
    return tuple(v/norm for v in q)


def _quat_slerp(a, b, t):
    import math
    na = math.sqrt(sum(v*v for v in a)); nb = math.sqrt(sum(v*v for v in b))
    if min(na, nb) < 1e-8:
        raise ValueError('zero curve quaternion')
    a = tuple(v/na for v in a); b = tuple(v/nb for v in b)
    dot = sum(x*y for x, y in zip(a, b))
    if dot < 0:
        b = tuple(-v for v in b); dot = -dot
    if dot > .9995:
        q = tuple(x + t*(y-x) for x, y in zip(a, b))
        n = math.sqrt(sum(v*v for v in q))
        return tuple(v/n for v in q)
    angle = math.acos(min(1, dot)); den = math.sin(angle)
    return tuple((x*math.sin((1-t)*angle) + y*math.sin(t*angle))/den
                 for x, y in zip(a, b))


def sample_track(track, frame):
    import bisect
    if track.times is None:
        return track.value
    time = frame * (.5 if track.opcode >> 8 & 1 else 1)
    i = bisect.bisect_right(track.times, time) - 1
    if i < 0:
        return track.samples[0]
    if i >= len(track.times)-1:
        return track.samples[-1]
    f = (time-track.times[i])/(track.times[i+1]-track.times[i])
    a, b = track.samples[i:i+2]
    if track.kind == 'quaternion':
        if track.opcode & 255 in (0x61, 0x63, 0x69, 0x6d):
            return _quat_dense_lerp(a, b, f)
        return _quat_slerp(a, b, f)
    return a + f*(b-a)


def _parse_curve(data, start, end):
    """Dense, sparse and run-compressed scalar/quaternion channels.

    Run tables store start times, value-pool offsets and run lengths. They
    are not spline tangents: samples within a run are one time unit apart.
    """
    import math
    opcode, count = struct.unpack_from('>2H', data, start)
    code = opcode & 255
    scalar = code < 0x60
    byte_values = code in (0x21, 0x23, 0x24, 0x25, 0x27)
    packed = code in (0x63, 0x64, 0x65, 0x66, 0x68)
    segmented = code in (0x25, 0x27, 0x2a, 0x2c, 0x66, 0x68, 0x6c)
    dense = code in (0x21, 0x22, 0x2d, 0x61, 0x63, 0x69, 0x6d)
    header = (4 if code in (0x2d, 0x61, 0x6d) else
              (12 if segmented else 8) if scalar else
              (24 if segmented else 20) if packed else
              (18 if segmented else 14))
    if start + header > end or count == 0:
        raise ValueError('truncated/empty MOMO curve header at 0x%x' % start)
    pool_count = struct.unpack_from('>H', data, start+4)[0] if segmented else count
    value_size = 6 if code == 0x61 else 12 if code == 0x6d else 4 if packed or code == 0x2d else 1 if byte_values else 2
    p = start+header

    def array(n, short):
        nonlocal p
        size = n*2 if short else n+n%2
        if p+size > end:
            raise ValueError('truncated MOMO curve table')
        vals = list(struct.unpack_from('>%d%s' % (n, 'H' if short else 'B'), data, p))
        p += size
        return vals

    if segmented:
        flags = struct.unpack_from('>H', data, start+header-2)[0]
        if flags & ~(15 if scalar else 7) or (scalar and bool(flags & 8) != byte_values):
            raise ValueError('invalid MOMO run table flags')
        starts = array(count, flags & 1)
        offsets = array(count, flags & 2)
        lengths = array(count, flags & 4)
    elif dense:
        times = list(range(count))
    else:
        times = array(count, code in (0x24, 0x29, 0x65, 0x6b))
    size = pool_count*value_size
    width = p-start+size+(size % 2)
    if pool_count == 0 or start+width > end:
        raise ValueError('truncated/empty MOMO curve values at 0x%x' % start)
    t = Track(); t.opcode = opcode; t.offset = start
    shift = 6 if segmented else 4
    if scalar:
        t.kind = 'scalar'
        if code == 0x2d:
            pool = list(struct.unpack_from('>%df' % pool_count, data, p))
        else:
            base, scale = struct.unpack_from('>hH', data, start+shift)
            vals = struct.unpack_from('>%d%s' % (pool_count, 'b' if byte_values else 'h'), data, p)
            if byte_values:
                pool = [base/16384 + v*scale/(16384*256) for v in vals]
            else:
                if scale == 0:
                    raise ValueError('zero curve scalar divisor')
                pool = [base/32+v/scale for v in vals]
    elif code in (0x61, 0x6d):
        t.kind = 'quaternion'; pool = []
        for xyz in struct.iter_unpack('>3H' if code == 0x61 else '>3f', data[p:p+size]):
            x, y, z = [((v >> 1)*math.pi/16384) for v in xyz] if code == 0x61 else xyz
            if not all(math.isfinite(v) for v in (x, y, z)):
                raise ValueError('non-finite MOMO Euler sample')
            sx, sy, sz = (math.sin(v/2) for v in (x, y, z))
            cx, cy, cz = (math.cos(v/2) for v in (x, y, z))
            pool.append((sx*cy*cz-cx*sy*sz, cx*sy*cz+sx*cy*sz,
                         cx*cy*sz-sx*sy*cz, cx*cy*cz+sx*sy*sz))
    elif packed:
        t.kind = 'quaternion'; pool = []
        base = struct.unpack_from('>4h', data, start+shift)
        scale = struct.unpack_from('>4H', data, start+shift+8)
        for word in struct.unpack_from('>%dI' % pool_count, data, p):
            omitted = word & 3
            values = [(word >> bit) & 1023 for bit in (22, 12, 2)]
            q = [0.0]*4; j = 0
            for axis in range(4):
                if axis != omitted:
                    q[axis] = base[axis]/32768+values[j]/1024*scale[axis]/32768
                    j += 1
            q[omitted] = math.sqrt(max(0, 1-sum(v*v for v in q)))
            pool.append(tuple(q))
    else:
        t.kind = 'quaternion'; pool = []
        base, scale, x, y, z = struct.unpack_from('>hH3h', data, start+shift)
        for v in struct.unpack_from('>%dh' % pool_count, data, p):
            angle = (base/32767+v/32767*scale/32767)*math.pi/2
            s = math.sin(angle)
            pool.append((x/32767*s, y/32767*s, z/32767*s, math.cos(angle)))
    if segmented:
        times, samples = [], []
        for first, off, length in zip(starts, offsets, lengths):
            if length == 0 or off+length > pool_count:
                raise ValueError('MOMO run exceeds its sample pool')
            times.extend(range(first, first+length))
            samples.extend(pool[off:off+length])
    else:
        samples = pool
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError('overlapping/non-increasing MOMO curve times')
    if t.kind == 'scalar' and not all(math.isfinite(v) for v in samples):
        raise ValueError('non-finite MOMO curve values')
    t.times, t.samples = times, samples
    t.value = samples[0]
    t.words = struct.unpack_from('>%dH' % (width//2), data, start)
    return t, width


def _parse_tracks(data, off, ln):
    """TRK2 constant channels, as loaded by BinaryDomain.exe at VA D8B000.

    0020/0120: scalar, signed base/32 + signed numerator/unsigned denominator.
    002e/012e: zero scalar, no payload.
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
        widths = {0x20: 8, 0x2e: 2, 0x62: 10, 0x6e: 2}
        code = opcode & 0xff
        if opcode >> 8 in (0, 1) and code in (0x21, 0x22, 0x23, 0x24, 0x25, 0x27, 0x28, 0x29, 0x2a, 0x2c, 0x2d,
                                                0x61, 0x63, 0x64, 0x65, 0x66, 0x68, 0x69, 0x6a, 0x6b, 0x6c, 0x6d):
            if p + 4 > end:
                raise ValueError('truncated curve header')
            t, width = _parse_curve(data, p, end)
            tracks.append(t); p += width
            continue
        if opcode >> 8 not in (0, 1) or code not in widths:
            raise ValueError('unsupported MOMO channel opcode 0x%04x at 0x%x; '
                             'unsupported channel compression (not a known constant/dense/sparse/run codec)'
                             % (opcode, p))
        width = widths[code]
        if p + width > end:
            raise ValueError('truncated MOMO channel at 0x%x' % p)
        t = Track()
        t.opcode, t.offset = opcode, p
        t.words = struct.unpack_from('>%dH' % (width // 2), data, p)
        if code == 0x2e:
            t.kind, t.value = 'scalar', 0.0
        elif code == 0x20:
            base, divisor, numerator = struct.unpack_from('>hHh', data, p + 2)
            if not divisor:
                raise ValueError('zero MOMO scalar divisor')
            t.kind = 'scalar'
            t.value = base / 32.0 + numerator / divisor
        else:
            t.kind = 'quaternion'
            if code == 0x6e:
                t.value = (0.0, 0.0, 0.0, 1.0)
            else:
                t.value = tuple(v / 32767.0 for v in
                                struct.unpack_from('>4h', data, p + 2))
        tracks.append(t)
        p += width
    if p != end:
        raise ValueError('unexpected %d trailing TRK2 bytes' % (end - p))
    return tracks, count


def pose_nodes(clip, rig=None, frame=0, allow_missing_tail=False):
    """Decode node transforms, honoring NOD3 indirection when a rig is supplied.

    p_gen reserves channels 0..7 outside the node table. Flag-6 procedural
    nodes consume no channels and return None; they are NOT scalar rotations.
    """
    if rig is None:
        if len(clip.tracks) % 4:
            raise ValueError('MOMO channel count is not divisible by four')
        groups = [range(i, i + 4) for i in range(0, len(clip.tracks), 4)]
    else:
        indices = rig['channel_indices']
        groups = []
        for ni, (flag, off) in enumerate(zip(rig['node_flags'], rig['node_offsets'])):
            if flag == 6:
                groups.append(None)
            elif flag in (1, 65) and off + 4 <= len(indices):
                groups.append(indices[off:off + 4])
            elif flag == 0 and off < len(indices):
                groups.append(indices[off:off+1])
            else:
                raise ValueError('unsupported MOMA node declaration')
    nodes = []
    for ni, ids in enumerate(groups):
        if ids is None:
            nodes.append(None)
            continue
        if allow_missing_tail and all(i >= len(clip.tracks) for i in ids):
            nodes.append(None)
            continue
        if (allow_missing_tail and len(ids) == 4 and
                ids[-1] == len(clip.tracks) and all(i < len(clip.tracks) for i in ids[:3])):
            group = [clip.tracks[i] for i in ids[:3]]
            if any(t.kind != 'scalar' for t in group):
                raise ValueError('unexpected MOMO partial node layout')
            nodes.append((tuple(sample_track(t, frame) for t in group), (0, 0, 0, 1)))
            continue
        if any(i < 0 or i >= len(clip.tracks) for i in ids):
            raise ValueError('MOMA channel index exceeds MOMO channel count')
        group = [clip.tracks[i] for i in ids]
        if len(group) == 1 and group[0].kind == 'quaternion':
            nodes.append((tuple(rig['rest_positions'][ni]), sample_track(group[0], frame)))
            continue
        if [t.kind for t in group] != ['scalar', 'scalar', 'scalar', 'quaternion']:
            raise ValueError('unexpected MOMO node channel layout')
        nodes.append((tuple(sample_track(t, frame) for t in group[:3]), sample_track(group[3], frame)))
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
            if t.times is not None:
                raise ValueError('temporal channel editing is not supported by the fixed-layout patcher')
            if t.kind == 'scalar':
                if t.opcode & 0xff == 0x2e:
                    raise ValueError('zero scalar channel cannot expand in a fixed-layout patch')
                if not math.isfinite(value):
                    raise ValueError('non-finite MOMO scalar')
                base = math.floor(value * 32.0)
                divisor = words[2]
                numerator = round((value - base / 32.0) * divisor)
                if not (-32768 <= base <= 32767 and -32768 <= numerator <= 32767):
                    raise ValueError('MOMO scalar outside encoding range')
                words = (t.opcode, base & 0xffff, divisor, numerator & 0xffff)
            else:
                if t.opcode & 0xff == 0x6e:
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
        if count != len(res['nodes']):
            raise ValueError('unsupported MOMA node/channel declaration')
        res['channel_count'] = channels
        res['node_flags'] = list(struct.unpack_from('>%dI' % count, data, o + 8))
        parent_off = o + 8 + count * 4
        if parent_off + count * 2 > o + ln:
            raise ValueError('truncated MOMA parent table')
        res['parents'] = list(struct.unpack_from('>%dh' % count, data, parent_off))
        offsets_off = parent_off + count * 2
        indices_off = offsets_off + count * 2
        if indices_off + channels * 2 != o + ln:
            raise ValueError('invalid MOMA node/channel table size')
        res['node_offsets'] = list(struct.unpack_from('>%dH' % count, data, offsets_off))
        res['channel_indices'] = list(struct.unpack_from('>%dH' % channels, data, indices_off))
        for i, parent in enumerate(res['parents']):
            if parent >= i or parent < -1:
                raise ValueError('unsupported cyclic/out-of-order MOMA hierarchy')
    if b'NDT0' in inner:
        o, ln = inner[b'NDT0']
        count, scale = struct.unpack_from('>If', data, o)
        if count != len(res['nodes']) or ln != 16 + 16*count or scale <= 0:
            raise ValueError('invalid MOMA default positions')
        res['rest_positions'] = [[v/scale for v in struct.unpack_from('>3h', data, o+16+16*i)]
                                 for i in range(count)]
    if b'ANC0' in inner:
        o, ln = inner[b'ANC0']
        count, reserved = struct.unpack_from('>2H', data, o)
        if ln != 4+count*4:
            raise ValueError('invalid MOMA anchor table size')
        res['anchors'] = [list(v) for v in struct.iter_unpack('>2H', data[o+4:o+ln])]
        for end, root in res['anchors']:
            if end >= len(res['nodes']) or root >= len(res['nodes']):
                raise ValueError('MOMA anchor node out of range')
    if b'OPE0' in inner:
        o, ln = inner[b'OPE0']
        count, reserved = struct.unpack_from('>2I', data, o)
        if ln != 8+32*count:
            raise ValueError('invalid MOMA operation table size')
        res['operations'] = []
        for i in range(count):
            p = o+8+32*i
            code, target, *drivers = struct.unpack_from('>6h', data, p)
            if not 0 <= target < len(res['nodes']) or any(d >= len(res['nodes']) or d < -1 for d in drivers):
                raise ValueError('MOMA procedural node out of range')
            res['operations'].append(dict(code=code, target=target, drivers=drivers,
                                          raw=data[p:p+32].hex()))
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
