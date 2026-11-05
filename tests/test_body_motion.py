"""Real-data native-timeline body motion regression (local fixtures only)."""
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


def main():
    addon.register()
    path = '/tmp/motion_samples/p_man_adventure.mmo'
    data = Path(path).read_bytes()
    rig = mmo.parse_moma(Path('/tmp/motion_samples/p_man.mma').read_bytes())
    clips = {c.name: c for c in mmo.parse_momo(data, decode_tracks=False)
             if c.name in addon.BODY_MOTION_CLIPS}
    assert {n: c.frames for n, c in clips.items()} == {'p_Wnon_std_nml': 80, 'p_Wnon_std_nml_01': 200}
    for c in clips.values():
        assert c.fps == 30
        mmo.decode_clip(data, c)
        raw = bytearray(data)
        mmo.write_track_values(raw, c, [t.value for t in c.tracks])
        assert bytes(raw) == data
    for model in sorted(Path('/tmp/extract/out').glob('c_awmn_fey*.gmd')):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        addon.load_gmd(bpy.context, str(model)); arm = bpy.context.object
        assert addon.load_momo_actions(arm, path) == 2
        assert len(bpy.data.actions) == 1  # no eager baking of both motions
        assert len(arm.bdomain_momo_clips) == 2
        assert arm['gmd_momo_total_clips'] == 40
        assert bpy.context.scene.frame_end == 81
        mesh = next(o for o in bpy.data.objects if o.type == 'MESH' and len(o.data.vertices) > 500)
        def coords():
            ob = mesh.evaluated_get(bpy.context.evaluated_depsgraph_get()); me = ob.to_mesh()
            result = [v.co.copy() for v in me.vertices]; ob.to_mesh_clear(); return result
        first = coords()
        for name, c in clips.items():
            assert bpy.ops.bdomain.apply_clip(name=name) == {'FINISHED'}
            action = arm.animation_data.action
            assert action['gmd_momo_kind'] == 'body_motion'
            assert action['gmd_timing'] == 'source_timeline'
            assert bpy.context.scene.render.fps == 30
            assert bpy.context.scene.frame_end == c.frames + 1
            reference = None
            changed = False
            for frame in (1, 21, c.frames//2+1, c.frames+1):
                bpy.context.scene.frame_set(frame); bpy.context.view_layer.update()
                bases = addon._body_bases(arm, c, rig, frame=frame-1)
                for p in arm.pose.bones:
                    err = max(abs(p.matrix_basis[i][j]-bases[p.name][i][j]) for i in range(4) for j in range(4))
                    assert err < 4e-5, (model.name, name, frame, p.name, err)
                    if p.parent:
                        actual = (p.matrix.translation-p.parent.matrix.translation).length
                        rest = (p.bone.head_local-p.parent.bone.head_local).length
                        assert abs(actual-rest) < 3e-5
                    assert all(math.isfinite(v) for row in p.matrix for v in row)
                spine = arm.pose.bones['mune_c_n'].matrix.copy()
                if reference is None:
                    reference = spine
                changed |= max(abs(spine[i][j]-reference[i][j]) for i in range(4) for j in range(4)) > .001
            assert changed, 'motion must not be a static pose'
        bpy.context.scene.frame_set(61); bpy.context.view_layer.update()
        assert sum((a-b).length > .0001 for a,b in zip(first,coords())) > 10
        print(model.name, 'two genuine motions; native timeline; body transforms/lengths/geometry PASS', flush=True)
    current = arm.animation_data.action
    addon._cleanup_unused_momo_actions()
    assert arm.animation_data.action == current
    bpy.ops.wm.save_as_mainfile(filepath='/tmp/bd_body_motion.blend')
    bpy.ops.wm.open_mainfile(filepath='/tmp/bd_body_motion.blend'); arm=bpy.context.object
    arm['gmd_momo']='/tmp/missing.mmo'
    assert bpy.ops.bdomain.apply_clip(name='p_Wnon_std_nml_01') == {'FINISHED'}
    bpy.context.scene.frame_set(61); bpy.context.view_layer.update()
    assert arm.animation_data.action['gmd_momo_kind']=='body_motion'
    print('PASS: native body timeline, real movement, all five models, cleanup and saved Action',flush=True)

if __name__=='__main__':
    try:
        main()
    except Exception:
        traceback.print_exc(); os._exit(1)
    os._exit(0)
