@echo off
title QR Print Shop - Installer
cd /d "%~dp0"

echo ============================================
echo   Universal QR Print Shop - Quick Setup
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating Python environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Python 3.10+ is required. Download from https://www.python.org/downloads/
        pause
        exit /b 1
    )
    echo [2/3] Installing required packages...
    .\.venv\Scripts\pip.exe install --upgrade pip
    .\.venv\Scripts\pip.exe install -r requirements.txt
) else (
    echo [1/2] Environment already ready.
)

echo [3/3] Creating "QR Print Shop" shortcut on your Desktop...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\QR Print Shop.lnk'); $s.TargetPath='%~dp0start_server.bat'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='shell32.dll,16'; $s.Save()"

echo.
echo Done! You can now double-click "QR Print Shop" on your Desktop anytime.
echo Starting QR Print Shop now...
call "%~dp0start_server.bat"
