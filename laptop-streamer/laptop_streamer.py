"""Push laptop/Pi webcam frames as JPEGs to sentinelCam-web /api/ingest.

Reads config from environment variables (run.bat / run.sh set them):

    SC_WEB_URL       ws(s)://host[:port]            (required)
    SC_CAM_ID        integer camera id              (required)
    SC_CAM_TOKEN     sc-cam-<id>-<secret> bearer    (required)
    SC_DEVICE        camera index (default 0)
    SC_FPS           target send fps (default 15)
    SC_JPEG_QUALITY  JPEG quality 1-100 (default 80)
    SC_WIDTH/HEIGHT  capture resolution (default 1280x720)
    SC_INSECURE      "1" to skip TLS verification (default 0 = verify)

H.264 lane (SC_CODEC=h264 — ~1/10th the bandwidth of MJPEG, needs ffmpeg):

    SC_CODEC              "jpeg" (default) or "h264"
    SC_H264_BITRATE_KBPS  target bitrate (default 4500, good for 1080p30)
    SC_H264_GOP_SECONDS   keyframe interval (default 1.0)
    SC_H264_INPUT         auto|v4l2|v4l2-h264|pipe (default auto: v4l2 on
                          Linux so ffmpeg owns the camera, pipe elsewhere)
    SC_H264_ENCODER       override (default: h264_v4l2m2m if /dev/video11
                          exists — Pi 0-4 hardware encoder — else libx264)
    SC_SIDECAR_FPS        low-fps JPEG sidecar so the worker still runs YOLO
                          detection / auto-recording (default 2, 0 = off)
    SC_SIDECAR_HEIGHT     sidecar frame height (default 540)
    SC_FFMPEG             ffmpeg binary (default "ffmpeg")

The camera token is a bearer secret, so TLS verification is ON by default;
only set SC_INSECURE=1 for throwaway local testing with a self-signed cert.

Resilient by design: the camera is opened once and released cleanly on exit,
and the WebSocket reconnects with exponential backoff so a transient web/worker
outage does not require relaunching the streamer.
"""
from __future__ import annotations  # Pi OS Bullseye ships Python 3.9

import asyncio
import os
import shutil
import signal
import ssl
import sys
import time

import cv2
import websockets

from h264_source import (
    AnnexBSplitter,
    JpegStreamSplitter,
    build_ffmpeg_cmd,
    pick_encoder,
)


WEB_URL = os.environ["SC_WEB_URL"].rstrip("/")
CAM_ID = int(os.environ["SC_CAM_ID"])
CAM_TOKEN = os.environ["SC_CAM_TOKEN"]
DEVICE_IDX = int(os.environ.get("SC_DEVICE", "0"))
TARGET_FPS = float(os.environ.get("SC_FPS", "15"))
JPEG_Q = int(os.environ.get("SC_JPEG_QUALITY", "80"))
WIDTH = int(os.environ.get("SC_WIDTH", "1280"))
HEIGHT = int(os.environ.get("SC_HEIGHT", "720"))
# Secure by default — the camera token must only travel over a verified channel.
INSECURE = os.environ.get("SC_INSECURE", "0") == "1"

CODEC = os.environ.get("SC_CODEC", "jpeg").strip().lower()
H264_BITRATE_KBPS = int(os.environ.get("SC_H264_BITRATE_KBPS", "4500"))
H264_GOP_SECONDS = float(os.environ.get("SC_H264_GOP_SECONDS", "1.0"))
H264_INPUT = os.environ.get("SC_H264_INPUT", "auto").strip().lower()
H264_ENCODER = os.environ.get("SC_H264_ENCODER", "").strip()
SIDECAR_FPS = float(os.environ.get("SC_SIDECAR_FPS", "2"))
SIDECAR_HEIGHT = int(os.environ.get("SC_SIDECAR_HEIGHT", "540"))
FFMPEG_BIN = os.environ.get("SC_FFMPEG", "ffmpeg")

RECONNECT_INITIAL = 1.0
RECONNECT_MAX = 30.0


def _open_capture(idx: int) -> cv2.VideoCapture:
    # On Windows DSHOW is the most reliable backend for USB / built-in cams.
    cap = None
    if sys.platform.startswith("win"):
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(idx, cv2.CAP_MSMF)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(idx)
    else:
        cap = cv2.VideoCapture(idx)
    if not cap.isOpened():
        return cap
    # Request MJPG so USB cams deliver 720p/1080p at speed instead of silently
    # falling back to uncompressed YUYV (which caps FPS at high resolutions).
    try:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    except Exception:
        pass
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    # Keep only the freshest frame so we never stream a stale backlog.
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    return cap


