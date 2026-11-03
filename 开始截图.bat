@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
    if exist "..\.venv\Scripts\python.exe" set "PY=..\.venv\Scripts\python.exe"
)
"%PY%" -c "import numpy" >nul 2>nul
if errorlevel 1 (
    echo [!] 缺依赖，先运行:  pip install -r requirements.txt
    pause
    exit /b 1
)

echo 正在连接 Saleae Logic 2 ...
echo.
"%PY%" page_watcher.py --minutes 30
echo.
echo 已结束。截图在 pages 目录，双击 pages\index.html 看总览。
pause
