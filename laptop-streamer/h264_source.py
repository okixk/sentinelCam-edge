"""H.264 helpers for the edge streamer.

Annex-B access-unit splitting, MJPEG sidecar splitting, and ffmpeg command
construction for the SC_CODEC=h264 lane. Import-safe without cv2/websockets so
the unit tests (test_h264_source.py) run anywhere.

Wire contract (mirrors sentinelCam-web routes.py ingest):
- one WebSocket binary message == one complete Annex-B access unit, <= 2 MiB,
- every IDR access unit carries SPS+PPS inline so the web hub can start a
  viewer on it and the ffmpeg ``-c:v copy`` mux can build the MSE init segment.
"""
from __future__ import annotations

import os
import sys
from typing import Iterator, Optional

MAX_AU_BYTES = 2 * 1024 * 1024  # must match _MAX_H264_AU_BYTES on the web side

_START3 = b"\x00\x00\x01"

# NAL unit types (H.264 table 7-1)
_NAL_SLICE = 1
_NAL_IDR = 5
_NAL_SEI = 6
_NAL_SPS = 7
_NAL_PPS = 8
_NAL_AUD = 9


class AnnexBSplitter:
    """Reassemble an Annex-B byte stream into complete access units.

    AU boundaries are detected in priority order:
    - an AUD (NAL 9) always starts a new AU (we ask ffmpeg to insert them via
      the h264_metadata bitstream filter, making this the deterministic path),
    - a non-VCL NAL (SEI/SPS/PPS) after a slice belongs to the NEXT picture,
    - a slice with first_mb_in_slice == 0 after a slice starts a new picture
      (the leading ue(v) bit of the slice header is 1 exactly when it is 0).

    The latest SPS/PPS are cached and prepended to any IDR access unit that
    lacks them, so keyframes stay self-describing even if the encoder emits
    parameter sets only once at stream start (common for h264_v4l2m2m and
    camera-native UVC H.264).
    """

    def __init__(self, max_au_bytes: int = MAX_AU_BYTES) -> None:
        self._pending = b""          # raw bytes not yet parsed into NALs
        self._nals: list[bytes] = []  # NALs of the AU under assembly (no start codes)
        self._has_vcl = False
        self._has_idr = False
        self._has_sps = False
        self._sps: Optional[bytes] = None
        self._pps: Optional[bytes] = None
        self._max_au = max_au_bytes
        self.dropped_oversize = 0

    def feed(self, data: bytes) -> Iterator[tuple[bytes, bool]]:
        """Consume encoder output; yield (access_unit, is_keyframe) tuples."""
        self._pending += data
        while True:
            nal = self._next_complete_nal()
            if nal is None:
                return
            yield from self._push_nal(nal)

    def flush(self) -> Iterator[tuple[bytes, bool]]:
        """Emit the trailing AU at end of stream (the last NAL has no successor)."""
        tail = self._pending
        self._pending = b""
        start = tail.find(_START3)
        if start != -1:
            yield from self._push_nal(tail[start + 3:])
        if self._nals:
            au = self._assemble()
            if au is not None:
                yield au

    # -- internals ----------------------------------------------------------

    def _next_complete_nal(self) -> Optional[bytes]:
        """Pop one NAL (without start code) once its successor start code arrives."""
        buf = self._pending
        start = buf.find(_START3)
        if start == -1:
            # keep at most 2 bytes — they may be the prefix of a split start code
            self._pending = buf[-2:]
            return None
        nal_start = start + 3
        nxt = buf.find(_START3, nal_start)
        if nxt == -1:
            return None  # NAL still incomplete; wait for more bytes
        end = nxt
        if end > nal_start and buf[end - 1] == 0:  # 4-byte start code
            end -= 1
        self._pending = buf[nxt:]
        return buf[nal_start:end]

    def _push_nal(self, nal: bytes) -> Iterator[tuple[bytes, bool]]:
        if not nal:
            return
        ntype = nal[0] & 0x1F
        if ntype == _NAL_SPS:
            self._sps = nal
        elif ntype == _NAL_PPS:
            self._pps = nal

        if ntype == _NAL_AUD:
            au = self._assemble()
            if au is not None:
                yield au
            return  # AUDs are pure delimiters; do not forward them
        if ntype in (_NAL_SLICE, _NAL_IDR):
            if self._has_vcl and _first_mb_zero(nal):
                au = self._assemble()
                if au is not None:
                    yield au
        elif self._has_vcl:
            # non-VCL (SEI/SPS/PPS/...) after a slice opens the next picture
            au = self._assemble()
            if au is not None:
                yield au

        self._nals.append(nal)
        if ntype in (_NAL_SLICE, _NAL_IDR):
            self._has_vcl = True
        if ntype == _NAL_IDR:
            self._has_idr = True
        if ntype == _NAL_SPS:
            self._has_sps = True

    def _assemble(self) -> Optional[tuple[bytes, bool]]:
        nals, self._nals = self._nals, []
        has_vcl, self._has_vcl = self._has_vcl, False
        has_idr, self._has_idr = self._has_idr, False
        has_sps, self._has_sps = self._has_sps, False
        if not nals or not has_vcl:
            return None  # never ship parameter-set-only fragments
        if has_idr and not has_sps and self._sps and self._pps:
            nals = [self._sps, self._pps] + nals
            has_sps = True
        au = b"".join(b"\x00\x00\x00\x01" + n for n in nals)
        if len(au) > self._max_au:
            self.dropped_oversize += 1
            return None
        return au, (has_idr or has_sps)


