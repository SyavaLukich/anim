"""Pure Python MOMO codec tests, no game data or Blender needed."""
import importlib.util
import math
from pathlib import Path
import struct
import unittest

spec = importlib.util.spec_from_file_location(
    'momo_codec', Path(__file__).resolve().parents[1] / 'io_binary_domain/mmo.py')
mmo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mmo)


class ConstantChannels(unittest.TestCase):
    def parse(self, payload, count=1):
        data = struct.pack('>I', count) + payload
        return mmo._parse_tracks(data, 0, len(data))[0], bytearray(data)

    def test_signed_scalar_formula(self):
        tracks, _ = self.parse(struct.pack('>HhHh', 0x120, -1, 32767, 16))
        self.assertAlmostEqual(tracks[0].value, -1/32 + 16/32767)
        tracks, _ = self.parse(struct.pack('>HhHh', 0x120, 51, 32767, 217))
        self.assertAlmostEqual(tracks[0].value, 1.6003725165562914)

    def test_quaternion(self):
        tracks, _ = self.parse(struct.pack('>H4h', 0x162, 1001, 0, 0, 32752))
        self.assertEqual(tracks[0].value, (1001/32767, 0, 0, 32752/32767))
        tracks, _ = self.parse(b'\x01\x6e')
        self.assertEqual(tracks[0].value, (0, 0, 0, 1))

    def test_mixed_node_and_exact_identity_patch(self):
        p = b''.join(struct.pack('>HhHh', 0x120, i, 32767, 217)
                     for i in range(3)) + b'\x01\x6e'
        tracks, data = self.parse(p, 4)
        c = mmo.Clip()
        c.tracks = tracks
        original = bytes(data)
        self.assertEqual(mmo.write_track_values(data, c, [t.value for t in tracks]), 4)
        self.assertEqual(data, original)
        self.assertEqual(len(mmo.pose_nodes(c)), 1)
        values = [t.value for t in tracks]
        values[0] = -0.025
        mmo.write_track_values(data, c, values)
        changed = mmo._parse_tracks(data, 0, len(data))[0]
        self.assertLess(abs(changed[0].value + 0.025), 1/32767)
        self.assertEqual(bytes(data[12:]), original[12:])

    def test_unsupported_and_invalid(self):
        for payload in [b'\x01\x63', b'\x01\x20\x00',
                        struct.pack('>HhHh', 0x120, 0, 0, 0), b'\x01\x6e\x00\x00']:
            with self.assertRaises(ValueError):
                self.parse(payload)

    def test_no_partial_patch_on_error(self):
        tracks, data = self.parse(struct.pack('>HhHh', 0x120, 0, 32767, 1) + b'\x01\x6e', 2)
        c = mmo.Clip(); c.tracks = tracks
        original = bytes(data)
        with self.assertRaises(ValueError):
            mmo.write_track_values(data, c, [0.5, (1, 0, 0, 0)])
        self.assertEqual(data, original)
        with self.assertRaises(ValueError):
            mmo.write_track_values(data, c, [math.inf, tracks[1].value])
        self.assertEqual(data, original)


if __name__ == '__main__':
    unittest.main()
