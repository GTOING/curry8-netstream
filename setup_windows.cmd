@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_windows.ps1" %*
set "setup_exit_code=%errorlevel%"
echo.
if not "%setup_exit_code%"=="0" echo Environment setup failed. See the error above.
pause
exit /b %setup_exit_code%
