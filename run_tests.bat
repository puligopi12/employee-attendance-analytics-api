@echo off
echo ==========================================================
echo Running Automated Test Suite
echo ==========================================================
cd /d "%~dp0"

if not exist ".venv" (
    echo Creating virtual environment (.venv)...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    python -m pip install -r requirements-dev.txt
) else (
    call .venv\Scripts\activate.bat
)

pytest -v
pause
