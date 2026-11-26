@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Run Install.bat first.
    pause
    exit /b 1
)
rem Runs in the background (no black window). Logs: %LOCALAPPDATA%\QRPrintShop\qrprint.log
start "" ".venv\Scripts\pythonw.exe" run.py
