#!/usr/bin/env bash
set -e

# ----------------------------------------------------------------------
#  Edit these three for your setup, then run:  ./run.sh
# ----------------------------------------------------------------------
export SC_WEB_URL="wss://127.0.0.1:8443"
export SC_CAM_ID="3"
export SC_CAM_TOKEN="sc-cam-3-62f6d31fe42ea869de8758d588ecb71f"

# Optional knobs:
export SC_DEVICE="0"
export SC_INSECURE="1"

# --- H.264 mode: 1080p30 at ~4.5 Mbit/s (vs ~40-80 Mbit/s as MJPEG). ---
# Needs ffmpeg (sudo apt install ffmpeg). On a Pi 0-4 the VideoCore hardware
# encoder is auto-selected. Falls back to JPEG mode if ffmpeg is missing.
export SC_CODEC="h264"
export SC_WIDTH="1920"
export SC_HEIGHT="1080"
export SC_FPS="30"
export SC_H264_BITRATE_KBPS="4500"
export SC_H264_GOP_SECONDS="1.0"
# Low-fps JPEG sidecar keeps YOLO detection + auto-recording alive (0 = off):
export SC_SIDECAR_FPS="2"

# --- JPEG mode fallback (SC_CODEC="jpeg"): keep fps*quality modest — ---
# 15fps/q80@720p is ~10-15 Mbit/s; more starves a home VPN uplink.
export SC_JPEG_QUALITY="80"
# ----------------------------------------------------------------------

cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "Python 3 not found. Install with your package manager, e.g.:"
    echo "  sudo apt install python3 python3-venv python3-pip"
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating virtual environment..."
    "$PY" -m venv .venv || {
        echo "Failed to create venv. On Debian/Ubuntu you may need: sudo apt install python3-venv"
        exit 1
    }
    # shellcheck disable=SC1091
    source .venv/bin/activate
    python -m pip install --upgrade pip
    pip install -r requirements.txt || {
        echo "Failed to install dependencies."
        exit 1
    }
else
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

python laptop_streamer.py
EXITCODE=$?
echo
echo "Streamer exited with code $EXITCODE."
exit $EXITCODE
