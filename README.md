# sentinelCam Edge

`sentinelCam-edge` is the planned camera-side component of the sentinelCam stack.

This repo is intended for lightweight edge devices such as Raspberry Pi systems or small camera-adjacent nodes. Its job will be to capture or forward a video source and hand it off to [`sentinelCam-worker`](https://github.com/okixk/sentinelCam-worker), where the actual AI inference happens.

> This repository is currently a placeholder and does not contain the implementation yet.

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
