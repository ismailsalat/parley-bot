@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Waypoint - Setup

echo ============================================
echo   Installing Waypoint
echo ============================================
echo.

rem ---- 1. Find Python 3.12 or newer -----------------------------------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3.12 or newer is needed and was not found.
    echo.
    echo  1. Download it from https://www.python.org/downloads/
    echo  2. During installation tick "Add python.exe to PATH"
    echo  3. Double-click setup.bat again
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PY% --version') do echo Found %%v

rem ---- 2. Private Python environment for Waypoint ---------------------
if not exist ".venv\Scripts\python.exe" (
    echo Preparing Waypoint's own Python environment ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo Could not prepare the Python environment. Try running setup.bat again.
        pause
        exit /b 1
    )
)
call ".venv\Scripts\activate.bat"

rem ---- 3. Install the exact tested versions ---------------------------
echo Downloading what Waypoint needs (this can take a minute) ...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.lock --quiet
if errorlevel 1 (
    echo.
    echo Download failed. Check your internet connection and run setup.bat again.
    pause
    exit /b 1
)

rem ---- 4. Settings file ------------------------------------------------
set "NEW_ENV="
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    set "NEW_ENV=1"
)

rem ---- 5. Prepare the database -----------------------------------------
echo Preparing the database ...
python -m alembic upgrade head >nul 2>nul
if errorlevel 1 (
    echo Note: the database will be prepared when Waypoint starts.
)

rem ---- 6. Done -------------------------------------------------------------
echo.
echo ============================================
echo   Waypoint is installed.
echo ============================================
echo.
echo 1. Add your Discord token to .env
echo    (Discord Developer Portal - your app - Bot - Reset Token)
echo 2. Run start.bat
echo 3. In Discord use /setup
echo.
if defined NEW_ENV (
    choice /c YN /n /m "Open .env in Notepad now to paste the token? [Y/N] "
    if not errorlevel 2 start "" notepad ".env"
)
echo Press any key to exit.
pause >nul
exit /b 0
