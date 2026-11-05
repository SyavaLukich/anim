# SPDX-License-Identifier: GPL-2.0-or-later
"""
Binary Domain (.gmd / .mmo) import-export for Blender.

- Patch-based exporter: the original file is re-read and only bone records
  and vertex streams are overwritten, so an untouched import exports
  byte-identical to the source.
- Topology (vertices/faces/materials) is read-only in this version.
- MOMO constant expressions are decoded into position/quaternion bone Actions.
  Exact channel words are retained as Action metadata; preview timing is generated.
"""
bl_info = {
    "name": "Binary Domain: GMD / MOMO",
    "author": "Arena agent",
    "version": (0, 7, 0),
    "blender": (4, 1, 0),
    "location": "File > Import-Export, 3D View > N-panel > Binary Domain",
    "description": "Import/export Binary Domain (Sega, 2012) GSGM models and MOMO face/body animations",
    "category": "Import-Export",
}

import bpy
import json
import math
import mathutils
import os
import struct
from bpy.props import (StringProperty, BoolProperty, IntProperty, EnumProperty, CollectionProperty, FloatProperty)
from bpy_extras.io_utils import ImportHelper, ExportHelper

from . import gmd as gmd_mod
from . import mmo as mmo_mod
from . import body_corrections as correction_mod
from . import control_rig as control_mod

# Blender can re-execute the package after an update while retaining old
# parser submodules in sys.modules. Refresh both pure-Python modules too.
import importlib
importlib.reload(gmd_mod)
importlib.reload(mmo_mod)
importlib.reload(correction_mod)
importlib.reload(control_mod)

AXIS_CONV = mathutils.Matrix.Rotation(math.pi / 2, 3, 'X')  # game Y-up -> Blender Z-up (display only)
BONE_LEN = 0.05

RAW_ATTRS = ('gmd_w', 'gmd_bi', 'gmd_nb', 'gmd_w1', 'gmd_w2')


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _signed(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v >= 0x80000000 else v


def _unsigned(v):
    return v & 0xFFFFFFFF


def bone_matrix(b):
    """4x4 rest matrix of a GMD bone in game space."""
    R, t = gmd_mod.bone_world_matrix(b)
    mat = mathutils.Matrix.Identity(4)
    for i in range(3):
        for j in range(3):
            mat[i][j] = R[i][j]
    mat[0][3], mat[1][3], mat[2][3] = t
    return mat


def matrix_to_axis_angle(mat3):
    R = [[mat3[i][j] for j in range(3)] for i in range(3)]
    return gmd_mod.matrix_to_aa(R)


def matrix_to_quat(mat3):
    q = mat3.to_quaternion()
    return (q.x, q.y, q.z, q.w)


# ---------------------------------------------------------------------------
# materials
# ---------------------------------------------------------------------------
def _make_materials(g, basedir):
    mats = []
    for i, m in enumerate(g.materials):
        name = '%s_mat%02d' % (g.name, i)
        mat = bpy.data.materials.get(name)
        if mat is None:
            mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        nt = mat.node_tree
        for n in list(nt.nodes):
            nt.nodes.remove(n)
        out = nt.nodes.new('ShaderNodeOutputMaterial')
        bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (-300, 0)
        nt.links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
        uv_node = nt.nodes.new('ShaderNodeUVMap')
        uv_node.uv_map = 'UVMap'
        uv_node.location = (-950, 0)
        slot_names = []
        diffuse_connected = False
        for s, tex in enumerate(m['tex']):
            if tex < 0 or tex >= len(g.textures):
                slot_names.append('')
                continue
            tname = g.textures[tex]
            slot_names.append(tname)
            path = os.path.join(basedir, tname + '.dds')
            if not os.path.exists(path):
                continue
            img = bpy.data.images.load(path, check_existing=True)
            texn = nt.nodes.new('ShaderNodeTexImage')
            texn.image = img
            nt.links.new(uv_node.outputs['UV'], texn.inputs['Vector'])
            texn.location = (-700, -250 * s)
            low = tname.lower()
            if '(n)' in low:
                img.colorspace_settings.name = 'Non-Color'
                # Packed game normal maps are retained for manual setup;
                # their channel convention is not confirmed.
            elif '(d)' in low and not diffuse_connected:
                diffuse_connected = True
                nt.links.new(texn.outputs['Color'], bsdf.inputs['Base Color'])
                # Do not interpret game masks as surface transparency.
            elif '(a)' in low:
                img.colorspace_settings.name = 'Non-Color'
                # Mask semantics depend on the original game shader.
        mat['gmd_shader'] = g.shaders[m['shader']] if m['shader'] < len(g.shaders) else ''
        mat['gmd_tex_slots'] = ','.join(slot_names)
        mats.append(mat)
    return mats


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------
def load_gmd(context, filepath, import_momo=None, rig_mma=None, bake_euler=False):
    filepath = _input_file_path(filepath, '.gmd', 'GMD model')
    data = open(filepath, 'rb').read()
    g = gmd_mod.GMD(data, filepath)
    basedir = os.path.dirname(filepath)

    col = bpy.data.collections.new('GMD_' + g.name)
    context.scene.collection.children.link(col)

    # ---------------- armature ----------------
    arm_name = 'Arm_' + g.name
    arm_data = bpy.data.armatures.new(arm_name)
    arm_obj = bpy.data.objects.new(arm_name, arm_data)
    arm_obj.rotation_euler = AXIS_CONV.to_euler()
    col.objects.link(arm_obj)
    context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='EDIT')
    ebs = {}
    for b in g.bones:
        eb = arm_data.edit_bones.new(b.name)
        ebs[b.id] = eb
        m = bone_matrix(b)
        # NOTE: eb.matrix setter does not preserve the full rotation; set
        # direction + roll explicitly so matrix_local matches the file matrix.
        R3 = m.to_3x3()
        eb.head = m.translation
        eb.tail = m.translation + R3.col[1] * BONE_LEN
        eb.align_roll(R3.col[2])
    # hierarchy: the [3] parent field is unreliable (face bones carry
    # 0xFFFFFFFF there); derive parents from first_child/next_sibling chains
    parent_of = {}
    def _walk(bid, depth=0):
        if depth > 512:
            return
        bb = g.bones[bid]
        c = bb.first_child
        while c != 0xFFFFFFFF and c < len(g.bones):
            parent_of[c] = bid
            _walk(c, depth + 1)
            c = g.bones[c].next_sibling
    for b in g.bones:
        _walk(b.id)
    for b in g.bones:
        pid = parent_of.get(b.id, b.parent if b.parent != 0xFFFFFFFF else None)
        if pid is not None and pid in ebs:
            ebs[b.id].parent = ebs[pid]
    bpy.ops.object.mode_set(mode='OBJECT')
    arm_obj['gmd_source'] = filepath
    arm_obj['gmd_model'] = g.name
    arm_obj['gmd_bind_local_quats'] = json.dumps({
        b.name: list(struct.unpack_from('>4f', g.data, b.off + 0x30)) for b in g.bones})
    arm_obj['gmd_skinning_version'] = 2
    arm_obj['gmd_uv_decoder'] = 'binary16-declaration-v1'

    # ---------------- materials ----------------
    mats = _make_materials(g, basedir)

    # ---------------- meshes ----------------
    report_missing_tex = set()
    for m in g.meshes:
        vmap, faces = g.mesh_geometry(m)
        seen = {}
        order = []
        for i, v in enumerate(vmap):
            if v not in seen:
                seen[v] = len(order)
                order.append(v)
        verts = []
        for v in order:
            verts.append(g.decode_vertex(m.stream, v)['pos'])
        me = bpy.data.meshes.new('%s_m%03d' % (g.name, m.idx))
        me.from_pydata(verts, [], [tuple(f) for f in faces])
        me.update()
        ob = bpy.data.objects.new(me.name, me)
        col.objects.link(ob)
        ob.parent = arm_obj
        mod = ob.modifiers.new('Armature', 'ARMATURE')
        mod.object = arm_obj
        if 0 <= m.material < len(mats):
            ob.data.materials.append(mats[m.material])

        # raw preservation + normals + uv + skin
        s = g.streams[m.stream]
        stride = s['stride']
        # UV-less, texture-less records are auxiliary draw geometry, not a
        # second visible surface. Keep objects available for lossless export.
        has_texture = (0 <= m.material < len(g.materials) and any(
            0 <= t < len(g.textures) for t in g.materials[m.material]['tex']))
        auxiliary = stride == 20 and not has_texture
        ob['gmd_auxiliary'] = auxiliary
        if auxiliary:
            ob.hide_set(True)
            ob.hide_render = True
        need_nb = stride >= 32
        ws = []
        bis = []
        nbs = []
        w1s = []
        w2s = []
        normals = []
        for v in order:
            base = g.stream_base(m.stream) + v * stride
            ws.append(_signed(struct.unpack_from('>I', data, base + 12)[0]))
            bis.append(_signed(struct.unpack_from('>I', data, base + 16)[0]))
            if need_nb:
                nbs.append(data[base + 23])
                w1s.append(_signed(struct.unpack_from('>I', data, base + 24)[0]))
            if stride in (36, 40):
                w2s.append(_signed(struct.unpack_from('>I', data, base + 28)[0]))
            n = g.decode_vertex(m.stream, v)['nrm']
            normals.append(n if n else (0.0, 0.0, 1.0))
        # NOTE: re-fetch each attribute right before writing - bpy silently
        # invalidates .data handles of attributes created earlier when new
        # ones are added, and writing through a stale handle segfaults.
        me.attributes.new('gmd_w', 'INT', 'POINT')
        me.attributes['gmd_w'].data.foreach_set('value', ws)
        me.attributes.new('gmd_bi', 'INT', 'POINT')
        me.attributes['gmd_bi'].data.foreach_set('value', bis)
        if need_nb:
            me.attributes.new('gmd_nb', 'INT', 'POINT')
            me.attributes['gmd_nb'].data.foreach_set('value', nbs)
            me.attributes.new('gmd_w1', 'INT', 'POINT')
            me.attributes['gmd_w1'].data.foreach_set('value', w1s)
        if stride in (36, 40):
            me.attributes.new('gmd_w2', 'INT', 'POINT')
            me.attributes['gmd_w2'].data.foreach_set('value', w2s)
        _set_custom_normals(me, normals)
        # uv layers
        if stride >= 24:
            me.uv_layers.new(name='UVMap')
            # No speculative empty UV2: it can become the active UV set.
            # Fill via a freshly fetched layer, not a handle invalidated by
            # allocating another CustomData layer.
            uv_values = []
            for loop in me.loops:
                dv = g.decode_vertex(m.stream, order[loop.vertex_index])
                uv_values.extend((dv['uv'][0], 1.0 - dv['uv'][1]))
            me.uv_layers['UVMap'].data.foreach_set('uv', uv_values)
            me.uv_layers.active_index = 0
            me.uv_layers['UVMap'].active_render = True
        # skin weights (groups for deformation; raw attrs are the export source)
        pal = g.palette_bones(m)
        for i, v in enumerate(order):
            dv = g.decode_vertex(m.stream, v)
            w = dv['w']
            bi = dv['bi']
            per = {}
            for j in range(4):
                if w[j] == 0:
                    continue
                bone_id = pal[bi[j]] if bi[j] < len(pal) else 0
                if bone_id >= len(g.bones):
                    continue
                bn = g.bones[bone_id].name
                per[bn] = per.get(bn, 0) + w[j] / 255.0
            for bn, wgt in per.items():
                vg = ob.vertex_groups.get(bn) or ob.vertex_groups.new(name=bn)
                vg.add([i], wgt, 'REPLACE')
        # record metadata
        ob['gmd_source'] = filepath
        ob['gmd_mesh'] = m.idx
        ob['gmd_stream'] = m.stream
        ob['gmd_pal_base'] = m.pal_base
        ob['gmd_pal_size'] = m.pal_size
        ob['gmd_mat'] = m.material

    arm_obj['gmd_mesh_count'] = len(g.meshes)

    # ---------------- optional MOMO ----------------
    if import_momo:
        n = load_momo_actions(arm_obj, import_momo, rig_mma, bake_euler)
        return n
    return 0


