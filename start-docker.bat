@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Knowledge Base Management - Docker
if "%~1"=="" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" up
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
)
if errorlevel 1 (
    echo.
    echo Docker 部署命令执行失败，请查看上方错误。
    pause
) else (
    echo.
    echo 命令执行完成，按任意键关闭窗口。
    pause >nul
)
