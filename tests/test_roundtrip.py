"""Verification harness for io_binary_domain (run: python3 tests/test_roundtrip.py).

1. Import each GMD into Blender, export untouched, compare bytes.
2. Pose-edit one bone, export, check only that bone record changed, and
   re-import reproduces the pose.
3. Load MOMO clips, verify actions/custom props, re-patch MOMO byte-identical.
"""
import os
import sys
import struct
import json

sys.path.insert(0, '/home/user/anim')
os.chdir('/tmp/extract/out')

import bpy  # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)

import io_binary_domain as iobd  # noqa: E402
from io_binary_domain import gmd as gmd_mod  # noqa: E402
from io_binary_domain import mmo as mmo_mod  # noqa: E402

# Simulate Blender retaining a stale parser across addon package reload.
import importlib
gmd_mod.GMD.uv_offset = lambda self, sid: -999
importlib.reload(iobd)
# uv_offset needs an instance; the declaration assertions below verify reload.
iobd.register()
# File browsers cannot open a second file picker for optional paths.
assert bpy.ops.import_scene.gmd.get_rna_type().properties['mmo_path'].subtype == 'NONE'
assert bpy.ops.import_scene.gmd.get_rna_type().properties['rig_mma'].subtype == 'NONE'
assert bpy.ops.bdomain.load_momo.get_rna_type().properties['rig_mma'].subtype == 'NONE'
assert bpy.ops.bdomain.load_momo.get_rna_type().properties['filepath'].subtype == 'FILE_PATH'


GMD_FILES = ['c_awmn_fey.gmd', 'c_awmn_fey_head.gmd', 'c_awmn_fey_last.gmd',
             'c_awmn_fey_mant.gmd', 'c_awmn_fey_naked.gmd']
OUT = '/tmp/bd_out'
os.makedirs(OUT, exist_ok=True)

results = []

# Regression: two 40-byte declarations have different UV offsets.
probe = gmd_mod.GMD(open(GMD_FILES[0], 'rb').read())
for sid, stream in enumerate(probe.streams):
    if stream['flags'] == 0x1fe0ffc3:
        assert probe.uv_offset(sid) == 36
    elif stream['flags'] == 0x28e0ffc3:
        assert probe.uv_offset(sid) == 32


def fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # keep a scene
    if not bpy.data.scenes:
        bpy.ops.scene.new()


def bytecmp(a, b, label):
    da, db = open(a, 'rb').read(), open(b, 'rb').read()
    same = da == db
    if not same:
        n = sum(1 for x, y in zip(da, db) if x != y)
        first = next(i for i, (x, y) in enumerate(zip(da, db)) if x != y)
        print('   MISMATCH %s: %d bytes differ, first at 0x%x (len %d vs %d)'
              % (label, n, first, len(da), len(db)))
    else:
        print('   byte-identical (%d bytes)' % len(da))
    return same


