@echo off
rem Deletes all scans, uploads and measurements, then recreates demo users and site.
rem Use before the presentation so you can upload the scans live.
cd /d "%~dp0"
echo This deletes ALL scans in the demo database.
pause
if exist db.sqlite3 del db.sqlite3
if exist storage rmdir /s /q storage
del /q sample_data\*.upload.json 2>nul
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py seed_demo
echo Reset done.
pause
