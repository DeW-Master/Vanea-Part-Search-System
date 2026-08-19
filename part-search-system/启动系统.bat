@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

:: ============================================
::   F-Brain 零件查询系统 - 一键启动
::   版本: v1.3.0
:: ============================================

:: 切换到脚本所在目录
cd /d "%~dp0"

:: 颜色定义 (使用 ANSI 转义序列, Windows 10+ 支持)
for /F "tokens=1,2 delims=#" %%a in ('"prompt #$H#$E# & echo on & for %%b in (1) do rem"') do (
  set "ESC=%%b"
)
set "RED=%ESC%[91m"
set "GREEN=%ESC%[92m"
set "YELLOW=%ESC%[93m"
set "BLUE=%ESC%[94m"
set "CYAN=%ESC%[96m"
set "WHITE=%ESC%[97m"
set "BOLD=%ESC%[1m"
set "RESET=%ESC%[0m"

:: 清屏
cls

:: 打印标题
echo.
echo %BOLD%%CYAN%========================================================================%RESET%
echo %BOLD%%CYAN%           F-Brain 零件查询系统 v1.3.0 - 启动程序%RESET%
echo %BOLD%%CYAN%           Part Search System - Launcher%RESET%
echo %BOLD%%CYAN%========================================================================%RESET%
echo.

:: 记录运行模式
set "RUN_MODE="
set "DOCKER_AVAILABLE=0"
set "PYTHON_AVAILABLE=0"
set "PYTHON_CMD=python"

:: ============================================
::  [1/5] 检测运行环境
:: ============================================
echo %BOLD%%YELLOW%[1/5]%RESET% 正在检测运行环境...
echo.

:: 检测 Docker 是否安装
where docker >nul 2>&1
if %errorlevel% equ 0 (
    :: 检测 Docker 服务是否在运行
    docker info >nul 2>&1
    if %errorlevel% equ 0 (
        set "DOCKER_AVAILABLE=1"
        echo   %GREEN%[OK]%RESET% Docker 已安装且正在运行
    ) else (
        echo   %YELLOW%[WARN]%RESET% Docker 已安装但未启动
    )
) else (
    echo   %YELLOW%[INFO]%RESET% 未检测到 Docker
)

:: 检测 Python 是否可用
where python >nul 2>&1
if %errorlevel% equ 0 (
    set "PYTHON_AVAILABLE=1"
    set "PYTHON_CMD=python"
    echo   %GREEN%[OK]%RESET% Python 已安装 (python)
) else (
    :: 尝试常见安装路径
    if exist "%LOCALAPPDATA%\Programs\Python\Python39\python.exe" (
        set "PYTHON_AVAILABLE=1"
        set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
        echo   %GREEN%[OK]%RESET% Python 已安装 (Python39)
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" (
        set "PYTHON_AVAILABLE=1"
        set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        echo   %GREEN%[OK]%RESET% Python 已安装 (Python310)
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
        set "PYTHON_AVAILABLE=1"
        set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        echo   %GREEN%[OK]%RESET% Python 已安装 (Python311)
    ) else (
        echo   %RED%[FAIL]%RESET% 未检测到 Python
    )
)

echo.

:: ============================================
::  [2/5] 确定运行模式
:: ============================================
echo %BOLD%%YELLOW%[2/5]%RESET% 确定运行模式...
echo.

