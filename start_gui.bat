@echo off
rem MailBox Manager - запуск графического интерфейса (через venv проекта)
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo [ОШИБКА] Не найден venv проекта. Запустите setup.bat один раз.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" gui.py
