@echo off
setlocal EnableExtensions
set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"
set "PY=C:\Tools\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo ERROR: Shared Tools virtual environment not found.
    echo Run setup_env.bat first.
    exit /b 1
)

if "%GEO_STREAM_PORT%"=="" set "GEO_STREAM_PORT=8501"
powershell -NoProfile -Command "$port = 0; if (-not [int]::TryParse($env:GEO_STREAM_PORT, [ref]$port) -or $port -lt 1 -or $port -gt 65535) { exit 1 }"
if errorlevel 1 (
    echo ERROR: GEO_STREAM_PORT must be an integer from 1 to 65535.
    exit /b 1
)

cd /d "%APPDIR%" || exit /b 1
"%PY%" watch_and_run.py
endlocal
