@echo off
chcp 65001 >nul 2>&1
title comate2api 服务

cd /d "%~dp0"

echo.
echo   正在启动 comate2api ...
echo.

REM 优先用 py 启动器（Windows 官方安装器装的 Python 自带）
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 -X utf8 start.py
    goto :end
)

REM 退而求其次用 python
where python >nul 2>&1
if %errorlevel%==0 (
    python -X utf8 start.py
    goto :end
)

echo.
echo   [错误] 没有找到 Python
echo.
echo   请先安装 Python 3.8 或更高版本：
echo     https://www.python.org/downloads/
echo.
echo   安装时务必勾选 "Add Python to PATH"
echo.
pause

:end
echo.
pause