def _set_custom_normals(me, normals):
    """Blender >= 4.1: corner-domain 'custom_normal' attribute."""
    try:
        me.attributes.new('custom_normal', 'FLOAT_VECTOR', 'CORNER')
        flat = []
        for poly in me.polygons:
            for li in poly.loop_indices:
                flat.extend(normals[me.loops[li].vertex_index])
        me.attributes['custom_normal'].data.foreach_set('vector', flat)
        me.update()
    except Exception:
        pass  # older Blender: fall back to plain smooth shading


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
class GmdExportError(Exception):
    pass


def save_gmd(context, filepath, source=None):
    """Re-patch the source GMD with current armature pose + mesh geometry."""
    if source is None:
        source = context.object.get('gmd_source') if context.object else None
    if not source or not os.path.exists(source):
        raise GmdExportError('source .gmd not found (%r)' % source)
    data = bytearray(open(source, 'rb').read())
    g = gmd_mod.GMD(bytes(data), source)

    arm_obj = None
    for ob in context.scene.objects:
        if ob.type == 'ARMATURE' and ob.get('gmd_source') == source:
            arm_obj = ob
            break
    if arm_obj is None:
        raise GmdExportError('no imported armature found for %s' % source)
    arm = arm_obj.data
    pose = arm_obj.pose

    # ---- bones ----
    by_name = {b.name: b for b in g.bones}
    helper_by_bone = {h['bone']: h for h in g.helper}
    for b in g.bones:
        if b.name not in arm.bones:
            raise GmdExportError('bone %s missing from armature' % b.name)
        eb = arm.bones[b.name]
        pb = pose.bones.get(b.name)
        want = bone_matrix(b)
        got = eb.matrix_local
        diff = got - want
        if abs(max((diff[i][j] for i in range(4) for j in range(4)), key=abs)) > 1e-3:
            raise GmdExportError(
                'rest pose of bone %s was edited - rest edits are not '
                'supported (pose in Pose Mode only)' % b.name)
        mat = pb.matrix if pb else want
        # leave untouched bones byte-identical (axis-angle re-encoding is not
        # bit-exact for tiny/degenerate rotations; 1e-4 is far above the
        # ~1e-6 float32 rest round-trip noise and far below a real edit)
        d = mat - want
        if abs(max((d[i][j] for i in range(4) for j in range(4)), key=abs)) < 1e-4:
            continue
        R = mat.to_3x3()
        t = mat.translation
        if b.id in helper_by_bone:
            h = helper_by_bone[b.id]
            i = h['id']
            off = g.dir[1][0] + i * 0x40
            struct.pack_into('>3f', data, off + 16, t.x, t.y, t.z)
            qx, qy, qz, qw = matrix_to_quat(R)
            struct.pack_into('>4f', data, off + 48, qx, qy, qz, qw)
        else:
            ax, ay, az, ang = matrix_to_axis_angle(R)
            struct.pack_into('>3f', data, b.off + 0x50, t.x, t.y, t.z)
            struct.pack_into('>4f', data, b.off + 0x60, ax, ay, az, ang)

    # ---- meshes ----
    mesh_obs = [ob for ob in context.scene.objects
                if ob.get('gmd_source') == source and ob.type == 'MESH']
    mesh_obs.sort(key=lambda ob: ob.get('gmd_mesh'))
    if len(mesh_obs) != len(g.meshes):
        raise GmdExportError('expected %d mesh objects, found %d (topology '
                             'changes are not supported)' % (len(g.meshes), len(mesh_obs)))
    for ob, m in zip(mesh_obs, g.meshes):
        me = ob.data
        vmap, original_faces = g.mesh_geometry(m)
        if [tuple(p.vertices) for p in me.polygons] != original_faces:
            raise GmdExportError('mesh %s: face topology changed; topology edits '
                                 'are not supported' % ob.name)
        seen = {}
        order = []
        for v in vmap:
            if v not in seen:
                seen[v] = len(order)
                order.append(v)
        if len(me.vertices) != len(order):
            raise GmdExportError('mesh %s: vertex count changed (%d vs %d); '
                                 'topology edits are not supported'
                                 % (ob.name, len(me.vertices), len(order)))
        stride = g.streams[m.stream]['stride']
        # normals: prefer stored per-vertex normals from custom_normal
        vn = None
        an = me.attributes.get('custom_normal')
        if an is not None:
            vn = [None] * len(order)
            for poly in me.polygons:
                for li in poly.loop_indices:
                    vi = me.loops[li].vertex_index
                    if vn[vi] is None:
                        vn[vi] = tuple(an.data[li].vector)
            for i in range(len(vn)):
                if vn[i] is None:
                    vn[i] = (0.0, 0.0, 1.0)
        raw_w = me.attributes.get('gmd_w')
        raw_bi = me.attributes.get('gmd_bi')
        raw_nb = me.attributes.get('gmd_nb')
        raw_w1 = me.attributes.get('gmd_w1')
        raw_w2 = me.attributes.get('gmd_w2')
        uv_l = me.uv_layers.get('UVMap')
        uv2_l = me.uv_layers.get('UV2')
        luv = [None] * len(me.vertices)
        luv2 = [None] * len(me.vertices)
        if uv_l:
            for poly in me.polygons:
                for li in poly.loop_indices:
                    vi = me.loops[li].vertex_index
                    if luv[vi] is None:
                        luv[vi] = tuple(uv_l.data[li].uv)
            if uv2_l:
                for poly in me.polygons:
                    for li in poly.loop_indices:
                        vi = me.loops[li].vertex_index
                        if luv2[vi] is None:
                            luv2[vi] = tuple(uv2_l.data[li].uv)
        for i, v in enumerate(order):
            vtx = me.vertices[i]
            pos = (vtx.co.x, vtx.co.y, vtx.co.z)
            if raw_w is not None and raw_bi is not None:
                w = struct.unpack('>I', struct.pack('>i', raw_w.data[i].value))[0]
                biw = struct.unpack('>I', struct.pack('>i', raw_bi.data[i].value))[0]
                w = w.to_bytes(4, 'big')
                bi = biw.to_bytes(4, 'big')
            else:
                w, bi = _weights_from_groups(ob, i, g, m)
            nrm = vn[i] if vn else None
            uv = (luv[i][0], 1.0 - luv[i][1]) if luv[i] else None
            uv2 = (luv2[i][0], 1.0 - luv2[i][1]) if luv2[i] else None
            g.data = data
            g.encode_vertex(m.stream, v, pos, w, bi, nrm, uv, uv2)
            base = g.stream_base(m.stream) + v * stride
            if stride >= 32:
                if raw_nb is not None:
                    data[base + 23] = raw_nb.data[i].value & 0xFF
                if raw_w1 is not None:
                    struct.pack_into('>i', data, base + 24, raw_w1.data[i].value)
                else:
                    data[base + 23] = 0
            if stride in (36, 40) and raw_w2 is not None:
                struct.pack_into('>i', data, base + 28, raw_w2.data[i].value)

    with open(filepath, 'wb') as f:
        f.write(data)
    return len(data)


