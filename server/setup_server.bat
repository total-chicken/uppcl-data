@echo off
setlocal enableextensions
:: Batch script to setup and run the UPPCL Local Server as Administrator
set "targetDir=%~dp0"
set "PYVER=3.12.8"
set "PYCMD="

:: Check for Administrator privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [!] Requesting Administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo [1/4] Checking Python installation...
call :find_python
if defined PYCMD goto py_ok

echo     Python not found - downloading and installing Python %PYVER% ...

where winget >nul 2>&1
if %errorlevel%==0 (
    winget install -e --id Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements
    call :find_python
    if defined PYCMD goto py_ok
)

set "PYINST=%TEMP%\python-%PYVER%-amd64.exe"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PYURL%' -OutFile '%PYINST%' -UseBasicParsing } catch { Write-Host $_.Exception.Message; exit 1 }"

if not exist "%PYINST%" (
    echo [!] Could not download the Python installer.
    echo     Install Python 3.9+ manually from https://www.python.org/downloads/ and add it to PATH.
    pause
    exit /b
)

"%PYINST%" /quiet InstallAllUsers=1 PrependPath=1 Include_pip=1 Include_test=0
del "%PYINST%" >nul 2>&1

call :find_python
if not defined PYCMD (
    echo [!] Python installation failed. Install Python 3.9+ manually and add it to PATH.
    pause
    exit /b
)

:py_ok
echo     Using Python: %PYCMD%
%PYCMD% --version
call :add_python_to_path

echo [2/4] Ensuring target directory exists...
if not exist "%targetDir%" mkdir "%targetDir%"

echo [3/4] Installing requirements (skipping if already met)...
%PYCMD% -m pip install --upgrade pip --quiet
%PYCMD% -m pip install -r "%~dp0requirements.txt" --quiet

echo [4/4] Starting Local Sync Server...
%PYCMD% "%~dp0local_server.py"
pause
exit /b 0

REM ==========================================================
REM  :add_python_to_path  ->  add Python's folder + Scripts to
REM  the SYSTEM PATH (persistent, runs elevated) and to this
REM  session, so `python` / `pip` work from any terminal after.
REM ==========================================================
:add_python_to_path
set "PYDIR="
for /f "usebackq delims=" %%I in (`%PYCMD% -c "import sys,os;print(os.path.dirname(sys.executable))" 2^>nul`) do set "PYDIR=%%I"
if not defined PYDIR goto :eof
echo     Ensuring Python is on PATH: %PYDIR%
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d='%PYDIR%'; $s=Join-Path $d 'Scripts'; $m=[Environment]::GetEnvironmentVariable('Path','Machine'); $parts=@(); if($m){$parts=$m -split ';'}; $changed=$false; foreach($p in @($d,$s)){ if($parts -notcontains $p){ $m=($m.TrimEnd(';')+';'+$p); $changed=$true } }; if($changed){ [Environment]::SetEnvironmentVariable('Path',$m,'Machine'); Write-Host '    Python added to system PATH (new terminals).' } else { Write-Host '    Python already on system PATH.' }"
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
