@echo off
cd /d "%~dp0"
echo Starting Universal QR Print System on port 8500...
start "QR Print Server" cmd /k ".venv\Scripts\python.exe app.py"
timeout /t 2 /nobreak >nul
start "" http://localhost:8500/admin
