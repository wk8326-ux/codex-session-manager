@echo off
cd /d "%~dp0"
where py.exe >nul 2>nul
if not errorlevel 1 (
  py.exe -3 -u "%~dp0app.py" --service
  exit /b %errorlevel%
)
python.exe -u "%~dp0app.py" --service