def _first_mb_zero(nal: bytes) -> bool:
    """True when the slice header's first_mb_in_slice ue(v) is 0 (new picture).

    ue(v)==0 is encoded as a single '1' bit, so test the MSB of the first
    slice-header byte (the byte after the 1-byte NAL header).
    """
    return len(nal) >= 2 and (nal[1] & 0x80) != 0


class JpegStreamSplitter:
    """Split ffmpeg image2pipe output (concatenated JPEGs) into single frames.

    FF D9 cannot occur inside entropy-coded data (FF bytes are stuffed as
    FF 00, restart markers stop at FF D7), so scanning for SOI..EOI is safe
    for ffmpeg-produced MJPEG (no EXIF thumbnails).
    """

    def __init__(self, max_bytes: int = 4 * 1024 * 1024) -> None:
        self._buf = b""
        self._max = max_bytes

    def feed(self, data: bytes) -> Iterator[bytes]:
        self._buf += data
        while True:
            soi = self._buf.find(b"\xff\xd8\xff")
            if soi == -1:
                self._buf = self._buf[-2:]
                return
            eoi = self._buf.find(b"\xff\xd9", soi + 3)
            if eoi == -1:
                if len(self._buf) - soi > self._max:
                    self._buf = b""  # runaway frame; resync
                else:
                    self._buf = self._buf[soi:]
                return
            yield self._buf[soi:eoi + 2]
            self._buf = self._buf[eoi + 2:]


# ---------------------------------------------------------------------------
#  ffmpeg command construction
# ---------------------------------------------------------------------------

def pick_encoder(override: str = "") -> str:
    """Hardware encoder where available, software otherwise.

    /dev/video11 is the bcm2835-codec V4L2 M2M encoder on Pi 0-4 (VideoCore IV,
    rated for exactly 1080p30). The Pi 5 has no H.264 hardware encoder and
    falls through to libx264 like laptops do.
    """
    if override:
        return override
    if sys.platform.startswith("linux") and os.path.exists("/dev/video11"):
        return "h264_v4l2m2m"
    return "libx264"


def build_ffmpeg_cmd(
    *,
    mode: str,                 # "v4l2" | "v4l2-h264" | "pipe"
    device: int,
    width: int,
    height: int,
    fps: float,
    bitrate_kbps: int,
    gop_seconds: float,
    encoder: str,
    sidecar_fps: float = 0.0,
    sidecar_height: int = 540,
    sidecar_fd: int = 0,
    ffmpeg_bin: str = "ffmpeg",
) -> list[str]:
    """Build the ffmpeg argv for the H.264 lane (+ optional MJPEG sidecar).

    Output 1 (stdout): Annex-B H.264. ``h264_metadata=aud=insert`` gives the
    AU splitter deterministic boundaries; ``dump_extra`` re-attaches global
    SPS/PPS at keyframes when the encoder keeps them out-of-band. Short GOP
    (1-2 s) bounds both viewer join latency and the artifact window after a
    backpressure drop.

    Output 2 (fd ``sidecar_fd``): low-fps scaled MJPEG for YOLO detection /
    auto-recording / the MJPEG fallback view (Linux only — needs pass_fds).
    """
    gop = max(1, int(round(fps * gop_seconds)))
    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "warning"]
    if mode != "pipe":
        cmd.append("-nostdin")  # pipe mode reads the video FROM stdin

    if mode == "v4l2":
        # MJPEG from the cam: the only format USB2 sustains at 1080p30.
        cmd += ["-f", "v4l2", "-input_format", "mjpeg",
                "-video_size", f"{width}x{height}", "-framerate", f"{fps:g}",
                "-i", f"/dev/video{device}"]
    elif mode == "v4l2-h264":
        # Camera-native H.264 (e.g. Logitech C920): zero encode cost.
        cmd += ["-f", "v4l2", "-input_format", "h264",
                "-video_size", f"{width}x{height}", "-framerate", f"{fps:g}",
                "-i", f"/dev/video{device}"]
    elif mode == "pipe":
        # BGR frames piped from OpenCV (laptops / Windows).
        cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{width}x{height}", "-r", f"{fps:g}", "-i", "pipe:0"]
    else:
        raise ValueError(f"unknown mode {mode!r}")

    cmd += ["-an"]
    if mode == "v4l2-h264":
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", encoder,
                "-b:v", f"{bitrate_kbps}k", "-maxrate", f"{bitrate_kbps}k",
                "-bufsize", f"{2 * bitrate_kbps}k",
                "-g", str(gop), "-bf", "0", "-pix_fmt", "yuv420p"]
        if encoder == "libx264":
            cmd += ["-preset", "ultrafast", "-tune", "zerolatency",
                    "-x264-params",
                    f"repeat-headers=1:keyint={gop}:min-keyint={gop}:scenecut=0"]
    cmd += ["-bsf:v", "dump_extra=freq=keyframe,h264_metadata=aud=insert",
            "-f", "h264", "pipe:1"]

    if sidecar_fps > 0 and sidecar_fd > 0:
        cmd += ["-an", "-vf", f"fps={sidecar_fps:g},scale=-2:{sidecar_height}",
                "-c:v", "mjpeg", "-q:v", "7", "-f", "image2pipe",
                f"pipe:{sidecar_fd}"]
    return cmd
