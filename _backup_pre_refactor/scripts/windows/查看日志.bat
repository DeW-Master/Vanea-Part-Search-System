@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

:: ============================================
::   F-Brain 零件查询系统 - 查看日志
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
echo %BOLD%%CYAN%           F-Brain 零件查询系统 - 日志查看%RESET%
echo %BOLD%%CYAN%========================================================================%RESET%
echo.

:: 检测运行模式
set "RUN_MODE="

where docker >nul 2>&1
if %errorlevel% equ 0 (
    if exist "docker-compose.yml" (
        docker-compose ps --format "{{.State}}" 2>nul | findstr /i "running" >nul
        if !errorlevel! equ 0 (
            set "RUN_MODE=docker"
        )
    )
)

if not defined RUN_MODE (
    tasklist /FI "IMAGENAME eq python.exe" /V 2>nul | findstr /i "app.py" >nul
    if !errorlevel! equ 0 (
        set "RUN_MODE=python"
    )
)

if not defined RUN_MODE (
    echo   %YELLOW%[INFO]%RESET% 未检测到运行中的服务
    echo.
    echo   请选择要查看的日志类型:
    echo.
    echo     %BOLD%1.%RESET% Docker 容器日志
    echo     %BOLD%2.%RESET% Python 运行日志
    echo     %BOLD%3.%RESET% 退出
    echo.
    set /p "CHOICE=请输入选项 (1/2/3): "

    if "!CHOICE!"=="1" set "RUN_MODE=docker"
    if "!CHOICE!"=="2" set "RUN_MODE=python"
    if "!CHOICE!"=="3" exit /b 0

    if not defined RUN_MODE (
        echo   %RED%[错误] 无效选项%RESET%
        pause >nul
        exit /b 1
    )
)

echo.

if "%RUN_MODE%"=="docker" (
    :: Docker 模式
    echo   运行模式: %GREEN%Docker 容器模式%RESET%
    echo.
    echo %BOLD%%YELLOW%正在显示 Docker 容器实时日志 (按 Ctrl+C 退出)%RESET%
    echo.
    echo %BOLD%========================================================================%RESET%
    echo.

    :: 检查 docker-compose 是否有 --tail 选项（兼容旧版本）
    docker-compose logs -f --tail=100 2>nul
    if !errorlevel! neq 0 (
        docker-compose logs -f
    )

) else (
    :: Python 模式
    echo   运行模式: %YELLOW%Python 直连模式%RESET%
    echo.

    if exist "logs" (
        :: 列出日志文件
        echo   %CYAN%可用的日志文件:%RESET%
        echo.

        set "COUNT=0"
        for %%F in ("logs\*.log") do (
            set /a COUNT+=1
            set "FILE_!COUNT!=%%F"
            set "SIZE_!COUNT!=%%~zF"
            echo     !COUNT!. %%~nxF  (!SIZE_!COUNT! 字节)
        )

        if !COUNT! equ 0 (
            echo   %YELLOW%[INFO]%RESET% logs 目录下没有日志文件
            echo.
            echo   日志目录: %CD%\logs
            echo.
            echo   请先启动系统，日志会自动生成。
            echo   按任意键退出...
            pause >nul
            exit /b 0
        )

        echo.
        echo     0. 退出
        echo.
        set /p "FILE_NUM=请选择要查看的日志文件编号 (默认最新): "

        if "!FILE_NUM!"=="" (
            :: 默认查看最新的日志文件
            set "LATEST="
            for /f "delims=" %%F in ('dir /b /o-d "logs\*.log" 2^>nul') do (
                set "LATEST=logs\%%F"
                goto :found_latest
            )
            :found_latest
            if defined LATEST (
                set "SELECTED_FILE=!LATEST!"
            ) else (
                echo   %RED%[错误] 没有找到日志文件%RESET%
                pause >nul
                exit /b 1
            )
        ) else if "!FILE_NUM!"=="0" (
            exit /b 0
        ) else (
            set "SELECTED_FILE=!FILE_%FILE_NUM%!"
            if not defined SELECTED_FILE (
                echo   %RED%[错误] 无效的文件编号%RESET%
                pause >nul
                exit /b 1
            )
        )

        echo.
        echo   正在查看: %CYAN%!SELECTED_FILE!%RESET%
        echo.
        echo %BOLD%========================================================================%RESET%
        echo.

        :: 使用 PowerShell 实现类似 tail -f 的功能
        powershell -Command "Get-Content '!SELECTED_FILE!' -Wait -Tail 100" 2>nul
        if !errorlevel! neq 0 (
            :: 如果 PowerShell 方式失败，使用 type 显示全部内容
            echo   %YELLOW%[INFO]%RESET% 实时查看不可用，显示文件最后 100 行:
            echo.
            powershell -Command "Get-Content '!SELECTED_FILE!' -Tail 100" 2>nul
            if !errorlevel! neq 0 (
                type "!SELECTED_FILE!"
            )
        )
    ) else (
        echo   %YELLOW%[INFO]%RESET% 未找到 logs 目录
        echo.
        echo   日志目录路径: %CD%\logs
        echo.
        echo   请先启动系统，日志会自动生成。
    )
)

echo.
echo   按任意键退出...
pause >nul
exit /b 0
