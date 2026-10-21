@echo off
rem One-time setup: virtual environment, packages, database, demo users, sample scans.
cd /d "%~dp0"
if not exist .venv (
  py -3.12 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py seed_demo
.venv\Scripts\python tools\make_sample_scans.py sample_data
echo.
echo Setup done. Run start_demo.bat
pause
