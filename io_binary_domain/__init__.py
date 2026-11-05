# SPDX-License-Identifier: GPL-2.0-or-later
"""
Binary Domain (.gmd / .mmo) import-export for Blender.

- Patch-based exporter: the original file is re-read and only bone records
  and vertex streams are overwritten, so an untouched import exports
  byte-identical to the source.
- Topology (vertices/faces/materials) is read-only in this version.
- MOMO face-animation clips are imported as Actions; per-node raw track
  values are kept in pose-bone custom properties gmd_c0..gmd_c2 (0..1023).
"""
bl_info = {
    "name": "Binary Domain: GMD / MOMO",
    "author": "Arena agent",
    "version": (0, 1, 3),
    "blender": (4, 1, 0),
    "location": "File > Import-Export, 3D View > N-panel > Binary Domain",
    "description": "Import/export Binary Domain (Sega, 2012) GSGM models and MOMO face animations",
    "category": "Import-Export",
}

import bpy
import json
import math
import mathutils
import os
import struct
from bpy.props import (StringProperty, BoolProperty, IntProperty)
from bpy_extras.io_utils import ImportHelper, ExportHelper

from . import gmd as gmd_mod
from . import mmo as mmo_mod

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
            uvl = me.uv_layers.new(name='UVMap')
            uv2l = me.uv_layers.new(name='UV2') if stride == 40 else None
            for poly in me.polygons:
                for li in poly.loop_indices:
                    vi = me.loops[li].vertex_index
                    v = order[vi]
                    dv = g.decode_vertex(m.stream, v)
                    if dv['uv']:
                        uvl.data[li].uv = (dv['uv'][0], 1.0 - dv['uv'][1])
                    if uv2l is not None and dv['uv2']:
                        uv2l.data[li].uv = (dv['uv2'][0], 1.0 - dv['uv2'][1])
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
                bone_id = g.palette[m.pal_base + bi[j]] if bi[j] < m.pal_size else 0
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
            if g.palette[m.pal_base + k] == bone_id:
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
def _default_rig_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, 'rigs', 'f_man.mma')


def load_momo_actions(arm_obj, mmo_path, mma_path=None, bake_euler=False):
    data = open(mmo_path, 'rb').read()
    clips = mmo_mod.parse_momo(data)
    if not mma_path:
        mma_path = _default_rig_path()
    rig = mmo_mod.parse_moma(open(mma_path, 'rb').read())
    nodes = rig['nodes']
    if not nodes:
        raise GmdExportError('no node names found in rig %s' % mma_path)
    pose = arm_obj.pose
    missing = [n for n in nodes if n not in pose.bones]
    if arm_obj.animation_data is None:
        arm_obj.animation_data_create()
    prefix = os.path.splitext(os.path.basename(mmo_path))[0]
    maxf = 1
    for c in clips:
        act = bpy.data.actions.new('%s__%s' % (prefix, c.name))
        prev = arm_obj.animation_data.action
        arm_obj.animation_data.action = act
        maxf = max(maxf, c.frames)
        for ni, node in enumerate(nodes):
            pb = pose.bones.get(node)
            if pb is None:
                continue
            base = 1 + ni * 3
            vals = [c.tracks[base + k].value for k in range(3)]
            for k in range(3):
                pb['gmd_c%d' % k] = vals[k]
                pb.keyframe_insert(data_path='["gmd_c%d"]' % k, frame=1)
            if bake_euler:
                e = [(v / 1024.0 * 360.0 - 180.0) * math.pi / 180.0 for v in vals]
                pb.rotation_mode = 'XYZ'
                pb.rotation_euler = e
                pb.keyframe_insert(data_path='rotation_euler', frame=1)
        act.frame_range = (1, max(1, c.frames))
        arm_obj.animation_data.action = prev
    arm_obj['gmd_momo'] = mmo_path
    arm_obj['gmd_clips'] = json.dumps([c.name for c in clips])
    bpy.context.scene.frame_end = max(1, maxf)
    return len(clips)


class BDOMAIN_OT_load_momo(bpy.types.Operator):
    """Import a MOMO face-animation file as Actions on the selected armature"""
    bl_idname = 'bdomain.load_momo'
    bl_label = 'Load MOMO clips'
    bl_options = {'REGISTER', 'UNDO'}

    filepath: StringProperty(subtype='FILE_PATH')
    rig_mma: StringProperty(subtype='FILE_PATH',
                            description='MOMA rig with node names (default: bundled f_man.mma)')
    bake_euler: BoolProperty(
        name='Experimental Euler bake', default=False,
        description='Also keyframe rotation_euler from raw values (semantics unconfirmed)')
    filter_glob: StringProperty(default='*.mmo', options={'HIDDEN'})

    def execute(self, context):
        ob = context.object
        if ob is None or ob.type != 'ARMATURE' or not ob.get('gmd_source'):
            self.report({'ERROR'}, 'select an imported GMD armature first')
            return {'CANCELLED'}
        try:
            n = load_momo_actions(ob, self.filepath, self.rig_mma or None,
                                  self.bake_euler)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Imported %d MOMO clips' % n)
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class BDOMAIN_OT_apply_clip(bpy.types.Operator):
    """Set the armature action to a MOMO clip"""
    bl_idname = 'bdomain.apply_clip'
    bl_label = 'Apply MOMO clip'
    bl_options = {'REGISTER', 'UNDO'}

    name: StringProperty()

    def execute(self, context):
        ob = context.object
        act = bpy.data.actions.get('%s__%s' % (
            os.path.splitext(os.path.basename(ob.get('gmd_momo', '')))[0],
            self.name))
        if act is None:
            self.report({'ERROR'}, 'action not found')
            return {'CANCELLED'}
        if ob.animation_data is None:
            ob.animation_data_create()
        ob.animation_data.action = act
        context.scene.frame_set(1)
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {'FINISHED'}

    def draw(self, context):
        self.layout.prop(self, 'name')

    @classmethod
    def description(cls, context, properties):
        return 'Apply clip %s' % properties.name


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
        name='MOMO animation', subtype='FILE_PATH',
        description='Optional .mmo face animation to import with the model')
    rig_mma: StringProperty(
        name='MOMA rig', subtype='FILE_PATH',
        description='Rig with node names (default: bundled f_man.mma)')
    bake_euler: BoolProperty(name='Experimental Euler bake', default=False)

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


class BDOMAIN_PT_panel(bpy.types.Panel):
    bl_label = 'Binary Domain'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Binary Domain'

    def draw(self, context):
        lay = self.layout
        ob = context.object
        if ob and ob.type == 'ARMATURE' and ob.get('gmd_clips'):
            lay.operator('bdomain.load_momo')
            lay.operator('bdomain.apply_clip')
            box = lay.box()
            box.label(text='%d clips' % len(json.loads(ob['gmd_clips'])))
        else:
            if ob and ob.type == 'ARMATURE' and ob.get('gmd_source'):
                lay.operator('bdomain.load_momo')
            else:
                lay.label(text='Select an imported GMD armature')


def menu_import(self, context):
    self.layout.operator(BDOMAIN_OT_import_gmd.bl_idname, text='Binary Domain Model (.gmd)')


def menu_export(self, context):
    self.layout.operator(BDOMAIN_OT_export_gmd.bl_idname, text='Binary Domain Model (.gmd)')


classes = (BDOMAIN_OT_import_gmd, BDOMAIN_OT_export_gmd, BDOMAIN_OT_load_momo,
           BDOMAIN_OT_apply_clip, BDOMAIN_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == '__main__':
    register()
