@echo off
cd /d "%~dp0"
echo Building dist\QRPrintShop.exe ...
".venv\Scripts\python.exe" -m pip install --quiet pyinstaller
".venv\Scripts\pyinstaller.exe" --noconfirm --onefile --windowed --name "QRPrintShop" ^
  --add-data "static;static" ^
  --hidden-import win32timezone ^
  --collect-submodules uvicorn ^
  run.py
echo.
echo Done: dist\QRPrintShop.exe  - copy this single file to any shop PC and double-click it.
pause
