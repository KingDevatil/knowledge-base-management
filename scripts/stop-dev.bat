@echo off
chcp 65001 >nul
title knowledge-base-management Service Stopper
for %%I in ("%~dp0..") do set "ROOT_DIR=%%~fI"
cd /d "%ROOT_DIR%"
echo ========================================
echo   Stopping all services...
echo ========================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT_DIR%\start-dev.ps1" -Stop
echo.
echo Done. Press any key to exit...
pause >nul
