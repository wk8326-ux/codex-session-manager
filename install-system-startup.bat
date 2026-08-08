@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage-system-startup.ps1" -Action Install -StartNow
if errorlevel 1 pause
