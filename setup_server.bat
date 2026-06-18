@echo off
:: Batch script to setup and run the UPPCL Local Server as Administrator
set "targetDir=C:\DEVSOF\APP SCRIPT PROJECT\GIT ANSHU\UPPCL"

:: Check for Administrator privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [!] Requesting Administrator privileges...
    powershell -Command "Start-Process '%~0' -Verb RunAs"
    exit /b
)

:: FIX: Set working directory to the script's folder
cd /d "%~dp0"

echo [1/4] Checking Python installation...
python --version >nul 2>&1
if %errorLevel% neq 0 (
    echo [!] Python not found. Please install Python 3.9+ and add it to PATH.
    pause
    exit /b
)

echo [2/4] Ensuring target directory exists...
if not exist "%targetDir%" mkdir "%targetDir%"

echo [3/4] Installing requirements (skipping if already met)...
pip install -r requirements.txt --quiet

echo [4/4] Starting Local Sync Server...
python local_server.py
pause
