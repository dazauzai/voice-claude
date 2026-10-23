@echo off
chcp 65001 >nul
rem Starts voice-claude without a console window (pythonw); its log is voice-claude.log in this folder.
rem To watch the log live while debugging, run instead:  .venv\Scripts\python.exe -X utf8 -m voiceclaude
cd /d "%~dp0"
rem The self-contained zip brings its own Python in python\; a normal install uses .venv.
if exist "python\pythonw.exe" (
  start "" "python\pythonw.exe" -X utf8 -m voiceclaude
  exit /b 0
)
if not exist ".venv\Scripts\pythonw.exe" (
  echo 还没有安装：先双击 install.bat。
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -X utf8 -m voiceclaude
