@echo off
rem MailBox Manager - разовая настройка окружения (venv + зависимости)
cd /d "%~dp0"
set PY=py -3.12
%PY% --version >nul 2>&1 || set PY=python

echo [1/3] Создаю venv (.venv)...
%PY% -m venv .venv || goto :err

echo [2/3] Ставлю зависимости...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err

echo [3/3] Проверяю браузер Chromium для Playwright...
".venv\Scripts\python.exe" -m playwright install chromium || goto :err

echo.
echo Готово. Запуск: start_gui.bat  (консоль: cli.bat)
pause
exit /b 0

:err
echo.
echo [ОШИБКА] Что-то пошло не так - см. текст выше.
pause
exit /b 1
