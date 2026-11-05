# Binary Domain Blender addon — v0.2.0

Import Binary Domain GSGM (`.gmd`) models and decode the constant facial
expressions in `f_man_pattern_fey.mmo` into actual bone transforms.

## Install / update

Download `binary_domain_addon.zip`, install from disk in Blender Preferences,
enable **Binary Domain: GMD / MOMO**, then restart Blender.

**Old scenes need a skinning migration.** Earlier versions assigned skin groups
with an off-by-one palette error. Either re-import the GMD in v0.2.0, or:

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
3. **Apply MOMO expression** opens a searchable list. Select e.g.
   `f_man_fey__eye_close`, `f_man_fey__open_a` or `f_man_fey__jaw_down`.
   The pose now changes the face, not just custom properties.
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
full-body motion/root-motion blending is outside this implementation.

### Exact source data

Each pose Action keeps:

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
| `0120` | `s16 base, u16 divisor, s16 fraction` | `base / 32 + fraction / divisor` |
| `0162` | `s16 x, y, z, w` | signed normalized quaternion components `/32767` |
| `016e` | none | identity quaternion `(0,0,0,1)` |

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
