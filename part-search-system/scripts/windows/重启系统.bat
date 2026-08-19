@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

:: ============================================
::   F-Brain 零件查询系统 - 重启服务
::   版本: v1.3.0
:: ============================================

:: 切换到项目根目录（脚本在 scripts\windows\，往上两级）
cd /d "%~dp0..\.."

:: 颜色定义
for /F "tokens=1,2 delims=#" %%a in ('"prompt #$H#$E# & echo on & for %%b in (1) do rem"') do (
  set "ESC=%%b"
)
set "RED=%ESC%[91m"
set "GREEN=%ESC%[92m"
set "YELLOW=%ESC%[93m"
set "CYAN=%ESC%[96m"
set "BOLD=%ESC%[1m"
set "RESET=%ESC%[0m"

cls

echo.
echo %BOLD%%CYAN%========================================================================%RESET%
echo %BOLD%%CYAN%           F-Brain 零件查询系统 - 重启服务%RESET%
echo %BOLD%%CYAN%========================================================================%RESET%
echo.

:: ============================================
::  [1/3] 检测运行模式
:: ============================================
echo %BOLD%%YELLOW%[1/3]%RESET% 检测运行模式...
echo.

set "RUN_MODE=python"
set "DOCKER_RUNNING=0"

where docker >nul 2>&1
if %errorlevel% equ 0 (
    if exist "docker-compose.yml" (
        docker-compose ps --format "{{.State}}" 2>nul | findstr /i "running" >nul
        if !errorlevel! equ 0 (
            set "DOCKER_RUNNING=1"
            set "RUN_MODE=docker"
            echo   检测到运行模式: %GREEN%Docker 容器模式%RESET%
        )
    )
)

if %DOCKER_RUNNING% equ 0 (
    :: 检查 Python 进程
    tasklist /FI "IMAGENAME eq python.exe" /V 2>nul | findstr /i "app.py" >nul
    if !errorlevel! equ 0 (
        set "RUN_MODE=python"
        echo   检测到运行模式: %YELLOW%Python 直连模式%RESET%
    ) else (
        echo   %YELLOW%[INFO]%RESET% 未检测到运行中的服务，将执行启动操作
        set "RUN_MODE="
    )
)

echo.

:: ============================================
::  [2/3] 停止服务
:: ============================================
echo %BOLD%%YELLOW%[2/3]%RESET% 正在停止服务...
echo.

if "%RUN_MODE%"=="docker" (
    echo   %CYAN%重启 Docker 容器...%RESET%
    docker-compose restart
    if !errorlevel! equ 0 (
        echo   %GREEN%[OK]%RESET% Docker 容器已重启
    ) else (
        echo   %RED%[FAIL]%RESET% Docker 容器重启失败
        echo   %YELLOW%尝试 down + up...%RESET%
        docker-compose down
        docker-compose up -d
    )
) else if "%RUN_MODE%"=="python" (
    echo   %CYAN%停止 Python 服务进程...%RESET%

    :: 终止所有运行 app.py 的 Python 进程
    for /f "tokens=2 delims=," %%P in ('wmic process where "commandline like '%%app.py%%' and name='python.exe'" get processid /format:csv 2^>nul ^| findstr /v "ProcessId"') do (
        taskkill /F /PID %%P >nul 2>&1
    )
    taskkill /F /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq Part-Search-System" >nul 2>&1

    timeout /t 2 /nobreak >nul
    echo   %GREEN%[OK]%RESET% Python 服务已停止
    echo.

    :: 重新启动 Python 服务
    echo   %CYAN%启动 Python 服务...%RESET%

    :: 检测 Python 路径
    set "PYTHON_CMD=python"
    where python >nul 2>&1
    if !errorlevel! neq 0 (
        if exist "%LOCALAPPDATA%\Programs\Python\Python39\python.exe" (
            set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
        ) else if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" (
            set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        ) else if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
            set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        )
    )

    if not exist "logs" mkdir logs
    set "LOG_FILE=logs\app_%date:~0,4%%date:~5,2%%date:~8,2%.log"

    start "Part-Search-System" /B cmd /c ""!PYTHON_CMD!" backend\app.py >> "!LOG_FILE!" 2>&1"
    echo   %GREEN%[OK]%RESET% Python 服务已启动
) else (
    echo   %YELLOW%[INFO]%RESET% 没有运行中的服务，直接启动...
    call "%CD%\启动系统.bat"
    exit /b 0
)

echo.

:: ============================================
::  [3/3] 等待服务就绪
:: ============================================
echo %BOLD%%YELLOW%[3/3]%RESET% 等待服务就绪...
echo.

set "WAIT_COUNT=0"
set "MAX_WAIT=30"

:wait_loop
timeout /t 2 /nobreak >nul
set /a WAIT_COUNT+=1

curl -s -o nul -w "%%{http_code}" http://localhost:5000/ 2>nul | findstr "200 302" >nul
if !errorlevel! equ 0 (
    echo   %GREEN%[OK]%RESET% 服务已就绪 (等待 !WAIT_COUNT! 次)
    goto :restart_success
)

if !WAIT_COUNT! geq %MAX_WAIT% (
    echo   %YELLOW%[WARN]%RESET% 等待超时，服务可能尚未完全启动
    goto :restart_success
)
goto :wait_loop

:restart_success
echo.
echo %BOLD%%GREEN%========================================================================%RESET%
echo %BOLD%%GREEN%                          系统重启完成！%RESET%
echo %BOLD%%GREEN%========================================================================%RESET%
echo.
echo   %BOLD%查询页面:%RESET%     %CYAN%http://localhost:5000%RESET%
echo   %BOLD%管理后台:%RESET%     %CYAN%http://localhost:5000/admin%RESET%
echo   %BOLD%运行模式:%RESET%     %CYAN%!RUN_MODE!%RESET%
echo.
echo   按任意键退出...
echo.
pause >nul
exit /b 0
