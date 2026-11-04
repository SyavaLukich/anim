"""Headless .gmd -> glTF (GLB) exporter for Binary Domain models.

Parses the raw ``.gmd`` file with the (pure-Python) structure layer of the
yk_gmd_io project (Blender addon by theturboturnip, CC BY-SA 4.0) and writes
a glTF 2.0 binary file containing:

* the character meshes (positions, normals, first UV set, triangle indices)
* the skeleton (bone hierarchy with rest-pose TRS)
* skinning (4 influence bones + weights per vertex, inverse bind matrices)
* simple PBR materials approximated from the Yakuza material struct
* embedded diffuse textures (decoded from the DXT1 ``.dds`` files, optional)
* per-node "extras" keeping the original GMD metadata (matrix lists, flags,
  anim axes) so the data can be re-packed into a .gmd later

Binary Domain notes (GMD 3.11):
  BD character files bind most meshes *rigidly*: every mesh declares a matrix
  list of influencing bones, but the vertex buffers (except a handful of
  verts) carry no per-vertex bone/weight data.  Rigid meshes are exported
  with all vertices weighted 100% to the first bone of the matrix list.
  A small compatibility patch relaxes the addon's skinned-mesh assertions
  (see install_bd_compat).

Coordinate space: glTF canonical (right-handed, +Y up).  The GMD world is
mapped by the same transform the yk_gmd_io addon uses to bring the model
into Blender, followed by the standard Blender->glTF axis change:

    gmd (x, y, z)  ->  blender (-x, z, y)  ->  gltf (-x, -y, z)

Run as a module:

    python3 -m bdtools.gltf_export in.gmd out.glb [--dds-dir DIR]
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import struct
import sys
import zlib
from typing import Dict, List, Optional, Tuple

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))


def setup_gmd_import_paths(yk_gmd_io_root: Optional[str] = None) -> None:
    """Make the mathutils shim and yk_gmd_io importable (idempotent)."""
    shim_dir = os.path.join(_HERE, "_vendor")
    if shim_dir not in sys.path:
        sys.path.insert(0, shim_dir)
    root = yk_gmd_io_root or os.environ.get("YK_GMD_IO_ROOT")
    if not root:
        # fallback: <repo>/work/yk_gmd_io  (repo = parent of tools/)
        root = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "work", "yk_gmd_io")
    if os.path.isdir(root) and root not in sys.path:
        sys.path.insert(0, root)


class _Rep:
    def recoverable(self, msg):
        pass

    def fatal_exception(self, ex):
        raise ex

    def fatal(self, msg):
        raise RuntimeError(msg)

    def info(self, msg):
        pass

    def debug(self, cat, msg):
        return False


def install_bd_compat() -> None:
    """Compatibility patch for Binary Domain .gmd files (GMD version 3.11).

    BD character files bind most meshes *rigidly*: every mesh declares a
    matrix list of influencing bones, but the vertex buffers (except for a
    tiny handful of verts) carry NO per-vertex bone/weight data.  The
    yk_gmd_io addon (written against Yakuza 0 style files) asserts that a
    skinned mesh always has per-vertex skin data and that all referenced
    bone indices are inside the matrix list.  We relax both: rigid meshes
    get zeroed skin arrays here, and the exporter binds their vertices 100%
    to the first bone in the matrix list.  The 255 sentinel (no bone) that
    appears in real BD skin data is also tolerated.
    """
    if getattr(install_bd_compat, "_done", False):
        return
    from yk_gmd_blender.gmdlib.abstract import gmd_mesh, gmd_shader

    orig_copy = gmd_shader.GMDVertexBuffer.copy_as_skinned

    def copy_as_skinned(self, s: slice = slice(None)):
        if self.bone_data is None or self.weight_data is None:
            n = len(self.pos[s])
            return gmd_shader.GMDSkinnedVertexBuffer(
                layout=self.layout,
                pos=self.pos[s].copy(),
                bone_data=np.zeros((n, 4), dtype=np.uint8),
                weight_data=np.zeros((n, 4), dtype=np.float32),
                normal=self.normal[s].copy() if self.normal is not None else None,
                tangent=self.tangent[s].copy() if self.tangent is not None else None,
                unk=self.unk[s].copy() if self.unk is not None else None,
                col0=self.col0[s].copy() if self.col0 is not None else None,
                col1=self.col1[s].copy() if self.col1 is not None else None,
                uvs=[uv[s].copy() for uv in self.uvs],
            )
        return orig_copy(self, s)

    gmd_shader.GMDVertexBuffer.copy_as_skinned = copy_as_skinned

    orig_post_init = gmd_mesh.GMDSkinnedMesh.__post_init__

    def post_init(self):
        try:
            orig_post_init(self)
        except (TypeError, Exception) as e:
            msg = str(e)
            if "references no bones" in msg or "references {" in msg:
                return  # rigid BD mesh, or 255 sentinel bone index
            raise

    gmd_mesh.GMDSkinnedMesh.__post_init__ = post_init
    install_bd_compat._done = True


def load_gmd_scene(data: bytes):
    """Parse raw .gmd bytes into (version_props, header, abstract scene)."""
    from yk_gmd_blender.gmdlib.io import (
        FileImportMode,
        VertexImportMode,
        read_abstract_scene_from_filedata_object,
        read_gmd_structures,
    )

    install_bd_compat()
    rep = _Rep()
    version_props, header, contents = read_gmd_structures(data, rep)
    scene = read_abstract_scene_from_filedata_object(
        version_props, FileImportMode.SKINNED, VertexImportMode.IMPORT_VERTICES,
        contents, rep)
    return version_props, header, scene


# ---------------------------------------------------------------------------
# coordinate helpers
# ---------------------------------------------------------------------------

# gmd -> blender -> gltf  (see module docstring)
C_GMD_TO_BLENDER = np.array([[-1, 0, 0, 0],
                             [0, 0, 1, 0],
                             [0, 1, 0, 0],
                             [0, 0, 0, 1]], dtype=np.float64)
C_BLENDER_TO_GLSF = np.array([[1, 0, 0, 0],
                              [0, 0, -1, 0],
                              [0, 1, 0, 0],
                              [0, 0, 0, 1]], dtype=np.float64)
C = C_BLENDER_TO_GLSF @ C_GMD_TO_BLENDER  # == diag(-1, -1, 1, 1)
C3 = C[:3, :3].astype(np.float32)


def _trs_from_matrix(m: np.ndarray) -> Dict:
    """Decompose a 4x4 into glTF translation/rotation/scale."""
    t = m[:3, 3].astype(np.float64)
    r3 = m[:3, :3].astype(np.float64)
    s = np.empty(3, dtype=np.float64)
    for i in range(3):
        s[i] = float(np.linalg.norm(r3[i]))
    if float(np.linalg.det(r3)) < 0:
        s[2] = -s[2]
    rot = r3 / s
    tr = rot[0, 0] + rot[1, 1] + rot[2, 2]
    if tr > 0:
        sf = math.sqrt(tr + 1.0) * 2
        w = 0.25 * sf
        x = (rot[2, 1] - rot[1, 2]) / sf
        y = (rot[0, 2] - rot[2, 0]) / sf
        z = (rot[1, 0] - rot[0, 1]) / sf
    elif rot[0, 0] > rot[1, 1] and rot[0, 0] > rot[2, 2]:
        sf = math.sqrt(1.0 + rot[0, 0] - rot[1, 1] - rot[2, 2]) * 2
        w = (rot[2, 1] - rot[1, 2]) / sf
        x = 0.25 * sf
        y = (rot[0, 1] + rot[1, 0]) / sf
        z = (rot[0, 2] + rot[2, 0]) / sf
    elif rot[1, 1] > rot[2, 2]:
        sf = math.sqrt(1.0 + rot[1, 1] - rot[0, 0] - rot[2, 2]) * 2
        w = (rot[0, 2] - rot[2, 0]) / sf
        x = (rot[0, 1] + rot[1, 0]) / sf
        y = 0.25 * sf
        z = (rot[1, 2] + rot[2, 1]) / sf
    else:
        sf = math.sqrt(1.0 + rot[2, 2] - rot[0, 0] - rot[1, 1]) * 2
        w = (rot[1, 0] - rot[0, 1]) / sf
        x = (rot[0, 2] + rot[2, 0]) / sf
        y = (rot[1, 2] + rot[2, 1]) / sf
        z = 0.25 * sf
    q = np.array([w, x, y, z], dtype=np.float64)
    q /= max(float(np.linalg.norm(q)), 1e-30)
    out = {"translation": [float(t[0]), float(t[1]), float(t[2])]}
    if (abs(float(q[1])) > 1e-9 or abs(float(q[2])) > 1e-9
            or abs(float(q[3])) > 1e-9 or abs(float(q[0]) - 1) > 1e-9):
        out["rotation"] = [float(q[1]), float(q[2]), float(q[3]), float(q[0])]
    if abs(s[0] - 1) > 1e-9 or abs(s[1] - 1) > 1e-9 or abs(s[2] - 1) > 1e-9:
        out["scale"] = [float(s[0]), float(s[1]), float(s[2])]
    return out


def _local_matrix_from_node(node) -> np.ndarray:
    p = node.pos
    r = node.rot
    s = node.scale
    x, y, z, w = float(r.x), float(r.y), float(r.z), float(r.w)
    rot = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ], dtype=np.float64)
    m = np.identity(4, dtype=np.float64)
    m[:3, :3] = rot * np.array([float(s.x), float(s.y), float(s.z)], dtype=np.float64)
    m[:3, 3] = [float(p.x), float(p.y), float(p.z)]
    return m


# ---------------------------------------------------------------------------
# DXT1 (DDS) decoding + minimal PNG writer (for embedded textures)
# ---------------------------------------------------------------------------

def _unpack_rgb565(c: int) -> Tuple[int, int, int]:
    r = (c >> 11) & 0x1F
    g = (c >> 5) & 0x3F
    b = c & 0x1F
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def _dxt_palette(c0: int, c1: int):
    r0, g0, b0 = _unpack_rgb565(c0)
    r1, g1, b1 = _unpack_rgb565(c1)
    if c0 > c1:
        return [(r0, g0, b0), (r1, g1, b1),
                ((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3),
                ((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3)]
    return [(r0, g0, b0), (r1, g1, b1),
            ((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2),
            (0, 0, 0)]


def _decode_dxt_blocks(data: bytes, width: int, height: int, block: int,
                       dxt5: bool) -> bytes:
    """Decode the base mipmap level of a DXT1/DXT5 stream -> RGBA8.

    Only the first (w/4)*(h/4) blocks are read, so trailing mip levels
    (BD stores a full chain) are ignored.
    """
    out = bytearray(width * height * 4)
    nxb = (width + 3) // 4
    nyb = (height + 3) // 4
    for by in range(nyb):
        for bx in range(nxb):
            base = (by * nxb + bx) * block
            pal = _dxt_palette(data[base] | data[base + 1] << 8,
                               data[base + 2] | data[base + 3] << 8)
            for i in range(16):
                py = by * 4 + (i >> 2)
                px = bx * 4 + (i & 3)
                if py >= height or px >= width:
                    continue
                code = (data[base + 4 + (i >> 2)] >> ((i & 3) * 2)) & 3
                r, g, b = pal[code]
                o = (py * width + px) * 4
                out[o] = r
                out[o + 1] = g
                out[o + 2] = b
                if dxt5:
                    a0 = data[base + 8]
                    a1 = data[base + 9]
                    a = (data[base + 10 + (i >> 2)] >> ((i & 3) * 2)) & 3
                    if a == 0:
                        av = a0
                    elif a == 1:
                        av = a1
                    elif a0 > a1:
                        av = (2 * a0 + a1 + 1) // 3 if a == 2 else (a0 + 2 * a1 + 1) // 3
                    else:
                        av = (a0 + a1) // 2 if a == 2 else (0 if a == 3 else 255)
                    out[o + 3] = av
                else:
                    out[o + 3] = 255
    return bytes(out)


def decode_dds(d: bytes) -> Tuple[bytes, int, int]:
    """Decode the base level of a BD .dds -> (rgba8, width, height).

    Verified BD DDS layout: +0x00 "DDS ", +0x0C height, +0x10 width,
    +0x24 "PXDE" custom tag, +0x54 format (b'DXT1'/b'DXT5'/zero=raw RGBA),
    +0x80 pixel data (base level first, then full mip chain).
    """
    if d[:4] != b"DDS ":
        raise ValueError("not a DDS file")
    height = struct.unpack_from("<I", d, 0x0C)[0]
    width = struct.unpack_from("<I", d, 0x10)[0]
    fmt = d[0x54:0x58]
    data = d[128:]
    nxb = (width + 3) // 4
    nyb = (height + 3) // 4
    if fmt == b"DXT1":
        need = nxb * nyb * 8
        if len(data) < need:
            raise ValueError("truncated DXT1 data")
        return _decode_dxt_blocks(data, width, height, 8, False), width, height
    if fmt == b"DXT5":
        need = nxb * nyb * 16
        if len(data) < need:
            raise ValueError("truncated DXT5 data")
        return _decode_dxt_blocks(data, width, height, 16, True), width, height
    if fmt == b"\x00\x00\x00\x00":
        need = width * height * 4
        if len(data) < need:
            raise ValueError("truncated raw RGBA data")
        return data[:need], width, height
    raise ValueError(f"unsupported DDS pixel format {fmt!r}")


def read_dds(path: str) -> Tuple[bytes, int, int]:
    """Read a .dds base level -> (rgba8, width, height)."""
    with open(path, "rb") as fh:
        return decode_dds(fh.read())


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload +
            struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def encode_png(rgba: bytes, width: int, height: int) -> bytes:
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw.extend(rgba[y * stride:(y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" +
            _png_chunk(b"IHDR", ihdr) +
            _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
            _png_chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# glTF assembly
# ---------------------------------------------------------------------------

class _GLBBuilder:
    def __init__(self):
        self.json: Dict = {}
        self.bin = bytearray()
        self.buffer_views: List[Dict] = []
        self.accessors: List[Dict] = []

    def _align(self, n: int) -> None:
        pad = (-len(self.bin)) % n
        if pad:
            self.bin += b"\x00" * pad

    def add_view(self, data: bytes, target: Optional[int] = None) -> int:
        self._align(4)
        off = len(self.bin)
        self.bin += data
        bv = {"buffer": 0, "byteOffset": off, "byteLength": len(data)}
        if target is not None:
            bv["target"] = target
        self.buffer_views.append(bv)
        return len(self.buffer_views) - 1

    def add_accessor(self, view: int, comp_type: int, type_: str, count: int,
                     min_max: Optional[Tuple] = None) -> int:
        acc = {"bufferView": view, "componentType": comp_type,
               "type": type_, "count": count}
        if min_max is not None:
            mn, mx = min_max
            acc["min"] = [float(v) for v in mn]
            acc["max"] = [float(v) for v in mx]
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def build(self, name: str) -> bytes:
        self.json["asset"] = {"version": "2.0",
                              "generator": "bdtools gltf_export (Binary Domain .gmd)"}
        self.json["buffers"] = [{"byteLength": len(self.bin)}]
        self.json["bufferViews"] = self.buffer_views
        self.json["accessors"] = self.accessors
        j = json.dumps(self.json, separators=(",", ":")).encode("utf-8")
        j += b" " * ((4 - len(j) % 4) % 4)
        out = struct.pack("<III", 0x46546C67, 2,
                          12 + 8 + len(j) + 8 + len(self.bin))
        out += struct.pack("<II", len(j), 0x4E4F534A) + j
        out += struct.pack("<II", len(self.bin), 0x004E4942) + bytes(self.bin)
        return out


def _first_uv(buf) -> Optional[np.ndarray]:
    uvs = getattr(buf, "uvs", None)
    if uvs and len(uvs) > 0 and uvs[0] is not None and len(uvs[0]) > 0:
        u = np.asarray(uvs[0], dtype=np.float32)
        if u.ndim == 2 and u.shape[1] >= 2:
            return u[:, :2]
    return None


def _node_extras(node) -> Dict:
    """Preserve original GMD metadata on the glTF node for re-import."""
    ex = {"gmd_node_type": node.node_type.name}
    try:
        ex["gmd_anim_axis"] = [float(node.anim_axis.x), float(node.anim_axis.y),
                               float(node.anim_axis.z)]
        ex["gmd_flags"] = [int(f) for f in node.flags]
    except Exception:
        pass
    return ex


def export_gmd(gmd_path: str, glb_path: str,
               dds_dir: Optional[str] = None,
               include_textures: bool = True) -> Dict:
    """Export one .gmd to a .glb.  Returns a small stats dict."""
    setup_gmd_import_paths()
    with open(gmd_path, "rb") as fh:
        data = fh.read()
    version_props, header, scene = load_gmd_scene(data)
    name = scene.name or os.path.splitext(os.path.basename(gmd_path))[0]

    glb = _GLBBuilder()

    # -- node bookkeeping ----------------------------------------------------
    nodes: List[Dict] = []          # glTF node dicts
    glb_idx_of: Dict[int, int] = {}  # id(GMDNode) -> glTF node index
    bone_name_to_glb: Dict[str, int] = {}
    bone_depth_first: List = []     # GMDBone in depth-first order

    def add_node(nd: Dict) -> int:
        nodes.append(nd)
        return len(nodes) - 1

    root_idx = add_node({"name": name, "children": []})
    skeleton_idx = add_node({"name": f"{name}_skeleton", "children": []})
    nodes[root_idx]["children"].append(skeleton_idx)

    # -- walk the hierarchy: world matrices (gmd space) + glTF nodes ---------
    world_of: Dict[int, np.ndarray] = {}

    def walk(node, parent_glb: int, parent_world: np.ndarray) -> None:
        L = _local_matrix_from_node(node)
        W = parent_world @ L
        world_of[id(node)] = W
        Lt = C @ L @ np.linalg.inv(C)
        nd = {"name": node.name,
              "children": [],
              **_trs_from_matrix(Lt),
              **_node_extras(node)}
        gidx = add_node(nd)
        glb_idx_of[id(node)] = gidx
        nodes[parent_glb]["children"].append(gidx)

        if hasattr(node, "matrix") and node.node_type.name == "MatrixTransform":
            bone_name_to_glb[node.name] = gidx
            bone_depth_first.append(node)
        for child in node.children:
            walk(child, gidx, W)

    for root in scene.overall_hierarchy.roots:
        walk(root, skeleton_idx, np.identity(4, dtype=np.float64))

    # -- skin ------------------------------------------------------------------
    meshes: List[Dict] = []
    materials: List[Dict] = []
    mat_cache: List[tuple] = []
    images: List[Dict] = []
    textures: List[Dict] = []
    skins: List[Dict] = []

    joint_glb_indices = [bone_name_to_glb[b.name] for b in bone_depth_first]
    inv_bind = []
    for b in bone_depth_first:
        Wt = C @ world_of[id(b)] @ np.linalg.inv(C)
        inv_bind.append(np.linalg.inv(Wt).astype("<f4").T.tobytes())
    if joint_glb_indices:
        ibv = glb.add_view(b"".join(inv_bind), None)
        ib_acc = glb.add_accessor(ibv, 5126, "MAT4", len(joint_glb_indices))
        skin = {"joints": joint_glb_indices, "inverseBindMatrices": ib_acc}
        skins.append(skin)
        skin_idx = len(skins) - 1
    else:
        skin_idx = None
    # glTF joint index for every bone name (used to remap vertex influences)
    bone_name_to_joint = {b.name: i for i, b in enumerate(bone_depth_first)}

    def get_material(attr_set) -> Optional[int]:
        if attr_set is None or getattr(attr_set, "material", None) is None:
            return None
        m = attr_set.material.origin_data
        tex_name = getattr(attr_set, "texture_diffuse", None) or ""
        alpha_name = getattr(attr_set, "texture_alpha", None) or ""
        key = (tuple(m.diffuse), m.opacity, tuple(m.specular),
               round(float(m.power), 3), tex_name, alpha_name)
        for i, k in enumerate(mat_cache):
            if k == key:
                return i
        mat: Dict = {}
        pbr: Dict = {}
        diffuse = [max(0.0, min(1.0, c / 255.0)) for c in list(m.diffuse)[:3]]
        pbr["baseColorFactor"] = [diffuse[0], diffuse[1], diffuse[2],
                                  max(0.0, min(1.0, m.opacity / 255.0))]
        pbr["roughnessFactor"] = max(0.04, min(1.0, 1.0 - float(m.power) / 255.0))
        pbr["metallicFactor"] = 0.0
        mat["name"] = tex_name or f"material_{len(materials)}"
        mat["pbrMetallicRoughness"] = pbr
        if m.opacity < 255:
            mat["alphaMode"] = "BLEND"
            mat["doubleSided"] = True
        if tex_name and dds_dir and include_textures:
            tex_path = os.path.join(dds_dir, f"{tex_name}.dds")
            if os.path.isfile(tex_path):
                try:
                    rgba, w, h = read_dds(tex_path)
                    a = None
                    # composite the separate GMD alpha mask into the PNG alpha
                    if alpha_name:
                        a_path = os.path.join(dds_dir, f"{alpha_name}.dds")
                        if os.path.isfile(a_path):
                            try:
                                a_rgba, aw, ah = read_dds(a_path)
                                if (aw, ah) == (w, h):
                                    a = a_rgba[3::4]
                                    if a.min() == 255:  # DXT1 mask: use luma
                                        r = a_rgba[0::4].astype(np.int32)
                                        g = a_rgba[1::4].astype(np.int32)
                                        b = a_rgba[2::4].astype(np.int32)
                                        a = ((r * 77 + g * 151 + b * 29) >> 8)
                                    rgba = bytearray(rgba)
                                    rgba[3::4] = a
                            except Exception:
                                pass
                    v = glb.add_view(encode_png(bytes(rgba), w, h), None)
                    images.append({"bufferView": v, "mimeType": "image/png"})
                    textures.append({"source": len(images) - 1})
                    pbr["baseColorTexture"] = {"index": len(textures) - 1}
                    if a is not None and a.min() < 250 and mat.get("alphaMode") != "BLEND":
                        mat["alphaMode"] = "BLEND"
                        mat["doubleSided"] = True
                except Exception:
                    pass
        materials.append(mat)
        mat_cache.append(key)
        return len(materials) - 1

    def export_object(node) -> None:
        gidx = glb_idx_of[id(node)]
        mesh_list = node.mesh_list
        if not mesh_list:
            return
        # concatenate all sub-meshes of this object into one glTF mesh
        verts_pos, verts_nrm, verts_uv = [], [], []
        joints_all, weights_all = [], []
        prim_specs = []
        vbase = 0
        for gm in mesh_list:
            buf = gm.vertices_data
            n = len(buf)
            pos = np.asarray(buf.pos, dtype=np.float32)[:, :3] @ C3
            nrm = None
            if buf.normal is not None:
                nrm = np.asarray(buf.normal, dtype=np.float32)[:, :3] @ C3
                nn = np.linalg.norm(nrm, axis=1, keepdims=True)
                nrm = np.divide(nrm, nn, out=np.zeros_like(nrm), where=nn > 1e-12)
            uv = _first_uv(buf)
            verts_pos.append(pos)
            verts_nrm.append(nrm)
            verts_uv.append(uv)
            if skin_idx is not None and buf.bone_data is not None and buf.weight_data is not None:
                bones = np.asarray(buf.bone_data)[:, :4].astype(np.int64)
                weights = np.asarray(buf.weight_data, dtype=np.float32)[:, :4].copy()
                # plain GMDMesh (not GMDSkinnedMesh) has no relevant_bones:
                # its bone_data indices are relative to the object's own bone
                rel_bones = getattr(gm, "relevant_bones", None) or []
                nb = len(rel_bones)
                remap = np.full((n, 4), -1, dtype=np.int64)
                valid = (bones >= 0) & (bones < nb)
                rows, cols = np.nonzero(valid)
                for r, c in zip(rows.tolist(), cols.tolist()):
                    joint = bone_name_to_joint.get(rel_bones[int(bones[r, c])].name)
                    if joint is not None:
                        remap[r, c] = joint
                # unusable influences (255 sentinel, out of range, missing
                # bone node): zero the weight for that slot
                weights[~valid | (remap < 0)] = 0.0
                if not bool(np.any(weights)):
                    # rigid BD mesh: bind 100% to first bone of the matrix list;
                    # plain GMDMesh (no matrix list) binds to the object's bone
                    if nb:
                        fallback = bone_name_to_joint.get(rel_bones[0].name, 0)
                    else:
                        parent = getattr(node, "parent", None)
                        fallback = bone_name_to_joint.get(parent.name, 0) \
                            if parent is not None else 0
                    remap = np.zeros((n, 4), dtype=np.int64)
                    remap[:, 0] = fallback
                    weights = np.zeros((n, 4), dtype=np.float32)
                    weights[:, 0] = 1.0
                joints_all.append(np.where(remap < 0, 0, remap).astype(np.uint16))
                weights_all.append(weights)
            idx = np.frombuffer(np.asarray(gm.triangles.triangle_list, dtype=np.uint32).tobytes(),
                                dtype=np.uint32) + vbase
            mat_idx = get_material(gm.attribute_set)
            prim_specs.append((idx, mat_idx, gm))
            vbase += n

        total_v = vbase
        if total_v == 0:
            return

        all_pos = np.concatenate(verts_pos).astype("<f4")
        pmin = all_pos.min(axis=0).tolist()
        pmax = all_pos.max(axis=0).tolist()
        pv = glb.add_view(all_pos.tobytes(), 34962)
        pa = glb.add_accessor(pv, 5126, "VEC3", total_v, (pmin, pmax))
        attrs = {"POSITION": pa}
        if all(x is not None for x in verts_nrm):
            all_nrm = np.concatenate(verts_nrm).astype("<f4")
            nv = glb.add_view(all_nrm.tobytes(), 34962)
            attrs["NORMAL"] = glb.add_accessor(nv, 5126, "VEC3", total_v)
        if all(u is not None for u in verts_uv):
            all_uv = np.concatenate(verts_uv).astype("<f4")
            uvv = glb.add_view(all_uv.tobytes(), 34962)
            attrs["TEXCOORD_0"] = glb.add_accessor(uvv, 5126, "VEC2", total_v)
        if skin_idx is not None and joints_all:
            j_all = np.concatenate(joints_all).astype("<u2")
            w_all = np.concatenate(weights_all).astype("<f4")
            jv = glb.add_view(j_all.tobytes(), 34962)
            attrs["JOINTS_0"] = glb.add_accessor(jv, 5123, "VEC4", total_v)
            wv = glb.add_view(w_all.tobytes(), 34962)
            attrs["WEIGHTS_0"] = glb.add_accessor(wv, 5126, "VEC4", total_v)

        # object-level GMD metadata for re-import: matrix list per sub-mesh
        obj_extras = {"gmd_matrix_lists": [
            [rb.name for rb in (getattr(gm, "relevant_bones", None) or [])]
            for gm in mesh_list
        ]}
        if "extras" in nodes[gidx]:
            nodes[gidx]["extras"].update(obj_extras)
        else:
            nodes[gidx]["extras"] = obj_extras

        primitives = []
        for idx, mat_idx, gm in prim_specs:
            if int(idx.max()) < 0x10000:
                dt, itype = idx.astype("<u2"), 5123
            else:
                dt, itype = idx.astype("<u4"), 5125
            iv = glb.add_view(dt.tobytes(), 34963)
            ia = glb.add_accessor(iv, itype, "SCALAR", len(idx))
            prim = {"attributes": attrs, "indices": ia, "mode": 4}
            if mat_idx is not None:
                prim["material"] = mat_idx
            primitives.append(prim)

        meshes.append({"name": node.name, "primitives": primitives})
        nodes[gidx]["mesh"] = len(meshes) - 1
        if skin_idx is not None:
            nodes[gidx]["skin"] = skin_idx

    for _, node in scene.overall_hierarchy.depth_first_iterate():
        if type(node).__name__ in ("GMDSkinnedObject", "GMDUnskinnedObject"):
            export_object(node)

    # -- finalize -------------------------------------------------------------
    glb.json["scene"] = 0
    glb.json["scenes"] = [{"name": name, "nodes": [root_idx]}]
    glb.json["nodes"] = nodes
    glb.json["meshes"] = meshes
    if materials:
        glb.json["materials"] = materials
    if images:
        glb.json["images"] = images
    if textures:
        glb.json["textures"] = textures
    if skins:
        glb.json["skins"] = skins

    out = glb.build(name)
    with open(glb_path, "wb") as fh:
        fh.write(out)

    total_vertices = 0
    for m in meshes:
        a = glb.accessors[m["primitives"][0]["attributes"]["POSITION"]]
        total_vertices += a["count"]
    return {
        "name": name,
        "vertices": total_vertices,
        "meshes": len(meshes),
        "bones": len(bone_depth_first),
        "materials": len(materials),
        "textures": len(images),
        "bytes": len(out),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Export a Binary Domain .gmd to glTF (GLB)")
    ap.add_argument("input", help="path to a .gmd file, or a directory containing them")
    ap.add_argument("output", nargs="?", help="output .glb (or directory)")
    ap.add_argument("--dds-dir", help="directory with the .dds textures")
    ap.add_argument("--no-textures", action="store_true")
    ap.add_argument("--yk-gmd-io", dest="yk", help="path to the yk_gmd_io checkout")
    args = ap.parse_args(argv)

    if args.yk:
        os.environ["YK_GMD_IO_ROOT"] = args.yk
    dds_dir = args.dds_dir
    if dds_dir is None and os.path.isdir(args.input):
        dds_dir = args.input  # BD keeps .dds next to the .gmd
    if dds_dir is None and not os.path.isdir(args.input):
        dds_dir = os.path.dirname(os.path.abspath(args.input))

    if os.path.isdir(args.input):
        ins = sorted(glob.glob(os.path.join(args.input, "**", "*.gmd"), recursive=True))
        if not ins:
            ins = sorted(glob.glob(os.path.join(args.input, "*.gmd")))
        outs = args.output or os.path.join(args.input, "glb")
        os.makedirs(outs, exist_ok=True)
        is_dir_out = True
    else:
        ins = [args.input]
        outs = args.output or os.path.splitext(args.input)[0] + ".glb"
        parent = os.path.dirname(outs)
        if parent:
            os.makedirs(parent, exist_ok=True)
        is_dir_out = False

    fails = 0
    for gmd in ins:
        out = os.path.join(outs, os.path.splitext(os.path.basename(gmd))[0] + ".glb") \
            if is_dir_out else outs
        try:
            stats = export_gmd(gmd, out, dds_dir=dds_dir,
                               include_textures=not args.no_textures)
            print(f"OK   {gmd} -> {out}  "
                  f"({stats['vertices']} verts, {stats['bones']} bones, "
                  f"{stats['materials']} mats, {stats['textures']} texs, "
                  f"{stats['bytes'] / 1e6:.1f} MB)", flush=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            fails += 1
            print(f"FAIL {gmd}: {type(e).__name__}: {e}", flush=True)
    print(f"done: {len(ins) - fails}/{len(ins)} exported", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
