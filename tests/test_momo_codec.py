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

    def test_constant_flag_variants_and_zero(self):
        payload = (struct.pack('>HhHh', 0x20, -1, 32767, 16) +
                   struct.pack('>H4h', 0x62, 1001, 0, 0, 32752) +
                   b'\x00\x6e\x00\x2e')
        tracks, data = self.parse(payload, 4)
        self.assertAlmostEqual(tracks[0].value, -1/32 + 16/32767)
        self.assertEqual(tracks[1].value, (1001/32767, 0, 0, 32752/32767))
        self.assertEqual(tracks[2].value, (0, 0, 0, 1))
        self.assertEqual(tracks[3].value, 0)
        clip = mmo.Clip(); clip.tracks = tracks
        before = bytes(data)
        mmo.write_track_values(data, clip, [t.value for t in tracks])
        self.assertEqual(bytes(data), before)
        values = [t.value for t in tracks]; values[3] = 1
        with self.assertRaisesRegex(ValueError, 'zero scalar'):
            mmo.write_track_values(data, clip, values)
        self.assertEqual(bytes(data), before)
        with self.assertRaises(ValueError):
            self.parse(b'\x02\x2e')

    def test_metadata_index_and_deferred_decode(self):
        def chunk(tag, payload):
            return tag + struct.pack('>I', len(payload) + 8) + payload
        def clip(name, channel):
            header = struct.pack('>4HIf', 0, 0, 0, 1, 0, 30.0)
            return chunk(b'RAW0', chunk(b'HED0', header) +
                         chunk(b'HED1', bytes((0, len(name))) + name) +
                         chunk(b'PRG2', b'') +
                         chunk(b'TRK2', struct.pack('>I', 1) + channel))
        data = b'MOMO' + bytes(12) + clip(b'zero', b'\x00\x2e')
        clips = mmo.parse_momo(bytearray(data), decode_tracks=False)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].name, 'zero')
        self.assertEqual(clips[0].header, 1)
        self.assertIsNone(clips[0].tracks)
        mmo.decode_clip(data, clips[0])
        self.assertEqual(clips[0].tracks[0].value, 0)
        unknown = b'MOMO' + bytes(12) + clip(b'unknown', b'\x01\x63')
        indexed = mmo.parse_momo(unknown, decode_tracks=False)
        with self.assertRaises(ValueError):
            mmo.decode_clip(unknown, indexed[0])

    def test_node_table_indirection_and_procedural_node(self):
        tracks, _ = self.parse(b'\x00\x2e' * 8 +
                               b'\x00\x2e' * 3 + b'\x00\x6e', 12)
        clip = mmo.Clip(); clip.tracks = tracks
        rig = {'channel_indices': [8, 9, 10, 11],
               'node_flags': [1, 6], 'node_offsets': [0, 4]}
        self.assertEqual(mmo.pose_nodes(clip, rig), [((0, 0, 0), (0, 0, 0, 1)), None])
        rig['channel_indices'][-1] = 12
        with self.assertRaisesRegex(ValueError, 'channel index'):
            mmo.pose_nodes(clip, rig)

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
