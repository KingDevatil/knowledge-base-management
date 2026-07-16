@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Knowledge Base Management - 初始化 Docker 配置
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" init
if errorlevel 1 (
    echo.
    echo 配置初始化失败。
)
echo.
pause
