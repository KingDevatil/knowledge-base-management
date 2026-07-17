@echo off
setlocal EnableDelayedExpansion
set "KNOWBASE_CLI_SHIM=%~f0"
set "KNOWBASE_HOME_FILE=%LOCALAPPDATA%\KnowledgeBaseManagement\knowbase-home.txt"
if exist "%~dp0knowbase-home.txt" set "KNOWBASE_HOME_FILE=%~dp0knowbase-home.txt"
if not exist "%KNOWBASE_HOME_FILE%" (
    echo [ERROR] knowbase-home.txt not found. Re-run start.ps1 cli-install.
    exit /b 1
)
set "KNOWBASE_HOME="
set /p "KNOWBASE_HOME="<"%KNOWBASE_HOME_FILE%"
if not defined KNOWBASE_HOME (
    echo [ERROR] Knowbase project path is empty. Re-run start.ps1 cli-install.
    exit /b 1
)
if not exist "%KNOWBASE_HOME%\scripts\knowbase.ps1" (
    echo [ERROR] Knowbase project was moved or removed: %KNOWBASE_HOME%
    echo         Run start.ps1 cli-install again from the new project directory.
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%KNOWBASE_HOME%\scripts\knowbase.ps1" %* & exit /b !ERRORLEVEL!
