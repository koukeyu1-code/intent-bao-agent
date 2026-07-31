@echo off
setlocal
cd /d "%~dp0"
echo.
echo [意向宝] 正在启动本地服务。
echo [意向宝] 首次启动需要编译前端，通常约 1-2 分钟，请保持本窗口开启。
echo [意向宝] 页面准备完成后会自动打开浏览器。
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-local.ps1"
if errorlevel 1 pause
