@echo off
setlocal

REM ----------------------------------------------------------------------
REM  Edit these three for your setup, then double-click this file.
REM ----------------------------------------------------------------------
set "SC_WEB_URL=wss://127.0.0.1:8443"
set "SC_CAM_ID=3"
set "SC_CAM_TOKEN=sc-cam-3-62f6d31fe42ea869de8758d588ecb71f"

REM Optional knobs:
set "SC_DEVICE=0"
set "SC_INSECURE=1"

REM --- H.264 mode: 1080p30 at ~4.5 Mbit/s (vs ~40-80 Mbit/s as MJPEG). ---
REM ffmpeg comes bundled via imageio-ffmpeg (requirements.txt); on Windows
REM laptops the software encoder (libx264) is used. Set SC_CODEC=jpeg to revert.
set "SC_CODEC=h264"
set "SC_WIDTH=1920"
set "SC_HEIGHT=1080"
set "SC_FPS=30"
set "SC_H264_BITRATE_KBPS=4500"
set "SC_H264_GOP_SECONDS=1.0"
REM Low-fps JPEG sidecar keeps YOLO detection + auto-recording alive (0 = off):
set "SC_SIDECAR_FPS=2"

REM --- JPEG mode fallback (SC_CODEC=jpeg): keep fps*quality modest — ---
REM 15fps/q80@720p is ~10-15 Mbit/s; more starves a home VPN uplink.
set "SC_JPEG_QUALITY=80"
REM ----------------------------------------------------------------------

cd /d "%~dp0"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PY=py -3"
) else (
    where python >nul 2>nul
    if %ERRORLEVEL%==0 (
        set "PY=python"
    ) else (
        echo Python 3 not found. Install from https://www.python.org/downloads/ ^(tick "Add to PATH"^).
        pause
        exit /b 1
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo Failed to create venv.
        pause
        exit /b 1
    )
    call ".venv\Scripts\activate.bat"
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies.
        pause
        exit /b 1
    )
) else (
    call ".venv\Scripts\activate.bat"
)

python laptop_streamer.py
set "EXITCODE=%ERRORLEVEL%"
echo.
echo Streamer exited with code %EXITCODE%.
pause
exit /b %EXITCODE%
