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
two helper nodes have a scalar in the fourth slot. `p_gen.mma` NOD3 declares
161 nodes but 636 indexed channels, with two node flags equal to 6 instead
of 1. MOMA metadata parsing no longer equates this declaration with 4×nodes;
the face retargeter still explicitly rejects this different body rig.
All three files pass unchanged, byte-exact channel patching. This validates
binary preservation, **not** body retargeting or a full-body pose in Blender.

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
