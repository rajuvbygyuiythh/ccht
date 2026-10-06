@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set "PYTHONBIN=.venv\Scripts\python.exe"
) else (
  set "PYTHONBIN=python"
)

set PYTHONIOENCODING=utf-8

echo ---- EVA automated checks (pools + funnel + fuzz) -------------------
echo [0/4] markup-change check: selector doctor + self-healing (SMS detection)
"%PYTHONBIN%" test_selector_doctor.py
if errorlevel 1 (
  echo [ERROR] selector doctor test failed.
  pause
  exit /b 1
)
echo [1/4] pool check: input triggers - output replies (renamed pools)
"%PYTHONBIN%" test_matcher.py
if errorlevel 1 (
  echo [ERROR] matcher test failed.
  pause
  exit /b 1
)
echo [2/4] funnel check: first-SMS routing - country-once - asksc - share - END
"%PYTHONBIN%" test_live.py
if errorlevel 1 (
  echo [ERROR] live chat test failed.
  pause
  exit /b 1
)
echo [3/4] fuzz check: random inputs - states stay valid, no repeats
"%PYTHONBIN%" test_fuzz.py
if errorlevel 1 (
  echo [ERROR] fuzz test failed.
  pause
  exit /b 1
)
echo [4/4] real-user demo: scripted strangers chat the full funnel to END
"%PYTHONBIN%" demo_chat.py
if errorlevel 1 (
  echo [ERROR] demo chat failed.
  pause
  exit /b 1
)

echo.
echo ALL AUTOMATED CHECKS PASSED.
echo.
echo ---- EVA live chat test (RuleEngine, manual - /quit to exit) --------
"%PYTHONBIN%" tools\live_chat.py
if errorlevel 1 (
  echo [ERROR] live chat failed.
  pause
  exit /b 1
)
endlocal