@echo off
title QR Print Shop - Setup
cd /d "%~dp0"
echo ============================================
echo   QR Print Shop - one-time setup
echo ============================================
echo.
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating Python environment...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/
        echo Tick "Add python.exe to PATH" during install, then run this again.
        pause
        exit /b 1
    )
)
echo [2/3] Installing packages (takes a minute the first time)...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo Package install failed. Check your internet connection and run again.
    pause
    exit /b 1
)
where cloudflared >nul 2>&1
if errorlevel 1 (
    echo Installing Cloudflare Tunnel so customers on mobile data can print too...
    winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements >nul 2>&1
)
echo [3/3] Creating "QR Print Shop" icon on your Desktop...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\QR Print Shop.lnk'); $s.TargetPath='%~dp0Start QR Print Shop.bat'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='shell32.dll,16'; $s.WindowStyle=7; $s.Save()"
echo.
echo Done! Starting QR Print Shop...
call "%~dp0Start QR Print Shop.bat"
