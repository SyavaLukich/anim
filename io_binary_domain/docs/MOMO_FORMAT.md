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

## v0.5.0 general p_man libraries (supersedes the two-idle restriction)

All 116 `p_man*.mmo` archive members were examined: 106 contain 19,739 RAW0
entries (6,765 distinct names, with repeats in aggregate libraries); 10 contain
no embedded clips. All entries decode and map to the MOMA at three source
times. Channel-count distribution: 19,682 × 85, 54 × 55, 3 × 54.
MTS0's two padded u16-length strings name the library and the required MOMA;
rig selection uses that declaration, not animation filenames or clip names.

Additional temporal codecs (opcodes below omit the 00/01 time-rate prefix):

| Codec | Header after opcode/count | Times / values |
|---|---|---|
| 21 / 22 | s16 base, u16 scale/divisor | dense signed-byte / signed-short samples |
| 23 / 24 | same as 21 | byte/u16 sparse times, signed-byte values |
| 2D | none | dense f32 scalar samples |
| 63 | 4 s16 bases, 4 u16 ranges | dense packed u32 quaternion samples |
| 69 | s16 base, u16 range, 3 s16 axis | dense s16 angular samples |
| 61 | none | dense 3 u16 Euler turns, discard low bit before half-angle |
| 6D | none | dense 3 f32 Euler radians, quaternion = Qz * Qy * Qx |
| 25 / 27 / 2A / 2C | u16 pool count, s16 base, u16 scale/divisor, u16 flags | run tables; signed byte / byte / short / short pool |
| 66 / 68 | u16 pool count, 4 s16 bases, 4 u16 ranges, u16 flags | run tables, packed u32 quaternion pool |
| 6C | u16 pool count, s16 base, u16 range, 3 s16 axis, u16 flags | run tables, s16 angular pool |

Dense sample times are 0,1,...,count-1 before the opcode time-rate multiplier.
Byte scalars use base/16384 + signedSample * unsignedScale/(16384*256),
**not** the short scalar base/32 + sample/divisor formula. Both time and value
byte arrays are individually padded to an even byte count. 61 half-angles are
(u16 >> 1) * pi/32768; 6D half-angles are f32/2.

Run compression is **not a Hermite/Bézier spline**. Three arrays of `count`
entries hold start times, pool offsets, and run lengths. Flags bits 0/1/2
independently choose u16 instead of padded byte storage for each array.
Scalar flag bit 3 selects byte rather than short pool entries. Each run emits
consecutive sample times `start+j` from `pool[offset+j]`. Across gaps, the last
sample of one run interpolates to the first of the next. Reject zero-length,
overlapping runs, pool overruns and invalid flags. Header sizes including
opcode/count are scalar 12, packed quaternion 24, fixed-axis 18 bytes.
Loaders D8B091..D8B43F; wrappers D8B9E0, D8D3A0, D911E0; representative
kernels 409960, 40BFD0, 40DE40. All eight width combinations were checked.

Dense quaternion interpolation differs from sparse/run interpolation. EXE
D91590, D912E0, D90E50 and D90A60 use normalized adjusted lerp, with dot=d,
k=-0.39936140179634094*d+0.5069268941879272 and
u=t+k*t*(2*t*t-3*t+1), then normalize(a+u*(sign(d)*b-a)).
The constants at 03087CF0/03087D00 are BSS vectors initialized by F86780 and
F867A0 from 011CEEC0/011CEEC4. An oracle must initialize these vectors;
leaving BSS zero silently tests a different interpolant. Sparse/run kernels
use shortest-path SLERP. Blender bakes source integer frames and uses its own
linear component interpolation between those bake keys.

**Global motion:** moo_centroid is a separate NOD3 root; center_c_n also has
parent=-1. Flag-65 transforms are absolute in animation-model space, not
already in centroid-transformed scene space. Compose the skeletal hierarchy
first, then apply the centroid SRT to all other evaluated nodes. This retains
walking travel and turn yaw. Anchor horizontal translation to the first source
world center, but use the MOMA center bind height for vertical displacement
and height scale (using a crouched initial height loses the crouch and distorts
travel). The three leading auxiliary scalar channels are often nonzero
(e.g. 0,6,5); preserve their source timelines, but do not interpret them as XYZ
translation. PRG2 is preserved opaquely, not mistaken for an omission bitmask.

