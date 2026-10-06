@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Safe environment not installed.
  echo Run docs\INSTALL_SMOOTH.bat first.
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"
python -m py_compile entry\main.py entry\paths.py entry\thread_manager.py browser\browser_automation.py core\config_loader.py chat\rules.py test_fuzz.py
if errorlevel 1 (
  echo [ERROR] Python compile check failed.
  pause
  exit /b 1
)
echo.
echo === EVA matcher + category system test ===
python test_matcher.py
if errorlevel 1 (
  echo [ERROR] Matcher test failed.
  pause
  exit /b 1
)
echo.
echo === EVA live chat-result test (funnel + brain) ===
python test_live.py
if errorlevel 1 (
  echo [ERROR] Live chat test failed.
  pause
  exit /b 1
)
echo.
echo === EVA fuzz test (Section 5.8 fallback, random messages) ===
python test_fuzz.py
if errorlevel 1 (
  echo [ERROR] Fuzz test failed.
  pause
  exit /b 1
)
echo.
pause