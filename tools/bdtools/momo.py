"""Binary Domain MOMO (.mmo) animation container parser.

MOMO layout (reverse-engineered from the real BD archive, 2026-11-04):

    +0x00  b'MOMO'
    +0x04  u32 LE flags (0x01ff)
    +0x10  sections: [4B tag][u32 BE size (incl. 8B header)][payload]

    MTS0  motion-set header:
          +0x00 u16 LE name_len
          +0x02 set name (12B, null padded)
          +0x0e u8 char_len
          +0x0f char name (8B, null padded)
          +0x17 ...
    RAW0  one animation clip (the very first RAW0 is the base/t-pose):
          HED0 (16B):
            +0x00 u16 LE (clip_index << 8) | flags
            +0x02 u16 LE 3 << 8
            +0x04 u16 LE frame_count << 8        (1 for t-pose/loops)
            +0x06 u16 LE fps << 8                 (30)
            +0x08 u16 LE 0
            +0x0a u16 LE (1 << 8) if looping else 0
            +0x0c f32 BE 1.0 (time scale)
          HED1:
            +0x00 u16 LE name_len
            +0x02 clip name (up to 16B, null padded)
            +0x12 u8 flag
            +0x13 7B unknown (pose reference)
          PRG2 (24B): program/event data (unknown)
          TRK2: per-track keyframe data, custom compressed (not yet decoded)

Verified against: a_asa_basic.mmo (stub), p_man_all.mmo (6765 clips),
f_man_all.mmo, e_* character sets.

Usage:
    from bdtools.momo import parse_momo_file
    info = parse_momo_file('data/motion/binaries/p_man_all.mmo')
    for clip in info['clips']:
        print(clip['name'], clip['frames'], clip['fps'])
"""
from __future__ import annotations

import os
import struct
from typing import Dict, List, Optional

KNOWN_TAGS = (b"MTS0", b"RAW0", b"HED0", b"HED1", b"PRG2", b"TRK2")


def _read_section(buf: bytes, off: int):
    """Read one [4B tag][u32 BE size] section at off. -> (tag, payload, next) | None"""
    if off + 8 > len(buf):
        return None
    tag = buf[off:off + 4]
    if tag not in KNOWN_TAGS:
        return None
    size = struct.unpack_from(">I", buf, off + 4)[0]
    if size < 8 or off + size > len(buf):
        return None
    return tag, buf[off + 8:off + size], off + size


def _cstr(buf: bytes, max_len: int) -> str:
    return buf[:max_len].split(b"\x00")[0].decode("shift_jis", "replace")


def parse_momo_file(path: str) -> Dict:
    """Parse a .mmo (MOMO) file into a metadata dict.

    Returns:
        {
          'file': str, 'set': str, 'char': str,
          'clips': [
            {'name': str, 'index': int, 'frames': int, 'fps': int,
             'duration': float, 'loop': bool, 'trk2_bytes': int},
            ...
          ],
        }
    """
    d = open(path, "rb").read()
    if d[:4] != b"MOMO":
        raise ValueError(f"{path}: not a MOMO file")
    info: Dict = {
        "file": os.path.basename(path),
        "set": "",
        "char": "",
        "clips": [],
    }
    off = 0x10
    while off < len(d):
        s = _read_section(d, off)
        if s is None:
            break
        tag, payload, nxt = s
        if tag == b"MTS0":
            nlen = struct.unpack_from("<H", payload, 0)[0] >> 8
            info["set"] = _cstr(payload[2:2 + max(nlen, 12)], nlen)
            # char name: [u8 len][name] with variable padding after the
            # 12-byte set-name field -> scan for the len byte
            char = ""
            for idx in range(12, len(payload) - 9):
                clen = payload[idx]
                if 4 <= clen <= 12:
                    body = payload[idx + 1:idx + 1 + clen - 1]
                    if all(32 <= b < 127 for b in body):
                        char = body.decode("shift_jis", "replace")
                        break
            info["char"] = char
        elif tag == b"RAW0":
            children = {}
            io = 0
            while io < len(payload):
                si = _read_section(payload, io)
                if si is None:
                    break
                itag, ipayload, inxt = si
                children[itag] = ipayload
                io = inxt
            h0 = children.get(b"HED0")
            h1 = children.get(b"HED1")
            if h0 is None or h1 is None:
                off = nxt
                continue
            idx = struct.unpack_from("<H", h0, 0)[0] >> 8
            frames = struct.unpack_from("<H", h0, 4)[0] >> 8
            fps = struct.unpack_from("<H", h0, 6)[0] >> 8
            loop = (struct.unpack_from("<H", h0, 0xA)[0] >> 8) != 0
            nlen = struct.unpack_from("<H", h1, 0)[0]
            name = h1[2:2 + nlen].split(b"\x00")[0].decode("shift_jis",
                                                             "replace")
            info["clips"].append({
                "name": name,
                "index": idx,
                "frames": frames,
                "fps": fps,
                "duration": round(frames / fps, 4) if fps else 0.0,
                "loop": loop,
                "trk2_bytes": len(children.get(b"TRK2", b"")),
            })
        off = nxt
    return info


def catalog_motion_dir(motion_dir: str) -> List[Dict]:
    """Parse every .mmo under motion_dir -> list of per-file metadata dicts."""
    out = []
    for root, _dirs, files in os.walk(motion_dir):
        for f in sorted(files):
            if f.endswith(".mmo"):
                try:
                    out.append(parse_momo_file(os.path.join(root, f)))
                except Exception as e:  # keep going on malformed files
                    out.append({"file": f, "error": repr(e), "clips": []})
    return out
