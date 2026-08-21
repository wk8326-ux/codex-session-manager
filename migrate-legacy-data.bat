@echo off
setlocal
cd /d "%~dp0"
set "LEGACY_ROOT=%~1"
if not defined LEGACY_ROOT set "LEGACY_ROOT=%~dp0..\localhost-project-console"

if not exist "%LEGACY_ROOT%\watchdog.db" if not exist "%LEGACY_ROOT%\data\watchdog.db" (
  echo [CSM] No legacy watchdog.db found under:
  echo       %LEGACY_ROOT%
  exit /b 1
)

where py.exe >nul 2>nul
if not errorlevel 1 (
  py.exe -3 "%~dp0app.py" --migrate-from "%LEGACY_ROOT%"
  exit /b %errorlevel%
)
python.exe "%~dp0app.py" --migrate-from "%LEGACY_ROOT%"
