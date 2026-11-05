# Binary Domain Blender addon

GMD model import/patch export and decoded MOMO facial expressions.

- [Install, usage, limitations and tests](io_binary_domain/README.md)
- [MOMO channel decoding evidence](io_binary_domain/docs/MOMO_FORMAT.md)
- [Installable addon ZIP](binary_domain_addon.zip)

**v0.6.0:** optional **Blender-side** source-anchor IK, flat-ground foot
stabilization (automatic or manual contacts), and approximate twist/bend helper
rotation sharing. Bakes a separate Action and keeps the original. **Not exact
game IK, OPE0 execution or terrain collision.** See the detailed limitations.

**v0.5.0:** general human `p_man` animation libraries, without a clip-name
allowlist: **203/203** Fey handgun clips, **40/40** adventure clips. Dense,
sparse and segmented compression, 54/55/85-channel layouts, centroid root
travel/yaw and native-timeline cached Actions. Corpus: 19,739 entries in 106
nonempty libraries; 1,055 real executable-oracle comparisons plus 448 synthetic
run-table comparisons. Not creature/robot rigs or engine IK/blending/export.

**v0.4.0:** introduced the first two verified adventure body idle animations.
Superseded by v0.5.0's broader codec and catalog support.

**v0.3.0:** static full-body `p_gen_tpose` retarget with automatic bundled
`p_gen.mma`, true GMD bind rotations, preserved target bone lengths/root, and
five-model geometry/persistence regressions. Not temporal locomotion.

**v0.2.3:** standard MOMO file selector and explicit file-versus-folder
validation for MOMO/MOMA paths, including pasted quotes.

**v0.2.2:** visible clip list with a filter and Apply button, on-demand loading
for every MMO, and explicit cleanup of unused imported Actions (used/NLA
Actions preserved).

**v0.2.1:** on-demand loading for the 4032/3990-clip face libraries,
constant-channel flag variants and clearer body-rig limitations.

**v0.2.0:** 95 decoded Fey expression poses, actual bone deformation,
searchable expression selection, and explicitly generated moving previews.
Count-prefixed skin palettes are corrected; re-import old GMD scenes or use
**Repair old skin groups** before loading the new expressions. Source pattern poses are static, not temporal
performances. Locally tested on Blender 4.5.15; see the addon README for scope.
