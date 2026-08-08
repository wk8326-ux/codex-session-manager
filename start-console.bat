@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage-system-startup.ps1" -Action Start
if errorlevel 1 (
  pause
  exit /b 1
)
start "" "http://127.0.0.1:8765/"
