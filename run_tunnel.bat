@echo off
setlocal EnableExtensions
set "APPDIR=%~dp0"
if "%GEO_STREAM_PORT%"=="" set "GEO_STREAM_PORT=8501"
powershell -NoProfile -Command "$port = 0; if (-not [int]::TryParse($env:GEO_STREAM_PORT, [ref]$port) -or $port -lt 1 -or $port -gt 65535) { exit 1 }"
if errorlevel 1 (
    echo ERROR: GEO_STREAM_PORT must be an integer from 1 to 65535.
    exit /b 1
)
set "ORIGIN=http://127.0.0.1:%GEO_STREAM_PORT%"

where cloudflared >nul 2>&1
if errorlevel 1 (
    echo ERROR: cloudflared was not found on PATH.
    exit /b 1
)

powershell -NoProfile -Command "$ProgressPreference = 'SilentlyContinue'; $origin = 'http://127.0.0.1:' + $env:GEO_STREAM_PORT; try { $response = Invoke-WebRequest -UseBasicParsing -Uri ($origin + '/_stcore/health') -TimeoutSec 5; if ($response.StatusCode -ne 200) { exit 1 } } catch { exit 1 }"
if errorlevel 1 (
    echo ERROR: Geo Stream is not healthy at %ORIGIN%.
    echo Start run_local.bat or run_local_watch.bat first.
    exit /b 1
)

echo.
echo WARNING: This creates a public TryCloudflare URL with no access control.
echo Quick tunnels are for temporary development and review only.
echo The tunnel remains available only while this window is open.
echo Origin: %ORIGIN%
echo.
pause

cd /d "%APPDIR%" || exit /b 1
cloudflared tunnel --url "%ORIGIN%"
endlocal
