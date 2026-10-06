$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Safe environment not installed. Run INSTALL_SMOOTH.bat first." }
& ".venv\Scripts\python.exe" -m py_compile entry\main.py entry\paths.py entry\thread_manager.py browser\browser_automation.py core\config_loader.py
& ".venv\Scripts\python.exe" -m entry.main