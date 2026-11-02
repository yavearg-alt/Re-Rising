@echo off
rem SAOUB Launcher: start / stop / manage the local private server (tools\launcher\saoub_launcher.pyw).
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "tools\launcher\saoub_launcher.pyw"
  exit /b 0
)
where pyw >nul 2>nul && (start "" pyw -3 "tools\launcher\saoub_launcher.pyw" & exit /b 0)
echo [ERROR] Python not found. Install Python 3.13 from python.org, then in this folder run:
echo   python -m venv .venv
echo   .venv\Scripts\pip install -r requirements.txt
pause
