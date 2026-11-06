"""Real-data p_gen T-pose retarget: mapping, geometry, lengths, persistence.

LD_LIBRARY_PATH=/home/user/xlibs python3 tests/test_body_retarget.py
Local game-data fixtures only; no extracted model is included in Git.
"""
import json
import math
import os
from pathlib import Path
import sys
import traceback
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bpy
from mathutils import Matrix
import io_binary_domain as addon
from io_binary_domain import mmo


def evaluated(ob):
    ev = ob.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = ev.to_mesh()
    coords = [v.co.copy() for v in mesh.vertices]
    ev.to_mesh_clear()
    return coords


def main():
    addon.register()
    source = Path('/tmp/motion_samples/p_gen_basic.mmo').read_bytes()
    clip = mmo.parse_momo(source)[0]
    rig = mmo.parse_moma(Path('/tmp/motion_samples/p_gen.mma').read_bytes())
    nodes = mmo.pose_nodes(clip, rig)
    assert len(nodes) == 161 and nodes[38] is None and nodes[65] is None
    assert all(v is not None for i, v in enumerate(nodes) if i not in (38, 65))
    assert nodes[1][0][1] > 1.08  # center, not the reserved zero channels
    assert rig['channel_indices'][:4] == [8, 9, 10, 11]
    assert len(set(rig['channel_indices'])) == 636
    for path in sorted(Path('/tmp/extract/out').glob('c_awmn_fey*.gmd')):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        addon.load_gmd(bpy.context, str(path))
        arm = bpy.context.object
        meshes = [o for o in bpy.data.objects if o.type == 'MESH']
        before = {o.name: evaluated(o) for o in meshes}
        start = len(bpy.data.actions)
        assert addon.load_momo_actions(arm, '/tmp/motion_samples/p_gen_basic.mmo') == 1
        assert len(bpy.data.actions) == start + 1
        act = arm.animation_data.action
        assert act['gmd_momo_kind'] == 'constant_body_pose'
        assert act.use_fake_user
        assert json.loads(act['gmd_momo_raw_words']) == [list(t.words) for t in clip.tracks]
        for side in ('l', 'r'):
            joints = [arm.pose.bones[f'{name}_{side}_n'].matrix.translation
                      for name in ('ude1', 'ude2', 'ude3')]
            assert max(p.y for p in joints) - min(p.y for p in joints) < 1e-4
            assert max(p.z for p in joints) - min(p.z for p in joints) < 1e-4
            assert (joints[-1].x - joints[0].x) * (1 if side == 'l' else -1) > .45
        # Preserve every model parent-offset length, not generic source lengths.
        for bone in arm.data.bones:
            pose = arm.pose.bones[bone.name]
            assert all(math.isfinite(v) for row in pose.matrix for v in row)
            if bone.parent:
                original = (bone.head_local - bone.parent.head_local).length
                actual = (pose.matrix.translation - arm.pose.bones[bone.parent.name].matrix.translation).length
                assert abs(original - actual) < 2e-5, (bone.name, original, actual)
        root = arm.pose.bones['center_c_n']
        assert (root.matrix.translation - root.bone.head_local).length < 1e-5
        moved = 0
        for ob in meshes:
            after = evaluated(ob)
            for a, b in zip(before[ob.name], after):
                assert all(math.isfinite(v) and abs(v) < 3 for v in b)
                moved += (a - b).length > .001
        # The head-only variant has little/no arm geometry; full models move many vertices.
        if 'head' not in path.stem:
            assert moved > 500, (path.name, moved)
        print(path.name, 'T-pose / all bone lengths / finite geometry / moved', moved, flush=True)

    # Unsupported auxiliary controls and incomplete target rigs fail clearly.
    first = clip.tracks[0].value
    clip.tracks[0].value = 1.0
    try:
        addon._body_bases(arm, clip, rig)
    except addon.GmdExportError as exc:
        assert 'auxiliary' in str(exc)
    else:
        raise AssertionError('unsupported root channels accepted')
    clip.tracks[0].value = first
    empty = bpy.data.objects.new('partial rig', bpy.data.armatures.new('partial'))
    try:
        addon._body_bases(empty, clip, rig)
    except addon.GmdExportError as exc:
        assert 'Full body GMD skeleton' in str(exc)
    else:
        raise AssertionError('partial rig accepted')
    bpy.data.objects.remove(empty)
    # Old scenes can recover bind rotations from their unchanged source GMD.
    del arm['gmd_bind_local_quats']
    assert addon.load_momo_actions(arm, '/tmp/motion_samples/p_gen_basic.mmo') == 1
    assert arm.get('gmd_bind_local_quats')
    addon.load_momo_actions(arm, '/tmp/extract/out/f_man_pattern_fey.mmo')
    try:
        addon.load_momo_actions(arm, '/tmp/motion_samples/p_gen_basic.mmo', addon._default_rig_path())
    except addon.GmdExportError as exc:
        assert 'rig mismatch' in str(exc)
    else:
        raise AssertionError('wrong MOMA accepted')
    assert addon.load_momo_actions(arm, '/tmp/motion_samples/p_gen_basic.mmo') == 1
    assert bpy.ops.bdomain.apply_selected() == {'FINISHED'}
    matrices = {p.name: p.matrix.copy() for p in arm.pose.bones}
    bpy.ops.wm.save_as_mainfile(filepath='/tmp/bd_body_retarget.blend')
    bpy.ops.wm.open_mainfile(filepath='/tmp/bd_body_retarget.blend')
    arm = bpy.context.object
    arm['gmd_momo'] = '/tmp/missing_source.mmo'
    assert bpy.ops.bdomain.apply_selected() == {'FINISHED'}  # existing Action needs no source
    for name, expected in matrices.items():
        assert max(abs(arm.pose.bones[name].matrix[r][c] - expected[r][c])
                   for r in range(4) for c in range(4)) < 2e-5
    print('PASS: p_gen mapping, five rigs, geometry, anchored root, persistence, rig mismatch', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        os._exit(1)
    os._exit(0)
