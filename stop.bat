@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
chcp 65001 >nul

where python >nul 2>nul
if %ERRORLEVEL%==0 (
  python -u "%~dp0scripts\start_dev.py" --stop
  exit /b %ERRORLEVEL%
)

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 -u "%~dp0scripts\start_dev.py" --stop
  exit /b %ERRORLEVEL%
)

echo [start] 失败：未找到 Python。请先安装 Python 3 并加入 PATH。
exit /b 1
