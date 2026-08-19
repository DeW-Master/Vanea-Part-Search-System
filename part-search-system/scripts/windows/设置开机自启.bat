@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

:: ============================================
::   F-Brain 零件查询系统 - 设置开机自启
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
echo %BOLD%%CYAN%           F-Brain 零件查询系统 - 开机自启设置%RESET%
echo %BOLD%%CYAN%========================================================================%RESET%
echo.

:: 任务名称
set "TASK_NAME=F-Brain零件查询系统"
set "START_BAT=%CD%\启动系统.bat"

:: ============================================
::  检查管理员权限
:: ============================================
echo %BOLD%%YELLOW%[检查]%RESET% 管理员权限...
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo   %RED%[错误] 当前未以管理员身份运行%RESET%
    echo.
    echo   设置开机自启需要管理员权限。
    echo   请右键点击此脚本，选择 %YELLOW%"以管理员身份运行"%RESET%
    echo.
    echo   是否自动请求提升权限？(Y/N)
    set /p "ELEVATE=请选择: "
    if /i "!ELEVATE!"=="Y" (
        :: 使用 PowerShell 提升权限重新运行
        powershell -Command "Start-Process cmd -ArgumentList '/c ""%~f0""' -Verb RunAs"
        exit /b 0
    )
    echo.
    echo   按任意键退出...
    pause >nul
    exit /b 1
)

echo   %GREEN%[OK]%RESET% 已获得管理员权限
echo.

:: ============================================
::  显示菜单
:: ============================================
echo %BOLD%%YELLOW%[菜单]%RESET% 请选择操作:
echo.
echo     %BOLD%1.%RESET% 设置开机自启（启用）
echo     %BOLD%2.%RESET% 取消开机自启（禁用）
echo     %BOLD%3.%RESET% 查看当前状态
echo     %BOLD%0.%RESET% 退出
echo.
set /p "CHOICE=请输入选项 (0-3): "

if "!CHOICE!"=="1" goto :enable_autostart
if "!CHOICE!"=="2" goto :disable_autostart
if "!CHOICE!"=="3" goto :check_status
if "!CHOICE!"=="0" exit /b 0

echo   %RED%[错误] 无效选项%RESET%
timeout /t 2 >nul
exit /b 1

:: ============================================
::  设置开机自启
:: ============================================
:enable_autostart
echo.
echo %BOLD%%YELLOW%[1/2]%RESET% 正在创建计划任务...
echo.

:: 检查启动脚本是否存在
if not exist "%START_BAT%" (
    echo   %RED%[错误] 未找到启动脚本: %START_BAT%%RESET%
    echo   请确保 "启动系统.bat" 与此脚本在同一目录。
    echo.
    echo   按任意键退出...
    pause >nul
    exit /b 1
)

:: 删除已有的同名任务（如果存在）
schtasks /Query /TN "%TASK_NAME%" >nul 2>&1
if %errorlevel% equ 0 (
    echo   %YELLOW%[INFO]%RESET% 检测到已有计划任务，先删除旧任务...
    schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
)

:: 创建计划任务 - 开机启动
:: 使用 /RL HIGHEST 以最高权限运行
:: 使用 /SC ONLOGON 在用户登录时启动（更可靠）
schtasks /Create /TN "%TASK_NAME%" /TR "\"%START_BAT%\"" /SC ONLOGON /RL HIGHEST /F >nul 2>&1

if %errorlevel% equ 0 (
    echo   %GREEN%[OK]%RESET% 计划任务创建成功
) else (
    echo   %RED%[FAIL]%RESET% 计划任务创建失败
    echo.
    echo   尝试备用方案（启动文件夹快捷方式）...
    goto :enable_startup_folder
)

echo.
echo %BOLD%%YELLOW%[2/2]%RESET% 验证任务...
echo.

schtasks /Query /TN "%TASK_NAME%" /FO LIST 2>nul | findstr /i "TaskName Status" >nul
if %errorlevel% equ 0 (
    echo   %GREEN%[OK]%RESET% 任务已创建并启用
    echo.
    echo   任务名称: %TASK_NAME%
    echo   触发条件: 用户登录时
    echo   运行程序: %START_BAT%
    echo   运行权限: 最高权限
) else (
    echo   %YELLOW%[WARN]%RESET% 任务创建但验证失败
)

