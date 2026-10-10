@echo off
echo ==========================================================
echo Starting Employee Attendance & Analytics API
echo ==========================================================
cd /d "%~dp0"

if not exist ".venv" (
    echo Creating virtual environment (.venv)...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Checking/installing requirements...
python -m pip install -r requirements-dev.txt

set MONGO_URI=mongodb://localhost:27017
set MONGO_DB=attendance_db

echo Loading sample data into MongoDB...
python sample_seed.py

echo.
echo ==========================================================
echo API running at http://127.0.0.1:8000
echo Interactive Documentation: http://127.0.0.1:8000/docs
echo ==========================================================
echo.

uvicorn app.main:app --port 8000 --reload
pause
