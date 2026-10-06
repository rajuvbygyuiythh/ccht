@echo off
title Chitchat Bot - Quick Start
color 0A
cd /d "%~dp0.."

echo ============================================
echo        CHITCHAT BOT - QUICK START
echo ============================================
echo.
echo Starting bot directly...
echo No accounts needed - using Create mode.
echo.
echo ============================================

REM Activate virtual environment if exists
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

REM Run the bot
python -m entry.main

if %errorlevel% neq 0 (
    echo.
    echo Bot exited with error!
    pause
)