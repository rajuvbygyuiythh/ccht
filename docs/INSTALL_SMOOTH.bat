@echo off
setlocal
cd /d "%~dp0.."
where py >nul 2>&1
if errorlevel 1 (
  echo Python launcher ^(py^) was not found.
  echo Install Python 3.11+ and try again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r data\requirements.txt
python -m py_compile entry\main.py entry\paths.py entry\thread_manager.py browser\browser_automation.py core\config_loader.py
if errorlevel 1 (
  echo [ERROR] Compile check failed.
  pause
  exit /b 1
)
echo.
echo Installation complete.
echo Default behavior: 1 browser = Python-only, no Go, no Node.
echo Multi-browser: Go supervisor may start automatically from config.
echo.
pause