@echo off
cd /d "%~dp0"
echo Building dist\QRPrintShop.exe ...
".venv\Scripts\python.exe" -m pip install --quiet pyinstaller
if not exist "bin\cloudflared.exe" (
    echo Downloading Cloudflare tunnel - official release...
    mkdir bin 2>nul
    powershell -NoProfile -Command "Invoke-WebRequest https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe -OutFile bin\cloudflared.exe"
)
".venv\Scripts\pyinstaller.exe" --noconfirm --onefile --windowed --name "QRPrintShop" ^
  --add-data "static;static" ^
  --add-binary "bin\cloudflared.exe;." ^
  --hidden-import win32timezone ^
  --collect-submodules uvicorn ^
  --exclude-module pymupdf4llm --exclude-module pymupdf_layout --exclude-module pytest ^
  run.py
echo.
echo Done: dist\QRPrintShop.exe  - copy this single file to any shop PC and double-click it.
pause
