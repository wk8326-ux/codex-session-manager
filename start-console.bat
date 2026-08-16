@echo off
cd /d "%~dp0"
"%SystemRoot%\System32\curl.exe" --fail --silent --max-time 1 http://127.0.0.1:8765/api/health 2>nul | "%SystemRoot%\System32\findstr.exe" /C:"local-project-console" >nul
if not errorlevel 1 goto open_console

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage-system-startup.ps1" -Action Start
if errorlevel 1 (
  pause
  exit /b 1
)

:open_console
start "" "http://127.0.0.1:8765/"
