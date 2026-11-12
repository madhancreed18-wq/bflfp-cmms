@echo off
rem Opens this project in VS Code; the "CMMS server" task (.vscode\tasks.json) starts the server in a VS Code terminal.
cd /d "%~dp0"
where code >nul 2>nul
if %errorlevel%==0 (
  start "" code "%~dp0."
) else if exist "%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe" (
  start "" "%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe" "%~dp0."
) else if exist "%ProgramFiles%\Microsoft VS Code\Code.exe" (
  start "" "%ProgramFiles%\Microsoft VS Code\Code.exe" "%~dp0."
) else (
  echo VS Code not found - starting the server in this window instead.
  "%~dp0.venv\Scripts\python.exe" run.py
  pause
)
