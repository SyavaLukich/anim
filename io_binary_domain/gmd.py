# SPDX-License-Identifier: GPL-2.0-or-later
"""
Binary Domain / Yakuza-era "Dragon-ish" GSGM (.gmd) model format.

Structure (all integers big-endian):

  Header: 'GSGM', u32 version (0x102), 0x08: 5 fixed bytes + 5 file bytes,
          0x12: model name (NUL-terminated), 0xA0..0xCF: 12 floats.
  Directory: 14 (offset,count) pairs @0x30 + 3 @0xD0  ->  17 chunks:
     0 skeleton   1 helper-bones   2 meshes   3 materials   4 colors
     5 matrices   6 streams        7 vertex data           8 texture names
     9 shaders   10 bone names    11 index buffer (u16)   12 chunk12
    13 bone palette (u8 slot->bone)  14 mat params        15 chunk15
    16 mat params2
  Skeleton: root record (0x70B) @0x100, 'HSMH' 16B, 214*0x80 records.
     record: [0]id [1]first_child [2]next_sibling [3]parent [6]name_idx
             [12..14] 6*u16 angles (raw) [15..17] scale
             pos @0x50 (x,y,z,w)  axis-angle @0x60 (ax,ay,az,angle_rad)
  Helper bones (>=0xA0): e1 records 0x40B: floats[0..2] world pos,
             floats[8..11] quaternion (x,y,z,w).
  Meshes: 130 * 0x40: [0]id [1]material [2]stream [3]nverts [4]nidx
             [5]idx_start [6]vmap_count [7]vmap_start [9]vmap_end
             [10]pal_size [11]pal_base [12]c [13]d [14]e [15]min_vtx
  Vertex strides: 40 (pos3f w4 bi4 n4 tan4 FFFFFFFF uv2 uv2ch)
                  36 (no uv2ch)  32 (pos3f w4 bi4 n4 col4 uv2)  20 (pos w bi)
     normals are biased bytes: n = (b - 128) / 127
     uv: u16 / 65535
     weights: u8 (0..255, sum usually 255), bone index byte is LOCAL:
     bone_id = palette13[pal_base + byte]
"""
import struct
import math
import os

MAGIC = b'GSGM'


def _u32s(data, off, n):
    return struct.unpack_from('>%dI' % n, data, off)


def _f32s(data, off, n):
    return struct.unpack_from('>%df' % n, data, off)


def _cstr(b):
    return b.split(b'\0')[0].decode('ascii', 'replace')


class Bone:
    __slots__ = ('id', 'first_child', 'next_sibling', 'parent', 'cat',
                 'name_idx', 'name', 'angles6', 'scale', 'pos', 'aa',
                 'off', 'reclen', 'is_helper', 'quat', 'hpos')

    def __repr__(self):
        return '<Bone %d %s>' % (self.id, self.name)


class MeshRec:
    __slots__ = ('idx', 'mid', 'material', 'stream', 'nverts', 'nidx',
                 'idx_start', 'vmap_count', 'vmap_start', 'vmap_end',
                 'pal_size', 'pal_base', 'c', 'd', 'e', 'min_vtx', 'raw')

    def __repr__(self):
        return '<Mesh %d mat=%d stream=%d>' % (self.idx, self.material, self.stream)


