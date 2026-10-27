@echo off
chcp 65001 >nul
rem Standby: only the wake word runs ("hi chat" unless config.toml [wake] says otherwise); hearing it opens the window,
rem which goes back to standby after a few idle minutes. Its log is standby.log in ~/.voice-claude.
cd /d "%~dp0"
if exist "python\pythonw.exe" (
  start "" "python\pythonw.exe" -X utf8 -m voiceclaude.standby
  exit /b 0
)
if not exist ".venv\Scripts\pythonw.exe" (
  echo 还没有安装：先双击 install.bat。
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -X utf8 -m voiceclaude.standby