if %DOCKER_AVAILABLE% equ 1 (
    if exist "docker-compose.yml" (
        set "RUN_MODE=docker"
        echo   运行模式: %GREEN%Docker 容器模式 (生产环境推荐)%RESET%
    ) else (
        echo   %YELLOW%[WARN]%RESET% 未找到 docker-compose.yml，切换到 Python 模式
        set "RUN_MODE=python"
    )
) else if %PYTHON_AVAILABLE% equ 1 (
    set "RUN_MODE=python"
    echo   运行模式: %YELLOW%Python 直连模式 (开发模式)%RESET%
) else (
    echo.
    echo   %RED%[错误] 无法启动系统！%RESET%
    echo.
    echo   未检测到 Docker 和 Python，请先安装其中之一：
    echo.
    echo   1. Docker Desktop (推荐): 运行 scripts\deploy\install-docker.ps1
    echo   2. Python 3.9+: 从 https://www.python.org/downloads/ 下载安装
    echo.
    echo %BOLD%%RED%========================================================================%RESET%
    echo %BOLD%%RED%                        启动失败，按任意键退出%RESET%
    echo %BOLD%%RED%========================================================================%RESET%
    pause >nul
    exit /b 1
)

echo.

:: ============================================
::  [3/5] 启动服务
:: ============================================
echo %BOLD%%YELLOW%[3/5]%RESET% 正在启动服务...
echo.

if "%RUN_MODE%"=="docker" (
    :: Docker 模式启动
    echo   %CYAN%正在使用 docker-compose 启动容器...%RESET%
    echo.

    :: 检查是否已有容器在运行
    docker-compose ps --format "{{.State}}" 2>nul | findstr /i "running" >nul
    if %errorlevel% equ 0 (
        echo   %YELLOW%[INFO]%RESET% 检测到容器已在运行，正在重启...
        docker-compose restart
        if %errorlevel% neq 0 (
            echo   %RED%[错误] 容器重启失败！%RESET%
            goto :start_failed
        )
    ) else (
        :: 首次启动，构建并启动
        docker-compose up -d --build
        if %errorlevel% neq 0 (
            echo   %RED%[错误] Docker 容器启动失败！%RESET%
            goto :start_failed
        )
    )

    :: 等待服务就绪
    echo.
    echo   %CYAN%等待服务启动就绪...%RESET%
    set "WAIT_COUNT=0"
    :docker_wait_loop
    timeout /t 2 /nobreak >nul
    set /a WAIT_COUNT+=1

    :: 检查 Nginx 容器是否在运行（入口服务）
    docker ps --format "{{.Names}}" 2>nul | findstr /i "nginx" >nul
    if %errorlevel% equ 0 (
        :: 尝试访问 Nginx 80 端口
        curl -s -o nul -w "%%{http_code}" http://localhost/ 2>nul | findstr "200 302" >nul
        if !errorlevel! equ 0 (
            echo   %GREEN%[OK]%RESET% 服务已就绪 (等待 !WAIT_COUNT! 次)
            goto :start_success
        )
    )

    if !WAIT_COUNT! geq 45 (
        echo   %YELLOW%[WARN]%RESET% 等待超时，服务可能尚未完全启动
        goto :start_success
    )
    goto :docker_wait_loop

) else (
    :: Python 模式启动
    echo   %CYAN%正在使用 Python 直接启动...%RESET%
    echo.

    :: 检查依赖
    echo   %CYAN%检查 Python 依赖包...%RESET%
    "!PYTHON_CMD!" -m pip install flask flask-cors openpyxl --quiet 2>nul
    if %errorlevel% neq 0 (
        echo   %YELLOW%[WARN]%RESET% 依赖安装可能不完整，尝试继续启动...
    )

    :: 检查 app.py 是否存在
    if not exist "backend\app.py" (
        echo   %RED%[错误] 未找到 backend\app.py 文件！%RESET%
        goto :start_failed
    )

    :: 创建日志目录
    if not exist "logs" mkdir logs

    :: 后台启动 Python 服务
    set "LOG_FILE=logs\app_%date:~0,4%%date:~5,2%%date:~8,2%.log"
    echo   %CYAN%启动 Flask 服务，日志文件: !LOG_FILE!%RESET%

    :: 使用 start /B 后台运行
    start "Part-Search-System" /B cmd /c ""!PYTHON_CMD!" backend\app.py >> "!LOG_FILE!" 2>&1"

    :: 等待服务就绪
    echo.
    echo   %CYAN%等待服务启动就绪...%RESET%
    set "WAIT_COUNT=0"
    :python_wait_loop
    timeout /t 2 /nobreak >nul
    set /a WAIT_COUNT+=1

    :: 尝试访问服务端口
    curl -s -o nul -w "%%{http_code}" http://localhost:5000/ 2>nul | findstr "200 302" >nul
    if !errorlevel! equ 0 (
        echo   %GREEN%[OK]%RESET% 服务已就绪 (等待 !WAIT_COUNT! 次)
        goto :start_success
    )

    if !WAIT_COUNT! geq 30 (
        echo   %YELLOW%[WARN]%RESET% 等待超时，请检查日志文件: !LOG_FILE!
        goto :start_success
    )
    goto :python_wait_loop
)

