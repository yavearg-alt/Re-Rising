@echo off
rem Re:Rising - update this folder from GitHub (git clone of https://github.com/yavearg-alt/Re-Rising).
rem   Pulls the latest version of the code, then refreshes the Python packages if requirements.txt changed.
rem   Your game data (data\, dev_data\), database, logs and admin password are not tracked by git: they are kept.
rem   Stop the local server before updating.
title Re:Rising - update from GitHub
cd /d "%~dp0"

where git >nul 2>nul
if errorlevel 1 (
  echo [ERROR] git is not installed or not in PATH: https://git-scm.com/download/win
  pause
  exit /b 1
)
if not exist ".git" (
  echo [ERROR] This folder is not a git clone. Download it with:
  echo   git clone https://github.com/yavearg-alt/Re-Rising.git
  echo or use the launcher's Update button for the zip version.
  pause
  exit /b 1
)

rem Local edits of tracked files would be overwritten or block the update: list them and stop.
git diff --quiet
if errorlevel 1 goto :dirty
git diff --cached --quiet
if errorlevel 1 goto :dirty

for /f %%i in ('git rev-parse HEAD') do set "BEFORE=%%i"
echo Fetching the latest version...
git pull --ff-only
if errorlevel 1 (
  echo.
  echo [ERROR] The update failed ^(no network, or your branch has its own commits^). Nothing was changed.
  pause
  exit /b 1
)
for /f %%i in ('git rev-parse HEAD') do set "AFTER=%%i"

if "%BEFORE%"=="%AFTER%" (
  echo Already up to date.
  pause
  exit /b 0
)

echo.
echo Changes:
git log --oneline %BEFORE%..%AFTER%

git diff --quiet %BEFORE% %AFTER% -- requirements.txt
if errorlevel 1 if exist ".venv\Scripts\python.exe" (
  echo.
  echo requirements.txt changed: updating the Python packages...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

echo.
echo Update done. Start the server again with start_local_server.bat or SAOUB_Launcher.bat.
pause
exit /b 0

:dirty
echo [ERROR] You changed files tracked by git. Save them elsewhere or undo them, then run this again:
git status --short --untracked-files=no
pause
exit /b 1