# ---------------------------------------------------------------- 1. roundtrip
print('== 1. untouched import -> export roundtrip ==')
all_ok = True
import time
for f in GMD_FILES:
    t0 = time.time()
    print('*', f, flush=True)
    fresh()
    bpy.ops.import_scene.gmd(filepath=os.path.abspath(f))
    arm = [o for o in bpy.data.objects if o.type == 'ARMATURE'][0]
    bpy.context.view_layer.objects.active = arm
    n_mesh = len([o for o in bpy.data.objects if o.type == 'MESH'])
    n_bones = len(arm.data.bones)
    bpy.context.view_layer.update()
    for ob in bpy.data.objects:
        if ob.type == 'MESH':
            assert max(abs(ob.matrix_world[i][j] - arm.matrix_world[i][j])
                       for i in range(4) for j in range(4)) < 1e-6, 'Double axis conversion'
            if ob.data.uv_layers:
                assert len(ob.data.uv_layers) == 1, 'Unexpected empty UV2'
                assert ob.data.uv_layers.active.name == 'UVMap'
                assert ob.data.uv_layers['UVMap'].active_render
                for mat in ob.data.materials:
                    for node in mat.node_tree.nodes:
                        if node.type == 'TEX_IMAGE':
                            links = node.inputs['Vector'].links
                            assert links and links[0].from_node.type == 'UVMAP'
                            assert links[0].from_node.uv_map == 'UVMap'

    # Count-prefixed palettes: the first byte is a count, NOT bone slot zero.
    source_g = gmd_mod.GMD(open(f, 'rb').read())
    for ob in bpy.data.objects:
        if ob.type != 'MESH':
            continue
        m = source_g.meshes[ob['gmd_mesh']]
        vm, _ = source_g.mesh_geometry(m)
        order = list(dict.fromkeys(vm))
        palette = source_g.palette[m.pal_base + 1:m.pal_base + 1 + m.pal_size]
        for vi in range(min(12, len(order))):
            dv = source_g.decode_vertex(m.stream, order[vi])
            expected = {}
            for weight, slot in zip(dv['w'], dv['bi']):
                if weight and slot < len(palette):
                    name = source_g.bones[palette[slot]].name
                    expected[name] = expected.get(name, 0) + weight / 255.0
            actual = {ob.vertex_groups[e.group].name: e.weight
                      for e in ob.data.vertices[vi].groups}
            assert set(actual) == set(expected), (ob.name, vi, actual, expected)
            assert all(abs(actual[n] - w) < 1e-6 for n, w in expected.items())
    out = os.path.join(OUT, f)
    bpy.ops.export_scene.gmd(filepath=out)
    ok = bytecmp(os.path.abspath(f), out, f)
    print('   took %.1fs' % (time.time() - t0), flush=True)
    print('   objects: %d meshes, %d bones' % (n_mesh, n_bones))
    results.append((f, ok))
    all_ok &= ok

# ---------------------------------------------------------------- 2. pose edit
print('== 2. pose edit roundtrip ==')
fresh()
bpy.ops.import_scene.gmd(filepath=os.path.abspath('c_awmn_fey.gmd'))
arm_obj = [o for o in bpy.data.objects if o.type == 'ARMATURE'][0]
bpy.context.view_layer.objects.active = arm_obj
bpy.ops.object.mode_set(mode='POSE')
pb = arm_obj.pose.bones['face_c_n']
g0 = gmd_mod.GMD(open('c_awmn_fey.gmd', 'rb').read())
b0 = [b for b in g0.bones if b.name == 'face_c_n'][0]
import mathutils
import math
pb.rotation_mode = 'XYZ'
pb.rotation_euler = (0.1, 0.2, 0.3)
bpy.context.view_layer.update()
out2 = os.path.join(OUT, 'posed.gmd')
bpy.ops.object.mode_set(mode='OBJECT')
bpy.ops.export_scene.gmd(filepath=out2)
d0 = open('c_awmn_fey.gmd', 'rb').read()
d2 = open(out2, 'rb').read()
diff = [i for i in range(len(d0)) if d0[i] != d2[i]]
# changed bytes must lie inside face_c_n's record or the records of its
# descendants (children legitimately move when the parent rotates).
# Parent resolution mirrors the importer: sibling chains first, [3] field
# as fallback (helpers only have the [3] field).
parent_of = {}
def _walk(bid, depth=0):
    if depth > 512:
        return
    c = g0.bones[bid].first_child
    while c != 0xFFFFFFFF and c < len(g0.bones):
        parent_of[c] = bid
        _walk(c, depth + 1)
        c = g0.bones[c].next_sibling
for bb in g0.bones:
    _walk(bb.id)
parent_map = {}
for bb in g0.bones:
    parent_map[bb.id] = parent_of.get(
        bb.id, bb.parent if bb.parent != 0xFFFFFFFF else None)
desc = set()
for bb in g0.bones:
    cur = bb.id
    seen = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        if cur == b0.id:
            desc.update(seen)
            break
        cur = parent_map.get(cur)
ranges = []
for bid in desc:
    bb = g0.bones[bid]
    if bb.id >= 0xA0:
        h = [x for x in g0.helper if x['bone'] == bb.id]
        if h:
            o = g0.dir[1][0] + h[0]['id'] * 0x40
            ranges += [(o + 16, o + 32), (o + 48, o + 64)]
    else:
        ranges += [(bb.off + 0x50, bb.off + 0x70)]
bad = [i for i in diff if not any(a <= i < b for a, b in ranges)]
fc_changed = any(b0.off + 0x60 <= i < b0.off + 0x70 for i in diff)
in_rec = fc_changed and not bad
print('   changed %d bytes (%d unexpected), face_c_n aa written: %s -> in_rec %s'
      % (len(diff), len(bad), fc_changed, in_rec))
