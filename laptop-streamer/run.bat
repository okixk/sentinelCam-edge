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
set "SC_FPS=60"
set "SC_JPEG_QUALITY=100"
set "SC_INSECURE=1"
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
