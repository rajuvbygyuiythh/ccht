@echo off
setlocal EnableDelayedExpansion
title EVA Bot - Install (everything in one run)
cd /d "%~dp0"
chcp 65001 >nul

echo ============================================================
echo  EVA Bot installer - one run installs EVERYTHING:
echo  venv + all packages + Playwright browser + auto-repair
echo  Needs: Windows + Python 3.10 or newer + internet
echo ============================================================
echo.

REM ---- 1. Find Python (try multiple launchers) -----------------
set "PY="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  python3 --version >nul 2>&1
  if not errorlevel 1 set "PY=python3"
)
if not defined PY (
  echo [ERROR] Python not found. Install Python 3.10+ from python.org
  echo         IMPORTANT: tick "Add python.exe to PATH" during setup,
  echo         then run install.bat again.
  pause
  exit /b 1
)
echo [OK] Python: %PY%
%PY% --version
echo.

REM ---- 2. Virtual env (.venv) ----------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo [*] Creating .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [ERROR] venv failed.
    pause
    exit /b 1
  )
) else (
  echo [OK] .venv already exists.
)
set "VPY=.venv\Scripts\python.exe"
echo.

REM ---- 3. pip upgrade (retry 3x) -------------------------------
echo [*] Upgrading pip ...
set "OK=0"
for /L %%i in (1,1,3) do (
  if !OK! EQU 0 (
    "%VPY%" -m pip install --upgrade pip
    if not errorlevel 1 set "OK=1"
  )
)
if %OK% EQU 0 echo [WARN] pip upgrade failed, continuing anyway.
echo.

REM ---- 4. Requirements (bulk, then per-package fallback) -------
if not exist "data\requirements.txt" (
  echo [ERROR] data\requirements.txt missing.
  pause
  exit /b 1
)
echo [*] Installing packages from data\requirements.txt ...
set "OK=0"
for /L %%i in (1,1,3) do (
  if !OK! EQU 0 (
    "%VPY%" -m pip install -r data\requirements.txt
    if not errorlevel 1 set "OK=1"
  )
)
if %OK% EQU 0 (
  echo [WARN] Bulk install failed, trying packages one by one ...
  for /F "usebackq tokens=* delims=" %%p in ("data\requirements.txt") do (
    echo     - %%p
    "%VPY%" -m pip install "%%p"
    if errorlevel 1 echo     [WARN] %%p failed, will force-repair below.
  )
)
echo.

REM ---- 5. Playwright browser (needed for real chat) ------------
echo [*] Installing Playwright Chromium ...
"%VPY%" -m playwright install chromium
if errorlevel 1 (
  echo [WARN] Trying with system deps ...
  "%VPY%" -m playwright install --with-deps chromium
  if errorlevel 1 (
    echo [WARN] Chromium failed. Re-run later:
    echo        .venv\Scripts\python.exe -m playwright install chromium
  )
)
echo.

REM ---- 6. Check each package, FORCE-repair missing ones --------
echo [*] Checking every package (missing ones get force-reinstalled) ...
set "MISS=0"
for %%a in ("PyQt6=PyQt6" "playwright=playwright" "psutil=psutil" "requests=requests" "camoufox=camoufox[geoip]" "cloakbrowser=cloakbrowser[geoip]") do (
  for /F "tokens=1,2 delims==" %%m in (%%a) do (
    "%VPY%" -c "import %%m" >nul 2>&1
    if errorlevel 1 (
      echo     [MISSING] %%m - force-reinstalling ...
      "%VPY%" -m pip install --force-reinstall --no-cache-dir "%%n"
      "%VPY%" -c "import %%m" >nul 2>&1
      if errorlevel 1 (
        echo     [STILL MISSING] %%m
        set "MISS=1"
      ) else (
        echo     [FIXED] %%m
      )
    ) else (
      echo     [OK] %%m
    )
  )
)
echo.

REM ---- 7. Chat engine smoke test --------------------------------
"%VPY%" -c "import sys; sys.path.insert(0, '.'); from chat.rule_bot import ChatRuleBot; b = ChatRuleBot(); s = b.new_conversation(); r = b.reply('hi m21', s); assert r.strip(), 'empty'; print('[OK] chat engine:', repr(r))"
if errorlevel 1 set "MISS=1"
echo.

if %MISS% EQU 0 goto :all_ok
echo ============================================================
echo  SOMETHING IS STILL MISSING - see STILL MISSING above.
echo  Check internet / antivirus, then run install.bat again.
echo ============================================================
pause
exit /b 1

:all_ok
REM ---- 8. Create the "EVA Bot" APP on the Desktop -----------------
echo [*] Creating "EVA Bot" app on your Desktop ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws=New-Object -ComObject WScript.Shell; $d=[Environment]::GetFolderPath('Desktop'); $l=$ws.CreateShortcut($d+'\EVA Bot.lnk'); $l.TargetPath='%CD%\.venv\Scripts\pythonw.exe'; $l.Arguments='-m entry.main'; $l.WorkingDirectory='%CD%'; $l.IconLocation='%CD%\data\chitchat-bot.ico'; $l.Description='EVA Bot Control Center'; $l.Save()" >nul 2>&1
if exist "%USERPROFILE%\Desktop\EVA Bot.lnk" (
  echo [OK] App installed on Desktop: "EVA Bot"
) else (
  echo [WARN] Desktop shortcut not found ^(OneDrive desktop?^) - use run.vbs instead.
)

echo ============================================================
echo  INSTALL DONE - the app is now on your Desktop!
echo.
echo    DESKTOP "EVA Bot"  =  double-click to start ^(no CMD window^)
echo    run.vbs            =  same app launch from this folder
echo    run_console.bat    =  debug mode ^(shows console logs^)
echo    test.bat           =  automated checks + demo
echo.
echo  TIP: on a slow PC use the LOW performance preset
echo       in the dashboard ^(0%% lag^).
echo ============================================================
set /p LAUNCH="Launch EVA Bot now? [Y/N]: "
if /i "%LAUNCH%"=="Y" (
  if exist "%USERPROFILE%\Desktop\EVA Bot.lnk" (
    start "" "%USERPROFILE%\Desktop\EVA Bot.lnk"
  ) else (
    start "" wscript.exe "%CD%\run.vbs"
  )
)
endlocal