def _weights_from_groups(ob, vi, g, m):
    """Rebuild the 4 raw weight/index bytes from vertex groups."""
    bone_by_name = {b.name: b.id for b in g.bones}
    entries = []
    for ge in ob.data.vertices[vi].groups:
        if ge.weight <= 0:
            continue
        vg = ob.vertex_groups[ge.group]
        bone_id = bone_by_name.get(vg.name)
        if bone_id is None:
            continue
        entries.append((bone_id, int(round(ge.weight * 255))))
    entries.sort(key=lambda e: -e[1])
    entries = entries[:4]
    total = sum(e[1] for e in entries)
    if entries and total != 255:
        entries[0] = (entries[0][0], entries[0][1] + (255 - total))
    wb = bytearray(4)
    bb = bytearray(4)
    for j, (bone_id, w) in enumerate(entries):
        slot = None
        for k in range(m.pal_size):
            if g.palette_bones(m)[k] == bone_id:
                slot = k
                break
        if slot is None:
            raise GmdExportError('bone %s not in palette of mesh record %d'
                                 % (g.bones[bone_id].name, m.idx))
        wb[j] = max(0, min(255, w))
        bb[j] = slot
    return bytes(wb), bytes(bb)


# ---------------------------------------------------------------------------
# MOMO
# ---------------------------------------------------------------------------
def _input_file_path(value, extension, label, default=None):
    """Resolve a file field, never treating a directory as a selected file.

    Do not replace backslashes: they are real Windows separators (and two
    leading separators can be a UNC share). Python repr escaping is not a
    duplicated separator in the actual filename.
    """
    text = (value or '').strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        text = text[1:-1].strip()
    if not text:
        text = default or ''
    if not text:
        raise GmdExportError('%s: select a %s file, not just its folder' % (label, extension))
    if text.endswith(('\\', '/')):
        raise GmdExportError('%s: a folder was supplied; select the full %s file: %s'
                             % (label, extension, text))
    path = os.path.normpath(bpy.path.abspath(os.path.expanduser(text)))
    if os.path.isdir(path):
        raise GmdExportError('%s: a folder was supplied; select the full %s file: %s'
                             % (label, extension, path))
    if os.path.splitext(path)[1].lower() != extension:
        raise GmdExportError('%s: expected a %s file, got: %s' % (label, extension, path))
    if not os.path.isfile(path):
        raise GmdExportError('%s file not found: %s' % (label, path))
    return path


def _default_rig_path(body=False, motion=False):
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, 'rigs', 'p_man.mma' if motion else 'p_gen.mma' if body else 'f_man.mma')


def repair_gmd_skin_groups(context, arm):
    """Explicit migration of old imports, restoring ORIGINAL source weights.

    Validate all records/topology before modifying groups. UVs, materials,
    mesh coordinates, object transforms and Actions are not touched.
    """
    source = bpy.path.abspath(arm.get('gmd_source', ''))
    if not source or not os.path.isfile(source):
        raise GmdExportError('original GMD not found: %s' % source)
    g = gmd_mod.GMD(open(source, 'rb').read())
    objects = [ob for ob in context.scene.objects
               if ob.type == 'MESH' and ob.parent == arm and 'gmd_mesh' in ob]
    if (len(objects) != len(g.meshes) or
            {ob['gmd_mesh'] for ob in objects} != set(range(len(g.meshes)))):
        raise GmdExportError('imported mesh records are missing or duplicated')
    prepared = []
    for ob in objects:
        m = g.meshes[ob['gmd_mesh']]
        vm, faces = g.mesh_geometry(m)
        order = list(dict.fromkeys(vm))
        if (len(ob.data.vertices) != len(order) or
                [tuple(p.vertices) for p in ob.data.polygons] != faces):
            raise GmdExportError('mesh topology differs from source: %s' % ob.name)
        pal = g.palette_bones(m)
        groups = {}
        for vi, v in enumerate(order):
            dv = g.decode_vertex(m.stream, v)
            weights = {}
            for w, slot in zip(dv['w'], dv['bi']):
                if w and slot < len(pal):
                    name = g.bones[pal[slot]].name
                    weights[name] = weights.get(name, 0) + w
            for name, weight in weights.items():
                groups.setdefault(name, {}).setdefault(weight, []).append(vi)
        prepared.append((ob, groups))
    bone_names = {b.name for b in g.bones}
    for ob, groups in prepared:
        for vg in list(ob.vertex_groups):
            if vg.name in bone_names:
                ob.vertex_groups.remove(vg)
        for name in groups:
            if ob.vertex_groups.get(name) is None:
                ob.vertex_groups.new(name=name)
        for name, buckets in groups.items():
            for weight, indices in buckets.items():
                ob.vertex_groups[name].add(indices, weight / 255.0, 'REPLACE')
    arm['gmd_skinning_version'] = 2
    context.view_layer.update()
    return len(objects)


class BDOMAIN_OT_repair_skin(bpy.types.Operator):
    """Restore original GMD bone weights with the corrected counted palette; replaces bone-group edits"""
    bl_idname = 'bdomain.repair_skin'
    bl_label = 'Repair old skin groups'
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        self.layout.label(text='Restore original bone weights from the GMD?')
        self.layout.label(text='Any bone-weight edits will be replaced.', icon='ERROR')
        self.layout.label(text='UVs, materials and mesh coordinates are kept.')

    def execute(self, context):
        ob = context.object
        if ob is None or ob.type != 'ARMATURE' or ob.mode != 'OBJECT':
            self.report({'ERROR'}, 'select an imported armature in Object Mode')
            return {'CANCELLED'}
        try:
            n = repair_gmd_skin_groups(context, ob)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Restored original skin groups on %d meshes; now reload MOMO' % n)
        return {'FINISHED'}


def _expression_bases(arm_obj, clip, nodes, parents, scale):
    """Retarget the hierarchical position/quaternion channels to GMD bones.

    Keep the model's head anchor/rest orientations; source positions are in
    face-space game axes, while Blender matrix_basis is parent-relative.
    """
    values = mmo_mod.pose_nodes(clip)
    if len(values) != len(nodes):
        raise GmdExportError('MOMO/rig node count mismatch')
    root = arm_obj.data.bones.get(nodes[0])
    if root is None:
        raise GmdExportError('face root %s is missing from the armature' % nodes[0])
    if len(parents) != len(nodes) or parents[0] != -1:
        raise GmdExportError('MOMA hierarchy is missing or unsupported')
    targets = {root.name: root.matrix_local.copy()}
    source_world = [mathutils.Matrix.Identity(4)]
    for ni, (name, (position, xyzw)) in enumerate(zip(nodes[1:], values[1:]), 1):
        q = mathutils.Quaternion((xyzw[3], *xyzw[:3]))
        if q.magnitude < 1e-10:
            raise GmdExportError('zero MOMO quaternion for %s' % name)
        q.normalize()
        local = q.to_matrix().to_4x4()
        local.translation = mathutils.Vector(position) * scale
        parent_index = parents[ni]
        if parent_index < 0 or parent_index >= ni:
            raise GmdExportError('unsupported MOMA parent index')
        world = source_world[parent_index] @ local
        source_world.append(world)
        bone = arm_obj.data.bones.get(name)
        if bone is None:
            continue
        target = (world.to_3x3() @ bone.matrix_local.to_3x3()).to_4x4()
        target.translation = root.matrix_local.translation + world.translation
        targets[name] = target
    bases = {}
    for name, target in targets.items():
        bone = arm_obj.data.bones[name]
        if bone.parent:
            bases[name] = bone.convert_local_to_pose(
                target, bone.matrix_local,
                parent_matrix=targets.get(bone.parent.name, bone.parent.matrix_local),
                parent_matrix_local=bone.parent.matrix_local, invert=True)
        else:
            bases[name] = bone.convert_local_to_pose(
                target, bone.matrix_local, invert=True)
    return bases


