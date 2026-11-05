"""Blender-side IK mathematics and optional local-game-data bake regression."""
import json
import math
import os
from pathlib import Path
import sys
import traceback
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bpy
from mathutils import Vector, Matrix
import io_binary_domain as addon
from io_binary_domain import body_corrections as c, mmo


class CorrectionMath(unittest.TestCase):
    def test_reachable_and_unreachable(self):
        a,b,end = map(Vector, ((0,0,0), (.5,-.5,0), (0,-1,0)))
        for goal in map(Vector, ((.2,-.9,.2), (0,-10,0), (0,0,0), (0,-1,0))):
            middle, result, limited = c.two_bone(a,b,end,goal)
            self.assertAlmostEqual((middle-a).length, (b-a).length, places=5)
            self.assertAlmostEqual((result-middle).length, (end-b).length, places=5)
            if not limited:
                self.assertLess((result-goal).length, 1e-5)
            self.assertTrue(all(math.isfinite(v) for v in (*middle, *result)))
        with self.assertRaises(ValueError):
            c.two_bone(a,a,end,Vector((1,0,0)))

    def test_straight_and_pole_stability(self):
        a,b,end = map(Vector, ((0,0,0), (0,-1,0), (0,-2,0)))
        middle, result, limited = c.two_bone(a,b,end,Vector((.1,-1.5,0)), Vector((0,0,1)))
        self.assertGreater(middle.z, 0)
        self.assertLess((result-Vector((.1,-1.5,0))).length, 1e-5)

    def test_contact_parser(self):
        self.assertIsNone(c.parse_contacts('',50))
        self.assertEqual(c.parse_contacts('1-12, 30-42',50),[(0,11),(29,41)])
        for text in ('0-5','5-1','1-60','1-10, 9-12','abc','1','2-3, 1-1'):
            with self.assertRaises(ValueError): c.parse_contacts(text,50)

    def test_air_and_stance_detection(self):
        planted = [Vector((0,.08,0)) for _ in range(10)]
        self.assertEqual(c.detect_contacts(planted,30,0,.08,.02,.1), [(0,9)])
        air = [Vector((0,1,0)) for _ in range(10)]
        self.assertEqual(c.detect_contacts(air,30,0,.08,.02,.1), [])
        moving = [Vector((i*.1,.08,0)) for i in range(10)]
        self.assertEqual(c.detect_contacts(moving,30,0,.08,.02,.1), [])

    def test_lock_plateau_and_blend(self):
        positions = [Vector((i*.002,.09,0)) for i in range(12)]
        rotations = [Matrix.Identity(3).to_quaternion() for _ in positions]
        targets, plateau = c.lock_targets(positions,rotations,[(0,11)],0,.08,2)
        self.assertEqual(plateau,[(2,9)])
        self.assertEqual(targets[0][0], positions[0])
        for i in range(2,10): self.assertLess((targets[i][0]-targets[2][0]).length,1e-7)
        self.assertAlmostEqual(targets[2][0].y,.08)

    def test_helper_blend_is_bind_relative(self):
        rest = {n: Matrix.Identity(4) for n in ('a','b','elbow_l_sup')}
        rest['elbow_l_sup'].translation = (.1,.2,.3)
        worlds = {n: mat.copy() for n,mat in rest.items()}
        worlds['b'] = Matrix.Rotation(math.pi/2,4,'Y')
        ops = [dict(target=2,drivers=[0,1,-1,-1])]
        result, names = c.blend_helpers(worlds,dict(a=None,b=None,elbow_l_sup=None),
                                        rest,ops,['a','b','elbow_l_sup'])
        self.assertEqual(names,['elbow_l_sup'])
        self.assertLess((result['elbow_l_sup'].translation-rest['elbow_l_sup'].translation).length,1e-7)
        self.assertAlmostEqual(result['elbow_l_sup'].to_quaternion().angle,math.pi/4,places=6)


def coordinates(action):
    curves = action.layers[0].strips[0].channelbag(action.slots[0]).fcurves
    return [(f.data_path,f.array_index,[(p.co.x,p.co.y) for p in f.keyframe_points]) for f in curves]


