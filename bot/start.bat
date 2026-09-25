@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Parley

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Parley is not set up yet. Double-click setup.bat first.
    pause
    exit /b 1
)
if not exist ".env" (
    echo [ERROR] .env was not found. Run setup.bat, then put your bot token in .env
    pause
    exit /b 1
)

echo Starting Parley - press CTRL+C once to stop.
echo After it says "ready", configure everything in Discord with /setup and /settings.
echo.
".venv\Scripts\python.exe" -m bot.main
set "EXIT_CODE=%errorlevel%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo ============================================
    echo   Parley stopped with an error (code %EXIT_CODE%^).
    echo   Read the messages above, or logs\parley.log
    echo ============================================
    pause
) else (
    echo Parley stopped.
)
exit /b %EXIT_CODE%
