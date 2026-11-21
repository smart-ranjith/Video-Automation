@echo off
setlocal
cd /d "%~dp0"
set PATH=C:\Users\ranji\AppData\Roaming\npm;%PATH%
echo ===================================================
echo [CineMind] Triggering Autonomous Video Generator...
echo ===================================================
call .venv\Scripts\python.exe generator.py
echo.
echo ===================================================
echo [CineMind] Run Finished.
echo ===================================================
pause