class GMD:
    def __init__(self, data, filepath=None):
        self.data = bytearray(data)
        self.filepath = filepath
        if data[:4] != MAGIC:
            raise ValueError('not a GSGM file')
        self.version = _u32s(data, 4, 1)[0]
        self.name = _cstr(data[0x12:0x30])
        self.dir = []
        for i in range(14):
            self.dir.append(_u32s(data, 0x30 + i * 8, 2))
        for i in range(3):
            self.dir.append(_u32s(data, 0xD0 + i * 8, 2))
        self._parse()

    # ---------------- parse ----------------
    def _parse(self):
        data = self.data
        # bone names (chunk 10)
        off, cnt = self.dir[10]
        self.bone_names = []
        for i in range(cnt):
            rec = data[off + i * 32: off + i * 32 + 32]
            self.bone_names.append(_cstr(rec[2:32]))
        # skeleton (chunk 0): root record 0x70B @0x100, 'HSMH' 16B, then 0x80B recs
        nb = self.dir[0][1]
        if self.dir[0][0] != 0x100 or data[0x170:0x174] != b'HSMH':
            raise ValueError('unexpected skeleton layout')
        self.bones = []
        b = Bone()
        vals = _u32s(data, 0x100, 28)
        self._fill_bone(b, vals, 0x100, 0x70)
        self.bones.append(b)
        for k in range(1, nb):
            o = 0x180 + (k - 1) * 0x80
            vals = _u32s(data, o, 32)
            b = Bone()
            self._fill_bone(b, vals, o, 0x80)
            self.bones.append(b)
        # helper bones (chunk 1)
        off, cnt = self.dir[1]
        self.helper = []
        for i in range(cnt):
            o = off + i * 0x40
            hdr = _u32s(data, o, 4)
            fl = _f32s(data, o + 16, 12)
            self.helper.append({'id': hdr[0], 'bone': hdr[1], 'flag': hdr[3],
                                'pos': fl[0:3], 'quat': fl[8:12], 'fl': fl})
            bid = hdr[1]
            if bid < len(self.bones):
                self.bones[bid].is_helper = True
                self.bones[bid].hpos = fl[0:3]
                self.bones[bid].quat = fl[8:12]
        # meshes (chunk 2) -- count stored may be off by one; use span
        off = self.dir[2][0]
        n_mesh = (self.dir[3][0] - off) // 0x40
        self.meshes = []
        for i in range(n_mesh):
            v = _u32s(data, off + i * 0x40, 16)
            m = MeshRec()
            (m.mid, m.material, m.stream, m.nverts, m.nidx, m.idx_start,
             m.vmap_count, m.vmap_start, _, m.vmap_end, m.pal_size,
             m.pal_base, m.c, m.d, m.e, m.min_vtx) = v
            m.idx = i
            m.raw = v
            self.meshes.append(m)
        # materials (chunk 3)
        off, cnt = self.dir[3]
        self.materials = []
        for i in range(cnt):
            o = off + i * 0x80
            words = _u32s(data, o, 32)
            tex = []
            for s in words[8:16]:
                tex.append(s if s != 0xFFFFFFFF else -1)
            self.materials.append({'id': words[0], 'shader': words[1],
                                   'words': words, 'tex': tex})
        # textures / shaders
        off, cnt = self.dir[8]
        self.textures = []
        for i in range(cnt):
            rec = data[off + i * 32: off + i * 32 + 32]
            self.textures.append(_cstr(rec[2:32]))
        off, cnt = self.dir[9]
        self.shaders = []
        for i in range(cnt):
            rec = data[off + i * 32: off + i * 32 + 32]
            self.shaders.append(_cstr(rec[2:32]))
        # streams (chunk 6)
        off, cnt = self.dir[6]
        self.streams = []
        for i in range(cnt):
            v = _u32s(data, off + i * 0x20, 8)
            self.streams.append({'id': v[0], 'count': v[1], 'f2': v[2],
                                 'flags': v[3], 'start': v[4], 'bytes': v[5],
                                 'stride': v[6]})
        # index buffer (chunk 11)
        off, cnt = self.dir[11]
        self.index_off = off
        self.index_count = cnt
        self.indices = struct.unpack_from('>%dH' % cnt, data, off)
        # bone palette (chunk 13)
        off, cnt = self.dir[13]
        self.palette = bytes(data[off:off + cnt])
        self.vertex_off = self.dir[7][0]

    def _fill_bone(self, b, vals, off, reclen):
        b.id = vals[0]
        b.first_child = vals[1]
        b.next_sibling = vals[2]
        b.parent = vals[3]
        b.cat = vals[5]
        b.name_idx = vals[6]
        b.name = self.bone_names[b.name_idx] if b.name_idx < len(self.bone_names) else 'bone%03d' % b.id
        b.angles6 = struct.unpack_from('>6H', self.data, off + 0x30)
        b.scale = _f32s(self.data, off + 0x3C, 3)
        b.pos = _f32s(self.data, off + 0x50, 4)
        b.aa = _f32s(self.data, off + 0x60, 4)
        b.off = off
        b.reclen = reclen
        b.is_helper = False
        b.quat = None
        b.hpos = None

    # ---------------- geometry access ----------------
    def stream_base(self, sid):
        return self.vertex_off + self.streams[sid]['start']

    def decode_vertex(self, sid, vi):
        s = self.streams[sid]
        off = self.stream_base(sid) + vi * s['stride']
        st = s['stride']
        d = self.data
        pos = _f32s(d, off, 3)
        w = bytes(d[off + 12:off + 16])
        bi = bytes(d[off + 16:off + 20])
        nrm = None
        if st >= 32:
            nrm = tuple((d[off + 20 + j] - 128) / 127.0 for j in range(3))
        uv = None
        uv2 = None
        if st == 40:
            u0, u1, v0, v1 = struct.unpack_from('>4H', d, off + 32)
            uv = (u0 / 65535.0, v0 / 65535.0)
            uv2 = (u1 / 65535.0, v1 / 65535.0)
        elif st == 36:
            u0, v0 = struct.unpack_from('>2H', d, off + 32)
            uv = (u0 / 65535.0, v0 / 65535.0)
        elif st == 32:
            u0, v0 = struct.unpack_from('>2H', d, off + 28)
            uv = (u0 / 65535.0, v0 / 65535.0)
        return {'pos': pos, 'w': w, 'bi': bi, 'nrm': nrm, 'uv': uv, 'uv2': uv2}

    def encode_vertex(self, sid, vi, pos, w, bi, nrm, uv, uv2):
        s = self.streams[sid]
        off = self.stream_base(sid) + vi * s['stride']
        st = s['stride']
        d = self.data
        struct.pack_into('>3f', d, off, *pos)
        d[off + 12:off + 16] = bytes(w)
        d[off + 16:off + 20] = bytes(bi)
        if st >= 32 and nrm is not None:
            for j in range(3):
                v = int(round(nrm[j] * 127.0)) + 128
                d[off + 20 + j] = max(0, min(255, v))
        if uv is not None:
            u = int(round(uv[0] * 65535.0)) & 0xFFFF
            v = int(round(uv[1] * 65535.0)) & 0xFFFF
            if st == 40:
                u2 = int(round(uv2[0] * 65535.0)) & 0xFFFF
                v2 = int(round(uv2[1] * 65535.0)) & 0xFFFF
                struct.pack_into('>4H', d, off + 32, u, u2, v, v2)
            elif st == 36:
                struct.pack_into('>2H', d, off + 32, u, v)
            elif st == 32:
                struct.pack_into('>2H', d, off + 28, u, v)

    def mesh_geometry(self, m):
        """Return (vmap list, faces list of local index triples).

        Local indices refer to the deduplicated vertex list in
        first-occurrence order (same list order the importer builds).
        """
        vmap = list(self.indices[m.vmap_start:m.vmap_end])
        loc = {}
        for v in vmap:
            if v not in loc:
                loc[v] = len(loc)
        faces = []
        tri = self.indices[m.idx_start:m.idx_start + m.nidx]
        for t in range(0, len(tri) - 2, 3):
            a, b, c = tri[t], tri[t + 1], tri[t + 2]
            if a in loc and b in loc and c in loc:
                faces.append((loc[a], loc[b], loc[c]))
        return vmap, faces

    def palette_bones(self, m):
        return [self.palette[m.pal_base + i] for i in range(m.pal_size)]


