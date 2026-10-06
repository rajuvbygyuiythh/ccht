@echo off
setlocal EnableDelayedExpansion
title EVA Bot - Force Reinstall (fix missing/broken packages)
cd /d "%~dp0"
chcp 65001 >nul

echo ============================================================
echo  EVA Bot FORCE reinstall - deletes and reinstalls every
echo  package fresh (use when install.bat still leaves errors).
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv missing. Run install.bat first.
  pause
  exit /b 1
)
set "VPY=.venv\Scripts\python.exe"

echo [*] Upgrading pip (forced) ...
"%VPY%" -m pip install --upgrade --force-reinstall --no-cache-dir pip
echo.

echo [*] FORCE reinstalling data\requirements.txt ...
set "OK=0"
for /L %%i in (1,1,2) do (
  if !OK! EQU 0 (
    "%VPY%" -m pip install --force-reinstall --no-cache-dir -r data\requirements.txt
    if not errorlevel 1 set "OK=1"
  )
)
if %OK% EQU 0 (
  echo [WARN] Bulk force-install failed, forcing packages one by one ...
  for /F "usebackq tokens=* delims=" %%p in ("data\requirements.txt") do (
    echo     - %%p
    "%VPY%" -m pip install --force-reinstall --no-cache-dir "%%p"
    if errorlevel 1 echo     [WARN] %%p still failing.
  )
)
echo.

echo [*] Playwright Chromium (forced, with system deps) ...
"%VPY%" -m playwright install --with-deps chromium
if errorlevel 1 (
  echo [WARN] --with-deps failed - needs admin?, retrying without deps ...
  "%VPY%" -m playwright install --force chromium
  if errorlevel 1 echo [WARN] Chromium still failing.
)
echo.

echo [*] Checking each package one by one ...
set "MISS=0"
for %%m in (PyQt6 playwright psutil requests camoufox cloakbrowser) do (
  "%VPY%" -c "import %%m" >nul 2>&1
  if errorlevel 1 (
    echo     [MISSING] %%m
    set "MISS=1"
  ) else (
    echo     [OK] %%m
  )
)
echo.
"%VPY%" -c "import sys; sys.path.insert(0, '.'); from chat.rule_bot import ChatRuleBot; b = ChatRuleBot(); s = b.new_conversation(); r = b.reply('hi m21', s); assert r.strip(), 'empty'; print('[OK] chat engine:', repr(r))"
if errorlevel 1 set "MISS=1"
echo.

if %MISS% EQU 0 (
  echo ============================================================
  echo  FORCE INSTALL DONE - nothing missing.
  echo ============================================================
) else (
  echo ============================================================
  echo  STILL MISSING items above. Copy that part and send it,
  echo  or check internet / antivirus blocking pip.
  echo ============================================================
)
pause
endlocal
