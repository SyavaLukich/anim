# Binary Domain — character & animation extraction pipeline

Headless (Linux, no game files needed at parse time) extraction of **Binary
Domain** (2012) character models, skeletons, and animation data into
**glTF 2.0 (.glb)** files importable into Blender / any glTF tool.

## What's in the repo

| Path | What |
|---|---|
| `*.7z` (32 volumes) | Split archive of the game's binary data (chara/motion/etc. `.par` files) |
| `tools/bdtools/parc.py` | PARC archive parser + SLLZ v1/v2 decompressor (verified byte-identical to QuickBMS `unyakuza.c` on real BD data) |
| `tools/bdtools/gltf_export.py` | GMD → GLB exporter: meshes, skeleton, rigid skinning, PBR materials, DXT1/DXT5/RGBA texture decode, glTF skin |
| `tools/bdtools/momo.py` | MOMO (`.mmo`) animation container parser: clip names, frame counts, fps, loop flags |
| `tools/bdtools/_vendor/mathutils.py` | Pure-Python `mathutils` shim (yk_gmd_io dependency) |
| `anim_index.json` | Catalog of all 41,746 animation clips (39 characters): name, frames, duration, loop |

## Reconstruction steps (sandbox)

```bash
# 0) deps
pip install --break-system-packages numpy py7zr

# 1) rejoin split 7z, extract the .par files (the .exe needs BCJ2 → skip)
python3 - <<'EOF'
import glob, py7zr
open('work/BD_full.7z','wb').write(
    b''.join(open(f,'rb').read() for f in sorted(glob.glob('Binary Domain.7z.*'))))
z = py7zr.SevenZipFile('work/BD_full.7z')
z.extract('work/extracted', targets=[n for n in z.getnames() if n.endswith('.par')])
EOF

# 2) expand all .par archives (SLLZ) into work/data/
PYTHONPATH=tools python3 - <<'EOF'
from bdtools.parc import ParArchive
import os
for par in ['chara.par','expression.par','motion.par','motion_event.par','motion_set.par']:
    ParArchive(open(f'work/extracted/{par}','rb').read()).extract('work/data', prefix=par[:-4])
EOF

# 3) export all character models -> work/out/<char>.glb
PYTHONPATH=tools python3 - <<'EOF'
from bdtools.gltf_export import export_gmd
import glob, os
for g in sorted(glob.glob('work/data/chara/chara/*.gmd')):
    export_gmd(g, 'work/out/' + os.path.basename(g)[:-4] + '.glb',
               dds_dir='work/data/chara/chara')
EOF

# 4) animation metadata catalog
PYTHONPATH=tools python3 - <<'EOF'
from bdtools.momo import catalog_motion_dir
import json
print(len(catalog_motion_dir('work/data/motion')))
EOF
```

## Content census

- **132 characters** (`chara.par`, 1821 files): all export to valid GLB
  (70k–240k verts, 26–264 bones, 27–39 textures each; ~650 MB total).
- **929 motion files** (`motion.par`): 257 contain animation clips,
  **41,746 clips total** across 39 animated characters (protagonists
  `p_man`/`f_man` ≈ 32k clips, enemies `e_*`, `a_asa`, …).
- 1,689 DXT1/DXT5/RGBA textures (mip-mapped DDS, custom "PXDE" header).
- `.mmo` keyframe tracks (TRK2) are custom-compressed — metadata decoded,
  per-frame key data **not yet decoded** (see below).

## Reverse-engineered formats (this work)

### PARC (`.par`)
`'PARC'` u8 ver u8 endian u8 hdrHi u8 0 u32 0x00020001 u32 hdrSize u32
dirCount u32 dirOff u32 fileCount u32 fileOff; 64-B cp932 dir names, 32-B
dir entries, 32-B file entries; file offset = `offLo | ((unk&0xF)<<32)`;
entries flagged `0x80000000` are SLLZ-compressed.

### SLLZ (Simple LZSS, SEGA "0.1")
Header: `'SLLZ'` u8 endian u8 ver u16 hdr(16) u32 usize u32 csize.
v1: bit-packed ops, **MSB-first**; per bit: 1 = copy
(dist = 1 + ((p0>>4)|(p1<<4)), len = 3 + (p0&0xF)), 0 = literal.
**Gotcha:** when a flag byte is exhausted the *next flag byte is consumed
before the op data* (matches QuickBMS `unyakuza.c`, which this was
verified against byte-for-byte on real BD streams).

### GMD (`.gmd`, v3.11, "GSGM")
Parsed via `yk_gmd_io` with two BD-compat patches (null `bone_data` in
`copy_as_skinned`; 255 "no-bone" weight sentinel). BD uses **rigid
skin**: every vertex is 100% bound to the first bone of its mesh's matrix
list. Exposed per-node in `extras.gmd_matrix_lists` for re-import.
Coordinate transform to glTF: `C = diag(-1,-1,1,1)`.

### DDS (`.dds`)
Custom header ("PXDE" tag @0x24, format @0x54: `DXT1`/`DXT5`/zero=raw
RGBA). Pixel data at 0x80: **base level first, then a full mip chain**
(file size = mip sum; some are 6-face cubemaps). Base-level decode →
PNG, embedded in the GLB.

### MOMO (`.mmo`)
`'MOMO'` u32 flags(0x1ff); sections `[4B tag][u32 BE size]`:
`MTS0` (set/char names), one `RAW0` per clip containing
`HED0` (clip idx, **frame count**, **fps=30**, loop flag — all
`u16 << 8`, trailing `f32 BE`), `HED1` (clip name), `PRG2` (24 B,
unknown), `TRK2` (per-track keyframes, custom delta compression).
The first RAW0 is the t-pose/base section.

## Status

| Item | State |
|---|---|
| 132 character GLBs (mesh + rig + skin + textures) | ✅ done, structure-validated |
| Animation catalog (41,746 clips: name/duration/loop) | ✅ done (`anim_index.json`) |
| Per-frame keyframes (TRK2) → glTF animations | ⚠️ format identified, compression not yet decoded |
| `.mma` motion sets, `.imv` motion events, `.ved` expressions | ⚠️ structure only |

## GLB conventions

- One `skin` per GLB; joint names = BD bone names (`..._n` suffix).
- Mesh nodes carry `extras.gmd_matrix_lists` (the per-sub-mesh bone
  matrix lists from the GMD) so skinned meshes can be re-bound
  1:1 on re-import.
- Materials: `baseColorFactor` from GMD diffuse, `roughnessFactor` from
  GMD `power`, `baseColorTexture` from the `(d)` DDS; the `(u)` alpha
  DDS is composited into the PNG alpha and drives `alphaMode: BLEND`.