# ---------------- helpers ----------------
def aa_to_matrix(ax, ay, az, ang):
    n = math.sqrt(ax * ax + ay * ay + az * az)
    if n < 1e-12:
        return [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]
    x, y, z = ax / n, ay / n, az / n
    c, s = math.cos(ang), math.sin(ang)
    t = 1 - c
    return [[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
            [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
            [t * x * z - s * y, t * y * z + s * x, t * z * z + c]]


def matrix_to_aa(R):
    tr = R[0][0] + R[1][1] + R[2][2]
    ang = math.acos(max(-1.0, min(1.0, (tr - 1) / 2)))
    if ang < 1e-9:
        return (1.0, 0.0, 0.0, 0.0)
    s = 2 * math.sin(ang)
    ax = (R[2][1] - R[1][2]) / s
    ay = (R[0][2] - R[2][0]) / s
    az = (R[1][0] - R[0][1]) / s
    n = math.sqrt(ax * ax + ay * ay + az * az)
    if n < 1e-12:
        return (1.0, 0.0, 0.0, 0.0)
    return (ax / n, ay / n, az / n, ang)


def quat_to_matrix(x, y, z, w):
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n < 1e-12:
        return [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]
    x, y, z, w = x / n, y / n, z / n, w / n
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]


def bone_world_matrix(b):
    """(R 3x3 rows, t) in game space."""
    if b.is_helper and b.hpos is not None:
        t = tuple(b.hpos)
        q = b.quat
        R = quat_to_matrix(q[0], q[1], q[2], q[3])
    else:
        t = (b.pos[0], b.pos[1], b.pos[2])
        R = aa_to_matrix(*b.aa)
    return R, t
