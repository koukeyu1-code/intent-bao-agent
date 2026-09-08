@echo off
setlocal
cd /d "%~dp0"
echo.
echo [intent-bao] Starting local services...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-local.ps1" -FrontendPort 5173
if errorlevel 1 pause
