@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions DisableDelayedExpansion
title comate2api 服务

set "APPDIR=%~dp0app"
cd /d "%APPDIR%" || exit /b 1
set "PIDFILE=%APPDIR%\server.pid"
set "LOGFILE=%APPDIR%\server.log"
set "INFOFILE=%APPDIR%\connection.json"
if defined COMATE_DATA_DIR set "INFOFILE=%COMATE_DATA_DIR%\connection.json"

if /i "%~1"=="stop" goto stop
if /i "%~1"=="restart" goto stop
if /i "%~1"=="status" goto status
if /i "%~1"=="start" goto background
if not "%~1"=="" goto usage

echo.
echo   正在启动 comate2api ...
echo.

call :find_python
if errorlevel 1 goto missing_python
%PYTHON_CMD% -B -X utf8 "%APPDIR%\start.py"
set "RESULT=%errorlevel%"
echo.
pause
exit /b %RESULT%

:find_python
where py >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
    exit /b 0
)
where python >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    exit /b 0
)
exit /b 1

:missing_python
echo.
echo   [错误] 没有找到 Python
echo   请先安装 Python 3.8 或更高版本：
echo     https://www.python.org/downloads/
echo   安装时务必勾选 "Add Python to PATH"
echo.
if "%~1"=="" pause
exit /b 1

:running
set "PID="
if not exist "%PIDFILE%" exit /b 1
set /p PID=<"%PIDFILE%"
if not defined PID exit /b 1
tasklist /FI "PID eq %PID%" /FO CSV /NH 2>nul | find "%PID%" >nul
exit /b %errorlevel%

:background
call :running
if not errorlevel 1 goto showinfo
if exist "%PIDFILE%" del "%PIDFILE%" >nul 2>&1
call :find_python
if errorlevel 1 goto missing_python
echo   正在后台启动 comate2api ...
%PYTHON_CMD% -B -X utf8 -c "import subprocess,sys; log=open('server.log','a',encoding='utf-8'); subprocess.Popen([sys.executable,'-B','-X','utf8','-u','start.py'],stdin=subprocess.DEVNULL,stdout=log,stderr=log,close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP)"
if errorlevel 1 exit /b 1
set /a TRIES=0

:waitloop
set /a TRIES+=1 >nul
if %TRIES% GTR 30 (
    echo   [错误] 启动超时，请查看日志: %LOGFILE%
    exit /b 1
)
%PYTHON_CMD% -c "import time; time.sleep(1)"
call :running
if errorlevel 1 goto waitloop
if not exist "%INFOFILE%" goto waitloop

:showinfo
echo.
echo   服务已启动 ^(PID %PID%^)
if exist "%INFOFILE%" type "%INFOFILE%"
echo.
echo   日志: %LOGFILE%
echo   停止: "%~f0" stop
exit /b 0

:stop
call :running
if errorlevel 1 (
    echo   [未运行]
    if exist "%PIDFILE%" del "%PIDFILE%" >nul 2>&1
    goto donestop
)
taskkill /PID %PID% /F >nul 2>&1
if errorlevel 1 (
    echo   [错误] 停止失败，可能权限不足
    exit /b 1
)
if exist "%PIDFILE%" del "%PIDFILE%" >nul 2>&1
echo   已停止 PID %PID%

:donestop
if /i "%~1"=="restart" goto background
exit /b 0

:status
call :running
if errorlevel 1 (
    echo   [未运行]
) else (
    echo   [运行中] PID %PID%
    if exist "%INFOFILE%" type "%INFOFILE%"
)
exit /b 0

:usage
echo   双击启动服务；命令行用法: "%~nx0" [start^|stop^|restart^|status]
exit /b 1
