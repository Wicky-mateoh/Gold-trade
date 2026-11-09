@echo off
REM Runs the GITS server and restarts it if it stops. Logs go to var\logs.
REM Usage: double-click, or let install_autostart.ps1 start it at logon.
setlocal
cd /d "%~dp0\..\.."
set PYTHONPATH=%CD%\backend
set GITS_ROOT=%CD%
if not exist var\logs mkdir var\logs

:loop
echo [%date% %time%] Starting GITS >> var\logs\supervisor.log
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >> var\logs\server.out 2>&1
echo [%date% %time%] GITS exited with code %errorlevel%; restarting in 15 seconds >> var\logs\supervisor.log
ping -n 16 127.0.0.1 > nul
goto loop
