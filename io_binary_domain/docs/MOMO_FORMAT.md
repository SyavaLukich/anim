# MOMO constant expression channel decoding

## Evidence and reproducibility

Inputs: the user's original `Binary Domain.7z` archive, its `BinaryDomain.exe`,
`motion.par` / bundled `f_man.mma`, and `f_man_pattern_fey.mmo` from the Fey pack.
No executable, extracted corpus or external disassembler is required at addon
runtime. No game binary is added to this repository.

The executable was extracted with a locally built upstream 7-Zip (the earlier
Python extractor did not support the archive's BCJ2 filter). Static inspection
of the 32-bit PE located the big-endian TRK2 loader through its magic constant.
Relevant virtual addresses in this executable (image base `0x00400000`):

- `0x00D9A154–0x00D9A214`: read u32 channel count, allocate track pointers, walk
  the individual channels using the decoder at `0x00D8B000`.
- `0x00D8B038–0x00D8B090`, channel `0x20`: consume three words; convert signed
  base and numerator / unsigned divisor, multiply base by `0.03125`, add.
  The constant at `0x011CCDFC` is IEEE float32 `0.03125`.
- `0x00D8B2E2–0x00D8B311`, channel `0x62`: four 16-bit words, not a curve.
- `0x6E` has no payload; it represents an identity quaternion in the corpus.
- `NOD3` loader near `0x00D9B090`: node count, channel count, per-node u32
  declarations, signed 16-bit parents, offsets and channel indices.

The leading header byte is retained verbatim. The loader skips it before
switching on the low-byte type. v0.2.1 accepts the observed `00` and `01`
variants of the supported constants, rejecting other flag bytes. `002e`
is a no-payload scalar-zero channel in the body T-pose corpus (the loader's
`0x2e` dispatch also consumes no payload). No temporal interpretation is
inferred from these flag variants.

## Scalar example

Head Y channel bytes:

```
01 20 00 33 7f ff 00 d9
```

`51/32 + 217/32767 = 1.6003725165562914`.

Head Z:

```
01 20 ff ff 7f ff 00 10
```

`-1/32 + 16/32767 = -0.030761703848384045`.

The previous parser incorrectly split at `7fff`, retaining only small tail
words and shifting the base word into the preceding fake "track time".
This explains both the misleading 0..1023 range and apparent mirror rule.

## Corpus validation

All 95 clips have exactly 184 channels, grouped as 46 × XYZ + XYZW. Across the
corpus: 13,110 scalar channels, 907 packed quaternions, 3,463 identities.
The cursor finishes exactly at each TRK2 payload end; identity patching of
all channels is byte-identical. Raw words are retained separately from the
normalized quaternions used by Blender.

MOMA `NOD3` parent indices are `-1` for node 0, `0` for nodes 1..32, and `32`
for nodes 33..45. Jaw-relative lower-face coordinates must be composed with
the jaw transform, or every expression incorrectly displaces the chin.

## Retargeting vs decoding

The scalar formula and channel layout are supported by executable inspection
and complete corpus consumption. Converting those values to Blender animation
also requires model retargeting. The implementation:

1. keeps the model's head anchor, using source root height only to estimate a
   uniform scale (recorded on the armature and each Action);
2. composes decoded local translations/quaternions through MOMA parents;
3. transfers those deltas/positions to the model's rest bone frames;
4. uses Blender `convert_local_to_pose(..., invert=True)` for parent-relative
   matrix_basis and keys location, quaternion and scale.

This mapping was visually checked with bilateral eye closure and an open-mouth
pose on the naked Fey model, and numerically checked on all five variants.
It is not compared against game screenshots or proven engine-identical for
other characters. Unsupported temporal channels fail explicitly.

The source file is a pattern-pose library. A one-key expression Action is
faithful to that static data. The separate generated 1/16/31 preview makes
movement visible without falsely attributing that timing to MOMO.

## Related skinning correction

GMD palette blocks start with a count byte. All five corpus files agree with
each mesh's `pal_size`. Skin slot `i` maps at `pal_base + 1 + i`. The old
mapping included the count as a bone id, shifting every group by one and
causing unilateral eye/neck deformation even with correctly decoded poses.
Raw GMD weight/index bytes were never changed by that display bug; their
preservation alone could not detect it. Regression tests now compare actual
Blender groups with the source counted palette and test evaluated deformation.

## Extended corpus / v0.2.1

All channel boundaries were checked in the extracted `motion.par` resources:

| Resource | Clips | Header counts |
|---|---:|---|
| f_man_all.mmo | 4032 | 556415 × 0120, 134376 × 016e, 51096 × 0162, 1 × 0020 |
| f_man_motionviewer.mmo | 3990 | 550620 × 0120, 132444 × 016e, 51096 × 0162 |
| p_gen_basic.mmo | 1 (p_gen_tpose) | 299 × 0020, 186 × 002e, 124 × 006e, 35 × 0062 |

Every face clip has 184 channels. The body T-pose has 644 channels, and
its layout must not be assumed to be 161 identical XYZ+quaternion groups:
the first eight channels are reserved scalar zeros and are not bone transforms.
The previous flat grouping incorrectly assigned those channels to the first
two nodes and shifted later transforms. `p_gen.mma` NOD3 declares
161 nodes but 636 indexed channels, with two node flags equal to 6 instead
of 1. Each flag-1 node has four channel-table entries at its declared offset;
flag-6 nodes have none. The table references TRK2 indices 8..643. Node 38 and
65 offsets do not advance; they are procedural nodes, not scalar rotations.
`pose_nodes(clip, rig)` now follows this indirection explicitly.
All three files pass unchanged, byte-exact channel patching. This validates binary preservation; the separate v0.3.0 body retargeter and
its Blender regressions are described below.

Large-library loading first reads metadata without allocating channel objects.
Only the selected clip is decoded. The original file remains external; loaded
Actions and rig metadata persist in `.blend`, but unloaded clips require it.

## v0.2.2 Blender catalog UI and lifecycle

All libraries now default to on-demand Actions, including the 95-pose file.
The optional eager operator setting / `load_momo_actions(..., eager=True)`
retains batch import for the original all-Actions persistence regression.
The panel uses a persistent per-armature collection and case-insensitive
filter, with an explicit Apply selected clip button, not only an invoke-only
search popup. It avoids repeatedly deserializing the catalog on redraw.

Old imported Actions remain until explicit cleanup. Cleanup removes only
addon-tagged Actions whose users consist solely of Fake User (or no users);
it never unlinks an object/NLA user and leaves unrelated user Actions alone.
The confirmation explicitly warns that edited unused MOMO copies are included.
Tests cover this distinction, list selection/filtering, small-file lazy loading,
and unregister/register lifecycle in addition to the large-file tests.

## v0.3.0 full-body T-pose retarget

The GMD bone record stores local translation at `+0x20`, a unit xyzw bind
quaternion at `+0x30`, and scale at `+0x40`. The existing armature display
uses `+0x60` as an axis-angle-like frame; those direction/length fields must
**not** be mistaken for the physical bind quaternion in body retargeting.
The importer remains byte-preserving; the body path stores the real bind
quaternions separately instead of changing existing import/export semantics.

For mapped target bone i, compose the actual GMD local quaternions along the
target hierarchy to obtain B_i. Compose decoded MOMA transforms to obtain
source world rotation S_i. The deformation rotation is D_i = S_i B_i^-1.
For a target parent p, retargeted joint position is
P_i = P_p + D_p (restHead_i - restHead_p), with each target root held at rest.
The Blender target matrix has rotation D_i * importedRestRotation_i and
translation P_i. Convert to matrix_basis using `convert_local_to_pose` with
the retargeted parent matrix, not a guessed Euler order. Thus the displayed
rest basis cancels in skinning while true target bind orientations are used.
Unmapped/procedural target nodes inherit the target parent's deformation.

Only the verified p_gen_tpose clip (644 channels, first eight scalar zeros,
161-node p_gen rig) is enabled. This is a length-preserving static retarget:
root-motion controls and arbitrary body animation codecs are not inferred.
Matched face/hair/accessory rotations are included; unmatched model nodes
retain target offsets and follow their parents. Target proportions, topology,
weights and rest frames are not replaced by the generic rig's positions.

Tests on all five supplied Fey GMDs check horizontal bilateral arms, unchanged
length of every target parent offset, real vertex movement/finite geometry,
raw-word preservation, wrong-rig/partial-skeleton rejection, legacy bind-data
migration, unsupported auxiliary controls and Action save/reopen application.
A front-view Cycles render of the naked model was inspected in rest and T-pose.
This is not a comparison against engine screenshots or validation of game-side
export, temporal locomotion or engine procedural-node evaluation.

## v0.4.0 temporal body idle corpus

From the same user-provided motion.par: p_man_adventure.mmo, 40 RAW0 clips,
and p_man.mma. Verified clips p_Wnon_std_nml and p_Wnon_std_nml_01 have 85
channels and durations 80 and 200. Other clips require additional codecs;
the Blender catalog exposes only these two validated names/durations.

HED0 u16 at +4 is duration in source frames, u16 at +6 is **FPS**, not duration.
Earlier static-pose code treated +6 as a duration; this is corrected, with FPS
kept separately. Speed at +12 is 1.0 for both enabled clips. Bake source samples
0..duration onto Blender 1..duration+1 at 30 FPS. Per-track header bit 0 in the
leading byte multiplies evaluation time by 0.5 (EXE D8A0C0/D8A100), not a flag
to discard when evaluating curves.

Implemented temporal types (all arrays big-endian on disk):

- 28/29: u16 count, s16 base, u16 divisor, byte/u16 key times, then count s16
  samples. Byte time arrays are padded to even length. Values are base/32 +
  sample/divisor, with piecewise-linear interpolation and endpoint clamping.
  Loader D8B122, scalar evaluator 409590 for byte times.
- 64/65: u16 count, 4 s16 base components, 4 u16 ranges, byte/u16 times,
  then count packed u32 quaternions. Low 2 bits select the omitted component;
  remaining unsigned 10-bit fields occupy 22..31, 12..21 and 2..11, assigned
  to the other components in XYZ W order. Component = base/32768 +
  quantized/1024 * range/32768. Reconstruct the omitted positive component
  as sqrt(max(0,1-sum(other^2))). Shortest-path SLERP between keys.
  Loader D8B37E; evaluator 40BAC0, unpack helper D8C3B0.
- 6A/6B: u16 count, s16 angular base, u16 angular range, 3 s16 fixed-axis
  components, byte/u16 times, count s16 angular samples. Quaternion half-angle
  = (base/32767 + sample/32767 * range/32767) * pi/2; xyz = axis/32767 * sin,
  w = cos. Evaluate keys then shortest-path SLERP (not guessed Euler curves).
  Loader D8B5A1; evaluator 40D9B0 and angular helper D8D510.

All raw temporal words are preserved. Unchanged channel patching remains
byte-exact; attempting to edit a temporal channel with the fixed-layout
constant patcher fails explicitly instead of corrupting the stream.

p_man NOD3 maps 82 entries to source channels 3..84; first three auxiliary
scalar channels are zero in these clips. Flags: 0 = rotation-only (translation
from NDT0), 1 = XYZ+quaternion, 6 = procedural/no channels, 0x41 = world-space
XYZ+quaternion. The world-space flag stops parent composition; EXE D8AAD1
and D8D050 distinguish it explicitly. NDT0 default positions use s16 XYZ /
the float scale at header +4. Target bind-relative rotations use the v0.3
method; target lengths are kept, with root displacement relative to the first
source sample. Exact game IK/foot locking and procedural helpers are not claimed.

`tests/test_temporal_oracle.py` executes the isolated evaluator kernels in
Unicorn with native little-endian loaded-track layout, never starting the game.
336 comparisons across every temporal track in both clips (including fractional
times and endpoints) pass with maximum error 4.731045787242927e-6. This is an
optional research regression; EXE/Unicorn/pefile are not shipped/runtime dependencies.
Baked Actions use continuous quaternion signs and LINEAR native-frame keys,
bulk-filled FCurves rather than millions of per-frame keyframe_insert calls.
Subframe playback between the integer-frame bake keys is Blender interpolation,
not a claim of exact engine subframe SLERP.
