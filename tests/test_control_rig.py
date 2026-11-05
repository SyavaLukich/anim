"""Persistent animator control rig: separate armature, IK/FK, bake, removal.

Runs under Blender Python.  The fixture part uses the same local game data
as test_body_corrections.py and is skipped when it is absent.
"""
import math
import os
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bpy
from mathutils import Matrix, Vector
import io_binary_domain as addon
from io_binary_domain import control_rig as rig

GMD = '/tmp/extract/out/c_awmn_fey_naked.gmd'
MMO = '/tmp/motion_samples/p_man_fay_whan.mmo'
CLIP = 's_Whan_walkA_slow_f'
FRAMES = (1, 10, 20, 30, 40, 51)


def snapshot(arm, frames=FRAMES):
    out = {}
    for frame in frames:
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        out[frame] = {pb.name: pb.matrix.copy() for pb in arm.pose.bones}
    return out


PROCEDURAL = set()


def max_diff(a, b, names=None):
    # *_sup bones are procedural in the game (OPE0-driven), so they and
    # their descendants are excluded from raw-channel comparisons.
    return max(abs(a[n][i][j] - b[n][i][j])
               for n in (names or a) if n not in PROCEDURAL
               for i in range(4) for j in range(4))


def action_coords(action):
    if hasattr(action, 'slots') and len(action.slots) and len(action.layers):
        curves = action.layers[0].strips[0].channelbag(action.slots[0]).fcurves
    else:
        curves = action.fcurves
    return [(f.data_path, f.array_index,
             [(p.co.x, p.co.y) for p in f.keyframe_points]) for f in curves]


def segment_error(arm):
    worst = 0.0
    for pb in arm.pose.bones:
        if pb.parent:
            actual = (pb.matrix.translation - pb.parent.matrix.translation).length
            rest = (pb.bone.head_local - pb.parent.bone.head_local).length
            worst = max(worst, abs(actual - rest))
    return worst


