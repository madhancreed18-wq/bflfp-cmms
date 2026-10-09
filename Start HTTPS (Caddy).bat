@echo off
cd /d "%~dp0"
echo ============================================================
echo   BFLFP CMMS - HTTPS for phones (Caddy reverse proxy)
echo   The app itself must ALSO be running: Start CMMS.bat
echo ============================================================
python deploy\start_caddy.py
pause
