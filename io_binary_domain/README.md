# Binary Domain Blender addon — v0.4.0

Import Binary Domain GSGM (`.gmd`) models and decode the constant facial
expressions in `f_man_pattern_fey.mmo` into actual bone transforms.

## Install / update

Download `binary_domain_addon.zip`, install from disk in Blender Preferences,
enable **Binary Domain: GMD / MOMO**, then restart Blender.

**Old scenes need a skinning migration.** Earlier versions assigned skin groups
with an off-by-one palette error. Either re-import the GMD in v0.2.0 or later, or:

1. Save a backup and select the old armature in **Object Mode**.
2. Open **N > Binary Domain > Repair old skin groups** and confirm.
3. Load the original MOMO again to create decoded expression Actions.

Repair restores original bone weights from the GMD at `gmd_source`, replacing
any bone-weight edits. UVs, materials, mesh coordinates and existing Actions
are kept. All records/topology are checked before modifying groups. The
original GMD must still be available at its stored path. Until repaired,
old armatures are rejected by the decoded expression importer.

Locally tested with `bpy` / Blender **4.5.15**. The user's Blender 5.1 GUI has
not been tested locally. Action slots are explicitly assigned for 4.4+.

## Model import

1. `File > Import > Binary Domain Model (.gmd)`.
2. Keep DDS textures in the same directory as the model.
3. Select the imported armature. The armature carries the game-Y-up to
   Blender-Z-up rotation; child meshes inherit it. Do not apply this rotation.

One mesh object is retained per game draw record. UV-less, texture-less
auxiliary records are hidden, not deleted. Overlapping game draw geometry
and shader variants are not automatically merged. Materials provide a basic
diffuse preview, not a full recreation of the game shaders. Packed normal
and mask maps are retained as nodes without speculative connections.

Vertex declarations determine UV layout, not just stride: `0x1fe0ffc3`
has color at +32 and binary16 UV at +36; `0x28e0ffc3` has UV at +32 and opaque
extra data at +36. Only verified `UVMap` is created and explicitly connected
to image nodes. Bone palettes are **count-prefixed**: local slot zero reads
`palette[pal_base + 1]`, not the count at `pal_base`.

## Facial expressions and playback

1. In the 3D View, press **N > Binary Domain > Load MOMO clips**.
2. Choose `f_man_pattern_fey.mmo`. Leave MOMA rig blank to use bundled
   `rigs/f_man.mma`.
3. A visible clip list appears in the panel. Use **Filter** to find e.g.
   `f_man_fey__eye_close`, `f_man_fey__open_a` or `f_man_fey__jaw_down`.
   Select the row and click **Apply selected clip**. The pose changes the face,
   not just custom properties. **Search clips (popup)** remains an alternative.
   In an older saved scene, click **Show clip list (existing scene)** first.
4. For moving playback, click **Preview expression blend**, then press Play.
   Frames **1 → 16 → 31** blend **rest → expression → rest**.

**Important distinction:** this pattern file contains 95 **constant expression
poses**, not 95 time-varying facial performances. The preview transition is
explicitly **generated timing**, not reconstructed source timing. The original
pose Action remains available; select it again before generating another
preview. No phoneme/audio synchronization is implemented.

The old "Experimental Euler bake" guess is removed from the UI. Positions and
quaternions are decoded from the real channel layout. The MOMA parent table
is used: lower lips and chin are children of the jaw (node 32), not directly
of the head. A recorded, automatically estimated uniform scale adapts source
head height to the imported GMD. This retarget scale is a model adaptation,
not part of the verified binary encoding and not a claim of engine-identical
retargeting for arbitrary characters. The head anchor is held at model rest;
the body retargeter below supports the static p_gen T-pose and two verified
p_man idle animations. General body/root-motion blending is not implemented.

### Clip list and memory usage (v0.2.2)

`f_man_all.mmo` contains **4032** static face poses and
`f_man_motionviewer.mmo` contains **3990**, not time-varying performances.
All files now load a searchable **catalog** and create only the first Action. Selecting a different expression decodes that clip
and creates its Action once; selecting it again reuses the Action. This also applies to the original 95 Fey poses. The optional **Load all Actions
(slow)** setting in the MOMO file browser restores eager import when needed.
This avoids allocating millions of FCurves on file load. Retarget scale is
computed separately for each selected source pose.

The catalog, rig metadata and loaded Actions survive saving the `.blend`.
**Keep the original `.mmo` at its stored path**: unloaded poses still need
that file. If the source changes or moves, load its catalog again. Already
loaded Actions remain usable without the source. Old scenes that already
contain thousands of Actions need explicit cleanup:

1. Save a backup of the `.blend`.
2. Click **Clean unused MOMO Actions** in the panel and confirm the dialog.
3. This removes unused addon-tagged MOMO Actions, **including Fake User and
   edited copies with no real users**. Actions assigned to objects or used
   in NLA, and unrelated user-created Actions, are preserved.
4. Unloaded poses can be loaded again from the original MMO. For scenes
   imported before rig/catalog metadata was stored, reload the MMO first.

Loading another MMO intentionally does not silently delete earlier work.
The panel now draws its list from a persistent collection rather than
repeatedly parsing the entire clip-name JSON on every redraw. Playback is
stopped when a new catalog is loaded. These changes reduce addon overhead;
other causes of slow viewports (modifiers, scene complexity, drivers) still
need to be diagnosed in the affected scene.

### Full-body T-pose (v0.3.0)

1. Select an imported **full GMD armature**.
2. **Load MOMO clips** → choose `p_gen_basic.mmo`.
3. Leave **Optional MOMA file** blank: bundled `rigs/p_gen.mma` is selected
   automatically. If supplying a rig manually, select `p_gen.mma`, not `f_man.mma`.
4. The single `p_gen_tpose` clip is applied immediately and is also available
   in the panel list via **Apply selected clip**.

This creates a real bone-transform Action for the body, arms, hands/fingers,
legs and matching rig nodes. It is **one static T-pose**, not a walk cycle.
The body path does not generate a synthetic facial preview. Target bone
parent-offset lengths and root anchor are preserved; model-specific unmapped
bones follow their target parents rather than being moved onto generic rig
positions. No geometry, rest pose, skin weights or materials are overwritten.

The source node/channel mapping comes from MOMA NOD3: 8 reserved zero channels
precede 636 indexed channels. Two flag-6 procedural nodes consume no channels
and are not treated as quaternion nodes. Rotations are retargeted relative
to the target's actual GMD bind quaternions at bone-record `+0x30`, composed
through its hierarchy; these are not the `+0x60` direction/length fields.
New GMD imports store these bind quaternions in the scene. Older scenes read
them from the original `gmd_source`, so keep that GMD available or re-import.
Loaded Actions themselves survive save/reopen without the source files.

Validated on all five supplied Fey variants, including visible mesh movement,
horizontal bilateral arms, every parent-offset length, finite geometry and
save/reopen. Body animations other than the two verified idles below, nonzero reserved controls,
engine procedural-node simulation, and game-side export of this retargeted
body pose are **not validated/supported by this new body path**. A partial
skeleton missing required body joints or a mismatched MOMA fails explicitly.

### Real body animation (v0.4.0)

The existing `motion.par` contains **`p_man_adventure.mmo`**. Two genuine
body idle animations are now supported:

| Clip | Source duration | Source rate | Blender keys |
|---|---:|---:|---:|
| `p_Wnon_std_nml` | 80 frame intervals | 30 FPS | 1–81 |
| `p_Wnon_std_nml_01` | 200 frame intervals | 30 FPS | 1–201 |

Load this `.mmo` on the GMD armature, leaving the MOMA field blank. Bundled
**`p_man.mma`** is selected automatically. The first idle loads immediately;
select the longer idle in the panel and click **Apply selected clip**, then
press **Play**. The scene rate is set to the source 30 FPS. Source frame 0
maps to Blender frame 1, including the source endpoint at 80/200. These are
actual source-motion samples, **not** the generated facial preview.

Only **2 of the 40 clips** in this file are enabled, and the panel says so.
Other clips use additional unimplemented codecs and are not advertised as
supported. This is body idle movement, not walking/running support.
Each selected motion is baked once at native-frame resolution using bulk
FCurve writes and cached as an Action. The other motion remains unloaded.
At native integer frames it follows decoded scalar/SLERP source curves;
Blender's interpolation between those baked frames is linear in channel space.

The 79-node body rig has rotation-only nodes, default local positions from
NDT0, procedural nodes, and absolute/world-space nodes (flag `0x40`). The
retargeter handles these separately rather than using the facial layout.
Target lengths are retained. Root translation is relative to the first source
pose and scaled to target height; auxiliary travel controls must remain zero
for these verified clips. Engine IK, foot locking, procedural helper simulation
and exact generic-rig end-effector positions are not reconstructed. Imported
skin weights, topology, rest pose and materials remain unchanged.

Both clips were tested on all five Fey variants for real mesh movement,
frame-by-frame transform agreement at sample frames, preserved parent-offset
lengths, 30 FPS, save/reopen and reuse without source files. An optional x86
emulator test compares 336 curve evaluations with the game's isolated math
kernels: maximum observed error **4.74e-6**. The game binary and emulator are
not required at addon runtime or included in the addon.

### Exact source data

Each pose/motion Action keeps:

