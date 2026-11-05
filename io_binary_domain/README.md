# Binary Domain Blender addon — v0.7.0

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
the body retargeter below supports the static p_gen T-pose and general
p_man body-motion libraries. Engine animation blending is not implemented.

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
save/reopen. Engine procedural-node simulation and game-side export of
retargeted animation are **not supported**. Nonzero p_gen reserved controls
remain unsupported; p_man auxiliary controls are preserved separately. A partial
skeleton missing required body joints or a mismatched MOMA fails explicitly.

### General human body animation (v0.5.0)

Load **`p_man_fay_whan.mmo`**, `p_man_adventure.mmo`, or another `p_man`
library on the imported GMD armature, leaving the MOMA field blank. The
MTS0 rig declaration selects bundled **`p_man.mma`** automatically. There is
**no clip-name/duration allowlist**: all **203/203** clips in
`p_man_fay_whan.mmo` and **40/40** in `p_man_adventure.mmo` are exposed.
Choose a clip in the filtered list, click **Apply selected clip**, then Play.
Do not use the generated facial-preview button for body animation.

The first Action loads immediately; others are decoded/baked once on selection
and cached. Source frame 0 maps to Blender frame 1, including the duration
endpoint. FPS comes from HED0 (30 throughout the tested corpus); positive
source speed is retained via FPS base. For example, `s_Whan_walkA_slow_f`
bakes frames 1–51 and `s_Whan_std_a_turnL180` frames 1–36.
Native-frame keys use bulk FCurve writes and continuous quaternion signs.
Between baked integer frames Blender interpolates linearly in channel space;
exact arbitrary-subframe engine playback is not claimed.

Supported compression includes constant, dense, sparse, and segmented-run
scalar/quaternion channels, packed and float Euler rotations, and fixed-axis
rotations. Dense rotation curves use the game's cubic-adjusted normalized
interpolant, not an assumed exact SLERP. Sparse/run curves use shortest-path
SLERP. Original raw words, HED0, PRG2 and the three auxiliary scalar timelines
are retained in Action properties. Auxiliary values are **not guessed to be
XYZ travel** or used to simulate engine procedural controls.

The separate **`moo_centroid`** supplies locomotion and global yaw. It is
composed with the model-space skeleton after resolving the MOMA hierarchy
and world-space flags. Horizontal origin is relative to the first source
frame; vertical position is relative to the MOMA bind height, preserving
crouches and airborne motion. Motion scale uses bind height, not the possibly
crouched first-frame height. Target bone lengths/proportions remain unchanged.
Full 85-channel clips and truncated 55-/54-channel prefixes are supported:
missing finger channels retain target bind offsets and inherit wrist motion;
the 54-channel T-pose also omits the final weapon quaternion (identity).
No missing finger animation is invented.

**Coverage:** all 19,739 clip entries (6,765 distinct names, including repeated
aggregate libraries) from **106 nonempty `p_man*.mmo` libraries** in the local
`motion.par` decode and map onto the source rig at three sample times. Ten
other files contain no embedded RAW0 clips. All 203 Fey handgun clips were
retargeted onto the naked Fey model at start/middle/end, checking finite
transforms and every parent-offset length. Baked idle, 180-degree turn,
walking, and 55-channel Actions match direct evaluation. Walking root travel,
turn yaw, 54-channel T-pose, Euler codecs and save/reopen are tested separately.
The historical two-idle/five-model and static/face/GMD tests remain regressions.

An optional Unicorn executable oracle verifies **1,055 real-data evaluations**
(maximum error **2.67e-6**) plus **448 synthetic run-table evaluations** across
all eight width-flag combinations, wide offsets and gaps, plus **70 synthetic
scalar evaluations** for byte padding, sparse times and both time rates. This is mathematical
kernel validation, not an engine screenshot comparison. Game files and emulator
packages are not shipped or required at runtime.

This is general **human `p_man`** support, not a claim to support every creature,
robot, custom rig, or every possible MMO encoding. Unknown layouts/codecs fail
explicitly. Engine IK, foot locking, animation blending and procedural helper
simulation are not reconstructed; differing proportions may cause foot sliding.
Topology, skin weights, rest pose and materials are not overwritten. Animation
export to the game is not implemented.

### Optional IK / foot stabilization (v0.6.0)

**This is Blender-side adaptation, NOT an exact port of the game's IK,
foot-contact controller or OPE0 procedural operators.** The raw import path
remains unchanged. There are no live constraints, frame handlers or hidden
changes to the GMD/rest pose.

1. Apply an original `p_man` clip and select its GMD armature.
2. Click **Bake body corrections (new Action)** in the Binary Domain panel.
3. Choose the correction options and confirm. The new `__corrected` Action
   becomes active. The original Action and its keys are retained.
4. To return to the uncorrected animation, apply the original clip in the list.
   The corrected Action remains available in Blender's Action selector.

Options:

- **Source anchor IK:** requires the matching source MMO. Reads MOMA `ANC0`
  chain endpoints and the decoded source positions. Analytic two-bone solving
  adjusts the two leg chains and, optionally, the two arm chains. Bone-head
  distances, not Blender's short display tails, define segment lengths. Goals
  use source motion/bind-height scaling. The spine anchor is not solved.
  The current pose supplies the bend plane; a deterministic fallback handles
  straight/folded chains. Unreachable goals are clamped, not stretched.