echo.
echo %BOLD%%GREEN%========================================================================%RESET%
echo %BOLD%%GREEN%                        开机自启已设置成功！%RESET%
echo %BOLD%%GREEN%========================================================================%RESET%
echo.
echo   系统将在每次用户登录时自动启动。
echo   如需取消，请重新运行此脚本并选择 "取消开机自启"。
echo.
echo   按任意键退出...
pause >nul
exit /b 0

:: ============================================
::  备用方案：启动文件夹快捷方式
:: ============================================
:enable_startup_folder

set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT_PATH=%STARTUP_DIR%\%TASK_NAME%.lnk"

powershell -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT_PATH%'); $s.TargetPath = '%START_BAT%'; $s.WorkingDirectory = '%CD%'; $s.Save()" 2>&1

if %errorlevel% equ 0 (
    echo   %GREEN%[OK]%RESET% 启动文件夹快捷方式创建成功
    echo.
    echo   快捷方式: %SHORTCUT_PATH%
    echo   目标程序: %START_BAT%
) else (
    echo   %RED%[FAIL]%RESET% 启动文件夹快捷方式创建失败
    echo.
    echo   按任意键退出...
    pause >nul
    exit /b 1
)

echo.
echo %BOLD%%GREEN%========================================================================%RESET%
echo %BOLD%%GREEN%                        开机自启已设置成功！%RESET%
echo %BOLD%%GREEN%========================================================================%RESET%
echo.
echo   系统将在每次用户登录时自动启动。
echo   如需取消，请重新运行此脚本并选择 "取消开机自启"。
echo.
echo   按任意键退出...
pause >nul
exit /b 0

:: ============================================
::  取消开机自启
:: ============================================
:disable_autostart
echo.
echo %BOLD%%YELLOW%[1/2]%RESET% 正在删除计划任务...
echo.

set "REMOVED=0"

:: 删除计划任务
schtasks /Query /TN "%TASK_NAME%" >nul 2>&1
if %errorlevel% equ 0 (
    schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
    if %errorlevel% equ 0 (
        echo   %GREEN%[OK]%RESET% 计划任务已删除
        set "REMOVED=1"
    ) else (
        echo   %RED%[FAIL]%RESET% 计划任务删除失败
    )
) else (
    echo   %YELLOW%[INFO]%RESET% 未找到计划任务
)

:: 删除启动文件夹快捷方式
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT_PATH=%STARTUP_DIR%\%TASK_NAME%.lnk"

if exist "%SHORTCUT_PATH%" (
    del "%SHORTCUT_PATH%"
    if %errorlevel% equ 0 (
        echo   %GREEN%[OK]%RESET% 启动文件夹快捷方式已删除
        set "REMOVED=1"
    )
)

if %REMOVED% equ 0 (
    echo.
    echo   %YELLOW%[INFO]%RESET% 未检测到开机自启配置
)

echo.
echo %BOLD%%GREEN%========================================================================%RESET%
echo %BOLD%%GREEN%                        开机自启已取消%RESET%
echo %BOLD%%GREEN%========================================================================%RESET%
echo.
echo   系统将不再自动启动。
echo   如需重新设置，请运行此脚本并选择 "设置开机自启"。
echo.
echo   按任意键退出...
pause >nul
exit /b 0

:: ============================================
::  查看当前状态
:: ============================================
:check_status
echo.
echo %BOLD%%YELLOW%[状态]%RESET% 当前开机自启状态:
echo.

set "ENABLED=0"

:: 检查计划任务
schtasks /Query /TN "%TASK_NAME%" /FO LIST 2>nul | findstr /i "TaskName" >nul
if %errorlevel% equ 0 (
    echo   %GREEN%[已启用]%RESET% 计划任务方式
    echo     任务名称: %TASK_NAME%
    echo     触发条件: 用户登录时
    set "ENABLED=1"
) else (
    echo   %YELLOW%[未启用]%RESET% 计划任务方式
)

:: 检查启动文件夹
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT_PATH=%STARTUP_DIR%\%TASK_NAME%.lnk"

if exist "%SHORTCUT_PATH%" (
    echo   %GREEN%[已启用]%RESET% 启动文件夹方式
    echo     快捷方式: %SHORTCUT_PATH%
    set "ENABLED=1"
) else (
    echo   %YELLOW%[未启用]%RESET% 启动文件夹方式
)

echo.
if %ENABLED% equ 1 (
    echo   总结: %GREEN%开机自启已启用%RESET%
) else (
    echo   总结: %YELLOW%开机自启未启用%RESET%
)

echo.
echo   按任意键返回菜单...
pause >nul
cls
goto :eof