def fixture_test():
    addon.register()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    addon.load_gmd(bpy.context, GMD)
    arm = bpy.context.object
    for bone in arm.data.bones:
        chain = bone
        while chain:
            if chain.name.endswith('_sup'):
                PROCEDURAL.add(bone.name)
                break
            chain = chain.parent
    addon.load_momo_actions(arm, MMO)
    bpy.ops.bdomain.apply_clip(name=CLIP)
    original = arm.animation_data.action
    original_coords = action_coords(original)
    before = snapshot(arm)

    # Animated clips start in FK: playback must be unchanged.
    ctrl = rig.create(arm)
    assert ctrl.parent is arm and ctrl.get('bd_control_kind') == 'animator_v1'
    assert ctrl is not arm and len(ctrl.data.bones) > len(arm.data.bones)
    assert arm.animation_data.action is original
    assert action_coords(original) == original_coords
    assert all(ctrl['bd_ik_' + limb] == 0.0 for limb in rig.LIMBS)
    after = snapshot(arm)
    for frame in FRAMES:
        assert max_diff(before[frame], after[frame]) < 5e-4, frame

    # Matching one leg to IK at a frame must not pop; foot roll pivots.
    bpy.context.scene.frame_set(20)
    bpy.context.view_layer.update()
    pose20 = {pb.name: pb.matrix.copy() for pb in arm.pose.bones}
    rig.match(ctrl, 'leg_l', 'IK', key=False)
    now = {pb.name: pb.matrix.copy() for pb in arm.pose.bones}
    assert max_diff(pose20, now, rig.LIMBS['leg_l']) < 5e-4
    foot = ctrl.pose.bones['CTRL_leg_l']
    toe_pivot = ctrl.pose.bones['MCH_leg_l_toe'].head.copy()
    toe_before = arm.pose.bones['asi4_l_n'].matrix.copy()
    foot['roll'] = 0.5
    rig.refresh(ctrl)
    assert (ctrl.pose.bones['MCH_leg_l_toe'].head - toe_pivot).length < 1e-5
    turned = toe_before.to_quaternion().rotation_difference(
        arm.pose.bones['asi4_l_n'].matrix.to_quaternion()).angle
    assert abs(turned - 0.5) < 1e-3, turned
    foot['roll'] = 0.0
    rig.refresh(ctrl)

    # Converting the FK clip keys goals/poles; sampled poses are preserved
    # and both feet then stay planted while CTRL_body moves.
    rig.convert_to_ik(ctrl, 1, 31)
    assert all(ctrl['bd_ik_' + limb] == 1.0 for limb in rig.LIMBS)
    converted = snapshot(arm, (1, 10, 20, 30))
    for frame in (1, 10, 20, 30):
        assert max_diff(before[frame], converted[frame]) < 5e-4, frame
    bpy.context.scene.frame_set(20)
    bpy.context.view_layer.update()
    ankles = {n: arm.pose.bones[n].matrix.translation.copy()
              for n in ('asi3_l_n', 'asi3_r_n')}
    body = ctrl.pose.bones['CTRL_body']
    moved = body.matrix.copy()
    moved.translation.x += 0.15
    body.matrix = moved
    rig.refresh(ctrl)
    pelvis_dx = arm.pose.bones['center_c_n'].matrix.translation.x - \
        converted[20]['center_c_n'].translation.x
    assert abs(pelvis_dx) > 0.1
    for name, point in ankles.items():
        drift = (arm.pose.bones[name].matrix.translation - point).length
        assert drift < 1e-3, (name, drift)
    body.matrix_basis.identity()
    rig.refresh(ctrl)

    # Optional helper sharing is finite, small here and fully reversible.
    base = {pb.name: pb.matrix.copy() for pb in arm.pose.bones}
    ctrl['bd_helpers'] = 1.0
    rig.refresh(ctrl)
    assert all(math.isfinite(v) for pb in arm.pose.bones
               for row in pb.matrix for v in row)
    ctrl['bd_helpers'] = 0.0
    rig.refresh(ctrl)
    assert max_diff(base, {pb.name: pb.matrix.copy() for pb in arm.pose.bones}) < 1e-5

    # Shoulder controls: FK-follow by default, a manual offset moves the
    # clavicle/arm root, and switching back restores the pose exactly.
    bpy.context.scene.frame_set(20)
    bpy.context.view_layer.update()
    # Conversion leaves the clavicles keyed on (that is how the clip's
    # clavicle motion survives in IK); switch them off for FK follow.
    ctrl['bd_shoulder_l'] = 0.0
    ctrl['bd_shoulder_r'] = 0.0
    rig.refresh(ctrl)
    base = {pb.name: pb.matrix.copy() for pb in arm.pose.bones}
    rig.match_shoulder(ctrl, 'shoulder_l', key_switch=False)
    rig.refresh(ctrl)
    assert max_diff(base, {pb.name: pb.matrix.copy() for pb in arm.pose.bones}) < 5e-4
    ctl = ctrl.pose.bones['CTRL_shoulder_l']
    ctl.matrix_basis = Matrix.Rotation(0.5, 4, Vector((0, 0, 1))) @ ctl.matrix_basis
    rig.refresh(ctrl)
    moved = (arm.pose.bones['ude1_l_n'].matrix.translation -
             base['ude1_l_n'].translation).length
    assert moved > 0.02, moved
    rig.match_shoulder(ctrl, 'shoulder_l', key_switch=False)
    ctrl['bd_shoulder_l'] = 0.0
    rig.refresh(ctrl)
    assert max_diff(base, {pb.name: pb.matrix.copy() for pb in arm.pose.bones}) < 1e-5

    # Shoulder assist: raising the hand in IK lifts the clavicle with
    # the arm; with the assist at zero the clavicle stays put.
    hand = ctrl.pose.bones['CTRL_arm_l']
    hand_home = hand.matrix.copy()
    raised = hand_home.copy()
    raised.translation.y += 0.65
    raised.translation.z += 0.2
    hand.matrix = raised
    rig.refresh(ctrl)
    kata_on = max_diff({'kata_l_n': base['kata_l_n']},
                       {'kata_l_n': arm.pose.bones['kata_l_n'].matrix})
    ctrl['bd_shoulder_assist'] = 0.0
    rig.refresh(ctrl)
    kata_off = max_diff({'kata_l_n': base['kata_l_n']},
                        {'kata_l_n': arm.pose.bones['kata_l_n'].matrix})
    assert kata_on > kata_off + 0.01, (kata_on, kata_off)
    ctrl['bd_shoulder_assist'] = 0.35
    hand.matrix = hand_home
    rig.refresh(ctrl)

    # Joint limits clamp an extreme IK demand (deep knee fold) while
    # segment lengths stay rigid; disabling limits restores full range.
    def knee_flexion():
        j0 = arm.pose.bones['asi1_l_n'].matrix.translation
        j1 = arm.pose.bones['asi2_l_n'].matrix.translation
        j2 = arm.pose.bones['asi3_l_n'].matrix.translation
        v1 = (j1 - j0).normalized()
        v2 = (j2 - j1).normalized()
        return math.degrees(math.acos(max(-1.0, min(1.0, v1.dot(v2)))))

    bpy.context.scene.frame_set(20)
    bpy.context.view_layer.update()
    rig.match(ctrl, 'leg_l', 'IK')
    hip = arm.pose.bones['asi1_l_n'].matrix.translation.copy()
    foot = ctrl.pose.bones['CTRL_leg_l']
    fm = foot.matrix.copy()
    fm.translation = hip + Vector((0.03, -0.06, 0.04))
    foot.matrix = fm
    ctrl['bd_limits'] = 0.0
    rig.refresh(ctrl)
    free = knee_flexion()
    ctrl['bd_limits'] = 1.0
    rig.refresh(ctrl)
    clamped = knee_flexion()
    assert free > 150.0 and clamped < free - 10.0 and clamped < 165.0, (free, clamped)
    assert segment_error(arm) < 5e-4
    rig.match(ctrl, 'leg_l', 'FK')

    # Bake reproduces the live rig on the original skeleton, as a new Action.
    bpy.context.scene.frame_set(20)
    bpy.context.view_layer.update()
    live = snapshot(arm, (1, 10, 20, 30))
    baked = rig.bake(arm, 1, 31)
    assert arm.animation_data.action is baked and baked is not original
    assert ctrl['bd_enabled'] == 0.0 and original.use_fake_user
    assert action_coords(original) == original_coords
    baked_pose = snapshot(arm, (1, 10, 20, 30))
    for frame in (1, 10, 20, 30):
        assert max_diff(live[frame], baked_pose[frame]) < 5e-4, frame
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        assert segment_error(arm) < 5e-4, frame

    # Persistence, then clean removal.
    bpy.ops.wm.save_as_mainfile(filepath='/tmp/bd_control_rig_test.blend')
    bpy.ops.wm.open_mainfile(filepath='/tmp/bd_control_rig_test.blend')
    arm = next(o for o in bpy.data.objects if o.get('gmd_source'))
    ctrl = arm['bd_control_rig']
    assert ctrl and ctrl.get('bd_control_kind') == 'animator_v1'
    rig.remove(arm)
    assert not arm.get('bd_control_rig')
    assert not any(c.name.startswith(rig.PREFIX)
                   for pb in arm.pose.bones for c in pb.constraints)
    assert not any(o.get('bd_control_kind') == 'animator_v1'
                   for o in bpy.data.objects)
    assert arm.animation_data.action is not None

    # Rest pose regression: GMD bind legs are perfectly straight, which
    # is the pole-IK singularity.  Matching IK there must still let the
    # foot control lift the foot, bending the knee toward the pole
    # (forward, +Z), and CTRL_body must crouch without flipping legs.
    arm.animation_data.action = None
    for pb in arm.pose.bones:
        pb.matrix_basis.identity()
    bpy.context.view_layer.update()
    ctrl = rig.create(arm)
    rig.match(ctrl, 'leg_l', 'IK', key=False)
    rig.match(ctrl, 'leg_r', 'IK', key=False)
    ankle0 = arm.pose.bones['asi3_l_n'].matrix.translation.copy()
    foot = ctrl.pose.bones['CTRL_leg_l']
    fm = foot.matrix.copy()
    fm.translation.y += 0.25
    foot.matrix = fm
    rig.refresh(ctrl)
    ankle1 = arm.pose.bones['asi3_l_n'].matrix.translation.copy()
    assert (ankle1 - ankle0).y > 0.15, (ankle0, ankle1)
    hip = arm.pose.bones['asi1_l_n'].matrix.translation.copy()
    knee = arm.pose.bones['asi2_l_n'].matrix.translation.copy()
    axis = (ankle1 - hip).normalized()
    side = (knee - hip) - axis * (knee - hip).dot(axis)
    assert side.z > 0.05, side  # knee bends forward, never flips back
    foot.matrix_basis.identity()
    rig.refresh(ctrl)
    feet = {s: arm.pose.bones[f'asi3_{s}_n'].matrix.translation.copy()
            for s in 'lr'}
    body = ctrl.pose.bones['CTRL_body']
    bm = body.matrix.copy()
    bm.translation.y -= 0.3
    body.matrix = bm
    rig.refresh(ctrl)
    for s in 'lr':
        drift = (arm.pose.bones[f'asi3_{s}_n'].matrix.translation - feet[s]).length
        assert drift < 1e-3, (s, drift)
        knee = arm.pose.bones[f'asi2_{s}_n'].matrix.translation.copy()
        assert knee.z > 0.1, (s, knee)  # knees forward in the crouch
    # A rig saved by an older addon version keeps its limit values as
    # data; if those pin the chain, Match IK must recalibrate in place.
    body.matrix_basis.identity()
    rig.refresh(ctrl)
    for i in (0, 1):
        pb = ctrl.pose.bones[f'MCH_leg_l_{i}']
        for ax in 'xyz':
            setattr(pb, f'ik_min_{ax}', -0.05)
            setattr(pb, f'ik_max_{ax}', 0.05)
    rig.refresh(ctrl)
    ankle0 = arm.pose.bones['asi3_l_n'].matrix.translation.copy()
    fm = foot.matrix.copy()
    fm.translation.y += 0.25
    foot.matrix = fm
    rig.refresh(ctrl)
    stuck = (arm.pose.bones['asi3_l_n'].matrix.translation - ankle0).y
    assert stuck < 0.05, stuck  # legacy limits really do pin the leg
    rig.match(ctrl, 'leg_l', 'IK', key=False)
    ankle0 = arm.pose.bones['asi3_l_n'].matrix.translation.copy()
    fm = foot.matrix.copy()
    fm.translation.y += 0.25
    foot.matrix = fm
    rig.refresh(ctrl)
    lifted = (arm.pose.bones['asi3_l_n'].matrix.translation - ankle0).y
    assert lifted > 0.15, lifted  # recalibrated limits free the chain
    path = 'pose.bones["MCH_leg_l_0"].use_ik_limit_x'
    assert sum(1 for d in ctrl.animation_data.drivers
               if d.data_path == path) == 1  # no stacked duplicate drivers
    # Wrist: translating the hand control must not bend the wrist (the
    # hand follows the forearm); rotating the control adds the offset.
    import math as _math
    def _wq(n1, n2):
        return arm.pose.bones[n1].matrix.to_quaternion().rotation_difference(
            arm.pose.bones[n2].matrix.to_quaternion()).angle
    rig.match(ctrl, 'arm_l', 'IK', key=False)
    wrist0 = _wq('ude2_l_n', 'ude3_l_n')
    hand_ctrl = ctrl.pose.bones['CTRL_arm_l']
    hm = hand_ctrl.matrix.copy()
    hm.translation.y += 0.2
    hm.translation.z += 0.15
    hand_ctrl.matrix = hm
    rig.refresh(ctrl)
    assert abs(_wq('ude2_l_n', 'ude3_l_n') - wrist0) < 0.1, wrist0
    hand_ctrl.matrix_basis = hand_ctrl.matrix_basis @ Matrix.Rotation(
        0.6, 4, Vector((1, 0, 0)))
    rig.refresh(ctrl)
    assert abs(_wq('ude2_l_n', 'ude3_l_n') - wrist0) > 0.15
    hand_ctrl.matrix_basis = hand_ctrl.matrix_basis @ Matrix.Rotation(
        -0.6, 4, Vector((1, 0, 0)))
    rig.refresh(ctrl)

    # A twisted IK state (rolled spine solution saved by an older
    # version) must not become its own reference: Match IK reads the FK
    # bases and restores the untwisted pose.
    ctrl['bd_ik_spine'] = 0.0
    rig.refresh(ctrl)
    mune_fk = arm.pose.bones['mune_c_n'].matrix.copy()
    ctrl['bd_ik_spine'] = 1.0
    mech = ctrl.pose.bones['MCH_spine_1']
    mech.matrix_basis = mech.matrix_basis @ Matrix.Rotation(
        1.9, 4, Vector((0, 1, 0)))
    rig.refresh(ctrl)
    twisted = max(abs(mune_fk[i][j] - arm.pose.bones['mune_c_n'].matrix[i][j])
                  for i in range(4) for j in range(4))
    assert twisted > 0.05, twisted  # the corruption really shows
    rig.match(ctrl, 'spine', 'IK', key=False)
    rig.refresh(ctrl)
    healed = max(abs(mune_fk[i][j] - arm.pose.bones['mune_c_n'].matrix[i][j])
                 for i in range(4) for j in range(4))
    assert healed < 0.05 and healed < twisted * 0.25, (twisted, healed)
    rig.remove(arm)
    print('PASS: FK preservation, IK match/convert, planted feet, foot roll, '
          'helpers, bake, save/reopen, removal, rest-pose IK, limit repair',
          flush=True)


if __name__ == '__main__':
    try:
        if Path(GMD).exists() and Path(MMO).exists():
            fixture_test()
        else:
            print('SKIP: local game fixtures absent', flush=True)
    except Exception:
        traceback.print_exc()
        os._exit(1)
    os._exit(0)
