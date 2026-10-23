@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "python\python.exe" (
  echo 这个版本已经包含运行环境和语音模型，直接双击 run.bat。
  pause
  exit /b 0
)
rem voice-claude installer: finds Python 3.11+, then runs scripts\install.py (each step is skipped when done).
rem Options: --mirror (always use the China mirrors)  --no-mirror  --with-gpt  --yes

set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo 没有找到 Python 3.11 或更新的版本。
  echo.
  echo 请先安装 Python，安装时勾选 "Add python.exe to PATH"：
  echo   https://www.python.org/downloads/windows/
  echo   国内镜像：https://registry.npmmirror.com/binary.html?path=python/
  echo.
  echo 装好后关掉这个窗口，再双击 install.bat。
  pause
  exit /b 1
)

%PY% -X utf8 scripts\install.py %*
echo.
pause
