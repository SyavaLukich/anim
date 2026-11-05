"""Optional real-data Blender regression: large catalogs and saved lazy clips.

LD_LIBRARY_PATH=/home/user/xlibs python3 tests/test_momo_catalog.py
Fixtures are local game extractions, not distributed with the addon.
"""
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bpy
import io_binary_domain as addon
from io_binary_domain import mmo

addon.register()
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gmd(filepath='/tmp/extract/out/c_awmn_fey_naked.gmd')
arm = bpy.context.object
for filename, expected in [('f_man_all.mmo', 4032), ('f_man_motionviewer.mmo', 3990)]:
    path = '/tmp/motion_samples/' + filename
    data = bytearray(Path(path).read_bytes())
    clips = mmo.parse_momo(data, decode_tracks=False)
    assert len(clips) == expected and all(c.tracks is None for c in clips)
    # Validate every channel and byte-exact unchanged patch, not just first clip.
    original = bytes(data)
    for clip in clips:
        mmo.decode_clip(data, clip)
        assert len(mmo.pose_nodes(clip)) == 46
        mmo.write_track_values(data, clip, [t.value for t in clip.tracks])
        clip.tracks = None
    assert bytes(data) == original
    before = len(bpy.data.actions)
    start = time.perf_counter()
    assert addon.load_momo_actions(arm, path) == expected
    elapsed = time.perf_counter() - start
    assert len(bpy.data.actions) - before == 1
    assert len(json.loads(arm['gmd_clips'])) == expected
    assert len(json.loads(arm['gmd_clip_actions'])) == 1
    print(filename, 'indexed', expected, 'clips; one Action; seconds:', elapsed, flush=True)
    # Choose an actual Fey closure when available; otherwise a non-first clip.
    names = json.loads(arm['gmd_clips'])
    name = next((n for n in names if 'fey' in n and 'eye_close' in n), names[-1])
    assert bpy.ops.bdomain.apply_clip(name=name) == {'FINISHED'}
    assert len(bpy.data.actions) - before == 2
    assert arm.animation_data.action['gmd_expression'] == name
    assert bpy.ops.bdomain.apply_clip(name=name) == {'FINISHED'}
    assert len(bpy.data.actions) - before == 2  # cached Action reused
    print('selected:', name, flush=True)

# Unknown body rig must fail clearly and must not damage the current Action.
previous = arm.animation_data.action
try:
    addon.load_momo_actions(arm, '/tmp/motion_samples/p_gen_basic.mmo')
except addon.GmdExportError as exc:
    assert 'Body-rig retargeting' in str(exc)
else:
    raise AssertionError('body file incorrectly treated as face expressions')
assert arm.animation_data.action == previous
body = bytearray(Path('/tmp/motion_samples/p_gen_basic.mmo').read_bytes())
clips = mmo.parse_momo(body)
assert len(clips) == 1 and clips[0].name == 'p_gen_tpose'
assert len(clips[0].tracks) == 644
original = bytes(body)
mmo.write_track_values(body, clips[0], [t.value for t in clips[0].tracks])
assert bytes(body) == original
rig = mmo.parse_moma(Path('/tmp/motion_samples/p_gen.mma').read_bytes())
assert len(rig['nodes']) == 161 and rig['channel_count'] == 636
assert rig['node_flags'].count(6) == 2

# Catalog, rig metadata and loaded Actions survive restart. A previously
# unloaded clip can still be created; missing-source errors preserve the pose.
bpy.ops.wm.save_as_mainfile(filepath='/tmp/bd_momo_catalog.blend')
bpy.ops.wm.open_mainfile(filepath='/tmp/bd_momo_catalog.blend')
arm = bpy.context.object
names = json.loads(arm['gmd_clips'])
mapping = json.loads(arm['gmd_clip_actions'])
unloaded = next(n for n in names if n not in mapping)
before = len(bpy.data.actions)
assert bpy.ops.bdomain.apply_clip(name=unloaded) == {'FINISHED'}
assert len(bpy.data.actions) == before + 1
previous = arm.animation_data.action
source = arm['gmd_momo']
arm['gmd_momo'] = '/tmp/does_not_exist.mmo'
try:
    addon._load_catalog_expression(arm, unloaded)
except addon.GmdExportError as exc:
    assert 'Source MOMO' in str(exc)
else:
    raise AssertionError('missing source accepted')
assert arm.animation_data.action == previous
arm['gmd_momo'] = source
print('PASS: both catalogs, all-channel exact patches, selection, reload, body guard', flush=True)
os._exit(0)
