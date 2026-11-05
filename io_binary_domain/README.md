# Binary Domain (.gmd / .mmo) Blender addon

Import/export for the Sega **Binary Domain** (2012) GSGM model format and MOMO
face-animation format, based on a from-scratch reverse engineering of the
game archives (`chara.par`, `motion.par`).

Tested against the Fey character pack: `c_awmn_fey.gmd`, `c_awmn_fey_head.gmd`,
`c_awmn_fey_last.gmd`, `c_awmn_fey_mant.gmd`, `c_awmn_fey_naked.gmd` and
`f_man_pattern_fey.mmo` (95 clips) — an untouched import exports
**byte-identical** to the source file.

v0.1.1 fixes double axis conversion on parented meshes and decodes UVs as
binary16 pairs rather than normalized integers. Opaque non-finite UV words
are preserved during untouched export; their shader-specific meaning remains
unconfirmed.

## Install

Copy the `io_binary_domain/` folder into your Blender addons directory
(`Edit > Preferences > Add-ons > Install...`, or drop it into
`scripts/addons/`), then enable **"Binary Domain: GMD / MOMO"**.
Requires Blender 4.1+ (custom split normals via the `custom_normal`
attribute; older versions import without custom normals).

## Import

`File > Import > Binary Domain Model (.gmd)`

- Textures (`<name>.dds`) are loaded from the folder next to the `.gmd`.
- One Blender mesh object per draw record, parented to the armature with an
  Armature modifier; vertex groups match the record's bone palette.
- The model is stored in **raw game coordinates (Y-up)**; the armature carries a +90° X rotation, inherited by its meshes so it displays correctly in Blender's
  Z-up world. Do not apply/clear that rotation.
- Bone rest transforms are exact world matrices from the file; bone
  *directions* (head→tail) follow each bone's own rotation axis, so the
  armature may look like a "porcupine" — this is cosmetic only.
- Optional: pick a `.mmo` file in the import dialog to also import its clips.

## MOMO face animation

MOMO clips are imported as **Actions** on the armature (one per clip).
The 46 face nodes (names from the bundled `rigs/f_man.mma` rig) get pose-bone
custom properties `gmd_c0`, `gmd_c1`, `gmd_c2` holding the **raw track values
(0..1023)**, keyframed on frame 1.

Use the N-panel **"Binary Domain"** tab (select the armature) to load a MOMO
file and switch clips, or swap actions manually in the Action Editor.

> **Note on value semantics:** the exact rotation encoding of track values is
> not yet confirmed. Analysis of all 95 clips shows an exact L/R mirror rule
> (`c0_right = 1023 − c0_left`, `c1`/`c2` preserved), which rules out plain
> Euler/quat encodings. The optional *"Experimental Euler bake"* checkbox
> keyframes `rotation_euler = v/1024·360° − 180°` per component — it may look
> wrong for some nodes; the raw properties are always the source of truth.

## Export

`File > Export > Binary Domain Model (.gmd)` (select any imported object of
the model).

The exporter is **patch-based**: it re-reads the original file and overwrites
only

- bone records (position + axis-angle rotation, helper-bone quaternions), and
- vertex streams (position, normal, UV, UV2),

so anything the addon doesn't model is preserved perfectly.

What round-trips exactly:

| Data | Exported from | Notes |
|---|---|---|
| Bone pose | Pose Mode transforms | rest-pose edits are rejected with an error |
| Positions / normals | mesh vertices / custom split normals | normals are biased bytes, decode/encode is lossless |
| UV / UV2 | `UVMap` / `UV2` layers | binary16 UV pairs; V is flipped for Blender |
| Skin weights | raw `gmd_w` / `gmd_bi` attributes | **weight edits are NOT exported in v1** (groups are for deformation only); delete both attributes to force re-encoding from vertex groups |
| Tangents, misc bytes | raw `gmd_nb` / `gmd_w1` / `gmd_w2` attributes | opaque data, preserved verbatim |

Not supported in v1 (rejected with a clear error): adding/removing vertices
or faces, changing materials, editing the rest pose, editing the skeleton.

## Files

- `gmd.py` — GSGM parser/writer (pure Python, no Blender dependency)
- `mmo.py` — MOMO/MOMA parser (pure Python)
- `__init__.py` — Blender operator/UI layer
- `rigs/f_man.mma` — bundled rig providing the 46 face-node names in track
  order (from `motion.par`)
- `tests/test_roundtrip.py` — byte-comparison test suite (runs against a real
  Blender via the `bpy` module)

## Format summary (quick reference)

GMD = `GSGM` big-endian container; 17-chunk directory: skeleton (world
transforms, axis-angle), helper bones (pos+quat), 130 mesh records (vmap =
vertex list *with duplicates*; faces index stream-local vertex ids), 38
materials (shader index + 8 u32 texture slots), 5 vertex streams (strides
40/36/32/20; normals `(b−128)/127`, UVs IEEE 754 binary16 pairs), u16 index buffer,
u8 bone palette, texture/shader/bone-name tables.

MOMO = `MOMO` + clip containers `RAW0{HED0,HED1,PRG2,TRK2}`; TRK2 holds
`46·3+1` one-key tracks per clip separated by `0x7fff`; key kinds:
`{v,0120,t}`, `{v,016e,0120,t}`, `{v,0162,4×handle,0120,t}`; values are
10-bit (0..1023).

## Overlapping draw geometry (v0.1.2)

The source files contain overlapping draw records, not just one clean surface.
UV-less (20-byte stride), texture-less records are now hidden in the viewport
and render by default, but remain in the scene for byte-identical export.
They have `gmd_auxiliary = True`; unhide them in the Outliner for inspection.
Their precise engine pass (for example shadow/depth) is not yet confirmed.
Other overlaps are retained: coincident positions do not imply identical UVs,
materials or deformation. Do not delete objects before patch export.

`tests/audit_duplicate_geometry.py <model-directory>` reports exact coincident
triangle sets without requiring Blender. This is a positional comparison only.

## Material preview corrections (v0.1.3)

Vertex declarations, not stride alone, determine UV placement. The tested
40-byte declaration `0x1fe0ffc3` has color at +32 and binary16 UV at +36;
`0x28e0ffc3` has UV at +32 and opaque extra data at +36 (no longer exposed
as an unverified UV2). The 24-byte declaration uses UV at +20.

Materials use the first available diffuse `(d)` texture. Game normal and
mask textures are loaded as nodes but no longer automatically connected to
normal/alpha: their packed channel conventions are not confirmed. This is a
basic diffuse preview, not a recreation of the original game shaders.
A local Cycles render in Blender 4.5 confirms improved face/clothing mapping,
but residual overlapping/variant geometry artifacts remain (notably chest
and knee details). Blender 5.1 has not been locally validated.