def _assign_momo_action(arm, action):
    if arm.animation_data is None:
        arm.animation_data_create()
    arm.animation_data.action = action
    # Explicitly assign a slot for Blender 4.4+ layered Actions.
    if hasattr(action, 'slots') and len(action.slots):
        arm.animation_data.action_slot = action.slots[0]


def _key_momo_basis(pb, basis, frame):
    pb.rotation_mode = 'QUATERNION'
    pb.matrix_basis = basis
    pb.keyframe_insert(data_path='location', frame=frame)
    pb.keyframe_insert(data_path='rotation_quaternion', frame=frame)
    pb.keyframe_insert(data_path='scale', frame=frame)


# All catalogs load one Action by default, including small libraries.


def _is_body_motion(rig):
    return (len(rig['nodes']) == 79 and rig['name'] == 'p_man' and
            rig['nodes'][:2] == ['moo_centroid', 'center_c_n'])


def _bake_body_motion(arm, clip, rig):
    """Sample the source timeline once; bulk-fill FCurves, not per-frame inserts."""
    from array import array
    curves, previous = {}, {}
    for source_frame in range(clip.frames + 1):
        bases = _body_bases(arm, clip, rig, frame=source_frame)
        frame = source_frame + 1
        for name, basis in bases.items():
            loc, q, scale = basis.decompose()
            if name in previous and q.dot(previous[name]) < 0:
                q.negate()
            previous[name] = q.copy()
            pb = arm.pose.bones[name]
            for prop, vals in (('location', loc), ('rotation_quaternion', q), ('scale', scale)):
                path = pb.path_from_id(prop)
                for i, value in enumerate(vals):
                    curves.setdefault((path, i), array('f')).extend((frame, value))
    return curves


