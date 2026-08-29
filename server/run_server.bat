@echo off
setlocal enableextensions
title UPPCL Local Server
color 0A

REM ==========================================================
REM  Version of Python to auto-install if none is found
REM ==========================================================
set "PYVER=3.12.8"
set "PYCMD="

echo ============================================================
echo   UPPCL DATA SERVER  -  Starting on port 7321
echo ============================================================
echo.

REM ---- 1. Look for an existing Python -------------------------
call :find_python
if defined PYCMD goto have_python

echo Python was not found on this PC.
echo This script will now download and install Python %PYVER%.
echo (An internet connection is required for this one-time step.)
echo.

REM ---- 2. Try winget (built into Windows 10/11) --------------
where winget >nul 2>&1
if %errorlevel%==0 (
    echo Installing Python via winget, please wait...
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-source-agreements --accept-package-agreements
    call :find_python
    if defined PYCMD goto have_python
)

REM ---- 3. Fallback: download the official installer ---------
set "PYINST=%TEMP%\python-%PYVER%-amd64.exe"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"

echo Downloading Python installer...
echo   %PYURL%
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PYURL%' -OutFile '%PYINST%' -UseBasicParsing } catch { Write-Host $_.Exception.Message; exit 1 }"

if not exist "%PYINST%" (
    echo.
    echo ERROR: Could not download the Python installer.
    echo Please install Python 3.8+ manually from https://www.python.org/downloads/
    echo and be sure to tick "Add Python to PATH" during setup.
    pause
    exit /b 1
)

echo Installing Python %PYVER% ... this can take a few minutes.
"%PYINST%" /quiet PrependPath=1 Include_pip=1 Include_test=0
del "%PYINST%" >nul 2>&1

call :find_python
if not defined PYCMD (
    echo.
    echo ERROR: Python installation did not complete successfully.
    echo Please install Python 3.8+ manually from https://www.python.org/downloads/
    echo and be sure to tick "Add Python to PATH" during setup.
    pause
    exit /b 1
)

:have_python
echo Using Python: %PYCMD%
%PYCMD% --version
echo.

REM ---- 3b. Make sure Python is on PATH -----------------------
call :add_python_to_path

REM ---- 4. Install / upgrade dependencies -------------------
echo Installing dependencies...
%PYCMD% -m pip install --upgrade pip -q --disable-pip-version-check
%PYCMD% -m pip install flask flask-cors requests -q --disable-pip-version-check
echo Dependencies OK.
echo.

REM ---- 5. Check config.json exists ------------------------
if not exist "%~dp0config.json" (
    echo ERROR: config.json not found in this folder.
    echo        Make sure config.json is in the same folder as this script.
    pause
    exit /b 1
)

echo Starting server...
echo If Windows Firewall asks, click "Allow access" (Private networks).
echo.

%PYCMD% "%~dp0local_server.py"

echo.
echo Server stopped.
pause
exit /b 0

REM ==========================================================
REM  :add_python_to_path  ->  add Python's folder + Scripts to
REM  the user PATH (persistent) and to this session's PATH, so
REM  `python` / `pip` work from any terminal afterwards.
REM ==========================================================
:add_python_to_path
set "PYDIR="
for /f "usebackq delims=" %%I in (`%PYCMD% -c "import sys,os;print(os.path.dirname(sys.executable))" 2^>nul`) do set "PYDIR=%%I"
if not defined PYDIR goto :eof
echo Ensuring Python is on PATH: %PYDIR%
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d='%PYDIR%'; $s=Join-Path $d 'Scripts'; $u=[Environment]::GetEnvironmentVariable('Path','User'); $parts=@(); if($u){$parts=$u -split ';'}; $changed=$false; foreach($p in @($d,$s)){ if($parts -notcontains $p){ $u=($u.TrimEnd(';')+';'+$p); $changed=$true } }; if($changed){ [Environment]::SetEnvironmentVariable('Path',$u,'User'); Write-Host 'Python added to user PATH (new terminals).' } else { Write-Host 'Python already on user PATH.' }"
REM  update PATH for the current window too (persistent change only affects new ones)
echo ;%PATH%; | find /i ";%PYDIR%;" >nul || set "PATH=%PATH%;%PYDIR%;%PYDIR%\Scripts"
goto :eof

REM ==========================================================
REM  :find_python  ->  sets PYCMD if a usable Python is found
REM ==========================================================
:find_python
set "PYCMD="
python --version >nul 2>&1
if %errorlevel%==0 (
    set "PYCMD=python"
    goto :eof
)
py -3 --version >nul 2>&1
if %errorlevel%==0 (
    set "PYCMD=py -3"
    goto :eof
)
for %%P in (
    "%LocalAppData%\Programs\Python\Python313\python.exe"
    "%LocalAppData%\Programs\Python\Python312\python.exe"
    "%LocalAppData%\Programs\Python\Python311\python.exe"
    "%LocalAppData%\Programs\Python\Python310\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python311\python.exe"
    "%ProgramFiles%\Python310\python.exe"
) do (
    if exist "%%~P" (
        set "PYCMD="%%~fP""
        goto :eof
    )
)
goto :eof
