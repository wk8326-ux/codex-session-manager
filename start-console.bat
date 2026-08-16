@echo off
cd /d "%~dp0"
where py.exe >nul 2>nul
if not errorlevel 1 (
  py.exe -3 "%~dp0scripts\launch_console.py"
  goto launcher_done
)

where python.exe >nul 2>nul
if errorlevel 1 (
  echo Python 3 was not found. Run install-system-startup.bat after installing Python.
  pause
  exit /b 1
)
python.exe "%~dp0scripts\launch_console.py"

:launcher_done
if errorlevel 1 pause
