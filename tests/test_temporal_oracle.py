"""Optional isolated EXE-kernel oracle for human-motion temporal codecs.

Uses local BinaryDomain.exe, extracted p_man fixtures, pefile and Unicorn.
No game process, Windows APIs or external file loading is executed by kernels.
Includes synthetic run-table width/pool/gap and scalar encoding tests.
"""
import sys,struct,pefile,random,math
from unicorn import Uc,UC_ARCH_X86,UC_MODE_32
from unicorn.x86_const import *
from pathlib import Path
import importlib.util
s=importlib.util.spec_from_file_location('m',Path(__file__).resolve().parents[1] / 'io_binary_domain/mmo.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
p=pefile.PE('/tmp/repo/BinaryDomain.exe');uc=Uc(UC_ARCH_X86,UC_MODE_32);image=p.get_memory_mapped_image();uc.mem_map(0x400000,(len(image)+4095)//4096*4096);uc.mem_write(0x400000,image)
uc.mem_map(0x4000000,0x20000);uc.mem_map(0x4100000,0x20000);uc.mem_map(0x4200000,4096)
ADDRESSES={0x21:0xd8bd70,0x22:0xd8bc40,0x23:0xd8bc20,0x24:0xd8bbe0,0x25:0xd8b9e0,0x27:0xd8b9e0,0x28:0xd8bc00,0x29:0xd8bbc0,0x2a:0xd8b9e0,0x2c:0xd8b9e0,0x2d:0xd8a940,0x61:0xd90e50,0x63:0xd91590,0x64:0xd8d480,0x65:0xd8d460,0x66:0xd8d3a0,0x68:0xd8d3a0,0x69:0xd912e0,0x6a:0xd912c0,0x6b:0xd912a0,0x6c:0xd911e0,0x6d:0xd90a60}

def runtime_bytes(raw):
    op,n=struct.unpack_from('>HH',raw);code=op&255
    scalar=code<0x60;packed=code in (0x63,0x64,0x65,0x66,0x68)
    seg=code in (0x25,0x27,0x2a,0x2c,0x66,0x68,0x6c)
    dense=code in (0x21,0x22,0x2d,0x61,0x63,0x69,0x6d)
    hs=4 if code in (0x2d,0x61,0x6d) else (12 if seg else 8) if scalar else (24 if seg else 20) if packed else (18 if seg else 14)
    # The opcode bytes are left verbatim, as the game's loader does.
    out=raw[:2]+struct.pack('<%dH'%((hs-2)//2),*struct.unpack_from('>%dH'%((hs-2)//2),raw,2));pos=hs
    def table(width):
        nonlocal pos,out
        size=n*width+(n%2 if width==1 else 0)
        if width==1:out+=raw[pos:pos+size]
        else:out+=struct.pack('<%dH'%n,*struct.unpack_from('>%dH'%n,raw,pos))
        pos+=size
    if seg:
        flags=struct.unpack_from('>H',raw,hs-2)[0]
        for bit in range(3):table(2 if flags&(1<<bit) else 1)
    elif not dense:table(2 if code in (0x24,0x29,0x65,0x6b) else 1)
    word=4 if packed or code in (0x2d,0x6d) else 1 if code in (0x21,0x23,0x24,0x25,0x27) else 2
    payload=raw[pos:]
    if word==1:out+=payload
    else:
        fmt='%d%s'%(len(payload)//word,'I' if word==4 else 'H')
        out+=struct.pack('<'+fmt,*struct.unpack('>'+fmt,payload))
    return out

# Capture x87 return values of dense scalar 21/22 without host ABI assumptions.
uc.mem_write(0x4200000,b'\xd9\x1d'+struct.pack('<I',0x4200100))
uc.mem_write(0x3087cf0, struct.pack('<4f', *([-0.39936140179634094]*4)))
uc.mem_write(0x3087d00, struct.pack('<4f', *([0.5069268941879272]*4)))
count=0;worst=0;seen={};flags_seen={};half_seen=set()
files=['p_man_fay_whan.mmo','p_man_adventure.mmo','p_man_wasa.mmo','p_man_all.mmo']
for filename in files:
 b=Path('/tmp/motion_samples',filename).read_bytes()
 for c in m.parse_momo(b,False):
  m.decode_clip(b,c)
  for t in c.tracks:
   if t.times is None:continue
   op=t.opcode&255
   raw=b[t.offset:t.offset+len(t.words)*2]
   flag=struct.unpack_from('>H',raw,{0x2a:10,0x2c:10,0x66:22,0x68:22,0x6c:16}.get(op,0))[0] if op in (0x2a,0x2c,0x66,0x68,0x6c) else 0
   bucket=(op,flag,t.opcode>>8&1)
   if seen.get(bucket,0)>=3:continue
   seen[bucket]=seen.get(bucket,0)+1;flags_seen.setdefault(hex(op),set()).add(flag)
   uc.mem_write(0x4000000,runtime_bytes(raw))
   frames=[0,.5,7,21,c.frames/2,c.frames,c.frames+10]
   # Exercise run boundaries and gaps too, not just uniform frames.
   if op in (0x2a,0x2c,0x66,0x68,0x6c):
    frames+= [(v+d)*(2 if t.opcode>>8&1 else 1) for v in t.times[::max(1,len(t.times)//5)] for d in (.0,.25)]
   for frame in frames:
    time=frame*(.5 if t.opcode>>8&1 else 1)
    esp=0x411f000;uc.reg_write(UC_X86_REG_ESP,esp)
    uc.mem_write(esp,struct.pack('<IIf',0x4200000,0x4000000,time))
    uc.emu_start(ADDRESSES[op],0x4200006,count=1000000)
    val=struct.unpack('<4f',uc.reg_read(UC_X86_REG_XMM0).to_bytes(16,'little'))
    own=m.sample_track(t,frame)
    if op in (0x21,0x22,0x2d):val=(struct.unpack('<f',uc.mem_read(0x4200100,4))[0],)
    if op<0x60:err=abs(val[0]-own)
    else:err=min(max(abs(a-b) for a,b in zip(val,own)),max(abs(a+b) for a,b in zip(val,own)))
    assert err<3e-5,(filename,c.name,hex(op),flag,frame,val,own,err)
    count+=1;worst=max(worst,err)
print('ORACLE PASS',count,'samples','maxerror',worst,'flags', {k:sorted(v) for k,v in flags_seen.items()})

# Synthetic coverage for all run-table width flags and the byte scalar codecs
# not encountered in this human-motion corpus.
synthetic = 0
for code in (0x25, 0x27, 0x2a, 0x2c, 0x66, 0x68, 0x6c):
 for bits in range(8):
  byte=code in (0x25,0x27); packed=code in (0x66,0x68)
  flags=bits | (8 if byte else 0)
  n=264 if bits&2 else 4
  raw=struct.pack('>HHH',code,2,n)
  if code<0x60:raw+=struct.pack('>hHH',-16,128,flags)
  elif packed:raw+=struct.pack('>4h4HH',0,0,0,0,20000,20000,20000,20000,flags)
  else:raw+=struct.pack('>hH3hH',1000,32767,0,32767,0,flags)
  last=300 if bits&1 else 5
  for bit,values in enumerate(((0,last),(0,260 if bits&2 else 2),(2,2))):
   raw+=struct.pack('>2'+('H' if bits&(1<<bit) else 'B'),*values)
  if packed:raw+=struct.pack('>%dI'%n,*[(((i%50)<<22)|((i%60)<<12)|((i%70)<<2)|3) for i in range(n)])
  else:raw+=struct.pack('>%d%s'%(n,'b' if byte else 'h'),*[i%100-50 for i in range(n)])
  tracks,_=m._parse_tracks(struct.pack('>I',1)+raw,0,len(raw)+4);track=tracks[0]
  uc.mem_write(0x4000000,runtime_bytes(raw))
  for frame in (0,.25,1,1.5,last/2,last,last+.5,last+1):
   esp=0x411f000;uc.reg_write(UC_X86_REG_ESP,esp)
   uc.mem_write(esp,struct.pack('<IIf',0x4200000,0x4000000,frame))
   uc.emu_start(ADDRESSES[code],0x4200006,count=1000000)
   val=struct.unpack('<4f',uc.reg_read(UC_X86_REG_XMM0).to_bytes(16,'little'))
   own=m.sample_track(track,frame)
   err=abs(val[0]-own) if code<0x60 else min(max(abs(a-b) for a,b in zip(val,own)),max(abs(a+b) for a,b in zip(val,own)))
   assert err<3e-5,(hex(code),bits,frame,val,own,err)
   synthetic+=1
print('SYNTHETIC ORACLE PASS',synthetic,'evaluations; all 8 table-width combinations, pool offsets >255 and gaps')
scalar_samples = 0
for code in (0x21, 0x22, 0x23, 0x24, 0x2d):
 for rate in (0, 1):
  raw=struct.pack('>HH',(rate<<8)|code,3)
  if code==0x2d:raw+=struct.pack('>3f',-.2,.3,1.5)
  else:
   raw+=struct.pack('>hH',-16,128)
   if code in (0x23,0x24):raw+=struct.pack('>3B',0,2,6)+b'\0' if code==0x23 else struct.pack('>3H',0,2,6)
   raw+=struct.pack('>3h',-100,0,100) if code==0x22 else struct.pack('>3b',-100,0,100)+b'\0'
  tracks,_=m._parse_tracks(struct.pack('>I',1)+raw,0,len(raw)+4);track=tracks[0]
  uc.mem_write(0x4000000,runtime_bytes(raw))
  for frame in (0,.25,1,1.5,3,6,10):
   esp=0x411f000;uc.reg_write(UC_X86_REG_ESP,esp)
   uc.mem_write(esp,struct.pack('<IIf',0x4200000,0x4000000,frame*(.5 if rate else 1)))
   uc.emu_start(ADDRESSES[code],0x4200006,count=1000000)
   val=(struct.unpack('<f',uc.mem_read(0x4200100,4))[0] if code in (0x21,0x22,0x2d)
        else struct.unpack('<f',uc.reg_read(UC_X86_REG_XMM0).to_bytes(16,'little')[:4])[0])
   assert abs(val-m.sample_track(track,frame))<3e-5,(hex(code),rate,frame,val,m.sample_track(track,frame))
   scalar_samples+=1
print('SCALAR ORACLE PASS',scalar_samples,'evaluations; dense/byte-time/u16-time, byte padding and both time rates')