A 55-channel prefix ends before all 30 rotation-only finger channels. These
omitted target nodes inherit their target parent's deformation, retaining
bind offsets. The three 54-channel p_tpose entries additionally omit the final
weapon quaternion, defaulted to identity while retaining its XYZ channels.
Other incomplete node layouts remain errors; no arbitrary channel realignment.

Optional executable oracle: 1,055 real-data evaluations, max absolute component
error 2.665261514711581e-6; 448 synthetic evaluations cover all table-width
flags, >255 pool offsets, and segment gaps (including byte scalar variants).
Another 70 synthetic scalar evaluations check 21/22/23/24/2D, odd byte padding
and both full/half-rate time flags.
Blender regression: all 203 p_man_fay_whan clips at 609 times, plus actual
native Actions for idle/walk/turn/55-channel motion, the 54-channel T-pose,
float/packed Euler motion, root travel/yaw, lengths, and saved Action reuse.
This does not establish engine-equivalent IK/procedural controls, animation
blending, every nonhuman MOMA, or game-side animation export.

## v0.6.0 anchor and procedural metadata; optional Blender adaptation

p_man `ANC0` payload: u16 count=5, u16 reserved=0, then count pairs of
u16 endpoint/root node indices: (3,1), (17,15), (22,20), (29,27), (40,38).
For this rig each endpoint's parent is the middle joint, whose parent is the
listed root. These identify the spine, left/right leg and left/right arm chains.
The addon now parses and validates the table. Its optional analytic IK uses
the four limb chains and source endpoint positions; it does NOT reproduce the
engine's full solver, joint limits, pelvis/torso adjustment or terrain queries.

p_man `OPE0` payload: u32 count=24, u32 reserved=0, followed by count 32-byte
records. First six s16: operation ID, target node, four driver node indices
(-1 denotes unused). Remaining 20 bytes are preserved opaquely (zero in this
bundled rig). IDs observed: 1–6, 9–24 and 57/58. Nodes include sterno, muscle,
knee, twist, elbow and shoulder-pad helpers. Preserve full record bytes and
validate target/driver bounds; do NOT equate every flag-6 node to a quaternion.

EXE OPE0 loader D9B286..D9B456 converts the records to 16-byte runtime
operation entries, replacing negative driver indices with zero, and invokes
per-operation initialization through table 01366BC4. Runtime dispatch references
013669BC at D8A4EC/D8A5F8. Examples: ID 5/6 wrapper 40FA70 -> F1FE20;
ID 9/10 wrapper 40FAB0 -> F05E10. In the bundled rig, IDs 11/12 target
`asi2_l/r_sup` and dispatch through wrapper 40FAD0 -> F1E870. These
code-specific operators are **not** implemented by the optional helper blend.
Parsing the table is not executing it.

### Static analysis of the supplied game executable

The split archive's `BinaryDomain.exe` was extracted for static inspection only
(not executed). It is a 32-bit i386 PE (COFF timestamp 2012-05-31), SHA-256
`d77e345676d97acf931607d660fc9c4d16c90c5fff9eb2ebeae4df15b934462d`.
The executable contains class-name families `MotionFootwork_*` and
`MotionAimControl_Face/Muzzle/Waist_*`; no separate IK DLL or recognizable
generic `IKSolver` API was found. This absence is not proof that the stripped
binary contains no general solver.

The `MotionFootwork_RUN` path at VA `0x009AC280`/`0x009AC4B0` is a specialized
postprocessor: it tracks two foot nodes, checks motion-timed intervals plus
contact/geometric distance and angle conditions, then computes per-foot/root
transform corrections. The update path accumulates the two foot contributions
and applies a half-weighted root correction. It is therefore evidence for
motion-specific footwork/contact correction, not an unrestricted arbitrary
chain solver. Other `MotionFootwork_*` and aim-control classes are likewise
archetype/role-specific. These findings do not recover the full OPE0 operator
math or prove the complete absence of another solver elsewhere in the EXE.

The optional correction baker is a separate approximation layer. It keeps the
raw Action, solves fixed-length limb chains against source goals, then optionally
stabilizes ankles on manually specified or heuristically detected flat-plane
contacts. Approximate named twist/bend helpers blend driver bind-relative
world deformation rotations. Other procedural operators are untouched. It
stores settings/contact intervals/reach errors and installs no constraints or
handlers. Neither automatic contact detection nor quaternion averaging is
claimed to be the game's implementation. Terrain collision, full OPE0, foot
controller state, full-body IK and game animation export remain unimplemented.
