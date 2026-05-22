@echo off
chcp 65001 >nul
title knowledge-base-management Service Stopper
echo ========================================
echo   Stopping all services...
echo ========================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-dev.ps1" -Stop
echo.
echo Done. Press any key to exit...
pause >nul
