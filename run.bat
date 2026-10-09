@echo off
REM ===================================================================
REM  CodeReview Agent launcher
REM  Usage: double-click this file, or run  run.bat [args]  in a terminal
REM
REM  Finds a usable Python interpreter, adds the bundled vendor/ folder
REM  to PYTHONPATH (offline dependencies), then starts the agent.
REM
REM  NOTE: this file is intentionally ASCII-only. cmd.exe reads .bat files
REM  using the system ANSI codepage (GBK on Chinese Windows), so non-ASCII
REM  text here would be mis-parsed as commands. All Chinese documentation
REM  lives in README.md instead.
REM ===================================================================

setlocal
cd /d "%~dp0"

REM When double-clicked the window closes instantly, hiding any error.
set "PAUSE_AT_END="
echo %cmdcmdline% | find /i "/c" >nul && set "PAUSE_AT_END=1"

set "PY="

REM ---- 1) project virtualenv ----------------------------------------
REM  Accept python.exe (a real venv) or python.bat (a lightweight shim).
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.bat" set "PY=.venv\Scripts\python.bat"
if defined PY goto :found

REM ---- 2) real python on PATH (skip the Microsoft Store stub) -------
REM  NOTE: no absolute interpreter path is hard-coded here on purpose.
REM  Hard-coding one would leak the author's machine layout and would be
REM  dead code on any other computer.
for /f "delims=" %%P in ('where python 2^>nul') do (
    echo %%P | find /i "WindowsApps" >nul
    if errorlevel 1 set "PY=%%P"
)
if defined PY goto :found

REM ---- 3) py launcher, or a path registered in the registry ---------
where py >nul 2>nul && set "PY=py"
if defined PY goto :found
for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Python\PythonCore" /s /v ExecutablePath 2^>nul ^| find /i "ExecutablePath"') do (
    if exist "%%B" set "PY=%%B"
)
if defined PY goto :found

echo.
echo [ERROR] No usable Python interpreter found.
echo.
echo Common causes:
echo   * Python is not installed. The "python" on PATH may be the
echo     Microsoft Store stub, which does not actually run Python.
echo   * Python is installed but "Add python.exe to PATH" was not ticked.
echo.
echo How to fix:
echo   1) Install Python 3.10+ from https://www.python.org/downloads/
echo      and tick "Add python.exe to PATH" during setup.
echo   2) Or, inside this folder, run:
echo        python -m venv .venv
echo        .venv\Scripts\activate
echo        pip install -r requirements.txt
echo      then this script will use .venv automatically.
echo.
echo See the "Quick Start" section of README.md for details.
if defined PAUSE_AT_END pause
exit /b 1

:found
REM Make project modules importable; vendor/ provides offline deps.
REM
REM Beware two traps here:
REM   * "set VAR=" leaves VAR *defined but empty*, so "if defined" is not a
REM     reliable emptiness test;
REM   * writing "%NEW%;%PYTHONPATH%" when PYTHONPATH is empty leaves a
REM     trailing separator, which Python reads as "the current directory"
REM     and can abort startup with "failed to make path absolute".
REM So: remember the old value first, then append only if it was non-empty.
set "OLD_PATH=!PYTHONPATH!"
set "PYTHONPATH=%~dp0vendor"
if not "!OLD_PATH!"=="" set "PYTHONPATH=%~dp0vendor;!OLD_PATH!"

REM Verify the dependency actually imports. The interpreter's own error is
REM left visible on purpose: swallowing it turns a startup failure (bad
REM PYTHONPATH, broken venv) into a misleading "package not found" hint.
if not exist "vendor\openai" (
    "%PY%" -c "import openai" >nul
    if errorlevel 1 (
        echo.
        echo [HINT] Could not import the "openai" package with this interpreter:
        echo            %PY%
        echo        The error above, if any, is the real cause.
        echo        Install the dependencies with:
        echo            "%PY%" -m pip install -r requirements.txt
        echo.
        if defined PAUSE_AT_END pause
        exit /b 1
    )
)

"%PY%" main.py %*
set "EXITCODE=%ERRORLEVEL%"

if defined PAUSE_AT_END (
    echo.
    pause
)
endlocal & exit /b %EXITCODE%
