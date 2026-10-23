@echo off
cd /d "%~dp0"
title Elysium bot - panel
if not exist ".venv\Scripts\python.exe" (
  echo [error] .venv not found in the bot folder. Create it: python -m venv .venv
  pause
  exit /b 1
)
".venv\Scripts\python.exe" webui.py
pause
