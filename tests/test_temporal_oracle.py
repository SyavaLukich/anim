"""Optional executable-oracle regression for verified temporal codecs.

Requires local /tmp/repo/BinaryDomain.exe and extracted p_man_adventure.mmo,
plus pefile and unicorn. Executes isolated math kernels in an x86 emulator;
no game process, file loading or Windows APIs are invoked. Not addon runtime.
"""
import sys,struct,pefile,random,math
from unicorn import Uc,UC_ARCH_X86,UC_MODE_32
from unicorn.x86_const import *
from pathlib import Path
import importlib.util
s=importlib.util.spec_from_file_location('m',Path(__file__).resolve().parents[1] / 'io_binary_domain/mmo.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
p=pefile.PE('/tmp/repo/BinaryDomain.exe');uc=Uc(UC_ARCH_X86,UC_MODE_32);image=p.get_memory_mapped_image();uc.mem_map(0x400000,(len(image)+4095)//4096*4096);uc.mem_write(0x400000,image)
uc.mem_map(0x4000000,0x20000);uc.mem_map(0x4100000,0x20000);uc.mem_map(0x4200000,4096)
b=open('/tmp/motion_samples/p_man_adventure.mmo','rb').read();clips=m.parse_momo(b,False)[:2]
count=0;worst=0
for c in clips:
 m.decode_clip(b,c)
 for t in c.tracks:
  if t.times is None:continue
  op=t.opcode&255;n=len(t.times);raw=b[t.offset:t.offset+len(t.words)*2];hs={0x28:8,0x64:20,0x6a:14}[op];tf='HHhH' if op==0x28 else 'HH4h4H' if op==0x64 else 'HHhH3h'
  LE=raw[:2]+struct.pack('<'+tf[1:],*struct.unpack_from('>'+tf,raw)[1:])+raw[hs:hs+n+n%2]
  vf='I' if op==0x64 else 'h';LE+=struct.pack('<%d%s'%(n,vf),*struct.unpack_from('>%d%s'%(n,vf),raw,hs+n+n%2))
  uc.mem_write(0x4000000,LE)
  for frame in [0,.5,7,21,40,67,c.frames]:
   time=frame*(.5 if t.opcode>>8&1 else 1)
   esp=0x411f000;uc.reg_write(UC_X86_REG_ESP,esp);uc.mem_write(esp,struct.pack('<IIf',0x4200000,0x4000000,time))
   address={0x28:0xd8bc00,0x64:0xd8d480,0x6a:0xd912c0}[op]
   try:uc.emu_start(address,0x4200000,count=100000)
   except Exception as e:print('FAIL',hex(op),hex(uc.reg_read(UC_X86_REG_EIP)),e);raise
   val=struct.unpack('<4f',uc.reg_read(UC_X86_REG_XMM0).to_bytes(16,'little'))
   own=m.sample_track(t,frame)
   if op==0x28:err=abs(val[0]-own)
   else:err=min(max(abs(a-b) for a,b in zip(val,own)),max(abs(a+b) for a,b in zip(val,own)))
   assert err<2e-5,(hex(op),frame,val,own,err)
   count+=1;worst=max(worst,err)
print('ORACLE PASS',count,'samples','maxerror',worst)
