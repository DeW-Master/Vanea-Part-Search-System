@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

:: ============================================
::   F-Brain 零件查询系统 - 停止服务
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
echo %BOLD%%CYAN%           F-Brain 零件查询系统 - 停止服务%RESET%
echo %BOLD%%CYAN%========================================================================%RESET%
echo.

set "STOPPED_ANYTHING=0"
set "DOCKER_STOPPED=0"
set "PYTHON_STOPPED=0"

:: ============================================
::  [1/2] 停止 Docker 容器
:: ============================================
echo %BOLD%%YELLOW%[1/2]%RESET% 检查 Docker 容器...
echo.

where docker >nul 2>&1
if %errorlevel% equ 0 (
    if exist "docker-compose.yml" (
        :: 检查是否有容器在运行
        docker-compose ps --format "{{.State}}" 2>nul | findstr /i "running" >nul
        if !errorlevel! equ 0 (
            echo   %CYAN%正在停止 Docker 容器...%RESET%
            docker-compose down
            if !errorlevel! equ 0 (
                echo   %GREEN%[OK]%RESET% Docker 容器已停止
                set "DOCKER_STOPPED=1"
                set "STOPPED_ANYTHING=1"
            ) else (
                echo   %RED%[FAIL]%RESET% Docker 容器停止失败
            )
        ) else (
            echo   %YELLOW%[INFO]%RESET% 没有运行中的 Docker 容器
        )
    ) else (
        echo   %YELLOW%[INFO]%RESET% 未找到 docker-compose.yml
    )
) else (
    echo   %YELLOW%[INFO]%RESET% 未检测到 Docker
)

echo.

:: ============================================
::  [2/2] 停止 Python 进程
:: ============================================
echo %BOLD%%YELLOW%[2/2]%RESET% 检查 Python 进程...
echo.

:: 查找运行 app.py 的 Python 进程
tasklist /FI "IMAGENAME eq python.exe" /V 2>nul | findstr /i "app.py" >nul
if %errorlevel% equ 0 (
    echo   %CYAN%正在停止 Python 服务进程...%RESET%

    :: 使用 WMIC 获取 PID 并终止 (更精确)
    for /f "tokens=2 delims=," %%P in ('wmic process where "commandline like '%%app.py%%' and name='python.exe'" get processid /format:csv 2^>nul ^| findstr /v "ProcessId"') do (
        set "PID=%%P"
        taskkill /F /PID !PID! >nul 2>&1
        if !errorlevel! equ 0 (
            echo   %GREEN%[OK]%RESET% 已终止进程 PID: !PID!
        )
    )

    :: 二次确认，使用 taskkill 终止所有匹配进程
    taskkill /F /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq Part-Search-System" >nul 2>&1

    :: 检查是否还有进程
    timeout /t 1 /nobreak >nul
    tasklist /FI "IMAGENAME eq python.exe" /V 2>nul | findstr /i "app.py" >nul
    if !errorlevel! neq 0 (
        echo   %GREEN%[OK]%RESET% Python 服务已停止
        set "PYTHON_STOPPED=1"
        set "STOPPED_ANYTHING=1"
    ) else (
        echo   %YELLOW%[WARN]%RESET% 部分进程可能仍在运行，请手动检查
    )
) else (
    echo   %YELLOW%[INFO]%RESET% 未检测到运行中的 Python 服务
)

echo.

:: ============================================
::  结果汇总
:: ============================================
echo %BOLD%%GREEN%========================================================================%RESET%
if %STOPPED_ANYTHING% equ 1 (
    echo %BOLD%%GREEN%                          服务已停止%RESET%
    echo %BOLD%%GREEN%========================================================================%RESET%
    echo.
    if %DOCKER_STOPPED% equ 1 (
        echo   Docker 容器: %GREEN%已停止%RESET%
    )
    if %PYTHON_STOPPED% equ 1 (
        echo   Python 进程: %GREEN%已停止%RESET%
    )
) else (
    echo %BOLD%%YELLOW%                          没有运行中的服务%RESET%
    echo %BOLD%%YELLOW%========================================================================%RESET%
    echo.
    echo   系统当前没有运行中的服务实例。
    echo   如需启动，请运行 "启动系统.bat"
)
echo.
echo   按任意键退出...
echo.
pause >nul
exit /b 0
