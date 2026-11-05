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

    def test_temporal_scalar_and_flag_time_scale(self):
        payload = struct.pack('>HHhH', 0x28, 2, 32, 100) + bytes((0, 10)) + struct.pack('>2h', 0, 100)
        tracks, data = self.parse(payload)
        self.assertAlmostEqual(mmo.sample_track(tracks[0], 5), 1.5)
        self.assertEqual(mmo.sample_track(tracks[0], -1), 1)
        self.assertEqual(mmo.sample_track(tracks[0], 99), 2)
        flagged = bytearray(data); flagged[4:6] = b'\x01\x28'
        t = mmo._parse_tracks(flagged, 0, len(flagged))[0][0]
        self.assertAlmostEqual(mmo.sample_track(t, 10), 1.5)
        short_times = struct.pack('>HHhH2H2h', 0x29, 2, 32, 100, 0, 300, 0, 100)
        short_tracks, _ = self.parse(short_times)
        self.assertAlmostEqual(mmo.sample_track(short_tracks[0], 150), 1.5)
        clip = mmo.Clip(); clip.tracks = tracks
        with self.assertRaisesRegex(ValueError, 'temporal channel editing'):
            mmo.write_track_values(data, clip, [4])

    def test_temporal_smallest_three_and_axis_angle(self):
        # Omitted W, all quantized XYZ zero: identity at both keys.
        payload = struct.pack('>HH4h4H', 0x64, 2, 0, 0, 0, 0, 32768, 32768, 32768, 32768)
        payload += bytes((0, 10)) + struct.pack('>2I', 3, 3)
        tracks, _ = self.parse(payload)
        self.assertEqual(mmo.sample_track(tracks[0], 5), (0, 0, 0, 1))
        # Fixed X axis, half-angle goes from 0 to pi/2.
        payload = struct.pack('>HHhH3h', 0x6a, 2, 0, 32767, 32767, 0, 0)
        payload += bytes((0, 10)) + struct.pack('>2h', 0, 32767)
        tracks, _ = self.parse(payload)
        q = mmo.sample_track(tracks[0], 5)
        self.assertAlmostEqual(q[0], math.sqrt(.5))
        self.assertAlmostEqual(q[3], math.sqrt(.5))
        # Both truncation and non-increasing times must fail, no partial import.
        with self.assertRaises(ValueError):
            self.parse(payload[:-1])
        with self.assertRaises(ValueError):
            self.parse(payload[:14] + bytes((10, 10)) + payload[16:])

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

    def test_dense_scalar_and_padding(self):
        for code in (0x21, 0x22):
            vals = (-100, 0, 100)
            payload = struct.pack('>HHhH', code, 3, -32, 128)
            payload += struct.pack('>3b', *vals)+b'\0' if code == 0x21 else struct.pack('>3h', *vals)
            tracks, data = self.parse(payload)
            t = tracks[0]
            self.assertEqual(t.times, [0, 1, 2])
            expected = (-32/16384+100*128/(16384*256)) if code == 0x21 else -1+100/128
            self.assertAlmostEqual(mmo.sample_track(t, 2), expected)
            self.assertEqual(mmo.sample_track(t, 20), expected)
            c = mmo.Clip(); c.tracks = tracks
            before = data[:]; mmo.write_track_values(data, c, [t.value])
            self.assertEqual(data, before)
        tracks, _ = self.parse(struct.pack('>HH3f', 0x2d, 3, 0, -1, 3))
        self.assertAlmostEqual(mmo.sample_track(tracks[0], 1.5), 1)

    def test_all_run_flags_gaps_and_bounds(self):
        for code in (0x25, 0x27, 0x2a, 0x2c):
            byte = code in (0x25, 0x27)
            for bits in range(8):
                flags = bits | (8 if byte else 0)
                payload = struct.pack('>HHHhHH', code, 2, 4, 0, 128, flags)
                for bit, values in enumerate(((0, 5), (0, 2), (2, 2))):
                    payload += struct.pack('>2'+('H' if bits & (1<<bit) else 'B'), *values)
                payload += struct.pack('>4'+('b' if byte else 'h'), 0, 10, 30, 40)
                t = self.parse(payload)[0][0]
                self.assertEqual(t.times, [0, 1, 5, 6])
                self.assertAlmostEqual(mmo.sample_track(t, 3), (t.samples[1]+t.samples[2])/2)
                self.assertEqual(mmo.sample_track(t, 100), t.samples[-1])
                # Each segment must stay inside the value pool.
                bad = bytearray(payload)
                # Last length is located before the four value entries.
                width = 2 if bits & 4 else 1
                pos = len(payload)-4*(1 if byte else 2)-width
                bad[pos:pos+width] = (99).to_bytes(width, 'big')
                with self.assertRaisesRegex(ValueError, 'run exceeds'):
                    self.parse(bad)

    def test_dense_euler_codecs(self):
        for code, fmt, xyz in ((0x61, '3H', (2000, 4000, 6000)),
                                (0x6d, '3f', (.2, .3, .4))):
            t = self.parse(struct.pack('>HH'+fmt, code, 1, *xyz))[0][0]
            self.assertAlmostEqual(sum(v*v for v in t.value), 1)
            self.assertEqual(mmo.sample_track(t, 100), t.value)
        t = self.parse(struct.pack('>HH3f', 0x6d, 1, .2, .3, .4))[0][0]
        for own, native in zip(t.value, (.0672042668, .1653387547, .1808355749, .9671841860)):
            self.assertAlmostEqual(own, native, places=6)

    def test_partial_tail_mapping(self):
        tracks, _ = self.parse(b'\0\x2e'*3, 3)
        c = mmo.Clip(); c.tracks = tracks
        rig = {'channel_indices': [0, 1, 2, 3, 4], 'node_flags': [1, 0],
               'node_offsets': [0, 4], 'rest_positions': [(0, 0, 0)]*2}
        with self.assertRaises(ValueError):
            mmo.pose_nodes(c, rig)
        self.assertEqual(mmo.pose_nodes(c, rig, allow_missing_tail=True),
                         [((0, 0, 0), (0, 0, 0, 1)), None])

    def test_mts_rig_declaration(self):
        payload = struct.pack('>H', 4)+b'lib\0'+struct.pack('>H', 6)+b'p_man\0'
        data = b'MOMO'+bytes(12)+b'MTS0'+struct.pack('>I', len(payload)+8)+payload
        self.assertEqual(mmo.momo_rig_name(data), 'p_man')
        self.assertIsNone(mmo.momo_rig_name(b'MOMO'+bytes(12)))
        with self.assertRaises(ValueError):
            mmo.momo_rig_name(
                b'MOMO'+bytes(12)+b'MTS0'+struct.pack('>I', 10)+b'\0\xff')


if __name__ == '__main__':
    unittest.main()