def _fill_body_motion_curves(action, samples, preserve_extra=False):
    if hasattr(action, 'slots') and len(action.slots) and len(action.layers):
        curves = action.layers[0].strips[0].channelbag(action.slots[0]).fcurves
    else:
        curves = action.fcurves
    for curve in curves:
        key = (curve.data_path, curve.array_index)
        if preserve_extra and key not in samples:
            continue
        coords = samples[key]
        curve.keyframe_points.clear()
        curve.keyframe_points.add(len(coords)//2)
        curve.keyframe_points.foreach_set('co', coords)
        for point in curve.keyframe_points:
            point.interpolation = 'LINEAR'
        curve.update()


def _is_body_rig(rig):
    return _is_body_motion(rig) or (len(rig['nodes']) == 161 and rig['nodes'][:2] ==
            ['moo_centroid', 'center_c_n'])


def _body_source_worlds(clip, rig, frame, motion=True):
    """Evaluate model-space MOMA transforms and separate centroid motion."""
    values = mmo_mod.pose_nodes(clip, rig, frame=frame, allow_missing_tail=motion)
    worlds = []
    for i, value in enumerate(values):
        parent = rig['parents'][i]
        if value is None:
            worlds.append(None)
            continue
        pos, rot = value
        q = mathutils.Quaternion((rot[3], *rot[:3]))
        if q.magnitude < 1e-8:
            raise GmdExportError('invalid body quaternion')
        local = q.normalized().to_matrix().to_4x4()
        local.translation = pos
        if parent >= 0 and not rig['node_flags'][i] & 0x40:
            if worlds[parent] is None:
                raise GmdExportError('unsupported child of procedural body node')
            local = worlds[parent] @ local
        worlds.append(local)
    if motion:
        # moo_centroid is a separate motion root, not the NOD3 parent of
        # center_c_n. World-space flags refer to animation-model space;
        # locomotion/yaw must be applied after composing that skeleton.
        centroid = worlds[rig['nodes'].index('moo_centroid')]
        worlds = [w if i == 0 or w is None else centroid @ w
                  for i, w in enumerate(worlds)]
    return {n: w for n, w in zip(rig['nodes'], worlds) if w is not None}


def _body_bases(arm, clip, rig, frame=0):
    """Length-preserving anatomical retarget for p_gen poses / p_man motion.

    Use actual GMD local quaternions at record +0x30, NOT direction/length
    fields at +0x60 interpreted as axis-angle. Keep proportions/root anchor.
    Model-specific face/hair/support bones follow their target parents rigidly.
    Procedural MOMA nodes absent on the target are not invented/animated.
    """
    motion = _is_body_motion(rig)
    if motion:
        if not (math.isfinite(clip.speed) and clip.speed > 0 and clip.fps > 0):
            raise GmdExportError('Invalid body source FPS/speed')
    elif clip.name != 'p_gen_tpose' or clip.header != 644 or not _is_body_rig(rig):
        raise GmdExportError('Body retarget requires a matching p_gen T-pose or p_man rig')
    if any(t.kind != 'scalar' for t in clip.tracks[:3 if motion else 8]):
        raise GmdExportError('Invalid auxiliary body channel types')
    if not motion and any(abs(mmo_mod.sample_track(t, frame)) > 1e-8 for t in clip.tracks[:8]):
        raise GmdExportError('Nonzero p_gen auxiliary channels are not supported')
    source = _body_source_worlds(clip, rig, frame, motion)
    root_delta = mathutils.Vector((0, 0, 0))
    if motion:
        root_index = rig['nodes'].index('center_c_n')
        initial_values = mmo_mod.pose_nodes(clip, rig, frame=0, allow_missing_tail=True)
        initial = mathutils.Vector(initial_values[root_index][0])
        pos0, rot0 = initial_values[0]
        initial = mathutils.Quaternion((rot0[3], *rot0[:3])).normalized() @ initial
        initial += mathutils.Vector(pos0)
        current = source['center_c_n'].translation
        root_height = arm.data.bones.get('center_c_n')
        bind_height = rig['rest_positions'][root_index][1]
        if root_height is not None and bind_height > 1e-8:
            root_delta = mathutils.Vector((current.x-initial.x,
                                          current.y-bind_height, current.z-initial.z))
            root_delta *= root_height.head_local.y / bind_height

    required = ['center_c_n', 'ketu_c_n', 'kosi_c_n', 'mune_c_n', 'kubi_c_n', 'face_c_n']
    required += [f'{part}_{side}_n' for side in ('l', 'r')
                 for part in ('kata', 'ude1', 'ude2', 'ude3', 'asi1', 'asi2', 'asi3', 'asi4')]
    missing = [n for n in required if n not in arm.data.bones or n not in source]
    if missing:
        raise GmdExportError('Full body GMD skeleton required; missing: ' + ', '.join(missing))
    raw = arm.get('gmd_bind_local_quats')
    if raw:
        local_quats = json.loads(raw)
    else:
        # Migration for scenes imported before v0.3. The GMD is read only.
        path = _input_file_path(arm.get('gmd_source'), '.gmd', 'Original GMD bind reference')
        g = gmd_mod.GMD(open(path, 'rb').read())
        if set(b.name for b in g.bones) != set(b.name for b in arm.data.bones):
            raise GmdExportError('Original GMD skeleton differs from the current armature')
        local_quats = {b.name: list(struct.unpack_from('>4f', g.data, b.off + 0x30))
                       for b in g.bones}
    bind = {}

    def bind_rotation(bone):
        if bone.name in bind:
            return bind[bone.name]
        xyzw = local_quats.get(bone.name)
        if xyzw is None or not all(math.isfinite(v) for v in xyzw):
            raise GmdExportError('Missing/invalid GMD bind quaternion: ' + bone.name)
        q = mathutils.Quaternion((xyzw[3], *xyzw[:3]))
        if abs(q.magnitude - 1) > .01:
            raise GmdExportError('Non-unit GMD bind quaternion: ' + bone.name)
        rot = q.normalized().to_matrix()
        if bone.parent:
            rot = bind_rotation(bone.parent) @ rot
        bind[bone.name] = rot
        return rot

    rotations = {bone.name: source[bone.name].to_3x3() @ bind_rotation(bone).transposed()
                 for bone in arm.data.bones if bone.name in source}
    targets, deltas = {}, {}

    def target(bone):
        name = bone.name
        if name in targets:
            return targets[name]
        parent = bone.parent
        if parent:
            pm = target(parent)
            inherited = deltas[parent.name]
            pos = pm.translation + inherited @ (bone.head_local - parent.head_local)
        else:
            inherited = mathutils.Matrix.Identity(3)
            pos = bone.head_local.copy()
        if name == 'center_c_n':
            pos += root_delta
        # Unmapped model-specific bones follow their target parent rigidly.
        delta = rotations.get(name, inherited)
        mat = (delta @ bone.matrix_local.to_3x3()).to_4x4()
        mat.translation = pos
        targets[name], deltas[name] = mat, delta
        return mat

    bases = {}
    for bone in arm.data.bones:
        mat = target(bone)
        if bone.parent:
            bases[bone.name] = bone.convert_local_to_pose(
                mat, bone.matrix_local, parent_matrix=target(bone.parent),
                parent_matrix_local=bone.parent.matrix_local, invert=True)
        else:
            bases[bone.name] = bone.convert_local_to_pose(mat, bone.matrix_local, invert=True)
    if not raw:
        arm['gmd_bind_local_quats'] = json.dumps(local_quats)
    return bases


def _momo_bases(arm, clip, rig, scale):
    if _is_body_rig(rig):
        return _body_bases(arm, clip, rig)
    return _expression_bases(arm, clip, rig['nodes'], rig['parents'], scale)


def _momo_retarget_scale(arm, clip, rig):
    if _is_body_rig(rig):
        return 1.0  # Body mapping preserves model lengths, not source height.
    if (len(rig['nodes']) != 46 or rig['nodes'][0] != 'face_c_n' or
            clip.header != 184):
        raise GmdExportError('Face retarget requires the 46-node f_man.mma and 184 MOMO channels')
    root = arm.data.bones.get(rig['nodes'][0])
    if root is None:
        raise GmdExportError('face root is missing from the armature')
    height = mmo_mod.pose_nodes(clip)[0][0][1]
    scale = root.head_local.y / height if abs(height) > 1e-8 else 1.0
    if not math.isfinite(scale) or scale <= 0:
        raise GmdExportError('cannot determine MOMO retarget scale')
    return scale


def _create_momo_action(arm, clip, rig, path, bases=None):
    nodes = rig['nodes']
    scale = _momo_retarget_scale(arm, clip, rig)
    if bases is None:
        bases = _momo_bases(arm, clip, rig, scale)
    motion_samples = _bake_body_motion(arm, clip, rig) if _is_body_motion(rig) else None
    prefix = os.path.splitext(os.path.basename(path))[0]
    act = bpy.data.actions.new('%s__%s' % (prefix, clip.name))
    act.use_fake_user = True
    act['gmd_raw_momo'] = True
    act['gmd_expression'] = clip.name
    act['gmd_momo_nodes'] = json.dumps(nodes)
    act['gmd_momo_scale'] = scale
    act['gmd_momo_raw_words'] = json.dumps([list(t.words) for t in clip.tracks])
    act['gmd_source_frames'] = clip.frames
    act['gmd_source_header'] = clip.hed0_raw.hex()
    act['gmd_momo_kind'] = ('body_motion' if _is_body_motion(rig) else
                            'constant_body_pose' if _is_body_rig(rig) else 'constant_expression')
    if _is_body_motion(rig):
        act['gmd_source_fps'] = clip.fps
        act['gmd_source_speed'] = clip.speed
        act['gmd_source_channels'] = clip.header
        act['gmd_source_prg2'] = clip.prg2.hex()
        act['gmd_auxiliary_samples'] = json.dumps(
            [[mmo_mod.sample_track(t, f) for f in range(clip.frames+1)]
             for t in clip.tracks[:3]])
        act['gmd_timing'] = 'source_timeline'
        act['gmd_root_motion'] = 'horizontal_first_frame_vertical_bind_relative'
    act['gmd_momo_retarget'] = 'length_preserving_body' if _is_body_rig(rig) else 'face'
    _assign_momo_action(arm, act)
    for name, basis in bases.items():
        _key_momo_basis(arm.pose.bones[name], basis, 1)
    for ni, name in enumerate([] if _is_body_rig(rig) else nodes):
        pb = arm.pose.bones.get(name)
        if pb is None:
            continue
        for k in range(3):
            pb['gmd_c%d' % k] = clip.tracks[ni * 4 + k].value
            pb.keyframe_insert(data_path='["gmd_c%d"]' % k, frame=1)
    if motion_samples is not None:
        _fill_body_motion_curves(act, motion_samples)
    act.frame_range = (1, max(1, clip.frames + (1 if motion_samples is not None else 0)))
    return act


def load_momo_actions(arm_obj, mmo_path, mma_path=None, bake_euler=False, eager=False):
    if arm_obj.get('gmd_skinning_version') != 2:
        raise GmdExportError('Re-import GMD or use Repair old skin groups first')
    path = _input_file_path(mmo_path, '.mmo', 'MOMO animation')
    data = open(path, 'rb').read()
    # Read clip metadata only. Large libraries must not retain every channel
    # object or create thousands of Actions just to show a searchable list.
    clips = mmo_mod.parse_momo(data, decode_tracks=False)
    if not clips:
        raise GmdExportError('MOMO contains no embedded clips (this may be a reference-only library)')
    total_clips = len(clips)
    declared_rig = mmo_mod.momo_rig_name(data)
    motion = declared_rig == 'p_man' or (declared_rig is None and
                                        all(c.header in (54, 55, 85) for c in clips))
    if motion and any(c.header not in (54, 55, 85) for c in clips):
        raise GmdExportError('Unsupported p_man channel layout; expected 54, 55 or 85 channels')
    body = motion or all(c.header == 644 and c.name == 'p_gen_tpose' for c in clips)
    if not body and any(c.header != 184 for c in clips):
        raise GmdExportError('Unsupported MOMA rig %r; supported: f_man faces, p_gen T-pose, p_man body motion' % declared_rig)
    rig_path = _input_file_path(mma_path, '.mma', 'MOMA rig', _default_rig_path(body, motion))
    rig = mmo_mod.parse_moma(open(rig_path, 'rb').read())
    if body != _is_body_rig(rig) or motion != _is_body_motion(rig):
        raise GmdExportError('MOMO/MOMA rig mismatch: use the matching f_man, p_gen or p_man rig')
    lazy = not eager
    selected = clips[:1] if lazy else clips
    prepared = []
    for clip in selected:
        mmo_mod.decode_clip(data, clip)
        scale = _momo_retarget_scale(arm_obj, clip, rig)
        prepared.append((clip, _momo_bases(arm_obj, clip, rig, scale)))
    if bpy.context.screen and bpy.context.screen.is_animation_playing:
        bpy.ops.screen.animation_cancel(restore_frame=False)
    if arm_obj.animation_data and arm_obj.animation_data.action:
        arm_obj.animation_data.action.use_fake_user = True
    action_map = {}
    first_action = None
    for clip, bases in prepared:
        act = _create_momo_action(arm_obj, clip, rig, path, bases)
        action_map[clip.name] = act.name
        if first_action is None:
            first_action = act
    stat = os.stat(path)
    arm_obj['gmd_momo_file_version'] = json.dumps([stat.st_size, stat.st_mtime_ns])
    arm_obj['gmd_momo'] = path
    arm_obj['gmd_momo_mode'] = ('decoded_body_motion' if motion else
                                'decoded_constant_body_poses' if body else 'decoded_constant_expressions')
    arm_obj['gmd_momo_total_clips'] = total_clips
    arm_obj['gmd_momo_lazy'] = lazy
    arm_obj['gmd_momo_rig'] = json.dumps(rig)
    arm_obj['gmd_momo_scale'] = first_action['gmd_momo_scale']
    arm_obj['gmd_clips'] = json.dumps([c.name for c in clips])
    arm_obj['gmd_clip_actions'] = json.dumps(action_map)
    arm_obj['gmd_momo_clip_count'] = len(clips)
    arm_obj['gmd_momo_loaded_count'] = len(action_map)
    if hasattr(arm_obj, 'bdomain_momo_clips'):
        _refresh_clip_list(arm_obj)
    _momo_enum_cache.clear()
    _assign_momo_action(arm_obj, first_action)
    bpy.context.scene.frame_start = 1
    bpy.context.scene.frame_end = (int(first_action.frame_range[1]) if motion else
                                   max(1, max(c.frames for c in clips)))
    if motion:
        bpy.context.scene.render.fps = clips[0].fps
        bpy.context.scene.render.fps_base = 1 / first_action.get('gmd_source_speed', 1)
    bpy.context.scene.frame_set(1)
    bpy.context.view_layer.update()
    return len(clips)


def _load_catalog_expression(arm, name):
    path = bpy.path.abspath(arm.get('gmd_momo', ''))
    if not os.path.isfile(path):
        raise GmdExportError('Source MOMO is required for an unloaded clip: ' + path)
    stat = os.stat(path)
    expected = json.loads(arm.get('gmd_momo_file_version', '[]'))
    if expected != [stat.st_size, stat.st_mtime_ns]:
        raise GmdExportError('Source MOMO changed or moved; reload its catalog first')
    data = open(path, 'rb').read()
    clips = mmo_mod.parse_momo(data, decode_tracks=False)
    clip = next((c for c in clips if c.name == name), None)
    if clip is None:
        raise GmdExportError('Clip not found in source MOMO; reload the catalog')
    rig = json.loads(arm['gmd_momo_rig'])
    mmo_mod.decode_clip(data, clip)
    # Validate/decode before changing the current Action.
    scale = _momo_retarget_scale(arm, clip, rig)
    bases = _momo_bases(arm, clip, rig, scale)
    previous = arm.animation_data.action if arm.animation_data else None
    try:
        act = _create_momo_action(arm, clip, rig, path, bases)
    except Exception:
        if previous is not None:
            _assign_momo_action(arm, previous)
        raise
    mapping = json.loads(arm['gmd_clip_actions'])
    mapping[name] = act.name
    arm['gmd_clip_actions'] = json.dumps(mapping)
    arm['gmd_momo_loaded_count'] = len(mapping)
    return act


def preview_momo_expression(context, arm):
    """Create an explicitly synthetic rest -> expression -> rest blend."""
    source = arm.animation_data.action if arm.animation_data else None
    if source is None or source.get('gmd_momo_kind') != 'constant_expression':
        raise GmdExportError('select a decoded MOMO expression first')
    nodes = json.loads(source['gmd_momo_nodes'])
    _assign_momo_action(arm, source)
    context.scene.frame_set(1)
    context.view_layer.update()
    target = {name: arm.pose.bones[name].matrix_basis.copy()
              for name in nodes if name in arm.pose.bones}
    act = bpy.data.actions.new(source.name + '__preview_blend')
    act.use_fake_user = True
    act['gmd_momo_kind'] = 'generated_preview'
    act['gmd_source_expression'] = source.name
    _assign_momo_action(arm, act)
    for frame, strength in ((1, 0.0), (16, 1.0), (31, 0.0)):
        for name, basis in target.items():
            _key_momo_basis(arm.pose.bones[name],
                            basis if strength else mathutils.Matrix.Identity(4), frame)
    act.frame_range = (1, 31)
    context.scene.frame_start, context.scene.frame_end = 1, 31
    context.scene.frame_set(1)
    context.view_layer.update()
    return act


class BDOMAIN_OT_preview_momo(bpy.types.Operator):
    """Generate a preview blend; timing is not from the source MOMO"""
    bl_idname = 'bdomain.preview_momo'
    bl_label = 'Preview expression blend'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = context.object
        if ob is None or ob.type != 'ARMATURE':
            self.report({'ERROR'}, 'select an imported GMD armature')
            return {'CANCELLED'}
        try:
            preview_momo_expression(context, ob)
        except GmdExportError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Generated preview on frames 1-31; press Play')
        return {'FINISHED'}


class BDOMAIN_OT_load_momo(bpy.types.Operator, ImportHelper):
    """Import face poses or native-timeline human body Actions"""
    bl_idname = 'bdomain.load_momo'
    bl_label = 'Load MOMO clips'
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = '.mmo'
    check_extension = False  # Never turn a directory into 'directory.mmo'.
    filepath: StringProperty(name='MOMO file (.mmo)', subtype='FILE_PATH')
    rig_mma: StringProperty(name='Optional MOMA file (.mma)', subtype='NONE',
                            description='Optional .mma; blank selects bundled f_man, p_gen or p_man rig automatically')
    eager: BoolProperty(name='Load all Actions (slow)', default=False,
                        description='Create Actions for every clip instead of loading on selection')
    bake_euler: BoolProperty(default=False, options={'HIDDEN'})
    filter_glob: StringProperty(default='*.mmo', options={'HIDDEN'})

    def execute(self, context):
        ob = context.object
        if ob is None or ob.type != 'ARMATURE' or not ob.get('gmd_source'):
            self.report({'ERROR'}, 'select an imported GMD armature first')
            return {'CANCELLED'}
        try:
            n = load_momo_actions(ob, self.filepath, self.rig_mma or None,
                                  self.bake_euler, eager=self.eager)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        count = len(json.loads(ob['gmd_clip_actions']))
        self.report({'INFO'}, '%d clips available; %d Actions created. Select a clip to load it.' % (n, count))
        return {'FINISHED'}

    def draw(self, context):
        self.layout.label(text='Select an .mmo FILE, not just a directory')
        self.layout.prop(self, 'rig_mma')
        self.layout.label(text='Leave MOMA blank for automatic face/body rig')
        self.layout.prop(self, 'eager')


_momo_enum_cache = {}


def _momo_clip_items(self, context):
    global _momo_enum_cache
    ob = context.object
    if ob is None or ob.type != 'ARMATURE':
        return []
    names = tuple(json.loads(ob.get('gmd_clips', '[]')))
    if names not in _momo_enum_cache:
        _momo_enum_cache[names] = [(n, n, 'Apply decoded MOMO pose ' + n) for n in names]
    return _momo_enum_cache[names]


class BDOMAIN_OT_apply_clip(bpy.types.Operator):
    """Apply a decoded static expression pose to the armature"""
    bl_idname = 'bdomain.apply_clip'
    bl_label = 'Search MOMO clips'
    bl_options = {'REGISTER', 'UNDO'}
    bl_property = 'name'

    name: EnumProperty(items=_momo_clip_items)

    def execute(self, context):
        ob = context.object
        if ob is None or ob.type != 'ARMATURE':
            self.report({'ERROR'}, 'select an imported GMD armature')
            return {'CANCELLED'}
        mapping = json.loads(ob.get('gmd_clip_actions', '{}'))
        action_name = mapping.get(self.name)
        act = bpy.data.actions.get(action_name) if action_name else None
        if act is None and ob.get('gmd_momo_lazy'):
            try:
                act = _load_catalog_expression(ob, self.name)
            except Exception as e:
                self.report({'ERROR'}, str(e))
                return {'CANCELLED'}
        if act is None:
            self.report({'ERROR'}, 'expression Action not found; reload MOMO')
            return {'CANCELLED'}
        index = ob.bdomain_momo_clips.find(self.name)
        if index >= 0:
            ob.bdomain_momo_clip_index = index
        _assign_momo_action(ob, act)
        if act.get('gmd_momo_kind') == 'body_motion':
            context.scene.render.fps = act['gmd_source_fps']
            context.scene.render.fps_base = 1 / act.get('gmd_source_speed', 1)
        context.scene.frame_start = 1
        context.scene.frame_end = max(1, int(act.frame_range[1]))
        context.scene.frame_set(1)
        context.view_layer.update()
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {'FINISHED'}


class BDOMAIN_PG_clip(bpy.types.PropertyGroup):
    pass  # Uses the built-in name field; stored on the armature in .blend.


class BDOMAIN_UL_clips(bpy.types.UIList):
    def filter_items(self, context, data, propname):
        needle = data.bdomain_momo_filter.casefold().strip()
        if not needle:
            return [], []
        flags = [self.bitflag_filter_item if needle in item.name.casefold() else 0
                 for item in getattr(data, propname)]
        return flags, []

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        layout.label(text=item.name, icon='ACTION')


def _refresh_clip_list(arm):
    arm.bdomain_momo_clips.clear()
    for name in json.loads(arm.get('gmd_clips', '[]')):
        arm.bdomain_momo_clips.add().name = name
    arm.bdomain_momo_clip_index = 0
    arm.bdomain_momo_filter = ''


class BDOMAIN_OT_refresh_clip_list(bpy.types.Operator):
    bl_idname = 'bdomain.refresh_clip_list'
    bl_label = 'Show clip list (existing scene)'

    def execute(self, context):
        _refresh_clip_list(context.object)
        return {'FINISHED'}


class BDOMAIN_OT_apply_selected(bpy.types.Operator):
    bl_idname = 'bdomain.apply_selected'
    bl_label = 'Apply selected clip'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        arm = context.object
        index = arm.bdomain_momo_clip_index
        if not 0 <= index < len(arm.bdomain_momo_clips):
            self.report({'ERROR'}, 'Select a clip in the list first')
            return {'CANCELLED'}
        return bpy.ops.bdomain.apply_clip(name=arm.bdomain_momo_clips[index].name)


def _cleanup_unused_momo_actions():
    """Never unlink an Action from an object, NLA strip or other real user."""
    removed = 0
    for action in list(bpy.data.actions):
        if not (action.get('gmd_raw_momo') or action.get('gmd_momo_kind') in
                ('constant_expression', 'constant_body_pose', 'body_motion', 'generated_preview')):
            continue
        if action.users > int(action.use_fake_user):
            continue
        action.use_fake_user = False
        bpy.data.actions.remove(action, do_unlink=False)
        removed += 1
    for arm in bpy.data.objects:
        if arm.type != 'ARMATURE' or not arm.get('gmd_clip_actions'):
            continue
        mapping = json.loads(arm['gmd_clip_actions'])
        mapping = {n: a for n, a in mapping.items() if bpy.data.actions.get(a)}
        arm['gmd_clip_actions'] = json.dumps(mapping)
        arm['gmd_momo_loaded_count'] = len(mapping)
        if arm.get('gmd_momo_rig'):
            arm['gmd_momo_lazy'] = True
    return removed


class BDOMAIN_OT_cleanup_momo(bpy.types.Operator):
    """Delete unused imported MOMO Actions, including Fake User copies.

    Edited MOMO Actions with no real users are also removed. Save a backup.
    Active Actions and Actions used in NLA or by other objects are kept.
    """
    bl_idname = 'bdomain.cleanup_momo'
    bl_label = 'Clean unused MOMO Actions'
    bl_options = {'REGISTER', 'UNDO'}

    def draw(self, context):
        self.layout.label(text='Save a backup first. Deletes unused MOMO Actions,')
        self.layout.label(text='including edited/Fake User copies from old imports.')
        self.layout.label(text='Actions assigned to objects or NLA are kept.')
        self.layout.label(text='Unloaded clips need the original MMO to load again.')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=520)

    def execute(self, context):
        count = _cleanup_unused_momo_actions()
        self.report({'INFO'}, 'Removed %d unused MOMO Actions; used Actions kept' % count)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# operators / UI
