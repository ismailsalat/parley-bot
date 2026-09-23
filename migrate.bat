@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Parley - Database migrations

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Parley is not set up yet. Double-click setup.bat first.
    pause
    exit /b 1
)
call ".venv\Scripts\activate.bat"
echo Applying database migrations ...
python -m alembic upgrade head
if errorlevel 1 (
    echo.
    echo [ERROR] Migrations failed. Check DATABASE_URL in .env and that the database is running.
    pause
    exit /b 1
)
echo.
echo Database is up to date.
pause
exit /b 0
