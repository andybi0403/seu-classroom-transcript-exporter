@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title SEU 课堂语音导出工具
set "APP=%~dp0seu_voice_exporter_app.py"
set "ARCH=%PROCESSOR_ARCHITECTURE%"
if defined PROCESSOR_ARCHITEW6432 set "ARCH=%PROCESSOR_ARCHITEW6432%"
set "RUNTIME=python"
if /i "%ARCH%"=="ARM64" set "RUNTIME=python-arm64"
if /i "%ARCH%"=="x86" set "RUNTIME=python-x86"
set "PY_ARGS="

if defined SEU_PYTHON (
    set "PY=%SEU_PYTHON%"
    call :check
    if not errorlevel 1 goto run
)
set "PY=%~dp0%RUNTIME%\python.exe"
call :check
if not errorlevel 1 goto run
set "PY=py.exe"
set "PY_ARGS=-3"
call :check
if not errorlevel 1 goto run
set "PY=python.exe"
set "PY_ARGS="
call :check
if not errorlevel 1 goto run

echo 找不到可用的 Python。请安装 Python 3.10 或更高版本。
pause
exit /b 1

:run
echo 使用期间请保留此窗口。
"%PY%" %PY_ARGS% -B -X utf8 "%APP%"
if not errorlevel 1 exit /b 0
pause
exit /b 1

:check
"%PY%" %PY_ARGS% -B -c "import ctypes, http.server, ssl, sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul
exit /b %errorlevel%