# ---------------------------------------------------------------------------
class BDOMAIN_OT_import_gmd(bpy.types.Operator, ImportHelper):
    """Import a Binary Domain GSGM model"""
    bl_idname = 'import_scene.gmd'
    bl_label = 'Import GMD'
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = '.gmd'
    filter_glob: StringProperty(default='*.gmd', options={'HIDDEN'})
    mmo_path: StringProperty(
        name='MOMO path (paste)', subtype='NONE',
        description='Paste an optional .mmo path, or import the model first and use N-panel > Binary Domain > Load MOMO clips')
    rig_mma: StringProperty(
        name='MOMA rig path (paste)', subtype='NONE',
        description='Optional rig file; blank selects bundled face or p_gen rig automatically')
    bake_euler: BoolProperty(default=False, options={'HIDDEN'})

    def draw(self, context):
        layout = self.layout
        layout.label(text='Optional paths: paste full file paths')
        layout.prop(self, 'mmo_path')
        layout.prop(self, 'rig_mma')
        layout.separator()
        layout.label(text='Or load MOMO after model import:')
        layout.label(text='N-panel > Binary Domain > Load MOMO clips')

    def execute(self, context):
        try:
            n = load_gmd(context, self.filepath, self.mmo_path or None,
                         self.rig_mma or None, self.bake_euler)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        msg = 'Imported %s' % os.path.basename(self.filepath)
        if self.mmo_path:
            msg += ' with %d clips' % n
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class BDOMAIN_OT_export_gmd(bpy.types.Operator, ExportHelper):
    """Export pose / geometry back to a GSGM file (patch-based)"""
    bl_idname = 'export_scene.gmd'
    bl_label = 'Export GMD'
    bl_options = {'REGISTER'}

    filename_ext = '.gmd'
    filter_glob: StringProperty(default='*.gmd', options={'HIDDEN'})

    def execute(self, context):
        try:
            n = save_gmd(context, self.filepath)
        except GmdExportError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Exported %d bytes' % n)
        return {'FINISHED'}


