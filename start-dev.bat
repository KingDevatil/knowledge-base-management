@echo off
chcp 65001 >nul
title knowledge-base-management Dev Starter
echo ========================================
echo   knowledge-base-management Dev Starter
echo   Log: start-dev.log
echo ========================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-dev.ps1" %*
set "SCRIPT_EXIT=%ERRORLEVEL%"
if not "%~1"=="" exit /b %SCRIPT_EXIT%
echo.
echo Script finished, press any key to exit...
pause >nul
exit /b %SCRIPT_EXIT%
