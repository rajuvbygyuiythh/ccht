$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw "Python launcher 'py' was not found. Install Python 3.11+ first." }
if (-not (Test-Path ".venv\Scripts\python.exe")) { py -3 -m venv .venv }
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -r data\requirements.txt
& ".venv\Scripts\python.exe" -m py_compile entry\main.py entry\paths.py entry\thread_manager.py browser\browser_automation.py core\config_loader.py
Write-Host "Installation complete. 1 browser = Python-only; multi-browser may auto-start Go. Node/JS remains disabled."