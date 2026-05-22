@echo off
chcp 65001 >nul
title Knowledge Base Management - 初始化配置
echo ========================================
echo   知识库管理系统 - 初始化配置
echo ========================================
echo.
echo 正在生成随机 SESSION_SECRET...
echo.

:: 生成 32 位随机密钥
for /f "delims=" %%i in ('powershell -NoProfile -Command "-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 32 | %% {[char]$_})"') do set KEY=%%i

echo 生成的密钥: %KEY%
echo.

:: 检查 .env 是否存在
if exist ".env" (
    echo 检测到 .env 文件已存在。
    choice /c YN /M "是否覆盖 SESSION_SECRET？"
    if errorlevel 2 goto :SKIP
)

:: 如果 .env 不存在，从模板创建
if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env >nul
        echo 已从 .env.example 创建 .env 文件
    ) else (
        echo [错误] 找不到 .env.example 模板文件
        pause
        exit /b 1
    )
)

:: 替换 SESSION_SECRET
powershell -NoProfile -Command "(Get-Content .env) -replace 'SESSION_SECRET=.*', 'SESSION_SECRET=%KEY%' | Set-Content .env"
echo ✅ SESSION_SECRET 已更新
echo.

:SKIP
echo.
echo ========================================
echo   配置完成！
echo   你现在可以启动服务：
echo     docker compose up -d
echo ========================================
echo.
pause
