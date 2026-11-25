@echo off
cd /d "%~dp0"
netstat -ano | findstr ":8500 " | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo QR Print Server is already running on port 8500. Opening Admin Dashboard...
    start "" http://localhost:8500/admin
    exit /b 0
)
echo Starting Universal QR Print System on port 8500...
start "QR Print Server" cmd /k ".venv\Scripts\python.exe app.py"
timeout /t 2 /nobreak >nul
start "" http://localhost:8500/admin
