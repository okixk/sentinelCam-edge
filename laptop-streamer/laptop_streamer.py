"""Push laptop webcam frames as JPEGs to sentinelCam-web /api/ingest.

Reads config from environment variables (run.bat sets them).
"""
import asyncio
import os
import ssl
import sys
import time

import cv2
import websockets


WEB_URL    = os.environ["SC_WEB_URL"].rstrip("/")
CAM_ID     = int(os.environ["SC_CAM_ID"])
CAM_TOKEN  = os.environ["SC_CAM_TOKEN"]
DEVICE_IDX = int(os.environ.get("SC_DEVICE", "0"))
TARGET_FPS = float(os.environ.get("SC_FPS", "15"))
JPEG_Q     = int(os.environ.get("SC_JPEG_QUALITY", "80"))
INSECURE   = os.environ.get("SC_INSECURE", "1") == "1"


def _open_capture(idx: int) -> cv2.VideoCapture:
    # On Windows DSHOW is the most reliable backend for USB / built-in cams.
    if sys.platform.startswith("win"):
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if cap.isOpened():
            return cap
        cap.release()
        cap = cv2.VideoCapture(idx, cv2.CAP_MSMF)
        if cap.isOpened():
            return cap
        cap.release()
    return cv2.VideoCapture(idx)


async def stream() -> None:
    cap = _open_capture(DEVICE_IDX)
    if not cap.isOpened():
        print(f"cannot open camera index {DEVICE_IDX}", file=sys.stderr)
        sys.exit(1)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    url = f"{WEB_URL}/api/ingest/{CAM_ID}"
    ssl_ctx = None
    if url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context()
        if INSECURE:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

    headers = [("Authorization", f"Bearer {CAM_TOKEN}")]
    period = 1.0 / TARGET_FPS

    print(f"connecting to {url} as cam {CAM_ID}")
    async with websockets.connect(
        url,
        additional_headers=headers,
        ssl=ssl_ctx,
        max_size=8 * 1024 * 1024,
        ping_interval=20,
    ) as ws:
        print("connected; streaming  (Ctrl+C to stop)")
        next_tick = time.monotonic()
        n = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                await asyncio.sleep(0.05)
                continue
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_Q])
            if not ok:
                continue
            await ws.send(buf.tobytes())
            n += 1
            if n % 30 == 0:
                print(f"  sent {n} frames")
            next_tick += period
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            else:
                next_tick = time.monotonic()


if __name__ == "__main__":
    try:
        asyncio.run(stream())
    except KeyboardInterrupt:
        pass
