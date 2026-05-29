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
export SC_FPS="60"
export SC_JPEG_QUALITY="100"
export SC_INSECURE="1"
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
