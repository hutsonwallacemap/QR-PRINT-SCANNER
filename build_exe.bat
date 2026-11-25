@echo off
cd /d "%~dp0"
echo Installing PyInstaller and building standalone QRPrintSystem.exe...
.\.venv\Scripts\pip.exe install pyinstaller
.\.venv\Scripts\pyinstaller.exe --noconfirm --onefile --name "QRPrintSystem" ^
  --add-data "static;static" ^
  --hidden-import "win32timezone" ^
  --hidden-import "uvicorn.logging" ^
  --hidden-import "uvicorn.loops" ^
  --hidden-import "uvicorn.loops.auto" ^
  --hidden-import "uvicorn.protocols" ^
  --hidden-import "uvicorn.protocols.http" ^
  --hidden-import "uvicorn.protocols.http.auto" ^
  --hidden-import "uvicorn.lifespan" ^
  --hidden-import "uvicorn.lifespan.on" ^
  app.py
echo.
echo Build complete! Share dist\QRPrintSystem.exe with any shopkeeper.
pause
