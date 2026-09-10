@echo off
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

if not exist "launcher.py" (
    echo [ERROR] launcher.py not found in project root.
    pause
    exit /b 1
)

"%PY%" launcher.py