def _build_ssl_context(url: str):
    if not url.startswith("wss://"):
        return None
    ctx = ssl.create_default_context()
    if INSECURE:
        print("WARNING: SC_INSECURE=1 — TLS verification disabled (camera token "
              "is exposed to MITM). Use only for local testing.", file=sys.stderr)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _send_loop(ws, cap, stop: asyncio.Event) -> None:
    period = 1.0 / max(TARGET_FPS, 1.0)
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_Q]
    next_tick = time.monotonic()
    n = 0
    while not stop.is_set():
        ok, frame = cap.read()
        if not ok:
            await asyncio.sleep(0.05)
            continue
        ok, buf = cv2.imencode(".jpg", frame, encode_params)
        if not ok:
            continue
        await ws.send(buf.tobytes())  # raises on closed socket -> outer reconnect
        n += 1
        if n % 30 == 0:
            print(f"  sent {n} frames")
        next_tick += period
        sleep_for = next_tick - time.monotonic()
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
        else:
            next_tick = time.monotonic()


async def stream() -> None:
    cap = _open_capture(DEVICE_IDX)
    if not cap.isOpened():
        print(f"cannot open camera index {DEVICE_IDX}", file=sys.stderr)
        sys.exit(1)

    url = f"{WEB_URL}/api/ingest/{CAM_ID}"
    ssl_ctx = _build_ssl_context(url)
    headers = [("Authorization", f"Bearer {CAM_TOKEN}")]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # not supported on Windows event loops; Ctrl+C still works

    print(f"connecting to {url} as cam {CAM_ID}")
    backoff = RECONNECT_INITIAL
    try:
        while not stop.is_set():
            try:
                async with websockets.connect(
                    url,
                    additional_headers=headers,
                    ssl=ssl_ctx,
                    max_size=8 * 1024 * 1024,
                    # Generous pong deadline: on a saturated VPN uplink the pong
                    # queues behind the JPEG backlog; the default 20s timeout
                    # then tears down a perfectly healthy stream every ~20-40s.
                    ping_interval=30,
                    ping_timeout=120,
                ) as ws:
                    print("connected; streaming  (Ctrl+C to stop)")
                    backoff = RECONNECT_INITIAL
                    await _send_loop(ws, cap, stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # connection refused/dropped, TLS, etc.
                if stop.is_set():
                    break
                print(f"connection lost: {exc}; reconnecting in {backoff:.1f}s", file=sys.stderr)
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, RECONNECT_MAX)
    finally:
        cap.release()
        print("camera released; bye")


# ---------------------------------------------------------------------------
#  H.264 lane (SC_CODEC=h264)
# ---------------------------------------------------------------------------

