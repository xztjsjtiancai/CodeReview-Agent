@echo off
REM ===================================================================
REM  Double-click this file to install the "xztcode" global command.
REM
REM  It simply calls tools_dev\install-command.ps1 with the right path,
REM  so you do not have to cd anywhere or type a long command.
REM
REM  ASCII-only on purpose (cmd reads .bat using the system codepage).
REM ===================================================================

setlocal
cd /d "%~dp0"

echo.
echo Installing the "xztcode" global command...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools_dev\install-command.ps1"
set "CODE=%ERRORLEVEL%"

echo.
if "%CODE%"=="0" (
    echo Done. Close this window, open a NEW terminal, then type:  xztcode
) else (
    echo Installation did not finish. See the messages above.
)

echo.
pause
endlocal & exit /b %CODE%
