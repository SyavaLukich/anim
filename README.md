# Binary Domain Blender addon

GMD model import/patch export and decoded MOMO facial expressions.

- [Install, usage, limitations and tests](io_binary_domain/README.md)
- [MOMO channel decoding evidence](io_binary_domain/docs/MOMO_FORMAT.md)
- [Installable addon ZIP](binary_domain_addon.zip)

**v0.2.1:** on-demand loading for the 4032/3990-clip face libraries,
constant-channel flag variants and clearer body-rig limitations.

**v0.2.0:** 95 decoded Fey expression poses, actual bone deformation,
searchable expression selection, and explicitly generated moving previews.
Count-prefixed skin palettes are corrected; re-import old GMD scenes or use
**Repair old skin groups** before loading the new expressions. Source pattern poses are static, not temporal
performances. Locally tested on Blender 4.5.15; see the addon README for scope.