def fixture_test():
    addon.register()
    rig = mmo.parse_moma(Path('io_binary_domain/rigs/p_man.mma').read_bytes())
    assert rig['anchors'] == [[3,1],[17,15],[22,20],[29,27],[40,38]]
    assert len(rig['operations']) == 24
    bpy.ops.wm.read_factory_settings(use_empty=True)
    addon.load_gmd(bpy.context,'/tmp/extract/out/c_awmn_fey_naked.gmd');arm=bpy.context.object
    addon.load_momo_actions(arm,'/tmp/motion_samples/p_man_fay_whan.mmo')
    bpy.ops.bdomain.apply_clip(name='s_Whan_walkA_slow_f')
    original=arm.animation_data.action; before=coordinates(original)
    bpy.context.scene.frame_set(13)
    corrected=addon.bake_body_corrections(arm,helpers=True)
    assert arm.animation_data.action==corrected and corrected!=original
    assert coordinates(original)==before
    assert bpy.context.scene.frame_current==13
    report=json.loads(corrected['gmd_body_corrections'])
    assert report['reach_clamps']==0 and report['max_lock_goal_error']<2e-6
    assert report['contacts']['l'] and report['contacts']['r'] and report['helpers']
    points={}
    for frame in range(1,52):
        bpy.context.scene.frame_set(frame);bpy.context.view_layer.update()
        for pb in arm.pose.bones:
            assert all(math.isfinite(v) for row in pb.matrix for v in row)
            if pb.parent:
                actual=(pb.matrix.translation-pb.parent.matrix.translation).length
                rest=(pb.bone.head_local-pb.parent.bone.head_local).length
                assert abs(actual-rest)<5e-5,(frame,pb.name,actual,rest)
        for side in ('l','r'):
            points[side,frame]=arm.pose.bones['asi3_%s_n'%side].matrix.translation.copy()
    for side,ranges in report['plateaus'].items():
        for first,last in ranges:
            drift=max((points[side,f]-points[side,first]).length for f in range(first,last+1))
            assert drift<4e-6,(side,first,last,drift)
    try: addon.bake_body_corrections(arm)
    except addon.GmdExportError: pass
    else: raise AssertionError('stacked correction accepted')
    bpy.ops.bdomain.apply_clip(name='s_Whan_walkA_slow_f')
    assert arm.animation_data.action==original
    oldcount=len(bpy.data.actions)
    try: addon.bake_body_corrections(arm,left_contacts='1-999')
    except ValueError: pass
    else: raise AssertionError('invalid contacts accepted')
    assert len(bpy.data.actions)==oldcount and arm.animation_data.action==original
    # Foot-lock-only reuses a saved Action without access to source MMO.
    oldpath=arm['gmd_momo'];arm['gmd_momo']='/tmp/absent.mmo'
    fallback=addon.bake_body_corrections(arm,source_ik=False,helpers=False,left_contacts='3-24',right_contacts='27-51')
    assert fallback!=original
    addon._assign_momo_action(arm,corrected);arm['gmd_momo']=oldpath
    bpy.ops.wm.save_as_mainfile(filepath='/tmp/bd_corrected_test.blend')
    bpy.ops.wm.open_mainfile(filepath='/tmp/bd_corrected_test.blend')
    arm=bpy.context.object
    assert arm.animation_data.action.get('gmd_body_corrections')
    bpy.context.scene.frame_set(12);bpy.context.view_layer.update()
    assert (arm.pose.bones['asi3_l_n'].matrix.translation-points['l',12]).length<4e-6
    print('PASS: separate Action, original unchanged, all lengths, planted feet, helpers, rollback, source-free locking, persistence',flush=True)


if __name__=='__main__':
    try:
        result=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(CorrectionMath))
        if not result.wasSuccessful(): os._exit(1)
        if Path('/tmp/motion_samples/p_man_fay_whan.mmo').exists(): fixture_test()
    except Exception:
        traceback.print_exc();os._exit(1)
    os._exit(0)
