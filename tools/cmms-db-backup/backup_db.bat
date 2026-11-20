@echo off
rem Stand-alone CMMS database backup (does not use or stop the CMMS app).
rem Backups go to the folder set in backup_db.py (BACKUP_DIR, default D:\cmms-db-backup).
rem
rem Run every night at 02:00 - paste once in an ADMIN Command Prompt on the server:
rem   schtasks /Create /TN "CMMS DB backup" /TR "\"D:\bflfp-cmms\tools\cmms-db-backup\backup_db.bat\"" /SC DAILY /ST 02:00 /RU SYSTEM /RL HIGHEST /F
cd /d "%~dp0"
set "PY=D:\bflfp-cmms\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0backup_db.py"
