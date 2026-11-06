"""Run in Blender's Text Editor after installing addon v0.1.5.
Restores ONLY UVs from the original GMD referenced by each imported object.
Validates topology before changing anything. Does not save the .blend file.
"""
import bpy
import importlib
import sys
from pathlib import Path


def repair():
    parsers = [m for name, m in list(sys.modules.items())
               if name.endswith('.gmd') and hasattr(m, 'GMD')]
    if not parsers:
        raise RuntimeError('Enable the Binary Domain addon first')
    parser = importlib.reload(parsers[0])
    if not hasattr(parser.GMD, 'uv_offset'):
        raise RuntimeError('Install addon v0.1.5 first')
    cache = {}
    plans = []
    for ob in bpy.context.scene.objects:
        if ob.type != 'MESH' or 'gmd_mesh' not in ob:
            continue
        if ob.mode != 'OBJECT':
            raise RuntimeError('Switch to Object Mode before repair')
        path = bpy.path.abspath(ob['gmd_source'])
        if path not in cache:
            if not Path(path).is_file():
                raise RuntimeError('Original GMD not found: ' + path)
            cache[path] = parser.GMD(Path(path).read_bytes())
        g = cache[path]
        m = g.meshes[ob['gmd_mesh']]
        if g.streams[m.stream]['stride'] < 24:
            continue
        vm, faces = g.mesh_geometry(m)
        order = list(dict.fromkeys(vm))
        if (len(ob.data.vertices) != len(order) or
                [tuple(p.vertices) for p in ob.data.polygons] != faces):
            raise RuntimeError('Topology differs from original: ' + ob.name)
        uv = []
        for loop in ob.data.loops:
            u, v = g.decode_vertex(m.stream, order[loop.vertex_index])['uv']
            uv.extend((u, 1.0 - v))
        plans.append((ob, uv))
    # All source files and topology validated before any scene modification.
    for ob, uv in plans:
        me = ob.data
        if 'UVMap' not in me.uv_layers:
            me.uv_layers.new(name='UVMap')
        me.uv_layers['UVMap'].data.foreach_set('uv', uv)
        me.uv_layers.active_index = list(me.uv_layers.keys()).index('UVMap')
        me.uv_layers['UVMap'].active_render = True
        me.update()
        ob['gmd_uv_decoder'] = 'binary16-declaration-v1'
    print('Binary Domain: restored UVMap on %d objects' % len(plans))
    return len(plans)


if __name__ == '__main__':
    repair()
