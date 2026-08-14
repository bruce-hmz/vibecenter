@echo off
rem Vibe Center for Windows launcher.
rem Requires Python 3.9+ on PATH. Installs PySide6 on first run.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [vibecenter] Python 3.9+ is required. Install from https://python.org
    pause
    exit /b 1
)

python -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo [vibecenter] Installing PySide6 ...
    python -m pip install -r requirements.txt
)

python vibecenter\main.py
endlocal
