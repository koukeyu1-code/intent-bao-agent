@echo off
setlocal
cd /d "%~dp0server"
if not exist ".venv\Scripts\python.exe" (
  echo [意向宝] 请先运行“首次安装依赖.bat”。
  pause
  exit /b 1
)
echo.
choice /C AP /N /M "请选择要重置的账号 [A=admin, P=planner]："
if errorlevel 2 (
  set "INTENT_BAO_RESET_USER=planner"
) else (
  set "INTENT_BAO_RESET_USER=admin"
)
echo.
choice /C GM /N /M "请选择设置方式 [G=生成随机强密码, M=手动设置]："
if errorlevel 2 (
  echo [意向宝] 输入密码时屏幕不会显示字符，这是正常的。
  ".venv\Scripts\python.exe" -m app.auth_admin "%INTENT_BAO_RESET_USER%" --data-dir "%CD%\data"
) else (
  ".venv\Scripts\python.exe" -m app.auth_admin "%INTENT_BAO_RESET_USER%" --data-dir "%CD%\data" --generate
)
set INTENT_BAO_RESET_EXIT=%ERRORLEVEL%
pause
exit /b %INTENT_BAO_RESET_EXIT%
