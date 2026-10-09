@echo off
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
echo Installing requirements...
"%PY%" -m pip install -r requirements.txt
echo Starting BFLFP CMMS...
"%PY%" run.py
pause
