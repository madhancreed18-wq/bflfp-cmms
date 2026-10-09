@echo off
rem ===========================================================================
rem  BFLFP CMMS - TEST / SIMULATION copy
rem
rem  Same code as the live system, completely separate data:
rem     database   data-dev\cmms.db
rem     photos     data-dev\uploads
rem     reports    data-dev\Report
rem     push key   data-dev\vapid_private.pem
rem
rem  It runs on port 8001 so it can sit alongside the live app on 8000, and the
rem  Cloudflare tunnel is off, so nothing here is ever reachable from outside
rem  the office. Every screen carries an orange TEST SYSTEM stripe.
rem
rem  To copy today's real data in and practise on it:  python refresh_dev.py
rem ===========================================================================
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"

set "CMMS_ENV=dev"
set "BFLFP_DATA=%~dp0data-dev"
set "PORT=8001"
set "CMMS_TUNNEL=0"

echo.
echo  ===========================================
echo    TEST SYSTEM - not live factory data
echo    data : %BFLFP_DATA%
echo    url  : http://localhost:8001
echo  ===========================================
echo.
"%PY%" run.py
pause
