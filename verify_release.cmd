@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv || exit /b 1
)
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt || exit /b 1
.venv\Scripts\python.exe -m compileall -q bot migrations || exit /b 1
.venv\Scripts\python.exe -m pyflakes bot tests migrations || exit /b 1
.venv\Scripts\python.exe -m pytest -q || exit /b 1
echo.
echo Parley local checks passed. Also verify PostgreSQL GitHub Actions before deploying.
