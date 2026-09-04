@echo off
setlocal
cd /d "%~dp0"
py -m pip install -r requirements.txt >nul 2>&1
if errorlevel 1 (
  echo 依赖安装失败，请检查网络后重试。
  pause
  exit /b 1
)
py desktop_app.py
