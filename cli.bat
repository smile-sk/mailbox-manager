@echo off
rem MailBox Manager - быстрые команды через venv проекта
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ОШИБКА] Не найден venv проекта. Запустите setup.bat один раз.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" app.py %*
