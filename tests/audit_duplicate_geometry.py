"""Report exact coincident triangle sets without deleting draw records.
Usage: python tests/audit_duplicate_geometry.py /path/to/models
Only positions/topology are compared, NOT UVs, materials or deformation.
"""
import importlib.util,glob,collections,struct,sys
from pathlib import Path
s=importlib.util.spec_from_file_location('g',str(Path(__file__).resolve().parents[1] / 'io_binary_domain/gmd.py'));g=importlib.util.module_from_spec(s);s.loader.exec_module(g)
for fn in glob.glob(str(Path(sys.argv[1] if len(sys.argv)>1 else '.') / '*.gmd')):
 x=g.GMD(open(fn,'rb').read());groups=collections.defaultdict(list)
 for m in x.meshes:
  vm,faces=x.mesh_geometry(m);order=list(dict.fromkeys(vm));pos=[tuple(x.decode_vertex(m.stream,v)['pos']) for v in order]
  tris=tuple(sorted(tuple(sorted(pos[i] for i in f)) for f in faces))
  groups[tris].append(m)
 print('\n',fn.split('/')[-1],len(x.meshes),'records; unique triangle sets',len(groups))
 for ms in groups.values():
  if len(ms)<2:continue
  print('DUP',[(m.idx,m.stream,m.material,len(x.mesh_geometry(m)[1]),m.raw[8],m.c,m.d,m.e) for m in ms])
