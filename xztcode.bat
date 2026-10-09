@echo off
REM ===================================================================
REM  xztcode launcher
REM
REM  Works two ways:
REM    1) double-clicked / called from inside the project folder
REM    2) invoked as a global command from any directory, after
REM       running tools_dev\install-command.ps1 once
REM
REM  It locates the project by walking up from its own location, so it
REM  contains no absolute paths and can be safely submitted.
REM
REM  NOTE: ASCII-only on purpose. cmd.exe reads .bat files using the
REM  system ANSI codepage, so non-ASCII text would be mis-parsed.
REM  Chinese documentation lives in README.md.
REM ===================================================================

setlocal enabledelayedexpansion

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

REM ---- locate the project root (the folder holding main.py) ----------
REM If this file sits in the project root, that is the root. Otherwise
REM the global copy is in <user>\.xztcode\bin, so the project path was
REM written next to it by install-command.ps1.
set "ROOT="

if exist "%HERE%\main.py" set "ROOT=%HERE%"

REM The project path may contain non-ASCII characters (Chinese folder names
REM are very common). cmd decodes a text file using the *active console
REM codepage*, so reading such a path only works when the writing codepage
REM and the active codepage agree. That is fragile, so we pin both sides:
REM
REM   attempt 1: switch to codepage 65001 and read the UTF-8 copy
REM   attempt 2: stay on the current codepage and read the default-codepage copy
REM
REM Whichever attempt yields a folder that really contains main.py wins.
set "OLDCP="
for /f "tokens=2 delims=:" %%C in ('chcp') do set "OLDCP=%%C"
REM strip the leading space that chcp prints
set "OLDCP=%OLDCP: =%"

if exist "%HERE%\..\project-utf8.txt" (
    chcp 65001 >nul
    set /p ROOT=<"%HERE%\..\project-utf8.txt"
)

if defined ROOT (
    if not exist "%ROOT%\main.py" set "ROOT="
)

if not defined ROOT (
    if defined OLDCP chcp %OLDCP% >nul
    if exist "%HERE%\..\project.txt" (
        set /p ROOT=<"%HERE%\..\project.txt"
    )
)

REM Anything that still does not point at main.py counts as a failure.
if defined ROOT (
    if not exist "%ROOT%\main.py" set "ROOT="
)

if not defined ROOT (
    echo.
    echo [ERROR] Could not locate the xztcode project folder.
    echo.
    echo If you copied xztcode.bat somewhere manually, either:
    echo   * put it back next to main.py, or
    echo   * run tools_dev\install-command.ps1 again from the project.
    echo.
    pause
    exit /b 1
)

cd /d "%ROOT%"

REM ---- pick an interpreter ------------------------------------------
set "PY="

if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.bat" set "PY=.venv\Scripts\python.bat"
if defined PY goto :run

for /f "delims=" %%P in ('where python 2^>nul') do (
    echo %%P | find /i "WindowsApps" >nul
    if errorlevel 1 set "PY=%%P"
)
if defined PY goto :run

where py >nul 2>nul && set "PY=py"
if defined PY goto :run

for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Python\PythonCore" /s /v ExecutablePath 2^>nul ^| find /i "ExecutablePath"') do (
    if exist "%%B" set "PY=%%B"
)
if defined PY goto :run

echo.
echo [ERROR] No usable Python interpreter found.
echo.
echo Install Python 3.10+ from https://www.python.org/downloads/ and
echo tick "Add python.exe to PATH", then create the environment:
echo     python -m venv .venv
echo     .venv\Scripts\activate
echo     pip install -r requirements.txt
echo.
pause
exit /b 1

:run
REM See run.bat for the full explanation. In short: remember the old value
REM first, because an empty PYTHONPATH must not leave a trailing separator
REM (Python would read that as "current directory" and may abort startup).
set "OLD_PATH=!PYTHONPATH!"
set "PYTHONPATH=%ROOT%\vendor"
if not "!OLD_PATH!"=="" set "PYTHONPATH=%ROOT%\vendor;!OLD_PATH!"

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
        pause
        exit /b 1
    )
)

"%PY%" main.py %*
set "CODE=%ERRORLEVEL%"

endlocal & exit /b %CODE%
