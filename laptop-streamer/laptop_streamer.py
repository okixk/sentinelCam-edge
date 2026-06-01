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

The camera token is a bearer secret, so TLS verification is ON by default;
only set SC_INSECURE=1 for throwaway local testing with a self-signed cert.

Resilient by design: the camera is opened once and released cleanly on exit,
and the WebSocket reconnects with exponential backoff so a transient web/worker
outage does not require relaunching the streamer.
"""
import asyncio
import os
import signal
import ssl
import sys
import time

import cv2
import websockets


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
                    ping_interval=20,
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


if __name__ == "__main__":
    try:
        asyncio.run(stream())
    except KeyboardInterrupt:
        pass
