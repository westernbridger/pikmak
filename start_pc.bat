@echo off
rem Sets up a local Python environment on first run, then starts the PC server.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: creating Python environment...
    python -m venv .venv || goto :fail
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements-pc.txt || goto :fail
)
".venv\Scripts\python.exe" pc_server.py
pause
exit /b

:fail
echo Setup failed. Make sure Python 3.10+ is installed and on PATH.
pause
