@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Knowledge Base Management - Docker 配置向导
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" configure
if errorlevel 1 (
    echo.
    echo 配置向导运行失败。
)
echo.
pause
