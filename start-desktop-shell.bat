@echo off
title KB Desktop Shell
cd /d "%~dp0"

REM ============================================
REM  Detect Python interpreter
REM ============================================
set "PYTHON="

where py >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=py"
    goto :found
)

where python >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=python"
    goto :found
)

where python3 >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=python3"
    goto :found
)

echo [ERROR] Python not found. Please install Python 3.9+ and add it to PATH.
echo.
pause
exit /b 1

:found
echo [INFO] Using interpreter: %PYTHON%

REM ============================================
REM  Check if pywebview is installed
REM ============================================
%PYTHON% -c "import webview" >nul 2>&1
if %errorlevel%==0 (
    echo [INFO] pywebview is installed.
    goto :launch
)

REM ============================================
REM  Auto-install dependencies
REM ============================================
echo [INFO] pywebview not found. Installing from requirements-desktop.txt ...
%PYTHON% -m pip install -r "%~dp0requirements-desktop.txt"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Failed to install dependencies.
    echo         Try manually: %PYTHON% -m pip install -r requirements-desktop.txt
    echo.
    pause
    exit /b 1
)

REM Verify install succeeded
%PYTHON% -c "import webview" >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] pywebview still missing after install.
    echo.
    pause
    exit /b 1
)

echo [INFO] Dependencies installed successfully.

:launch
REM ============================================
REM  Launch desktop shell
REM ============================================
%PYTHON% "%~dp0desktop_shell.pyw"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Desktop shell exited with code %errorlevel%
    pause
)