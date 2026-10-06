@echo off
title EVA Bot - Run (console mode, for debugging)
cd /d "%~dp0"
rem ============================================================
rem  Console launcher - shows logs/errors in a CMD window.
rem  Use this if the windowless run.bat has a problem.
rem ============================================================
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)
python -m entry.main
if %errorlevel% neq 0 (
    echo.
    echo Bot exited with error!
    pause
)