# re-import and compare world transforms
fresh()
bpy.ops.import_scene.gmd(filepath=out2)
arm2 = [o for o in bpy.data.objects if o.type == 'ARMATURE'][0]
g2 = gmd_mod.GMD(open(out2, 'rb').read())
b2 = [b for b in g2.bones if b.name == 'face_c_n'][0]
m_bl = arm2.data.bones['face_c_n'].matrix_local
R_g = m_bl.to_3x3()
# Blender 'XYZ' euler == RZ @ RY @ RX; use Euler().to_matrix() as reference
want = mathutils.Euler((0.1, 0.2, 0.3), 'XYZ').to_matrix()
m_orig = iobd.bone_matrix(b0).to_3x3()
expected = m_orig @ want
err = max(abs(R_g[i][j] - expected[i][j]) for i in range(3) for j in range(3))
print('   re-import rotation error vs expected: %.6f' % err)
pose_ok = in_rec and err < 1e-5
results.append(('pose-edit', pose_ok))

# ---------------------------------------------------------------- 3. MOMO
print('== 3. MOMO ==')
fresh()
bpy.ops.import_scene.gmd(filepath=os.path.abspath('c_awmn_fey.gmd'))
arm_obj = [o for o in bpy.data.objects if o.type == 'ARMATURE'][0]
bpy.context.view_layer.objects.active = arm_obj
# Migration must repair old skin groups without touching UVs/materials.
def skin_signature():
    return sorted((ob['gmd_mesh'], tuple(tuple(sorted(
        (ob.vertex_groups[e.group].name, round(e.weight, 6)) for e in v.groups))
        for v in list(ob.data.vertices)[:12]))
        for ob in bpy.data.objects if ob.type == 'MESH')
correct_skin = skin_signature()
ob = next(o for o in bpy.data.objects if o.type == 'MESH' and o.vertex_groups)
vg = ob.vertex_groups[0]
vg.add([0], 0.123, 'REPLACE')
del arm_obj['gmd_skinning_version']
assert bpy.ops.bdomain.repair_skin() == {'FINISHED'}
assert skin_signature() == correct_skin
assert arm_obj['gmd_skinning_version'] == 2
assert bpy.ops.bdomain.load_momo(filepath=os.path.abspath('f_man_pattern_fey.mmo')) == {'FINISHED'}
n = len(json.loads(arm_obj['gmd_clips']))
clips = mmo_mod.parse_momo(open('f_man_pattern_fey.mmo', 'rb').read())
names_ok = n == len(clips) == 95
act0 = bpy.data.actions.get('f_man_pattern_fey__f_man_fey__a')
pb = arm_obj.pose.bones['face_c_n']
vals_ok = act0 is not None
node_i = 0  # face_c_n is node 0 in RMN0 order
ref = [clips[0].tracks[node_i * 4 + k].value for k in range(3)]
got = [pb['gmd_c%d' % k] for k in range(3)]
vals_ok &= got == ref
print('   actions: %d (expected %d), face_c_n c0..c2 = %s (expected %s)'
      % (n, len(clips), got, ref))
# re-patch with identical values -> byte-identical
data = bytearray(open('f_man_pattern_fey.mmo', 'rb').read())
k = 0
for c in clips:
    k += mmo_mod.write_track_values(data, c, [t.value for t in c.tracks])
