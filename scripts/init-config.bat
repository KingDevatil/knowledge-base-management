@echo off
chcp 65001 >nul
for %%I in ("%~dp0..") do set "ROOT_DIR=%%~fI"
cd /d "%ROOT_DIR%"
title Knowledge Base Management - Docker 配置向导
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT_DIR%\start.ps1" configure
if errorlevel 1 (
    echo.
    echo 配置向导运行失败。
)
echo.
pause