def bake_body_corrections(arm, source_ik=True, hands=True, feet=True,
                          foot_lock=True, helpers=False, ground=0.0,
                          height_tolerance=.04, speed_limit=.35, blend_frames=2,
                          left_contacts='', right_contacts=''):
    """Bake an opt-in adaptation into a new Action; never modify source keys.

    Source-anchor IK uses ANC0 endpoints; contact detection and helper blending
    are explicitly heuristic, not game OPE0/terrain/controller emulation.
    """
    from array import array
    correction = correction_mod
    action = arm.animation_data.action if arm.animation_data else None
    if action is None or action.get('gmd_momo_kind') != 'body_motion':
        raise GmdExportError('Select an uncorrected p_man body Action first')
    if action.get('gmd_body_corrections'):
        raise GmdExportError('Apply the original clip first; do not stack correction bakes')
    if any(c for pb in arm.pose.bones for c in pb.constraints if not c.mute):
        raise GmdExportError('Mute existing pose constraints before baking body corrections')
    if any(not d.mute for d in arm.animation_data.drivers) or any(not t.mute for t in arm.animation_data.nla_tracks):
        raise GmdExportError('Mute NLA tracks and remove/mute drivers before correction baking')
    if not (source_ik or foot_lock or helpers):
        raise GmdExportError('Enable at least one correction')
    count = int(action['gmd_source_frames'])+1
    contacts = {side: correction.parse_contacts(text, count)
                for side, text in (('l', left_contacts), ('r', right_contacts))}
    rig = json.loads(arm.get('gmd_momo_rig', '{}'))
    if not _is_body_motion(rig):
        raise GmdExportError('The matching p_man MOMA is required')
    if 'anchors' not in rig or 'operations' not in rig:
        bundled = mmo_mod.parse_moma(open(_default_rig_path(True, True), 'rb').read())
        rig.setdefault('anchors', bundled['anchors'])
        rig.setdefault('operations', bundled['operations'])
    clip = None
    if source_ik:
        path = _input_file_path(arm.get('gmd_momo'), '.mmo', 'Source MOMO for anchor IK')
        data = open(path, 'rb').read()
        clip = next((c for c in mmo_mod.parse_momo(data, False)
                     if c.name == action.get('gmd_expression')), None)
        if clip is None or clip.frames+1 != count or clip.hed0_raw.hex() != action.get('gmd_source_header'):
            raise GmdExportError('Source MOMO no longer matches the selected Action')
        mmo_mod.decode_clip(data, clip)
        raw_words = json.loads(action.get('gmd_momo_raw_words', 'null'))
        if raw_words != [list(t.words) for t in clip.tracks]:
            raise GmdExportError('Source channel data changed; reload the original clip before source IK')
    chains = []
    for end, root in rig['anchors']:
        middle = rig['parents'][end]
        if middle < 0 or rig['parents'][middle] != root:
            continue
        names = [rig['nodes'][i] for i in (root, middle, end)]
        selected = (feet and names[-1].startswith('asi3_')) or (hands and names[-1].startswith('ude3_'))
        if selected and all(n in arm.pose.bones for n in names):
            chains.append(names)
    if source_ik and not chains:
        raise GmdExportError('Select at least one available hand/foot source IK chain')
    parents = {b.name: b.parent.name if b.parent else None for b in arm.data.bones}
    rest = {b.name: b.matrix_local.copy() for b in arm.data.bones}
    fps = action.get('gmd_source_fps', 30)*action.get('gmd_source_speed', 1)
    if not math.isfinite(fps) or fps <= 0:
        raise GmdExportError('Invalid Action FPS/speed')
    scale = arm.data.bones['center_c_n'].head_local.y/rig['rest_positions'][1][1]
    if not math.isfinite(scale) or scale <= 0:
        raise GmdExportError('Invalid target/source body height')
    origin = mathutils.Vector((0, 0, 0))
    if clip:
        first = _body_source_worlds(clip, rig, 0)['center_c_n'].translation
        target = arm.data.bones['center_c_n'].head_local
        origin = mathutils.Vector((target.x-first.x*scale,
                                   target.y-rig['rest_positions'][1][1]*scale,
                                   target.z-first.z*scale))
    scene = bpy.context.scene
    old_frame, old_subframe = scene.frame_current, scene.frame_subframe
    frames = []; clamped = 0; created = None; samples = {}; previous = {}
    report = dict(implementation='blender_adaptation_not_game_emulation',
                  source_action=action.name, source_ik=source_ik, hands=hands,
                  feet=feet, foot_lock=foot_lock, helper_blend_approx=helpers,
                  ground_gmd_y=ground, height_tolerance=height_tolerance,
                  speed_limit=speed_limit, blend_frames=blend_frames, contacts={}, plateaus={})
    try:
        for frame in range(count):
            scene.frame_set(frame+1); bpy.context.view_layer.update()
            worlds = {pb.name: pb.matrix.copy() for pb in arm.pose.bones}
            if clip:
                source = _body_source_worlds(clip, rig, frame)
                for names in chains:
                    goal = origin+source[names[-1]].translation*scale
                    fallback = rest[names[1]].translation-rest[names[0]].translation
                    worlds, limited = correction.solve_chain(worlds, parents, names, goal, fallback=fallback)
                    clamped += limited
            frames.append(worlds)
        locks = {}
        if foot_lock:
            for side in ('l', 'r'):
                name = 'asi3_%s_n' % side
                if name not in rest:
                    raise GmdExportError('Both ankle joints are required for foot locking')
                positions = [w[name].translation.copy() for w in frames]
                rotations = [w[name].to_quaternion() for w in frames]
                ankle_height = rest[name].translation.y
                runs = contacts[side]
                if runs is None:
                    runs = correction.detect_contacts(positions, fps, ground, ankle_height,
                                                       height_tolerance, speed_limit)
                locks[side], plateaus = correction.lock_targets(positions, rotations, runs, ground,
                                                               ankle_height, blend_frames)
                report['contacts'][side] = [[a+1, b+1] for a,b in runs]
                report['plateaus'][side] = [[a+1, b+1] for a,b in plateaus]
        changed_helpers = set(); lock_error = 0.0
        for frame, worlds in enumerate(frames):
            if foot_lock:
                for side, values in locks.items():
                    if frame not in values:
                        continue
                    names = ['asi%d_%s_n' % (i, side) for i in (1, 2, 3)]
                    goal, rot = values[frame]
                    fallback = rest[names[1]].translation-rest[names[0]].translation
                    worlds, limited = correction.solve_chain(worlds, parents, names, goal, rot, fallback)
                    clamped += limited
                    lock_error = max(lock_error, (worlds[names[-1]].translation-goal).length)
            if helpers:
                worlds, changed = correction.blend_helpers(worlds, parents, rest,
                                                           rig['operations'], rig['nodes'])
                changed_helpers.update(changed)
            for bone in arm.data.bones:
                parent = bone.parent
                kwargs = dict(parent_matrix=worlds[parent.name], parent_matrix_local=parent.matrix_local) if parent else {}
                basis = bone.convert_local_to_pose(worlds[bone.name], bone.matrix_local, invert=True, **kwargs)
                loc, q, size = basis.decompose()
                if bone.name in previous and q.dot(previous[bone.name]) < 0:
                    q.negate()
                previous[bone.name] = q.copy()
                for prop, values in (('location', loc), ('rotation_quaternion', q), ('scale', size)):
                    path = arm.pose.bones[bone.name].path_from_id(prop)
                    for i, value in enumerate(values):
                        if not math.isfinite(value):
                            raise GmdExportError('Non-finite corrected pose')
                        samples.setdefault((path, i), array('f')).extend((frame+1, value))
        report.update(reach_clamps=clamped, max_lock_goal_error=lock_error,
                      helpers=sorted(changed_helpers))
        created = action.copy(); created.name = action.name+'__corrected'
        created.use_fake_user = True
        created['gmd_body_corrections'] = json.dumps(report)
        _assign_momo_action(arm, created)
        # Recreate deleted pose channels too; preserve unrelated custom curves.
        for pb in arm.pose.bones:
            _key_momo_basis(pb, pb.matrix_basis.copy(), 1)
        _fill_body_motion_curves(created, samples, preserve_extra=True)
        action.use_fake_user = True
        return created
    except Exception:
        _assign_momo_action(arm, action)
        if created:
            bpy.data.actions.remove(created)
        raise
    finally:
        scene.frame_set(old_frame, subframe=old_subframe); bpy.context.view_layer.update()


