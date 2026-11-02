@echo off
rem Re:Rising - SAO Unleash Blading LOCAL server.
rem   Game      : http://127.0.0.1:8080 (BlueStacks: http://10.0.2.2:8080)
rem               Phone on the same Wi-Fi: uncomment "set SAOUB_HOST=0.0.0.0" below (opens the port to your network).
rem   Admin     : http://127.0.0.1:8082/saoub-admin/  (if server\admin_password.txt exists; 8081 is reserved by Windows)
rem Close this window (or Ctrl+C) to stop the server.
title Re:Rising - local server
cd /d "%~dp0server"

set SAOUB_PORT=8080
set SAOUB_ADMIN_PORT=8082
set SAOUB_LOG_MAX_BYTES=50000000
set PYTHONIOENCODING=utf-8
rem set SAOUB_HOST=0.0.0.0

rem Game data built from your own copy of the game (see README.md): SAOUB_DATA if already set,
rem else dev_data\ when present, else data\ at the repository root.
if not defined SAOUB_DATA if exist "%~dp0dev_data\server_cache" set "SAOUB_DATA=%~dp0dev_data"
if not defined SAOUB_DATA set "SAOUB_DATA=%~dp0data"

set SAOUB_ADMIN_PASSWORD=
if exist "admin_password.txt" set /p SAOUB_ADMIN_PASSWORD=<"admin_password.txt"

if not exist "..\.venv\Scripts\python.exe" (
  echo [ERROR] Python environment not found: ..\.venv (python -m venv .venv, then .venv\Scripts\pip install -r requirements.txt)
  pause
  exit /b 1
)

echo ============================================================
echo  Re:Rising local server - port %SAOUB_PORT%
echo  Data: %SAOUB_DATA%
if defined SAOUB_ADMIN_PASSWORD (echo  Admin panel: http://127.0.0.1:%SAOUB_ADMIN_PORT%/saoub-admin/) else (echo  Admin panel: off - create server\admin_password.txt)
echo ============================================================
"..\.venv\Scripts\python.exe" -u main.py
echo.
echo The server stopped.
pause
