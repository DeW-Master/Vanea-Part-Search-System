@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

:: ============================================
::   F-Brain 零件查询系统 - 数据备份
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
echo %BOLD%%CYAN%           F-Brain 零件查询系统 - 数据备份%RESET%
echo %BOLD%%CYAN%========================================================================%RESET%
echo.

:: 生成时间戳
set "TIMESTAMP=%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%"
set "TIMESTAMP=%TIMESTAMP: =0%"
set "BACKUP_DIR=%CD%\backup"
set "BACKUP_NAME=backup_%TIMESTAMP%"
set "BACKUP_PATH=%BACKUP_DIR%\%BACKUP_NAME%"

:: ============================================
::  [1/4] 创建备份目录
:: ============================================
echo %BOLD%%YELLOW%[1/4]%RESET% 准备备份目录...
echo.

if not exist "%BACKUP_DIR%" (
    mkdir "%BACKUP_DIR%"
    echo   %GREEN%[OK]%RESET% 已创建备份目录: %BACKUP_DIR%
) else (
    echo   %GREEN%[OK]%RESET% 备份目录已存在: %BACKUP_DIR%
)

mkdir "%BACKUP_PATH%" 2>nul
echo.

:: ============================================
::  [2/4] 备份数据库
:: ============================================
echo %BOLD%%YELLOW%[2/4]%RESET% 备份数据库文件...
echo.

set "DB_BACKUP_SUCCESS=0"
set "DB_FILE=data\parts.db"

if exist "%DB_FILE%" (
    :: 尝试使用 SQLite .backup 命令进行热备份（更安全）
    where sqlite3 >nul 2>&1
    if !errorlevel! equ 0 (
        echo   %CYAN%使用 SQLite 热备份...%RESET%
        sqlite3 "%DB_FILE%" ".backup '%BACKUP_PATH%parts.db'"
        if !errorlevel! equ 0 (
            echo   %GREEN%[OK]%RESET% 数据库热备份成功
            set "DB_BACKUP_SUCCESS=1"
        ) else (
            echo   %YELLOW%[WARN]%RESET% 热备份失败，尝试直接复制...
        )
    ) else (
        echo   %YELLOW%[INFO]%RESET% 未检测到 sqlite3，使用文件复制方式
    )

    if !DB_BACKUP_SUCCESS! equ 0 (
        :: 直接复制文件
        copy /Y "%DB_FILE%" "%BACKUP_PATH%parts.db" >nul
        if !errorlevel! equ 0 (
            echo   %GREEN%[OK]%RESET% 数据库文件备份成功
            set "DB_BACKUP_SUCCESS=1"
        ) else (
            echo   %RED%[FAIL]%RESET% 数据库文件备份失败
            echo   %YELLOW%[提示]%RESET% 数据库可能被占用，请先停止系统后再备份
        )
    )

    :: 显示文件大小
    if exist "%BACKUP_PATH%parts.db" (
        for %%A in ("%BACKUP_PATH%parts.db") do (
            set "DB_SIZE=%%~zA"
            set /a "DB_SIZE_MB=DB_SIZE / 1048576"
            set /a "DB_SIZE_KB=DB_SIZE %% 1048576 / 1024"
            echo   文件大小: !DB_SIZE_MB! MB !DB_SIZE_KB! KB (!DB_SIZE! 字节)
        )
    )
) else (
    echo   %YELLOW%[INFO]%RESET% 未找到数据库文件: %DB_FILE%
    echo   跳过数据库备份
)

echo.

:: ============================================
::  [3/4] 备份配置文件
:: ============================================
echo %BOLD%%YELLOW%[3/4]%RESET% 备份配置文件...
echo.

set "CONFIG_BACKUP_COUNT=0"

