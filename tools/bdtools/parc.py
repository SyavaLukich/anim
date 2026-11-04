"""PARC archive parser/extractor for Binary Domain (Yakuza-engine) files.

PARC layout (little-endian data, verified against Gibbed.Yakuza0
ArchiveFile.cs and the real BD .par files):

    +0x00  u32 BE  magic 'PARC' (0x50415243)
    +0x04  u8      version (2)
    +0x05  u8      endianness (0 = little, 1 = big)
    +0x06  u8      header size high byte
    +0x07  u8      unknown (0)
    +0x08  u32     0x00020001
    +0x0C  u32     header size low (0 = computed)
    +0x10  u32     directory count
    +0x14  u32     directory table offset (from file start)
    +0x18  u32     file count
    +0x1C  u32     file table offset (from file start)
    +0x20  dir names: 64 bytes each (SJIS/cp932, NUL padded)
             file names: 64 bytes each
    dir table: 32-byte entries  (s32 dirCount, s32 dirIndex, s32 fileCount, s32 fileIndex, 4x u32 unk)
    file table: 32-byte entries:
        +0x00 u32 compression flags   (bit 0x80000000 = SLLZ-compressed)
        +0x04 u32 uncompressed size
        +0x08 u32 compressed size (includes the 16-byte SLLZ header)
        +0x0C u32 data offset low
        +0x10 u32 unknown (header size?)
        +0x14 u32 unknown (low nibble = data offset high)
        +0x18 u32 unknown (0)
        +0x1C u32 unknown (data hash?)

Compressed files are SLLZ:

    +0x00 'SLLZ'   +0x04 u8 endian   +0x05 u8 version (1|2)
    +0x06 u16 header size (16)  +0x08 u32 uncompressed size
    +0x0C u32 compressed size   +0x10 payload

    v1: LZSS - per-bit opcodes (MSB first): 1 = copy (u16 LE: dist = 1 + f>>4,
        count = 3 + f&0xF), 0 = literal byte.
    v2: 5-byte chunk headers: csize = b0..b2 (bit 0x800000 = raw),
        dsize = (b3<<8|b4)+1; zlib stream or raw copy.

CLI:

    python3 -m bdtools.parc list   in.par
    python3 -m bdtools.parc extract in.par outdir [--prefix NAME]
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib

ALIGN = 2048
SIG = 0x50415243  # 'PARC'


# ---------------------------------------------------------------------------
# SLLZ
# ---------------------------------------------------------------------------

def sllz_decompress(data: bytes) -> bytes:
    """Decompress SLLZ v1 or v2 payload (header included)."""
    if len(data) < 16 or struct.unpack_from(">I", data, 0)[0] != 0x534C4C5A:
        raise ValueError("bad SLLZ magic")
    endian_byte = data[4]
    version = data[5]
    hdr_size = struct.unpack_from("<H", data, 6)[0]
    usize, csize = struct.unpack_from("<II", data, 8)
    payload = data[hdr_size:]
    if version == 1:
        return _sllz_v1(payload, usize)
    if version == 2:
        return _sllz_v2(payload, usize)
    raise ValueError(f"unknown SLLZ version {version}")


def _sllz_v1(src: bytes, usize: int) -> bytes:
    # Bit-packed ops, MSB first.  When the current flag byte is exhausted the
    # next one is consumed *before* the op data (matches the QuickBMS
    # unyakuza.c / Gibbed.Yakuza0 reference exactly).
    out = bytearray(usize)
    ip = op = 0
    flag = src[ip]
    ip += 1
    nbits = 8
    while op < usize:
        bit = (flag >> 7) & 1
        flag <<= 1
        nbits -= 1
        if nbits == 0:
            flag = src[ip]
            ip += 1
            nbits = 8
        if bit:
            f = src[ip] | (src[ip + 1] << 8)
            ip += 2
            dist = 1 + (f >> 4)
            cnt = 3 + (f & 0xF)
            for i in range(cnt):
                out[op] = out[op - dist]
                op += 1
        else:
            out[op] = src[ip]
            ip += 1
            op += 1
    return bytes(out)


def _sllz_v2(src: bytes, usize: int) -> bytes:
    out = bytearray(usize)
    ip = op = 0
    while op < usize:
        csize = (src[ip] << 16) | (src[ip + 1] << 8) | src[ip + 2]
        dsize = ((src[ip + 3] << 8) | src[ip + 4]) + 1
        if csize & 0x00800000:  # raw chunk
            csize &= 0xFF7FFFFF
            out[op:op + dsize] = src[ip + 5:ip + 5 + dsize]
        else:  # zlib chunk
            chunk = zlib.decompress(src[ip + 5:ip + csize])
            if len(chunk) != dsize:
                raise ValueError("SLLZ v2 chunk size mismatch")
            out[op:op + dsize] = chunk
        ip += csize
        op += dsize
    return bytes(out)


# ---------------------------------------------------------------------------
# PARC
# ---------------------------------------------------------------------------

class ParEntry:
    __slots__ = ("path", "compressed", "usize", "csize", "offset", "raw")

    def __init__(self, path, compressed, usize, csize, offset, raw=None):
        self.path = path
        self.compressed = compressed
        self.usize = usize
        self.csize = csize
        self.offset = offset
        self.raw = raw  # full 32-byte table entry (for faithful re-packing)


class ParArchive:
    def __init__(self, data: bytes):
        self.data = data
        self.entries: list[ParEntry] = []
        self.header_raw: bytes = b""
        self._parse()

    def _parse(self):
        d = self.data
        if len(d) < 32 or struct.unpack_from(">I", d, 0)[0] != SIG:
            raise ValueError("bad PARC magic")
        version = d[4]
        if version != 2:
            raise ValueError(f"bad PARC version {version}")
        endian = d[5]
        if endian not in (0, 1):
            raise ValueError("bad PARC endianness")
        fmt = "<" if endian == 0 else ">"
        hi = d[6]
        if d[7] != 0:
            raise ValueError("bad PARC header unknown07")
        unk08 = struct.unpack_from(fmt + "I", d, 8)[0]
        if unk08 != 0x00020001:
            raise ValueError(f"bad PARC header marker {unk08:08x}")
        hlo, dir_count, dir_off, file_count, file_off = \
            struct.unpack_from(fmt + "IIIII", d, 0x0C)
        header_size = (hi << 32) | hlo
        actual = 32 + 64 * (dir_count + file_count) + 32 * dir_count + 32 * file_count
        if header_size and header_size != (actual + ALIGN - 1) // ALIGN * ALIGN:
            raise ValueError("PARC header size mismatch")
        self.header_raw = d[:actual]

        def name(off: int) -> str:
            raw = d[off:off + 64]
            cut = raw.find(b"\x00")
            if cut != -1:
                raw = raw[:cut]
            return raw.decode("cp932", errors="replace")

        names_off = 0x20
        dir_names = [name(names_off + 64 * i) for i in range(dir_count)]
        file_names = [name(names_off + 64 * (dir_count + i)) for i in range(file_count)]

        dir_tab = [struct.unpack_from(fmt + "iiiiIIII", d, dir_off + 32 * i)
                   for i in range(dir_count)]
        file_tab = [struct.unpack_from(fmt + "IIIIIIII", d, file_off + 32 * i)
                    for i in range(file_count)]

        # BFS the directory tree (dir 0 is the root, name ".")
        if dir_count:
            stack = [(0, None)]
            while stack:
                di, base = stack.pop()
                dcnt, didx, fcnt, fidx = dir_tab[di][:4]
                dname = dir_names[di]
                is_root = di == 0 and dname == "."
                path = (base if is_root else
                        os.path.join(base, dname) if base else dname)
                for o in range(didx, didx + dcnt):
                    stack.append((o, path))
                for o in range(fidx, fidx + fcnt):
                    flags, usize, csize, off_lo, u10, u14, u18, u1c = file_tab[o]
                    if flags & 0x7FFFFFFF:
                        raise ValueError(f"bad PARC compression flags {flags:08x}")
                    offset = off_lo | ((u14 & 0xF) << 32)
                    p = os.path.join(path, file_names[o]) if path else file_names[o]
                    self.entries.append(ParEntry(
                        p, bool(flags & 0x80000000), usize, csize, offset,
                        raw=d[file_off + 32 * o:file_off + 32 * o + 32]))
        elif file_count:
            for o in range(file_count):
                flags, usize, csize, off_lo, u10, u14, u18, u1c = file_tab[o]
                offset = off_lo | ((u14 & 0xF) << 32)
                self.entries.append(ParEntry(
                    file_names[o], bool(flags & 0x80000000), usize, csize, offset,
                    raw=d[file_off + 32 * o:file_off + 32 * o + 32]))

    def get(self, path: str) -> bytes:
        for e in self.entries:
            if e.path.replace("\\", "/") == path.replace("\\", "/"):
                return self.read_entry(e)
        raise KeyError(path)

    def read_entry(self, e: ParEntry) -> bytes:
        chunk = self.data[e.offset:e.offset + e.csize]
        if len(chunk) != e.csize:
            raise ValueError(f"truncated data for {e.path}")
        if e.compressed:
            return sllz_decompress(chunk)
        return chunk

    def extract(self, outdir: str, prefix: str | None = None, verbose=False):
        os.makedirs(outdir, exist_ok=True)
        for i, e in enumerate(self.entries):
            out = os.path.join(outdir, prefix or "", e.path)
            os.makedirs(os.path.dirname(out) or outdir, exist_ok=True)
            data = self.read_entry(e)
            with open(out, "wb") as fh:
                fh.write(data)
            if verbose:
                print(f"[{i + 1}/{len(self.entries)}] {e.path} "
                      f"({e.usize}B{' c' if e.compressed else ''})", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PARC archive tool for Binary Domain")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list archive contents")
    p_list.add_argument("archive")

    p_ext = sub.add_parser("extract", help="extract an archive")
    p_ext.add_argument("archive")
    p_ext.add_argument("outdir")
    p_ext.add_argument("--prefix", help="extra directory under outdir")
    p_ext.add_argument("-q", "--quiet", action="store_true")

    args = ap.parse_args(argv)
    data = open(args.archive, "rb").read()
    arc = ParArchive(data)

    if args.cmd == "list":
        for e in arc.entries:
            print(f"{'C ' if e.compressed else '  '}{e.usize:>12} "
                  f"{e.csize:>12} {e.offset:#010x}  {e.path}")
        return 0

    arc.extract(args.outdir, prefix=args.prefix, verbose=not args.quiet)
    print(f"extracted {len(arc.entries)} files", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
