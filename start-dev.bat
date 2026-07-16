@echo off
chcp 65001 >nul
title knowledge-base-management Dev Starter
echo ========================================
echo   knowledge-base-management Dev Starter
echo   Log: start-dev.log
echo ========================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-dev.ps1" %*
echo.
echo Script finished, press any key to exit...
pause >nul