class _H264Source:
    """Owns the ffmpeg encoder process and its readers.

    Lives across WebSocket reconnects so a VPN blip never re-opens the camera.
    Output flows into a bounded drop-oldest queue: when the uplink can't keep
    up, the backlog is dropped and sending resumes at the next keyframe, so
    latency stays bounded instead of starving the keepalive (the failure mode
    that caused the periodic disconnects over VPN).
    """

    QUEUE_MAX = 64  # ~2s of AUs at 30 fps

    def __init__(self, mode: str, encoder: str, ffmpeg_bin: str, cap=None) -> None:
        self.mode = mode
        self.encoder = encoder
        self.ffmpeg_bin = ffmpeg_bin
        self.cap = cap  # only used in pipe mode
        self.au_q: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_MAX)
        self.need_idr = True
        self.aus_dropped = 0
        self._sidecar_jpeg = None
        self._proc = None
        self._tasks: list[asyncio.Task] = []
        self._spawned_at = 0.0
        self._fail_streak = 0
        self._last_drop_log = 0.0

    async def ensure_running(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        await self._cancel_tasks()
        if self._proc is not None:
            # Healthy long runs reset the backoff; rapid crash loops grow it.
            if time.monotonic() - self._spawned_at > 30.0:
                self._fail_streak = 0
            self._fail_streak += 1
            delay = min(2.0 ** self._fail_streak, 10.0)
            print(f"ffmpeg exited (rc={self._proc.returncode}); restarting in {delay:.0f}s",
                  file=sys.stderr)
            if self._fail_streak >= 5 and self.encoder != "libx264" and self.mode != "v4l2-h264":
                # The chosen (hardware) encoder keeps crashing — e.g. an ffmpeg
                # build without h264_v4l2m2m. Software encode keeps the stream
                # alive, but on a Pi 3 it will not sustain 1080p30: check the
                # ffmpeg errors above and install the system ffmpeg.
                print(f"encoder {self.encoder} keeps failing; falling back to libx264 (software)",
                      file=sys.stderr)
                self.encoder = "libx264"
                self._fail_streak = 0
            await asyncio.sleep(delay)

        sidecar_fd = 0
        rfd = wfd = None
        if SIDECAR_FPS > 0 and self.mode in ("v4l2", "v4l2-h264"):
            rfd, wfd = os.pipe()
            os.set_inheritable(wfd, True)
            sidecar_fd = wfd
        cmd = build_ffmpeg_cmd(
            mode=self.mode, device=DEVICE_IDX, width=WIDTH, height=HEIGHT,
            fps=TARGET_FPS, bitrate_kbps=H264_BITRATE_KBPS,
            gop_seconds=H264_GOP_SECONDS, encoder=self.encoder,
            sidecar_fps=SIDECAR_FPS if sidecar_fd else 0.0,
            sidecar_height=SIDECAR_HEIGHT, sidecar_fd=sidecar_fd,
            ffmpeg_bin=self.ffmpeg_bin,
        )
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if self.mode == "pipe" else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=(wfd,) if wfd is not None else (),
            )
        except Exception:
            # Don't leak the sidecar pipe on every failed respawn attempt.
            for fd in (rfd, wfd):
                if fd is not None:
                    os.close(fd)
            raise
        self._spawned_at = time.monotonic()
        if wfd is not None:
            os.close(wfd)  # child holds the write end now
            self._tasks.append(asyncio.create_task(self._read_sidecar(rfd)))
        self._tasks.append(asyncio.create_task(self._read_aus()))
        self._tasks.append(asyncio.create_task(self._log_stderr()))
        if self.mode == "pipe":
            self._tasks.append(asyncio.create_task(self._feed_frames()))
        self.need_idr = True

    def pop_sidecar(self):
        jpeg, self._sidecar_jpeg = self._sidecar_jpeg, None
        return jpeg

    async def close(self) -> None:
        await self._cancel_tasks()
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._proc.kill()

    # -- internals ----------------------------------------------------------

    async def _cancel_tasks(self) -> None:
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _enqueue(self, au: bytes, is_kf: bool) -> None:
        if self.au_q.full():
            dropped = 0
            while not self.au_q.empty():
                self.au_q.get_nowait()
                dropped += 1
            self.aus_dropped += dropped
            self.need_idr = True  # resume cleanly at the next keyframe
            now = time.monotonic()
            if now - self._last_drop_log > 5.0:
                self._last_drop_log = now
                print(f"uplink backpressure: dropped {dropped} AUs "
                      f"(total {self.aus_dropped}); resuming at next keyframe",
                      file=sys.stderr)
        self.au_q.put_nowait((au, is_kf))

    async def _read_aus(self) -> None:
        splitter = AnnexBSplitter()
        oversize_seen = 0
        try:
            while True:
                chunk = await self._proc.stdout.read(65536)
                if not chunk:
                    break
                for au, is_kf in splitter.feed(chunk):
                    self._enqueue(au, is_kf)
                if splitter.dropped_oversize > oversize_seen:
                    oversize_seen = splitter.dropped_oversize
                    # The web drops >2MiB AUs silently too — make it visible.
                    print(f"warning: dropped oversized access unit #{oversize_seen} "
                          "(>2MiB; lower SC_H264_BITRATE_KBPS or SC_H264_GOP_SECONDS)",
                          file=sys.stderr)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"h264 reader stopped: {exc}", file=sys.stderr)

    async def _read_sidecar(self, rfd: int) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        pipe = os.fdopen(rfd, "rb")
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), pipe)
        splitter = JpegStreamSplitter()
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                for jpeg in splitter.feed(chunk):
                    self._sidecar_jpeg = jpeg  # keep only the freshest
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            transport.close()

    async def _log_stderr(self) -> None:
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                print(f"ffmpeg: {line.decode(errors='replace').rstrip()}", file=sys.stderr)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _feed_frames(self) -> None:
        """Pipe-mode only: pace BGR frames from OpenCV into ffmpeg stdin."""
        period = 1.0 / max(TARGET_FPS, 1.0)
        next_tick = time.monotonic()
        sidecar_period = (1.0 / SIDECAR_FPS) if SIDECAR_FPS > 0 else 0.0
        next_sidecar = time.monotonic()
        try:
            while True:
                ok, frame = await asyncio.to_thread(self.cap.read)
                if not ok:
                    await asyncio.sleep(0.05)
                    continue
                if frame.shape[1] != WIDTH or frame.shape[0] != HEIGHT:
                    frame = cv2.resize(frame, (WIDTH, HEIGHT))
                self._proc.stdin.write(frame.tobytes())
                await self._proc.stdin.drain()
                if sidecar_period and time.monotonic() >= next_sidecar:
                    ok2, buf = cv2.imencode(".jpg", frame,
                                            [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_Q])
                    if ok2:
                        self._sidecar_jpeg = buf.tobytes()
                    next_sidecar = time.monotonic() + sidecar_period
                next_tick += period
                sleep_for = next_tick - time.monotonic()
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                else:
                    next_tick = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"frame feeder stopped: {exc}", file=sys.stderr)


