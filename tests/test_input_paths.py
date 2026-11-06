"""Blender path-field regression; no game files required."""
import os
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bpy
import io_binary_domain as addon


def main():
    addon.register()
    assert bpy.ops.bdomain.load_momo.get_rna_type().properties['filepath'].subtype == 'FILE_PATH'
    with tempfile.TemporaryDirectory(prefix='MOMO path spaces ') as folder:
        file = Path(folder) / 'expressions.MMO'
        file.write_bytes(b'test')
        for value in (str(file), '  "' + str(file) + '"  ', "'" + str(file) + "'"):
            assert addon._input_file_path(value, '.mmo', 'MOMO animation') == str(file)
        assert addon._input_file_path('  "" ', '.mmo', 'MOMO animation', str(file)) == str(file)
        # Blender // relative paths are resolved by Blender, not string replacement.
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(folder) / 'scene.blend'))
        assert addon._input_file_path('//expressions.MMO', '.mmo', 'MOMO animation') == str(file)
        for value in (folder, folder + '/', 'C:\\Binary Domain\\motion\\binaries\\'):
            try:
                addon._input_file_path(value, '.mmo', 'MOMO animation')
            except addon.GmdExportError as exc:
                assert 'folder' in str(exc) and 'MOMO animation' in str(exc)
            else:
                raise AssertionError('directory accepted: ' + value)
        for value in ('', str(Path(folder) / 'missing.mmo'), str(Path(folder) / 'wrong.mma')):
            try:
                addon._input_file_path(value, '.mmo', 'MOMO animation')
            except addon.GmdExportError as exc:
                assert 'MOMO animation' in str(exc)
            else:
                raise AssertionError('invalid file accepted')
        try:
            addon._input_file_path(folder, '.mma', 'MOMA rig')
        except addon.GmdExportError as exc:
            assert 'MOMA rig' in str(exc) and '.mma' in str(exc)
        else:
            raise AssertionError('rig directory accepted')
        # Standard ImportHelper must not append an extension to a folder.
        op = addon.BDOMAIN_OT_load_momo
        assert op.check_extension is False
    addon.unregister()
    print('PASS: spaces, quotes, Blender-relative path, Windows folder error, optional rig, missing/wrong files', flush=True)

if __name__ == '__main__':
    import traceback
    try:
        main()
    except Exception:
        traceback.print_exc()
        os._exit(1)
    os._exit(0)
