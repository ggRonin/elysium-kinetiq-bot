@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Elysium bot - panel
if not exist ".venv\Scripts\python.exe" (
  echo Не найдено окружение .venv в папке бота.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" webui.py
pause
