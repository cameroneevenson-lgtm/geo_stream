@echo off
setlocal EnableExtensions
set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"
set "PY=C:\Tools\.venv\Scripts\python.exe"
cd /d "%APPDIR%" || exit /b 1

if not exist "%PY%" (
    echo ERROR: Shared Tools virtual environment not found at:
    echo   C:\Tools\.venv\Scripts\python.exe
    exit /b 1
)

"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo ERROR: Geo Stream requires Python 3.11 or newer.
    exit /b 1
)

"%PY%" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

echo.
echo Geo Stream dependencies are installed in C:\Tools\.venv.
echo Run run_local.bat or run_local_watch.bat.
endlocal