same = bytes(data) == open('f_man_pattern_fey.mmo', 'rb').read()
print('   repatched %d tracks/clip, byte-identical: %s' % (k // len(clips), same))
momo_ok = names_ok and vals_ok and same
results.append(('momo', momo_ok))

# Decoded channels must deform the actual face, not just custom properties.
rig = mmo_mod.parse_moma(open(iobd._default_rig_path(), 'rb').read())
assert len(rig['parents']) == 46 and rig['parents'][33:] == [32] * 13
assert all(len(c.tracks) == 184 for c in clips)
for c in clips:
    assert len(mmo_mod.pose_nodes(c)) == 46
    act = bpy.data.actions[json.loads(arm_obj['gmd_clip_actions'])[c.name]]
    assert json.loads(act['gmd_momo_raw_words']) == [list(t.words) for t in c.tracks]
face_mesh = next(ob for ob in bpy.data.objects if ob.type == 'MESH'
                 and ob.vertex_groups.get('_eyelid_l_n')
                 and ob.vertex_groups.get('_eyelid_r_n'))
def evaluated_vertices(ob):
    deps = bpy.context.evaluated_depsgraph_get()
    evaluated = ob.evaluated_get(deps)
    mesh = evaluated.to_mesh()
    coords = [v.co.copy() for v in mesh.vertices]
    evaluated.to_mesh_clear()
    return coords
arm_obj.animation_data.action = None
for pb in arm_obj.pose.bones:
    pb.matrix_basis = mathutils.Matrix.Identity(4)
bpy.context.view_layer.update()
neutral = evaluated_vertices(face_mesh)
assert bpy.ops.bdomain.apply_clip(name='f_man_fey__eye_close') == {'FINISHED'}
closed = evaluated_vertices(face_mesh)
changed = sum((a-b).length > 1e-4 for a, b in zip(neutral, closed))
assert changed > 20, 'Decoded expression does not deform face mesh'
for name in ('_eyelid_l_n', '_eyelid_r_n'):
    pb = arm_obj.pose.bones[name]
    delta = pb.matrix.to_3x3() @ pb.bone.matrix_local.to_3x3().inverted()
    assert delta.to_quaternion().angle > 0.5, 'Both eyelids must rotate'
assert bpy.ops.bdomain.apply_clip(name='f_man_fey__open_a') == {'FINISHED'}
jaw = arm_obj.pose.bones['_jaw_c_n']
assert (jaw.matrix.to_3x3() @ jaw.bone.matrix_local.to_3x3().inverted()).to_quaternion().angle > .15
expression = {n: arm_obj.pose.bones[n].matrix_basis.copy() for n in rig['nodes']}
assert bpy.ops.bdomain.preview_momo() == {'FINISHED'}
for frame in (1, 16, 31):
    bpy.context.scene.frame_set(frame)
    bpy.context.view_layer.update()
    for name in rig['nodes']:
        expected = expression[name] if frame == 16 else mathutils.Matrix.Identity(4)
        got_matrix = arm_obj.pose.bones[name].matrix_basis
        assert max(abs(got_matrix[i][j]-expected[i][j])
                   for i in range(4) for j in range(4)) < 1e-5, (frame, name)
bpy.context.scene.frame_set(8)
bpy.context.view_layer.update()
assert arm_obj.pose.bones['_jaw_c_n'].matrix_basis.to_quaternion().angle > 0.01
results.append(('momo-visible-and-preview', True))
print('   closed-eye pose deforms %d face vertices; preview frames 1/16/31 verified' % changed)

# Unassigned Actions must survive saving/reopening, not just exist in memory.
saved = os.path.join(OUT, 'momo_persistence.blend')
bpy.ops.wm.save_as_mainfile(filepath=saved)
bpy.ops.wm.open_mainfile(filepath=saved, load_ui=False, use_scripts=False)
kept = [a for a in bpy.data.actions if a.get('gmd_raw_momo')]
persist_ok = len(kept) == len(clips) and all(a.use_fake_user for a in kept)
print('   MOMO Actions after save/reopen: %d (expected %d)' % (len(kept), len(clips)))
results.append(('momo-save-reopen', persist_ok))
arm_obj = next(o for o in bpy.data.objects if o.type == 'ARMATURE')
bpy.context.view_layer.objects.active = arm_obj
assert bpy.ops.bdomain.apply_clip(name='f_man_fey__eye_close') == {'FINISHED'}
for name in ('_eyelid_l_n', '_eyelid_r_n'):
    pb = arm_obj.pose.bones[name]
    delta = pb.matrix.to_3x3() @ pb.bone.matrix_local.to_3x3().inverted()
    assert delta.to_quaternion().angle > .5, 'Expression failed after save/reopen'


# ---------------------------------------------------------------- summary
print('== SUMMARY ==')
ok_all = True
for name, ok in results:
    print('  %-28s %s' % (name, 'OK' if ok else 'FAIL'))
    ok_all &= ok
print('ALL OK' if ok_all else 'FAILURES PRESENT')
sys.stdout.flush()
os._exit(0 if ok_all else 1)  # skip bpy's slow leak-detector teardown