class BDOMAIN_OT_body_corrections(bpy.types.Operator):
    """Bake optional source-anchor IK and heuristic flat-ground stabilization.

    This is NOT exact game IK, OPE0 execution or terrain collision.
    Original Action is kept; apply the original clip to restore it.
    """
    bl_idname = 'bdomain.body_corrections'
    bl_label = 'Bake body corrections (new Action)'
    bl_options = {'REGISTER', 'UNDO'}

    source_ik: BoolProperty(name='Source anchor IK (requires MMO)', default=True)
    hands: BoolProperty(name='Hands', default=True)
    feet: BoolProperty(name='Feet', default=True)
    foot_lock: BoolProperty(name='Flat-ground foot lock (heuristic)', default=True)
    helpers: BoolProperty(name='Twist/elbow/knee blend (approximate)', default=False)
    ground: FloatProperty(name='Ground height (GMD Y)', default=0, subtype='DISTANCE')
    height_tolerance: FloatProperty(name='Contact height tolerance', default=.04, min=.001, max=1, subtype='DISTANCE')
    speed_limit: FloatProperty(name='Contact speed limit (units/sec)', default=.35, min=.001, max=5)
    blend_frames: IntProperty(name='Entry/exit blend frames', default=2, min=0, max=30)
    left_contacts: StringProperty(name='Left contact ranges', description='Blank: automatic. Inclusive Blender frames: 1-12, 30-42')
    right_contacts: StringProperty(name='Right contact ranges', description='Blank: automatic. Inclusive Blender frames: 1-12, 30-42')

    @classmethod
    def poll(cls, context):
        ob = context.object
        return (ob is not None and ob.type == 'ARMATURE' and ob.animation_data is not None and
                ob.animation_data.action is not None and ob.animation_data.action.get('gmd_momo_kind') == 'body_motion')

    def draw(self, context):
        layout = self.layout
        layout.label(text='Blender adaptation, NOT exact game IK/OPE0', icon='INFO')
        layout.label(text='Original Action is kept. Flat ground only.')
        layout.prop(self, 'source_ik')
        row = layout.row(); row.enabled = self.source_ik
        row.prop(self, 'hands'); row.prop(self, 'feet')
        layout.prop(self, 'foot_lock')
        box = layout.box(); box.enabled = self.foot_lock
        for prop in ('ground', 'height_tolerance', 'speed_limit', 'blend_frames', 'left_contacts', 'right_contacts'):
            box.prop(self, prop)
        box.label(text='Ranges: 1-12, 30-42. Blank = automatic detection.')
        layout.prop(self, 'helpers')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=530)

    def execute(self, context):
        try:
            action = bake_body_corrections(context.object, **{name: getattr(self, name) for name in
                ('source_ik', 'hands', 'feet', 'foot_lock', 'helpers', 'ground', 'height_tolerance',
                 'speed_limit', 'blend_frames', 'left_contacts', 'right_contacts')})
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        report = json.loads(action['gmd_body_corrections'])
        intervals = sum(map(len, report['contacts'].values()))
        warning = report['reach_clamps'] or (self.foot_lock and intervals == 0)
        self.report({'WARNING'} if warning else {'INFO'},
                    'New Action: %d contact intervals; %d reach clamps; max lock error %.5f' %
                    (intervals, report['reach_clamps'], report['max_lock_goal_error']))
        return {'FINISHED'}


class BDOMAIN_PT_panel(bpy.types.Panel):
    bl_label = 'Binary Domain'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Binary Domain'

    def draw(self, context):
        lay = self.layout
        ob = context.object
        control_mod.draw(lay, ob)
        if ob and ob.get('bd_control_kind') == 'animator_v1':
            return
        if ob and ob.type == 'ARMATURE' and ob.get('gmd_source') and ob.get('gmd_skinning_version') != 2:
            lay.label(text='Old skin groups need repair', icon='ERROR')
            lay.operator('bdomain.repair_skin')
            lay.label(text='Restores original bone weights')
            lay.label(text='Or re-import the GMD')
            return
        if ob and ob.type == 'ARMATURE' and ob.get('gmd_clips'):
            lay.operator('bdomain.load_momo')
            if len(ob.bdomain_momo_clips):
                lay.prop(ob, 'bdomain_momo_filter', text='Filter', icon='VIEWZOOM')
                lay.template_list('BDOMAIN_UL_clips', '', ob, 'bdomain_momo_clips',
                                  ob, 'bdomain_momo_clip_index', rows=6)
                lay.operator('bdomain.apply_selected', icon='PLAY')
                lay.operator('bdomain.apply_clip', text='Search clips (popup)', icon='VIEWZOOM')
            else:
                lay.operator('bdomain.refresh_clip_list')
                lay.operator('bdomain.apply_clip', text='Search clips (popup)')
            lay.operator('bdomain.cleanup_momo', icon='TRASH')
            box = lay.box()
            box.label(text='%d clips' % ob.get('gmd_momo_clip_count', len(ob.bdomain_momo_clips)))
            if ob.get('gmd_momo_lazy'):
                box.label(text='Load one Action on selection')
                box.label(text='%d Actions loaded' % ob.get('gmd_momo_loaded_count', 0))
            if ob.get('gmd_momo_mode') == 'decoded_body_motion':
                box.label(text='Source body animation - press Play', icon='PLAY')
                box.label(text='Source FPS; original timing')
                box.operator('bdomain.body_corrections', icon='CON_KINEMATIC')
                box.label(text='Optional IK/lock; not exact game simulation')
                box.label(text='%d supported / %d source clips' % (
                    ob.get('gmd_momo_clip_count', 0), ob.get('gmd_momo_total_clips', 0)))
            elif ob.get('gmd_momo_mode') == 'decoded_constant_body_poses':
                box.label(text='Static full-body T-pose', icon='ARMATURE_DATA')
                box.label(text='Model proportions preserved')
            elif ob.get('gmd_momo_mode') == 'decoded_constant_expressions':
                box.label(text='Decoded expression poses', icon='INFO')
                box.operator('bdomain.preview_momo')
                box.label(text='Preview timing is generated')
            else:
                box.label(text='Old raw import: reload MOMO', icon='ERROR')
        else:
            if ob and ob.type == 'ARMATURE' and ob.get('gmd_source'):
                lay.operator('bdomain.load_momo')
            else:
                lay.label(text='Select an imported GMD armature')


def menu_import(self, context):
    self.layout.operator(BDOMAIN_OT_import_gmd.bl_idname, text='Binary Domain Model (.gmd)')


def menu_export(self, context):
    self.layout.operator(BDOMAIN_OT_export_gmd.bl_idname, text='Binary Domain Model (.gmd)')


classes = control_mod.CLASSES + (BDOMAIN_OT_body_corrections, BDOMAIN_PG_clip, BDOMAIN_UL_clips, BDOMAIN_OT_apply_selected,
           BDOMAIN_OT_refresh_clip_list, BDOMAIN_OT_cleanup_momo, BDOMAIN_OT_import_gmd, BDOMAIN_OT_export_gmd, BDOMAIN_OT_load_momo,
           BDOMAIN_OT_apply_clip, BDOMAIN_OT_preview_momo, BDOMAIN_OT_repair_skin, BDOMAIN_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Object.bdomain_momo_clips = CollectionProperty(type=BDOMAIN_PG_clip)
    bpy.types.Object.bdomain_momo_clip_index = IntProperty(default=0, min=0)
    bpy.types.Object.bdomain_momo_filter = StringProperty(name='Filter clips')
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    del bpy.types.Object.bdomain_momo_clips
    del bpy.types.Object.bdomain_momo_clip_index
    del bpy.types.Object.bdomain_momo_filter
    _momo_enum_cache.clear()
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == '__main__':
    register()
