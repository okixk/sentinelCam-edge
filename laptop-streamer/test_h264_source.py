"""Unit tests for the Annex-B access-unit splitter and the MJPEG sidecar
splitter. Pure Python — run with ``python3 -m unittest test_h264_source``.
"""
import unittest

from h264_source import AnnexBSplitter, JpegStreamSplitter, build_ffmpeg_cmd

S3 = b"\x00\x00\x01"
S4 = b"\x00\x00\x00\x01"

# first payload bit set => first_mb_in_slice == 0 (start of a new picture)
SPS = b"\x67\x42\x00\x1e\x8c"
PPS = b"\x68\xce\x3c\x80"
IDR = b"\x65\x88\x84\x00\x11"
P0 = b"\x41\x9a\x11\x22"      # P slice, first_mb_in_slice == 0
P_CONT = b"\x41\x0a\x33\x44"  # P slice, first_mb_in_slice != 0 (same picture)
AUD = b"\x09\xf0"
SEI = b"\x06\x05\x01\x00"


def _collect(splitter, *chunks, flush=True):
    out = []
    for c in chunks:
        out.extend(splitter.feed(c))
    if flush:
        out.extend(splitter.flush())
    return out


class AnnexBSplitterTest(unittest.TestCase):
    def test_aud_delimited_stream(self):
        stream = (S4 + AUD + S4 + SPS + S4 + PPS + S4 + IDR
                  + S4 + AUD + S4 + P0
                  + S4 + AUD + S4 + P0)
        aus = _collect(AnnexBSplitter(), stream)
        self.assertEqual(len(aus), 3)
        self.assertEqual(aus[0], (S4 + SPS + S4 + PPS + S4 + IDR, True))
        self.assertEqual(aus[1], (S4 + P0, False))
        self.assertEqual(aus[2], (S4 + P0, False))

    def test_no_aud_uses_first_mb_boundary(self):
        stream = S4 + SPS + S4 + PPS + S4 + IDR + S4 + P0 + S4 + P0
        aus = _collect(AnnexBSplitter(), stream)
        self.assertEqual([kf for _au, kf in aus], [True, False, False])

    def test_multi_slice_picture_stays_one_au(self):
        stream = S4 + IDR + S4 + P0 + S4 + P_CONT + S4 + P0
        aus = _collect(AnnexBSplitter(), stream)
        # IDR | (P0 + continuation slice) | P0
        self.assertEqual(len(aus), 3)
        self.assertEqual(aus[1][0], S4 + P0 + S4 + P_CONT)

    def test_sps_pps_injected_before_bare_idr(self):
        stream = (S4 + SPS + S4 + PPS + S4 + IDR   # stream start: params inline
                  + S4 + P0
                  + S4 + IDR                        # later IDR WITHOUT params
                  + S4 + P0)
        aus = _collect(AnnexBSplitter(), stream)
        self.assertEqual(len(aus), 4)
        bare_idr_au, is_kf = aus[2]
        self.assertTrue(is_kf)
        # cached SPS+PPS must be prepended so the AU is self-describing
        self.assertEqual(bare_idr_au, S4 + SPS + S4 + PPS + S4 + IDR)

    def test_three_byte_start_codes_and_chunking(self):
        stream = S3 + SPS + S3 + PPS + S3 + IDR + S3 + P0 + S3 + P0
        for cut in range(1, len(stream)):
            aus = _collect(AnnexBSplitter(), stream[:cut], stream[cut:])
            self.assertEqual(
                [kf for _au, kf in aus], [True, False, False],
                f"failed when chunked at byte {cut}",
            )

    def test_sei_belongs_to_next_au(self):
        stream = S4 + IDR + S4 + SEI + S4 + P0 + S4 + P0
        aus = _collect(AnnexBSplitter(), stream)
        self.assertEqual(len(aus), 3)
        self.assertEqual(aus[0][0], S4 + IDR)
        self.assertEqual(aus[1][0], S4 + SEI + S4 + P0)

    def test_oversize_au_dropped(self):
        sp = AnnexBSplitter(max_au_bytes=64)
        big = b"\x65" + b"\x88" + b"\x00" * 100
        aus = _collect(sp, S4 + big + S4 + P0 + S4 + P0)
        self.assertEqual(len(aus), 2)  # the oversized IDR AU is gone
        self.assertEqual(sp.dropped_oversize, 1)

    def test_param_only_fragment_not_shipped(self):
        aus = _collect(AnnexBSplitter(), S4 + SPS + S4 + PPS)
        self.assertEqual(aus, [])


class JpegStreamSplitterTest(unittest.TestCase):
    J1 = b"\xff\xd8\xff\xe0" + b"a" * 10 + b"\xff\xd9"
    J2 = b"\xff\xd8\xff\xdb" + b"b" * 20 + b"\xff\xd9"

    def test_split_concatenated(self):
        sp = JpegStreamSplitter()
        out = list(sp.feed(self.J1 + self.J2))
        self.assertEqual(out, [self.J1, self.J2])

    def test_split_across_chunks(self):
        stream = self.J1 + self.J2
        for cut in range(1, len(stream)):
            sp = JpegStreamSplitter()
            out = list(sp.feed(stream[:cut])) + list(sp.feed(stream[cut:]))
            self.assertEqual(out, [self.J1, self.J2], f"cut at {cut}")


class FfmpegCmdTest(unittest.TestCase):
    def test_v4l2_with_sidecar(self):
        cmd = build_ffmpeg_cmd(
            mode="v4l2", device=0, width=1920, height=1080, fps=30,
            bitrate_kbps=4500, gop_seconds=1.0, encoder="h264_v4l2m2m",
            sidecar_fps=2, sidecar_height=540, sidecar_fd=7,
        )
        joined = " ".join(cmd)
        self.assertIn("-input_format mjpeg", joined)
        self.assertIn("-video_size 1920x1080", joined)
        self.assertIn("-c:v h264_v4l2m2m", joined)
        self.assertIn("-g 30", joined)
        self.assertIn("h264_metadata=aud=insert", joined)
        self.assertIn("pipe:7", joined)
        self.assertLess(joined.index("pipe:1"), joined.index("pipe:7"))

    def test_pipe_mode_libx264(self):
        cmd = build_ffmpeg_cmd(
            mode="pipe", device=0, width=1280, height=720, fps=15,
            bitrate_kbps=2000, gop_seconds=2.0, encoder="libx264",
        )
        joined = " ".join(cmd)
        self.assertIn("-f rawvideo", joined)
        self.assertIn("repeat-headers=1:keyint=30:min-keyint=30", joined)
        self.assertNotIn("pipe:0 -i", joined)

    def test_native_h264_copy(self):
        cmd = build_ffmpeg_cmd(
            mode="v4l2-h264", device=2, width=1920, height=1080, fps=30,
            bitrate_kbps=4500, gop_seconds=1.0, encoder="h264_v4l2m2m",
        )
        joined = " ".join(cmd)
        self.assertIn("-input_format h264", joined)
        self.assertIn("-c:v copy", joined)
        self.assertNotIn("-b:v", joined)


if __name__ == "__main__":
    unittest.main()
