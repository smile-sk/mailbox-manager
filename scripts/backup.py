#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бэкап проекта mailbox_manager.

Кладёт zip-архив в <проект>/backups/mailbox_manager_backup_YYYYMMDD_HHMMSS.zip
Исключает тяжёлое/мусорное: .venv, __pycache__, logs, саму папку backups,
браузерные профили data/profiles (сотни МБ кэша; воспроизводимы - сессия
восстанавливается логином по паролю из БД).
Включает код + БД ящиков (data/mailboxes.db) + локали.

Запуск:
    python scripts/backup.py            - сделать бэкап
    python scripts/backup.py list       - показать имеющиеся бэкапы
    python scripts/backup.py prune 10   - оставить последние 10 (ротация)
"""
import os
import sys
import zipfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_DIR = os.path.join(ROOT, "backups")

EXCLUDE_DIRS = {".venv", "__pycache__", "backups", "logs", ".git", ".idea",
                "node_modules", "profiles"}  # profiles = data/profiles
EXCLUDE_EXTS = {".pyc", ".pyo"}
MAX_BACKUPS = 10


def ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def make_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = f"mailbox_manager_backup_{ts()}.zip"
    path = os.path.join(BACKUP_DIR, name)
    count = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for fn in filenames:
                if os.path.splitext(fn)[1] in EXCLUDE_EXTS:
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT)
                zf.write(full, rel)
                count += 1
    print(f"OK: {path} ({count} файлов)")
    prune()
    return path


def prune(keep=MAX_BACKUPS):
    files = sorted(
        f for f in os.listdir(BACKUP_DIR)
        if f.startswith("mailbox_manager_backup_") and f.endswith(".zip")
    )
    # ротация по перечитанному списку - без off-by-one
    while len(files) > keep:
        victim = os.path.join(BACKUP_DIR, files.pop(0))
        os.remove(victim)
        print(f"pruned: {os.path.basename(victim)}")


def list_backups():
    files = sorted(
        f for f in os.listdir(BACKUP_DIR)
        if f.startswith("mailbox_manager_backup_") and f.endswith(".zip")
    )
    if not files:
        print("(бэкапов нет)")
        return
    for f in files:
        full = os.path.join(BACKUP_DIR, f)
        size = os.path.getsize(full) / 1024
        print(f"{f}  ({size:.0f} КБ)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "backup"
    if cmd == "list":
        list_backups()
    elif cmd == "prune":
        keep = int(sys.argv[2]) if len(sys.argv) > 2 else MAX_BACKUPS
        prune(keep)
    else:
        make_backup()