- `gmd_momo_raw_words`: exact channel words, including opcodes and divisors;
- `gmd_source_frames`: source duration metadata;
- `gmd_momo_scale`: the retarget scale used;
- `gmd_momo_kind`: `constant_expression` (or `generated_preview` on previews).

Pose-bone `gmd_c0..2` are now **decoded position components**, not the former
misidentified 10-bit fields. Bone location/quaternion/scale channels drive the
visible expression. All imported Actions have Fake User enabled and survive
saving/reopening. Exact names are mapped per armature, so repeated imports
remain selectable even when Blender adds `.001` suffixes.

Optional MOMO/MOMA paths in the GMD picker are plain text fields: paste paths
there, or load MOMO separately as above. Blender cannot open a second file
picker inside an already open import browser.

## Format findings

TRK2 starts with a **u32 count of 184 channels = 46 × (X, Y, Z, quaternion)**.
Supported constant channel headers:

| Header | Payload | Decoding |
|---|---|---|
| `0020` / `0120` | `s16 base, u16 divisor, s16 fraction` | `base / 32 + fraction / divisor` |
| `0062` / `0162` | `s16 x, y, z, w` | signed normalized quaternion components `/32767` |
| `006e` / `016e` | none | identity quaternion `(0,0,0,1)` |
| `002e` / `012e` | none | zero scalar; cannot expand in a fixed-layout patch |

`7fff` is commonly the scalar divisor, **not a track separator**. `0162` is a
constant quaternion, **not Bezier handles**. The former 139-track interpretation
was incorrect. Every one of the 95 supported clips now consumes its TRK2
payload exactly, as 184 correctly typed channels.

Unknown/time-varying channel opcodes are rejected clearly; they are not
guessed or silently converted into static poses. See `docs/MOMO_FORMAT.md`
for reverse-engineering evidence and scope.

## Export / limitations

GMD export is patch-based and re-reads the original source path. Untouched
import/export of all five tested models is byte-identical. Vertex topology
changes are rejected. Raw weights/index bytes and opaque vertex fields are
preserved. Vertex group edits are not exported while the raw attributes exist.
UV edits use binary16; opaque/unverified words are preserved.

Bone pose export/re-import is tested within this addon, but modified-bone
behavior in the game is **not validated**. This release validates facial
playback in Blender; it does not export new animated MOMO performances.
The pure-Python MOMO patcher preserves untouched channels exactly and supports
fixed-layout constant-channel edits. Expanding an identity quaternion channel
into a longer channel is rejected rather than corrupting the container.

## Tests

Game files are not bundled. For Blender Python with access to the Fey corpus:

```sh
python tests/test_momo_codec.py
python tests/test_roundtrip.py
python tests/test_momo_variants.py /path/to/Fey/files
```

The roundtrip harness currently expects `/tmp/extract/out/`. It verifies all
five untouched GMDs, sampled skin groups against count-prefixed palettes,
UV layer selection, pose export/re-import, all 95 decoded MOMO poses, exact
184-channel identity patching, actual face-mesh deformation, both eyelids,
jaw rotation, preview frames/interpolation, and all 95 Actions after saving
and reopening. Variant tests apply expressions and previews on all five rigs.

Pure Python codec tests cover signed scalars, quaternions, byte-identical and
changed-value patching, malformed/unsupported channels and atomic failure.

Historical diagnostic helpers remain under `tools/` and `tests/`:
`repair_scene_uv.py` restores UVs only; it does **not** fix old skin groups.
Use a fresh v0.2.0 import or the skin-group repair operator for facial playback.

Additional optional game-data regression: `LD_LIBRARY_PATH=/home/user/xlibs python3 tests/test_momo_catalog.py` checks both large libraries, byte-exact patches of every channel, on-demand selection, save/reopen and the body-rig guard. Fixtures are not distributed.

## File paths (v0.2.3)

**Load MOMO clips** uses Blender's standard file-import helper. Select an
actual `.mmo` filename, not just `C:\Binary Domain\motion\binaries\`.
The optional MOMA field takes a full `.mma` filename; leave it blank for
the bundled face rig. Directories are rejected with a message identifying
the MOMO/MOMA field before any file is opened. Spaces, matching pasted
quotes and Blender `//` relative paths are handled. Backslashes are not
blindly replaced or removed (which would damage Windows/UNC paths).
Python exception text can show `\\` for a single actual backslash.
Path checks are covered by `tests/test_input_paths.py` under bpy; native
Windows GUI/filesystem behavior has not been exercised in this Linux sandbox.

Body regression: `LD_LIBRARY_PATH=/home/user/xlibs python3 tests/test_body_retarget.py` (local game fixtures, not distributed).

Temporal tests: `tests/test_body_motion.py` (bpy + local fixtures), `tests/test_temporal_oracle.py` (optional local EXE + pefile/unicorn).
