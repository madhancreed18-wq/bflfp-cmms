@echo off
cd /d "%~dp0"
echo Installing requirements (first run only)...
pip install -r requirements.txt >nul 2>&1
echo Starting BFLFP CMMS...
python run.py
pause
