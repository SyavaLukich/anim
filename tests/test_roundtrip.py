"""Verification harness for io_binary_domain (run: python3 tests/test_roundtrip.py).

1. Import each GMD into Blender, export untouched, compare bytes.
2. Pose-edit one bone, export, check only that bone record changed, and
   re-import reproduces the pose.
3. Load MOMO clips, verify actions/custom props, re-patch MOMO byte-identical.
"""
import os
import sys
import struct

sys.path.insert(0, '/home/user/anim')
os.chdir('/tmp/extract/out')

import bpy  # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)

import io_binary_domain as iobd  # noqa: E402
from io_binary_domain import gmd as gmd_mod  # noqa: E402
from io_binary_domain import mmo as mmo_mod  # noqa: E402

iobd.register()

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
n = iobd.load_momo_actions(arm_obj, os.path.abspath('f_man_pattern_fey.mmo'))
clips = mmo_mod.parse_momo(open('f_man_pattern_fey.mmo', 'rb').read())
names_ok = n == len(clips) == 95
act0 = bpy.data.actions.get('f_man_pattern_fey__f_man_fey__a')
pb = arm_obj.pose.bones['face_c_n']
vals_ok = act0 is not None
node_i = 0  # face_c_n is node 0 in RMN0 order
ref = [clips[0].tracks[1 + node_i * 3 + k].value for k in range(3)]
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

# ---------------------------------------------------------------- summary
print('== SUMMARY ==')
ok_all = True
for name, ok in results:
    print('  %-28s %s' % (name, 'OK' if ok else 'FAIL'))
    ok_all &= ok
print('ALL OK' if ok_all else 'FAILURES PRESENT')
sys.stdout.flush()
os._exit(0 if ok_all else 1)  # skip bpy's slow leak-detector teardown