- **Flat-ground foot lock:** an independent, optional second pass. Automatic
  detection requires a slow ankle near the configured ground for at least
  three frames; it is a heuristic, **not decoded game contact events**.
  Inclusive manual ranges such as `1-12, 30-42` override automatic detection
  separately for each foot. Blank fields select automatic detection.
  The fixed plateau locks ankle position and orientation, with eased entry/exit.
- **Ground height (GMD Y):** horizontal plane in armature-local, Y-up GMD
  coordinates, normally zero. It is **not world Z** and does not raycast scene
  meshes. The target bind ankle height provides the sole offset. Set tolerances,
  contact speed (model units/sec) and blend frames for the specific clip.
- **Twist/elbow/knee blend (approximate):** disabled by default. Uses `OPE0`
  target/driver identities to blend driver deformation rotations for named
  twist/bend helpers. This is **not the code-specific OPE0 evaluator**. Muscle,
  sterno, shoulder-pad and other unimplemented operators are left unchanged.

The result stores settings, detected/manual contact ranges, truly locked
plateaus, helper names, reach-clamp count and maximum solver goal error in
`gmd_body_corrections`. A warning is shown for reach clamps or no detected
contact intervals. A clamped unreachable foot cannot be perfectly planted;
inspect the error and adjust ground/contact ranges. Corrected Actions cannot
be re-corrected in place: select the original clip before rebaking.

Foot-lock-only baking can use an existing saved body Action without the MMO.
Source-anchor IK verifies the source header and raw channel words against the
Action. Existing live constraints, active drivers or active NLA tracks must be
muted first to avoid double evaluation. No animation is exported to the game.
Flat-plane contacts are inappropriate for stairs, uneven ground, swimming or
wall traversal; disable foot locking or specify suitable manual contacts.
Animation on the armature object itself is not world-space stabilized.

Validation: six math tests (reach, lengths, poles, contact ranges/detection,
plateaus and helper blending), plus a real walk bake checking every joint
length and every frame, unchanged source keys, foot drift below 4e-6 model
units on locked plateaus, explicit source-free locking and save/reopen.
On the supplied scene's `s_NT_std_lp`, both feet form a locked plateau at
frames 3–93, with zero reach clamps. The uploaded file is a compressed Blender
scene despite its `.png` suffix. It was inspected/tested in memory only: its
newer Blender version was **not downgraded or overwritten**.

Exact game behavior still requires reconstructing terrain queries, contact
state, joint limits, torso solving and all code-specific procedural operators.
Do not interpret successful math/Blender tests as game-equivalence validation.

### Animator control rig (v0.7.0)

A persistent, editable alternative to one-shot correction baking. Select an
imported GMD armature and click **Create animator control rig**. This builds
a **separate non-deforming control armature**, parented to the imported
armature object. The original bones, rest pose, skin weights, mesh parenting
and existing Actions are never modified; the deform skeleton simply follows
the controls through world-space constraints that can be disabled or removed.

- Same-named **FK bones** on the control armature carry a copy of the current
  Action, so an animated clip starts in FK and plays back unchanged.
- `CTRL_global` moves the whole rig; `CTRL_body` moves the pelvis/torso.
  Hand/foot goal controls and elbow/knee pole controls live under
  `CTRL_global`, so in IK the feet stay planted while the body moves.
- **Match IK / Match FK** buttons place the controls on the current limb
  pose and key the IK/FK switch at the current frame (with the previous
  state keyed one frame earlier), so switching does not pop. Matching is
  accurate to about 4e-05 matrix units on the tested walk clip.
- **Convert FK clip to IK keys** keys goals, poles and mechanism bones from
  the copied FK clip over a chosen range (up to 2001 frames), turning an
  imported clip into editable IK animation.
- Each foot control has a **roll** property (radians): positive rolls over
  the toe pivot, negative over the heel pivot, around armature-local ground
  height Y=0. This is a manual pivot, not terrain detection.
- **Helper blend (approx.)** optionally shares driver deformation onto the
  named twist/elbow/knee helper bones, using the same approximate
  OPE0-derived identities as the v0.6 bake. It is not OPE0 execution.
- After applying a different clip to the original skeleton, use **Copy
  source Action to FK controls** to re-sync the rig. **Bake controls to
  original skeleton** writes the evaluated result into a new Action and
  disables the live rig; **Remove control rig** deletes the controls and
  their follower constraints, restoring direct Action playback.

Limitations: this is Blender's IK, not the game's IK or contact system.
There are no joint limits and no stretching — an unreachable goal leaves
the limb pointing at it with preserved segment lengths. Feet are planted
only because their goal controls do not move; there is no ground raycast,
contact detection or terrain adaptation. The v0.6 bake and this rig are
alternatives: do not stack a correction bake on a rig-driven pose.

Validation: `tests/test_control_rig.py` checks FK preservation of a real
walk clip at sampled frames, no-pop IK matching, converted-IK preservation,
planted feet under body motion, foot-roll pivoting, reversible helper
blending, bake fidelity and segment lengths, save/reopen persistence and
clean removal.

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

Unknown channel opcodes are rejected clearly; they are not
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

Universal human-body corpus/Blender regression: `tests/test_body_universal.py` (local extracted `p_man*.mmo` fixtures; not distributed).

Correction tests: `LD_LIBRARY_PATH=/home/user/xlibs python3 tests/test_body_corrections.py` (six math tests; additional fixture tests run when local game data is available).

Control-rig tests: `LD_LIBRARY_PATH=/home/user/xlibs python3 tests/test_control_rig.py` (fixture test runs when local game data is available).
