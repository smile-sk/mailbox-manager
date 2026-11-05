# -*- coding: utf-8 -*-
"""
core/logger.py - файловое логирование для отладки.

Все записи журнала GUI и ошибки фоновых потоков (браузер, авторегистрация) падают в
logs/app.log рядом с проектом - с меткой времени и стектрейсами исключений.
Потокобезопасно (общий lock), создаёт папку logs/ автоматически.

Формат строки:  ГГГГ-ММ-ДД ЧЧ:ММ:СС  <сообщение>
"""
import datetime
import os
import threading
import traceback

from core import paths

LOG_DIR = paths.logs_dir()
LOG_PATH = os.path.join(LOG_DIR, "app.log")

_lock = threading.Lock()
_MAX_BYTES = 5 * 1024 * 1024  # 5 МБ - при переполнении ротация в app.1.log


def _ensure_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def log_file(msg):
    """Дописать строку в файл журнала (с ротацией по размеру)."""
    try:
        _ensure_dir()
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts}  {msg}\n"
        with _lock:
            if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > _MAX_BYTES:
                try:
                    if os.path.isfile(LOG_PATH + ".1"):
                        os.remove(LOG_PATH + ".1")
                    os.rename(LOG_PATH, LOG_PATH + ".1")
                except Exception:
                    pass
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass  # логирование никогда не должно ронять программу


def exception(context=""):
    """Записать текущее исключение со стектрейсом (вызывать в except)."""
    tb = traceback.format_exc()
    log_file(f"[EXC] {context}\n{tb}")
