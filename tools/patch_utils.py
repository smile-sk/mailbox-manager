# -*- coding: utf-8 -*-
"""
patch_utils - ЕДИНЫЙ хелпер правок файлов проекта.
Закрывает навсегда проблему CRLF/LF: любой файл читается и пишется
С СОХРАНЕНИЕМ исходного стиля переносов (CRLF на Windows, LF на Linux).

Правило (жёсткое): ЛЮБАЯ правка текстового файла проекта - только через
patch_file() из этого модуля. Никогда не писать обратно результат
read_text()/read() "как есть" - потеряешь стиль переносов.

Страховка: перед записью предыдущая версия файла сохраняется в
<project>/.file_backups/ (та же схема имён и ротации, что у ФС-брокера).

Использование:
    from tools.patch_utils import patch_file, file_style, read_normalized

    report = patch_file(path, [
        ("old string", "new string"),
        ("another", "replacement"),
    ])          # каждая замена обязана встретиться ровно 1 раз (по умолчанию)
    print(report)

    style = file_style(path)          # 'crlf' | 'lf'
    text  = read_normalized(path)     # текст с \n (стиль забыт, для чтения)
"""

from __future__ import annotations

import re
import shutil
import sys
import time
from pathlib import Path

BACKUP_KEEP = 10

BACKUP_MAX_AGE_DAYS = 7


def _backup_before(target: Path) -> None:
    # Mechanical safety net (mirrors the fs broker): before overwriting a file,
    # keep the previous version in <project>/.file_backups/.
    try:
        if not target.is_file():
            return
        if ".file_backups" in target.parts or ".snapshots" in target.parts:
            return
        bdir = Path(__file__).resolve().parents[1] / ".file_backups"
        bdir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.monotonic()*1e6) % 1000000:06d}"
        rel = str(target.resolve()).replace(":", "").replace("\\", "__").replace("/", "__")
        dest = bdir / f"{ts}__{rel}"
        shutil.copy2(target, dest)
        olds = sorted(bdir.glob(f"*__{rel}"))
        for old in olds[:-BACKUP_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
        # возрастная ротация: версии старше 7 дней - в утиль.
        # Ключ - дата в ИМЕНИ файла (mtime ненадёжен: сбрасывается при
        # восстановлении проекта из бекапа). Глубокая история - в снапшотах.
        cutoff = time.time() - BACKUP_MAX_AGE_DAYS * 86400
        for old in list(bdir.iterdir()):
            try:
                if not old.is_file():
                    continue
                m = re.match(r"\d{8}_\d{6}", old.name)
                stamp = (time.mktime(time.strptime(m.group(0), "%Y%m%d_%H%M%S"))
                         if m else old.stat().st_mtime)
                if stamp < cutoff:
                    old.unlink()
            except (OSError, ValueError):
                pass
    except OSError:
        pass


def file_style(path: str | Path) -> str:
    """Вернуть 'crlf' или 'lf' - стиль переносов файла на диске."""
    data = Path(path).read_bytes()
    return 'crlf' if b'\r\n' in data else 'lf'


def read_normalized(path: str | Path) -> str:
    """Прочитать файл, нормализуя переносы в \\n (удобно для анализа/поиска)."""
    text = Path(path).read_text(encoding='utf-8')
    return text.replace('\r\n', '\n')


def patch_file(path: str | Path, replacements: list, check_exact: bool = True) -> dict:
    """
    Применить замены old->new с СОХРАНЕНИЕМ исходного стиля переносов.
    replacements: список кортежей (old, new).
    check_exact=True (по умолчанию): каждая old обязана встретиться ровно 1 раз,
        иначе RuntimeError с подробностями - так ловим «замена не туда/не там».
    Возвращает dict {old: count} - сколько раз каждая замена легла.
    """
    p = Path(path)
    raw = p.read_bytes()
    style = 'crlf' if b'\r\n' in raw else 'lf'
    text = raw.decode('utf-8').replace('\r\n', '\n')

    report = {}
    for old, new in replacements:
        n = text.count(old)
        if check_exact and n != 1:
            raise RuntimeError(
                f"[patch_utils] {p.name}: замена встречается {n} раз (нужно 1): {old[:60]!r}\n"
                f"  стиль файла: {style}, размер: {len(raw)} байт"
            )
        text = text.replace(old, new)
        report[old] = n

    out = text.replace('\n', '\r\n') if style == 'crlf' else text
    raw_out = out.encode('utf-8')
    if raw_out != raw:
        _backup_before(p)
    p.write_bytes(raw_out)

    # проверка: перечитали - новые строки реально на месте, стиль цел
    raw2 = p.read_bytes()
    style2 = 'crlf' if b'\r\n' in raw2 else 'lf'
    assert style2 == style, f"[patch_utils] стиль изменился: {style} -> {style2}"
    for old, new in replacements:
        if new and new not in raw2.decode('utf-8', errors='replace').replace('\r\n', '\n'):
            raise RuntimeError(f"[patch_utils] {p.name}: новая строка НЕ найдена после записи: {new[:60]!r}")
    return report


if __name__ == '__main__':
    # самопроверка на временном файле: CRLF должен выжить
    import tempfile, os
    tmp = Path(tempfile.mkstemp(suffix='.txt')[1])
    bdir = Path(__file__).resolve().parents[1] / ".file_backups"
    rel = str(tmp.resolve()).replace(":", "").replace("\\", "__").replace("/", "__")
    try:
        tmp.write_bytes('line1\r\nline2\r\nline3\r\n'.encode('utf-8'))
        r = patch_file(tmp, [('line2', 'LINE2')])
        raw = tmp.read_bytes()
        assert raw == b'line1\r\nLINE2\r\nline3\r\n', raw
        assert file_style(tmp) == 'crlf'
        backups = sorted(bdir.glob(f"*__{rel}"))
        assert backups, 'SELFTEST: бэкап в .file_backups не создан'
        assert b'line2' in backups[-1].read_bytes(), 'SELFTEST: бэкап не содержит прежнюю версию'
        print('SELFTEST OK: CRLF сохранён, замена легла, бэкап создан, отчёт =', r)
    finally:
        for old in bdir.glob(f"*__{rel}"):
            try:
                old.unlink()
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