async def _drain_incoming(ws) -> None:
    """Discard server->edge messages so they can never back up the socket."""
    try:
        async for _ in ws:
            pass
    except Exception:
        pass


async def _h264_session(ws, src: _H264Source, stop: asyncio.Event) -> None:
    src.need_idr = True  # a (re)connected viewer must start on a keyframe
    drainer = asyncio.create_task(_drain_incoming(ws))
    n = 0
    try:
        while not stop.is_set():
            await src.ensure_running()
            try:
                au, is_kf = await asyncio.wait_for(src.au_q.get(), timeout=0.25)
            except asyncio.TimeoutError:
                au = None
            if au is not None:
                if src.need_idr and not is_kf:
                    continue  # skip deltas until the stream restarts clean
                src.need_idr = False
                await ws.send(au)  # raises on closed socket -> outer reconnect
                n += 1
                if n % 300 == 0:
                    print(f"  sent {n} access units")
            jpeg = src.pop_sidecar()
            if jpeg is not None:
                await ws.send(jpeg)
    finally:
        drainer.cancel()


def _resolve_ffmpeg() -> str | None:
    """System ffmpeg first (on a Pi it is the only build with the VideoCore
    hardware encoder enabled), then the static binary bundled by
    imageio-ffmpeg (installed via requirements.txt on laptops)."""
    if shutil.which(FFMPEG_BIN) is not None:
        return FFMPEG_BIN
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


async def stream_h264() -> None:
    ffmpeg_bin = _resolve_ffmpeg()
    if ffmpeg_bin is None:
        print(f"SC_CODEC=h264 but '{FFMPEG_BIN}' not found on PATH "
              "(install with: sudo apt install ffmpeg) — falling back to JPEG mode.",
              file=sys.stderr)
        await stream()
        return

    mode = H264_INPUT
    if mode == "auto":
        # On Linux ffmpeg owns the camera directly (no BGR round-trip through
        # Python — essential on a Pi 3 / Zero 2 W); elsewhere OpenCV pipes in.
        mode = "v4l2" if sys.platform.startswith("linux") else "pipe"
    encoder = pick_encoder(H264_ENCODER)
    print(f"h264 mode: input={mode} encoder={encoder} ffmpeg={ffmpeg_bin} "
          f"{WIDTH}x{HEIGHT}@{TARGET_FPS:g}fps {H264_BITRATE_KBPS}kbit/s "
          f"gop={H264_GOP_SECONDS:g}s sidecar={SIDECAR_FPS:g}fps")

    cap = None
    if mode == "pipe":
        cap = _open_capture(DEVICE_IDX)
        if not cap.isOpened():
            print(f"cannot open camera index {DEVICE_IDX}", file=sys.stderr)
            sys.exit(1)

    url = f"{WEB_URL}/api/ingest/{CAM_ID}"
    ssl_ctx = _build_ssl_context(url)
    headers = [("Authorization", f"Bearer {CAM_TOKEN}")]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # not supported on Windows event loops; Ctrl+C still works

    src = _H264Source(mode, encoder, ffmpeg_bin, cap)
    print(f"connecting to {url} as cam {CAM_ID}")
    backoff = RECONNECT_INITIAL
    try:
        while not stop.is_set():
            try:
                async with websockets.connect(
                    url,
                    additional_headers=headers,
                    ssl=ssl_ctx,
                    max_size=8 * 1024 * 1024,
                    # Same generous pong deadline as the JPEG path: never let a
                    # transient uplink backlog look like a dead connection.
                    ping_interval=30,
                    ping_timeout=120,
                ) as ws:
                    print("connected; streaming h264  (Ctrl+C to stop)")
                    backoff = RECONNECT_INITIAL
                    await src.ensure_running()
                    await _h264_session(ws, src, stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # connection refused/dropped, TLS, etc.
                if stop.is_set():
                    break
                print(f"connection lost: {exc}; reconnecting in {backoff:.1f}s", file=sys.stderr)
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, RECONNECT_MAX)
    finally:
        await src.close()
        if cap is not None:
            cap.release()
        print("encoder stopped; bye")


if __name__ == "__main__":
    try:
        asyncio.run(stream_h264() if CODEC == "h264" else stream())
    except KeyboardInterrupt:
        pass
