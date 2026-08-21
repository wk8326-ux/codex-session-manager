@echo off
cd /d "%~dp0"
where py.exe >nul 2>nul
if not errorlevel 1 (
  py.exe -3 "%~dp0scripts\register_with_console.py"
  if errorlevel 1 pause
  exit /b %errorlevel%
)
python.exe "%~dp0scripts\register_with_console.py"
if errorlevel 1 pause
