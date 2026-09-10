@echo off
setlocal
cd /d "%~dp0server"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python venv not found. Run the dependency setup bat first.
  pause
  exit /b 1
)
echo.
choice /C AP /N /M "Select account to reset [A=admin, P=planner]: "
if errorlevel 2 (
  set "INTENT_BAO_RESET_USER=planner"
) else (
  set "INTENT_BAO_RESET_USER=admin"
)
echo.
choice /C GM /N /M "Select method [G=generate strong password, M=manual input]: "
if errorlevel 2 (
  echo [INFO] Typed characters will not show on screen. This is normal.
  ".venv\Scripts\python.exe" -m app.auth_admin "%INTENT_BAO_RESET_USER%" --data-dir "%CD%\data"
) else (
  ".venv\Scripts\python.exe" -m app.auth_admin "%INTENT_BAO_RESET_USER%" --data-dir "%CD%\data" --generate
)
set INTENT_BAO_RESET_EXIT=%ERRORLEVEL%
pause
exit /b %INTENT_BAO_RESET_EXIT%
