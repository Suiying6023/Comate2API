@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "PIDFILE=%~dp0server.pid"
set "LOGFILE=%~dp0server.log"

echo.
echo ============================================================
echo   comate2api 服务控制
echo ============================================================
echo.

if "%~1"=="stop" goto stop
if "%~1"=="status" goto status
if "%~1"=="restart" goto stop
if "%~1"=="start" goto start
goto stop


:start
echo  正在检查服务状态...

if exist "%PIDFILE%" (
    set /p OLDPID=<"%PIDFILE%"
    tasklist /FI "PID eq !OLDPID!" 2>nul | find "!OLDPID!" >nul
    if !errorlevel!==0 (
        echo.
        echo  服务已在运行 ^(PID !OLDPID!^)
        echo.
        goto showinfo
    )
    echo  发现残留的 PID 文件，清理...
    del "%PIDFILE%" >nul 2>&1
)

echo  正在启动服务...
echo.

REM 用 pythonw 启动，不弹控制台窗口；日志写到文件
where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" /B pythonw -X utf8 "%~dp0start.py" >> "%LOGFILE%" 2>&1
    goto started
)

where py >nul 2>&1
if %errorlevel%==0 (
    start "" /B py -3 -X utf8 "%~dp0start.py" >> "%LOGFILE%" 2>&1
    goto started
)

where python >nul 2>&1
if %errorlevel%==0 (
    start "" /B python -X utf8 "%~dp0start.py" >> "%LOGFILE%" 2>&1
    goto started
)

echo.
echo   [错误] 没找到 Python
echo.
echo   请先安装 Python 3.8+：
echo     https://www.python.org/downloads/
echo   安装时勾选 "Add Python to PATH"
echo.
pause
exit /b 1


:started
REM 等端口起来
set /a tries=0
:waitloop
set /a tries+=1
if !tries! GTR 30 (
    echo  启动超时，请查看日志: %LOGFILE%
    pause
    exit /b 1
)
timeout /t 1 /nobreak >nul
if not exist "%PIDFILE%" goto waitloop
set /p PID=<"%PIDFILE%"
tasklist /FI "PID eq !PID!" 2>nul | find "!PID!" >nul
if !errorlevel! neq 0 goto waitloop
goto showinfo

:showinfo
echo.
echo ============================================================
echo   服务已启动
echo ============================================================
echo.

if exist "%~dp0connection.json" (
    type "%~dp0connection.json"
    echo.
)

echo   日志文件: %LOGFILE%
echo   停止服务: 停止服务.bat
echo.
pause
exit /b 0


:stop
echo  正在停止服务...
echo.

if not exist "%PIDFILE%" (
    echo  没有 PID 文件，未找到记录的服务
    goto donestop
)

set /p PID=<"%PIDFILE%"
tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul
if !errorlevel! neq 0 (
    echo  PID %PID% 未在运行，清理 PID 文件
    del "%PIDFILE%" >nul 2>&1
    goto donestop
)

taskkill /PID %PID% /F >nul 2>&1
if !errorlevel!==0 (
    echo  已停止 PID %PID%
    del "%PIDFILE%" >nul 2>&1
) else (
    echo  停止失败，可能权限不足
)

:donestop
echo.
if "%~1"=="restart" goto start
pause
exit /b 0


:status
echo.
echo ============================================================
echo   服务状态
echo ============================================================
echo.

set "RUNNING="
if exist "%PIDFILE%" (
    set /p PID=<"%PIDFILE%"
    tasklist /FI "PID eq !PID!" 2>nul | find "!PID!" >nul
    if !errorlevel!==0 (
        set "RUNNING=1"
        echo  [运行中] PID !PID!
    ) else (
        echo  [未运行] PID 文件是残留的
    )
) else (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8181" ^| findstr "LISTENING"') do (
        set "RUNNING=1"
        echo  [运行中] PID %%a ^(未记录 PID 文件^)
    )
)

if not defined RUNNING echo  [未运行]

echo.
if exist "%~dp0connection.json" (
    echo   连接信息:
    type "%~dp0connection.json"
    echo.
)

echo   日志: %LOGFILE%
echo.
pause
exit /b 0