:: 备份 cloud_config.json
if exist "data\cloud_config.json" (
    copy /Y "data\cloud_config.json" "%BACKUP_PATH%cloud_config.json" >nul
    if !errorlevel! equ 0 (
        echo   %GREEN%[OK]%RESET% cloud_config.json 已备份
        set /a CONFIG_BACKUP_COUNT+=1
    ) else (
        echo   %YELLOW%[WARN]%RESET% cloud_config.json 备份失败
    )
) else (
    echo   %YELLOW%[INFO]%RESET% cloud_config.json 不存在，跳过
)

:: 备份 .env 文件（如果存在）
if exist ".env" (
    copy /Y ".env" "%BACKUP_PATH%.env" >nul
    if !errorlevel! equ 0 (
        echo   %GREEN%[OK]%RESET% .env 已备份
        set /a CONFIG_BACKUP_COUNT+=1
    )
)

:: 备份 version.txt
if exist "data\version.txt" (
    copy /Y "data\version.txt" "%BACKUP_PATH%version.txt" >nul
    if !errorlevel! equ 0 (
        echo   %GREEN%[OK]%RESET% version.txt 已备份
        set /a CONFIG_BACKUP_COUNT+=1
    )
)

echo   共备份 %CONFIG_BACKUP_COUNT% 个配置文件
echo.

:: ============================================
::  [4/4] 清理过期备份
:: ============================================
echo %BOLD%%YELLOW%[4/4]%RESET% 清理过期备份（保留最近30天）...
echo.

set "DELETED_COUNT=0"

:: 使用 PowerShell 清理30天前的备份
powershell -Command "$limit = (Get-Date).AddDays(-30); Get-ChildItem '%BACKUP_DIR%' -Directory | Where-Object { $_.LastWriteTime -lt $limit } | ForEach-Object { Remove-Item $_.FullName -Recurse -Force; Write-Output $_.Name }" > "%TEMP%\backup_cleanup.txt" 2>&1

for /f "usebackq delims=" %%L in ("%TEMP%\backup_cleanup.txt") do (
    echo   %YELLOW%[已删除]%RESET% %%L
    set /a DELETED_COUNT+=1
)

if %DELETED_COUNT% equ 0 (
    echo   %GREEN%[OK]%RESET% 没有过期的备份文件
) else (
    echo.
    echo   共清理 %DELETED_COUNT% 个过期备份
)

del "%TEMP%\backup_cleanup.txt" >nul 2>&1

echo.

:: ============================================
::  备份完成汇总
:: ============================================
echo %BOLD%%GREEN%========================================================================%RESET%
echo %BOLD%%GREEN%                          备份完成！%RESET%
echo %BOLD%%GREEN%========================================================================%RESET%
echo.
echo   %BOLD%备份时间:%RESET%     %date% %time:~0,8%
echo   %BOLD%备份路径:%RESET%     %CYAN%%BACKUP_PATH%%RESET%
echo.

:: 计算总大小
set "TOTAL_SIZE=0"
if exist "%BACKUP_PATH%parts.db" (
    for %%A in ("%BACKUP_PATH%parts.db") do set /a TOTAL_SIZE+=%%~zA
)
if exist "%BACKUP_PATH%cloud_config.json" (
    for %%A in ("%BACKUP_PATH%cloud_config.json") do set /a TOTAL_SIZE+=%%~zA
)

set /a "TOTAL_MB=TOTAL_SIZE / 1048576"
set /a "TOTAL_KB=TOTAL_SIZE %% 1048576 / 1024"
echo   %BOLD%总大小:%RESET%       %TOTAL_MB% MB %TOTAL_KB% KB (%TOTAL_SIZE% 字节)
echo.

:: 统计备份数量
set "BACKUP_COUNT=0"
for /d %%D in ("%BACKUP_DIR%\*") do set /a BACKUP_COUNT+=1
echo   %BOLD%备份总数:%RESET%     %BACKUP_COUNT% 个
echo.

echo   提示: 系统将自动保留最近30天的备份
echo.
echo   按任意键退出...
echo.
pause >nul
exit /b 0
