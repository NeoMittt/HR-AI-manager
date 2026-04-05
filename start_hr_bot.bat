@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo HR Telegram Bot + Web Panel
echo ==========================================
echo(

echo [1/3] Checking Python...
python --version >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11+ and run again.
  pause
  exit /b 1
)

echo [2/3] Installing requirements...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install requirements.
  pause
  exit /b 1
)

set "ADMIN_URL_FILE=%CD%\runtime\admin_url.txt"
set "RUNNING_URL="

for /f "usebackq delims=" %%I in (`python admin_panel_launcher.py detect 2^>nul`) do set "RUNNING_URL=%%I"
if defined RUNNING_URL (
  echo [3/3] App is already running.
  echo Opening admin panel: %RUNNING_URL%
  start "" "%RUNNING_URL%"
  echo(
  echo Existing app detected. Close the running window first if you want a full restart.
  echo Or run restart_hr_bot.bat to stop the old process and start a fresh one.
  pause
  exit /b 0
)

if exist "%ADMIN_URL_FILE%" del /q "%ADMIN_URL_FILE%"

echo [3/3] Starting app...
echo Admin panel will open automatically when ready.
echo(

start "" /min python admin_panel_launcher.py wait-open
python main.py
set "APP_EXIT=%ERRORLEVEL%"

echo(
if not "%APP_EXIT%"=="0" (
  echo App crashed.
  echo Crash logs folder: %CD%\crash_logs
) else (
  echo App stopped.
)
pause
endlocal
