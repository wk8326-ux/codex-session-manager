@echo off
cd /d "%~dp0"
where py.exe >nul 2>nul
if not errorlevel 1 (
  py.exe -3 "%~dp0app.py" --stop
  exit /b %errorlevel%
)
python.exe "%~dp0app.py" --stop
