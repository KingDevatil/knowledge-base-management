@echo off
:: KB Launcher — 双击启动
for %%I in ("%~dp0..") do set "ROOT_DIR=%%~fI"
cd /d "%ROOT_DIR%"

set "PYTHON="
where py >nul 2>&1
if not errorlevel 1 set "PYTHON=py"
if defined PYTHON goto :python_found

where python >nul 2>&1
if not errorlevel 1 set "PYTHON=python"
if defined PYTHON goto :python_found

where python3 >nul 2>&1
if not errorlevel 1 set "PYTHON=python3"

if not defined PYTHON (
    echo [ERROR] Python not found. Please install Python 3.9+ and add it to PATH.
    pause
    exit /b 1
)

:python_found
%PYTHON% "%ROOT_DIR%\run_launcher.py"
set "SCRIPT_EXIT=%ERRORLEVEL%"
if not "%SCRIPT_EXIT%"=="0" pause
exit /b %SCRIPT_EXIT%
