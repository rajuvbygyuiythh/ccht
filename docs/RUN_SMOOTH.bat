@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Safe environment not installed.
  echo Run INSTALL_SMOOTH.bat first.
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"
python -m py_compile entry\main.py entry\paths.py entry\thread_manager.py browser\browser_automation.py core\config_loader.py
if errorlevel 1 (
  echo [ERROR] Python compile check failed.
  pause
  exit /b 1
)
echo Starting Chitchat Bot in AUTO/SMOOTH mode...
python -m entry.main