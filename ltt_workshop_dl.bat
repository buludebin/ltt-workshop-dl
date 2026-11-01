@echo off
setlocal
cd /d "%~dp0"

set "PRUN="

rem --- prefer the py launcher (works even when "python" is the Store alias) ---
where py >nul 2>nul
if not errorlevel 1 set "PRUN=py -3"

if not defined PRUN (
  for /f "delims=" %%P in ('where python 2^>nul') do (
    echo %%P | find /i "WindowsApps" >nul
    if errorlevel 1 if not defined PRUN set "PRUN=%%P"
  )
)

if not defined PRUN goto nopy

echo Launcher: %PRUN%
echo.
%PRUN% "%~dp0ltt_workshop_dl.py" %*
set "RC=%ERRORLEVEL%"
echo.
echo Exit code: %RC%
pause
exit /b %RC%

:nopy
echo [ERROR] Python 3 not found on PATH.
echo         Install Python 3, or ask me for a packaged .exe build.
pause
exit /b 1