:start_success
echo.

:: ============================================
::  [4/5] 打开浏览器
:: ============================================
echo %BOLD%%YELLOW%[4/5]%RESET% 正在打开浏览器...
echo.

if "%RUN_MODE%"=="docker" (
    start "" "http://localhost"
) else (
    start "" "http://localhost:5000"
)
timeout /t 1 /nobreak >nul

:: ============================================
::  [5/5] 显示访问信息
:: ============================================
echo %BOLD%%YELLOW%[5/5]%RESET% 系统启动完成！
echo.
echo %BOLD%%GREEN%========================================================================%RESET%
echo %BOLD%%GREEN%                          系统启动成功！%RESET%
echo %BOLD%%GREEN%========================================================================%RESET%
echo.

if "%RUN_MODE%"=="docker" (
echo   %BOLD%查询页面:%RESET%     %CYAN%http://localhost%RESET%
echo   %BOLD%管理后台:%RESET%     %CYAN%http://localhost/admin%RESET%
echo   %BOLD%监控页面:%RESET%     %CYAN%http://localhost/monitoring%RESET%
) else (
echo   %BOLD%查询页面:%RESET%     %CYAN%http://localhost:5000%RESET%
echo   %BOLD%管理后台:%RESET%     %CYAN%http://localhost:5000/admin%RESET%
echo   %BOLD%监控页面:%RESET%     %CYAN%http://localhost:5000/monitoring%RESET%
)
echo   %BOLD%默认密码:%RESET%     %YELLOW%admin2026%RESET%
echo.
echo   %BOLD%运行模式:%RESET%     %CYAN%!RUN_MODE!%RESET%
echo   %BOLD%项目目录:%RESET%     %CD%
echo.
if "%RUN_MODE%"=="docker" (
echo   %BOLD%查看日志:%RESET%     scripts\windows\查看日志.bat
echo   %BOLD%停止服务:%RESET%     scripts\windows\停止系统.bat
) else (
echo   %BOLD%日志文件:%RESET%     logs\app_*.log
echo   %BOLD%停止服务:%RESET%     scripts\windows\停止系统.bat
)
echo.
echo %BOLD%%GREEN%========================================================================%RESET%
echo.
echo   提示: 关闭此窗口 %YELLOW%不会%RESET% 停止服务，服务将在后台继续运行
echo   如需停止服务，请运行 "停止系统.bat"
echo.
echo   按任意键关闭此窗口...
echo.
pause >nul
exit /b 0

:start_failed
echo.
echo %BOLD%%RED%========================================================================%RESET%
echo %BOLD%%RED%                        启动失败！%RESET%
echo %BOLD%%RED%========================================================================%RESET%
echo.
echo   请检查以下内容：
echo   1. 端口 5000 是否被占用
echo   2. 网络连接是否正常
echo   3. 查看日志文件获取详细错误信息
echo.
if "%RUN_MODE%"=="docker" (
echo   运行 "查看日志.bat" 查看 Docker 容器日志
) else (
echo   查看 logs\ 目录下的日志文件
)
echo.
echo   按任意键退出...
echo.
pause >nul
exit /b 1
