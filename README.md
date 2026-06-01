# sentinelCam Edge

`sentinelCam-edge` is the camera-side component of the sentinelCam stack.

It is intended for lightweight edge devices such as Raspberry Pi systems or small camera-adjacent nodes. Its job is to capture a video source and push it to the **sentinelCam-web** server, which forwards frames to a worker for AI inference and relays the processed result back to browsers. The edge node never talks to the worker directly.

## Reference client: `laptop-streamer/`

`laptop-streamer/laptop_streamer.py` is the reference edge client. It captures a local webcam and streams JPEG frames over a WebSocket to `sentinelCam-web` at `/api/ingest/<cam_id>`.

It is configured entirely via environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `SC_WEB_URL` | — (required) | `wss://host` of the web server |
| `SC_CAM_ID` | — (required) | camera id issued by the web admin |
| `SC_CAM_TOKEN` | — (required) | `sc-cam-<id>-<secret>` bearer token |
| `SC_DEVICE` | `0` | camera index |
| `SC_FPS` | `15` | target send framerate |
| `SC_JPEG_QUALITY` | `80` | JPEG quality (1–100) |
| `SC_WIDTH` / `SC_HEIGHT` | `1280` / `720` | capture resolution |
| `SC_INSECURE` | `0` | set `1` to skip TLS verification (local testing only) |

```bash
pip install -r laptop-streamer/requirements.txt
SC_WEB_URL=wss://sentinelcam.ch SC_CAM_ID=1 SC_CAM_TOKEN=sc-cam-1-... \
  python laptop-streamer/laptop_streamer.py
```

The client requests MJPG capture (so USB cams hit 720p/1080p at speed), keeps
only the freshest frame (`CAP_PROP_BUFFERSIZE=1`), reconnects with exponential
backoff on a dropped link, and releases the camera cleanly on exit.

### 1080p @ 30 fps

Resolution and framerate are set on the **source** (here), not on the worker:

```powershell
$env:SC_WIDTH = "1920"; $env:SC_HEIGHT = "1080"; $env:SC_FPS = "30"
```

(The camera must support 1080p30; with MJPG most USB cams do.)

### Direct over the VPN (bypass Cloudflare)

Cloudflare is unreliable for sustained high-throughput WebSockets and will
"keepalive ping timeout" / drop the stream. For a stable feed, connect the
camera **directly to the origin over the VPN** instead of through Cloudflare:

1. Connect the WireGuard VPN (so the web server's bridge IP is reachable).
2. Map the hostname to that IP in your hosts file
   (`C:\Windows\System32\drivers\etc\hosts`, as admin):
   ```
   172.30.0.10   sentinelcam.ch
   ```
   `172.30.0.10` is the Traefik bridge IP for VPN clients; use the web host's
   LAN IP instead if you are on the same network.
3. Run with verification off (the Cloudflare Origin cert isn't trusted off-CF;
   the WireGuard tunnel already secures the link):
   ```powershell
   $env:SC_WEB_URL = "wss://sentinelcam.ch"   # name kept so Traefik routes correctly
   $env:SC_INSECURE = "1"
   ```

The hosts entry affects the whole machine, so this laptop's **browser** would
also bypass Cloudflare and show a cert warning — view the web UI from another
device (normal `https://sentinelcam.ch`) or install the Cloudflare Origin CA.

### Ingest contract

The wire contract the web server expects (see `sentinelCam-web` →
`app/streaming/routes.py` and `app/streaming/protocol.py`):

- WebSocket to `wss://<web>/api/ingest/<cam_id>` with header
  `Authorization: Bearer <SC_CAM_TOKEN>`.
- Each message is one raw JPEG frame (binary), ≤ 4 MiB, starting with the
  JPEG magic `FF D8 FF`.

A Raspberry Pi client should implement the same contract.

## Planned purpose

The edge node is meant to sit close to the camera and handle tasks like:

- camera capture
- stream forwarding
- source normalization
- lightweight pre-processing
- stable handoff to the worker backend

The main goal is to separate:
- **capture near the camera** from
- **heavy AI processing on the worker**

## Planned architecture

Target flow:

`camera -> sentinelCam-edge -> sentinelCam-worker -> sentinelCam-web`

Fallback flow without edge:

`camera -> sentinelCam-worker -> sentinelCam-web`

## Related repositories

- **Processing backend:** [`sentinelCam-worker`](https://github.com/okixk/sentinelCam-worker)  
  Main downstream target. The worker will consume the stream or source exposed by the edge node.

- **Browser frontend:** [`sentinelCam-web`](https://github.com/okixk/sentinelCam-web)  
  Viewer and control interface for the processed stream coming from the worker.

## Planned responsibilities

Possible future responsibilities of this repo:

- connect to USB / CSI / IP cameras
- expose a stable stream for the worker
- handle reconnect logic for unreliable cameras
- keep camera-specific code out of the worker
- allow distributed deployments with multiple camera nodes

## Planned boundary between repos

### `sentinelCam-edge`
- owns the camera
- captures or forwards the raw stream
- runs on the device closest to the camera

### `sentinelCam-worker`
- consumes the edge stream
- runs YOLO / pose inference
- exposes processed output and control API

### `sentinelCam-web`
- displays the processed result
- controls the worker from the browser

## Current status

Placeholder repository for the future edge component of sentinelCam.
