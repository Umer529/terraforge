@echo off
rem Starts the REST interface + dashboard and the processing worker in two windows.
cd /d "%~dp0"
start "TerraForge server" cmd /k .venv\Scripts\python manage.py runserver 0.0.0.0:8000
start "TerraForge worker" cmd /k .venv\Scripts\python manage.py run_worker
timeout /t 3 >nul
start http://127.0.0.1:8000/
