@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

:: ============================================
::   F-Brain 零件查询系统 - 创建桌面快捷方式
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
echo %BOLD%%CYAN%           F-Brain 零件查询系统 - 创建桌面快捷方式%RESET%
echo %BOLD%%CYAN%========================================================================%RESET%
echo.

set "SHORTCUT_NAME=F-Brain零件查询系统"
set "START_BAT=%CD%\启动系统.bat"
set "WORK_DIR=%CD%"
set "DESKTOP_DIR=%PUBLIC%\Desktop"

:: ============================================
::  [1/3] 检查启动脚本
:: ============================================
echo %BOLD%%YELLOW%[1/3]%RESET% 检查启动脚本...
echo.

if not exist "%START_BAT%" (
    echo   %RED%[错误] 未找到启动脚本: %START_BAT%%RESET%
    echo   请确保 "启动系统.bat" 与此脚本在同一目录。
    echo.
    echo   按任意键退出...
    pause >nul
    exit /b 1
)

echo   %GREEN%[OK]%RESET% 启动脚本存在: %START_BAT%
echo.

:: ============================================
::  [2/3] 创建快捷方式
:: ============================================
echo %BOLD%%YELLOW%[2/3]%RESET% 创建桌面快捷方式...
echo.

set "SHORTCUT_CREATED=0"

:: 尝试在用户桌面创建
if exist "%USERPROFILE%\Desktop" (
    set "DESKTOP_DIR=%USERPROFILE%\Desktop"
) else if exist "%USERPROFILE%\OneDrive\Desktop" (
    set "DESKTOP_DIR=%USERPROFILE%\OneDrive\Desktop"
)

set "SHORTCUT_PATH=%DESKTOP_DIR%\%SHORTCUT_NAME%.lnk"

echo   桌面路径: %DESKTOP_DIR%
echo   快捷方式: %SHORTCUT_PATH%
echo.

:: 使用 PowerShell 创建快捷方式
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT_PATH%'); $s.TargetPath = '%START_BAT%'; $s.WorkingDirectory = '%WORK_DIR%'; $s.Description = 'F-Brain 零件查询系统 - 一键启动'; $s.IconLocation = 'shell32.dll,13'; $s.Save()" 2>&1

if %errorlevel% equ 0 (
    if exist "%SHORTCUT_PATH%" (
        echo   %GREEN%[OK]%RESET% 桌面快捷方式创建成功
        set "SHORTCUT_CREATED=1"
    ) else (
        echo   %YELLOW%[WARN]%RESET% PowerShell 未报错但文件不存在，尝试备用方案...
    )
) else (
    echo   %YELLOW%[WARN]%RESET% PowerShell 创建失败，尝试备用方案...
)

:: 备用方案：使用 VBS 创建快捷方式
if %SHORTCUT_CREATED% equ 0 (
    set "VBS_FILE=%TEMP%\create_shortcut.vbs"
    echo Set WshShell = WScript.CreateObject("WScript.Shell") > "!VBS_FILE!"
    echo Set shortcut = WshShell.CreateShortcut("%SHORTCUT_PATH%") >> "!VBS_FILE!"
    echo shortcut.TargetPath = "%START_BAT%" >> "!VBS_FILE!"
    echo shortcut.WorkingDirectory = "%WORK_DIR%" >> "!VBS_FILE!"
    echo shortcut.Description = "F-Brain 零件查询系统 - 一键启动" >> "!VBS_FILE!"
    echo shortcut.IconLocation = "shell32.dll,13" >> "!VBS_FILE!"
    echo shortcut.Save >> "!VBS_FILE!"

    cscript //nologo "!VBS_FILE!" >nul 2>&1
    del "!VBS_FILE!" >nul 2>&1

    if exist "%SHORTCUT_PATH%" (
        echo   %GREEN%[OK]%RESET% 桌面快捷方式创建成功 (VBS方式)
        set "SHORTCUT_CREATED=1"
    ) else (
        echo   %RED%[FAIL]%RESET% 快捷方式创建失败
    )
)

echo.

:: ============================================
::  [3/3] 验证快捷方式
:: ============================================
echo %BOLD%%YELLOW%[3/3]%RESET% 验证快捷方式...
echo.

if %SHORTCUT_CREATED% equ 1 (
    echo   %GREEN%[OK]%RESET% 快捷方式已创建
    echo.
    echo   名称: %SHORTCUT_NAME%
    echo   位置: %DESKTOP_DIR%
    echo   目标: %START_BAT%
    echo   起始位置: %WORK_DIR%

    echo.
    echo %BOLD%%GREEN%========================================================================%RESET%
    echo %BOLD%%GREEN%                      桌面快捷方式创建成功！%RESET%
    echo %BOLD%%GREEN%========================================================================%RESET%
    echo.
    echo   您现在可以在桌面上看到 "%SHORTCUT_NAME%" 图标
    echo   双击即可启动零件查询系统
    echo.
    echo   是否现在启动系统？(Y/N)
    set /p "LAUNCH=请选择: "
    if /i "!LAUNCH!"=="Y" (
        start "" "%START_BAT%"
    )
) else (
    echo   %RED%[FAIL]%RESET% 快捷方式创建失败
    echo.
    echo   请尝试手动创建快捷方式:
    echo   1. 在桌面右键 - 新建 - 快捷方式
    echo   2. 输入位置: %START_BAT%
    echo   3. 名称: %SHORTCUT_NAME%
)

echo.
echo   按任意键退出...
pause >nul
exit /b 0
