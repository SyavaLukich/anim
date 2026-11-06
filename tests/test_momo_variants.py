"""Headless Blender integration: decoded expressions on all five GMD variants.
Usage: python tests/test_momo_variants.py /path/to/Fey/files
"""
import os
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    import bpy
    import io_binary_domain as addon
    root = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/extract/out')
    addon.register()
    paths = sorted(root.glob('c_awmn_fey*.gmd'))
    if len(paths) != 5:
        raise RuntimeError('Expected all five Fey GMD variants in %s' % root)
    for path in paths:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        addon.load_gmd(bpy.context, str(path))
        arm = next(o for o in bpy.data.objects if o.type == 'ARMATURE')
        bpy.context.view_layer.objects.active = arm
        assert addon.load_momo_actions(arm, str(root / 'f_man_pattern_fey.mmo')) == 95
        for expression in ('f_man_fey__eye_close', 'f_man_fey__open_a'):
            assert bpy.ops.bdomain.apply_clip(name=expression) == {'FINISHED'}
            bones = ('_eyelid_l_n', '_eyelid_r_n') if 'eye_close' in expression else ('_jaw_c_n',)
            for name in bones:
                pb = arm.pose.bones[name]
                delta = pb.matrix.to_3x3() @ pb.bone.matrix_local.to_3x3().inverted()
                assert delta.to_quaternion().angle > 0.15, (path.name, expression, name)
        assert bpy.ops.bdomain.preview_momo() == {'FINISHED'}
        bpy.context.scene.frame_set(16)
        bpy.context.view_layer.update()
        assert arm.pose.bones['_jaw_c_n'].matrix_basis.to_quaternion().angle > .1
        print(path.name, '95 expressions, bilateral eyelids, jaw and preview: PASS', flush=True)


if __name__ == '__main__':
    code = 0
    try:
        main()
    except Exception:
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
