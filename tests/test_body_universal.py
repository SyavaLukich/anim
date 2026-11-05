"""Optional Blender/corpus regression; uses locally extracted game fixtures."""
import json
import math
import os
from pathlib import Path
import sys
import traceback
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bpy
import io_binary_domain as addon
from io_binary_domain import mmo

ROOT = Path('/tmp/motion_samples')


def check_pose(arm, clip, rig, frame):
    bases = addon._body_bases(arm, clip, rig, frame)
    for name, basis in bases.items():
        assert all(math.isfinite(v) for row in basis for v in row), (clip.name, name)
        arm.pose.bones[name].matrix_basis = basis
    bpy.context.view_layer.update()
    for pb in arm.pose.bones:
        if pb.parent:
            actual = (pb.matrix.translation-pb.parent.matrix.translation).length
            rest = (pb.bone.head_local-pb.parent.bone.head_local).length
            assert abs(actual-rest) < 4e-5, (clip.name, frame, pb.name, actual, rest)
    return bases


def main():
    addon.register()
    rig = mmo.parse_moma((ROOT/'p_man.mma').read_bytes())
    # Every extracted human library, not a clip-name allowlist.
    total = 0
    for path in ([] if os.environ.get('SKIP_CORPUS') else sorted(ROOT.glob('p_man*.mmo'))):
        data = path.read_bytes()
        assert mmo.momo_rig_name(data) == 'p_man'
        for clip in mmo.parse_momo(data, False):
            mmo.decode_clip(data, clip)
            for frame in (0, clip.frames/2, clip.frames):
                values = mmo.pose_nodes(clip, rig, frame, allow_missing_tail=True)
                assert len(values) == 79
                assert all(v is not None for v in values[:6])
            total += 1
    if total:
        print('CORPUS PASS', total, 'clips: decode and source-rig mapping at three times', flush=True)
    model = '/tmp/extract/out/c_awmn_fey_naked.gmd'
    bpy.ops.wm.read_factory_settings(use_empty=True)
    addon.load_gmd(bpy.context, model); arm = bpy.context.object
    path = ROOT/'p_man_fay_whan.mmo'
    data = path.read_bytes(); clips = mmo.parse_momo(data, False)
    assert addon.load_momo_actions(arm, str(path)) == 203
    assert len(arm.bdomain_momo_clips) == 203
    assert len(bpy.data.actions) == 1
    # Validate every clip, including both short (finger-less) channel layouts.
    arm.animation_data.action = None
    for c in clips:
        mmo.decode_clip(data, c)
        for frame in (0, c.frames//2, c.frames):
            check_pose(arm, c, rig, frame)
    print('FEY PASS 203/203 clips, full retarget at 609 source times', flush=True)
    selected = [clips[0], next(c for c in clips if 'turnL180' in c.name),
                next(c for c in clips if 'walkA_slow_f' in c.name),
                next(c for c in clips if c.header == 55)]
    # Dense Actions must match direct evaluation, retain length and root travel.
    for c in selected:
        assert bpy.ops.bdomain.apply_clip(name=c.name) == {'FINISHED'}
        action = arm.animation_data.action
        assert action['gmd_source_channels'] == c.header
        assert action['gmd_source_fps'] == 30
        assert len(json.loads(action['gmd_auxiliary_samples'])) == 3
        assert bpy.context.scene.frame_end == c.frames+1
        positions, orientations = [], []
        for frame in (0, c.frames//2, c.frames):
            bpy.context.scene.frame_set(frame+1); bpy.context.view_layer.update()
            bases = addon._body_bases(arm, c, rig, frame)
            for name, basis in bases.items():
                err = max(abs(arm.pose.bones[name].matrix_basis[i][j]-basis[i][j])
                          for i in range(4) for j in range(4))
                assert err < 4e-5, (c.name, frame, name, err)
            positions.append(arm.pose.bones['center_c_n'].matrix.translation.copy())
            orientations.append(arm.pose.bones['center_c_n'].matrix.to_quaternion())
        if 'turnL180' in c.name:
            assert orientations[0].rotation_difference(orientations[-1]).angle > 2.5
        if 'walkA_slow_f' in c.name:
            assert max((v-positions[0]).length for v in positions) > .1
        print('ACTION PASS', c.name, c.header, c.frames+1, flush=True)
    # The 54-track T-pose omits only the weapon quaternion plus fingers.
    assert addon.load_momo_actions(arm, str(ROOT/'p_man_btl.mmo')) == 1
    c = mmo.parse_momo((ROOT/'p_man_btl.mmo').read_bytes())[0]
    assert c.header == 54
    check_pose(arm, c, rig, 0)
    # Previously unhandled float Euler + packed Euler codecs in the same clip.
    path = ROOT/'p_man_dao_talk_wdr.mmo'
    addon.load_momo_actions(arm, str(path))
    assert bpy.ops.bdomain.apply_clip(name='s_WDR_dao_woman_c_lp') == {'FINISHED'}
    bpy.context.scene.frame_set(70); bpy.context.view_layer.update()
    assert arm.animation_data.action['gmd_source_frames'] > 1
    bpy.ops.wm.save_as_mainfile(filepath='/tmp/bd_universal_motion.blend')
    bpy.ops.wm.open_mainfile(filepath='/tmp/bd_universal_motion.blend')
    arm = bpy.context.object
    arm['gmd_momo'] = '/tmp/unavailable_source.mmo'
    assert bpy.ops.bdomain.apply_clip(name='s_WDR_dao_woman_c_lp') == {'FINISHED'}
    print('PASS: generic catalogs, root travel, 54/55/85 channels, Euler codecs, persistence', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc(); os._exit(1)
    os._exit(0)
