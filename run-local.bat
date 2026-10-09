@echo off
REM ===================================================================
REM  LOCAL-ONLY setup helper  (this file is NOT part of the submission)
REM
REM  Why it exists: the machine used to build this project has no
REM  system-wide Python installation. The portable launchers (run.bat,
REM  xztcode.bat) look for, in order:
REM
REM      .venv\Scripts\python.exe  ->  python on PATH  ->  py  ->  registry
REM
REM  None of those exist here. So this script writes a tiny shim into
REM  .venv\Scripts\ that forwards to the interpreter bundled with the
REM  build environment. After that, every launcher finds it normally and
REM  nothing else needs changing.
REM
REM  It stays out of the zip on purpose: it hard-codes a path containing
REM  the local Windows user name.
REM ===================================================================

setlocal
cd /d "%~dp0"

set "REAL=C:\Users\18969\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
set "SHIM=%~dp0.venv\Scripts\python.bat"

if not exist "%REAL%" (
    echo.
    echo [ERROR] Bundled interpreter not found:
    echo         %REAL%
    echo.
    echo         This helper is only meant for the build machine.
    echo         On a normal machine just install Python 3.10+ and run:
    echo             python -m venv .venv
    echo             .venv\Scripts\activate
    echo             pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0.venv\Scripts" mkdir "%~dp0.venv\Scripts"

> "%SHIM%" echo @echo off
>>"%SHIM%" echo REM Auto-generated shim - see run-local.bat
>>"%SHIM%" echo "%REAL%" %%*

echo.
echo [OK] Interpreter shim installed:
echo      %SHIM%
echo      -^> %REAL%
echo.
echo Now you can use any of these:
echo      run.bat          (portable launcher)
echo      xztcode.bat      (branded launcher)
echo      xztcode          (if you ran tools_dev\install-command.ps1)
echo.

endlocal
exit /b 0
