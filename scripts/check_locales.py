# -*- coding: utf-8 -*-
"""Проверка локалей в ДВЕ стороны.

Запуск:  python scripts/check_locales.py
Выход:   0 - порядок, 1 - есть пропуски или лишние ключи.

1) Код -> локаль: ключи, которые интерфейс запрашивает, а в файле языка их нет.
   i18n.tr() при отсутствии ключа возвращает САМ КЛЮЧ - пользователь видит на
   экране «tab.mail_hint» (случилось 11.09).
2) Локаль -> код: ключи, которые код уже не зовёт (осиротели, когда функцию
   вырезали - так накопились 27 ключей от IMAP). Этот мусор копится молча.

Разбор идёт по синтаксическому дереву (ast), а не поиском текста, - поэтому примеры
в докстрингах не считаются вызовами. Динамические ключи вида tr("status." + s)
учитываются по префиксу, иначе живые status.* выглядели бы сиротами.
"""
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".venv", ".git", "__pycache__", "backups", ".file_backups", "data", "logs", "tools"}

# Ключи, которые сознательно лежат в файле языка, хотя код их пока не зовёт
# (задел на будущее). Всё прочее лишнее - мусор, и его надо убирать.
WHITELIST = set()


def _is_tr(node):
    """Это вызов tr(...) напрямую или через модуль (i18n.tr(...))?"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "tr"
    if isinstance(func, ast.Attribute):
        return func.attr == "tr"
    return False


def _prefix_of(node):
    """Префикс динамического ключа: tr("status." + s) / tr(f"status.{s}") -> "status."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = node.left
        if isinstance(left, ast.Constant) and isinstance(left.value, str):
            return left.value
    if isinstance(node, ast.JoinedStr) and getattr(node, "values", None):
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def collect_used():
    """(литеральные ключи, префиксы динамических, неразобранные аргументы)"""
    used = {}
    prefixes = set()
    dynamic = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            try:
                tree = ast.parse(open(full, encoding="utf-8", errors="replace").read())
            except SyntaxError as exc:
                print("! не разобрался файл %s: %s" % (full, exc))
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not _is_tr(node) or not node.args:
                    continue
                arg = node.args[0]
                rel = os.path.relpath(full, ROOT)
                # только литеральные ключи: строку-склейку tr("status." + s) пропускаем
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    used.setdefault(arg.value, set()).add(rel)
                else:
                    p = _prefix_of(arg)
                    if p:
                        prefixes.add(p)
                    else:
                        dynamic.append((rel, node.lineno, ast.unparse(arg)))
    return used, prefixes, dynamic


def main():
    locdir = os.path.join(ROOT, "locales")
    files = sorted(f for f in os.listdir(locdir) if f.endswith(".json")) if os.path.isdir(locdir) else []
    if not files:
        print("locales/: нет языковых файлов")
        return 1
    used, prefixes, dynamic = collect_used()
    bad = 0
    for fname in files:
        data = json.load(open(os.path.join(locdir, fname), encoding="utf-8"))
        keys = set(data) - {"_meta"}
        missing = {k: v for k, v in used.items() if k not in keys}
        orphans = sorted(
            k for k in keys
            if k not in used and k not in WHITELIST
            and not any(k.startswith(p) for p in prefixes)
        )
        if missing:
            bad += 1
            print("=== %s: НЕТ ПЕРЕВОДА у %d ключей ===" % (fname, len(missing)))
            for k in sorted(missing):
                print("   %-28s <- %s" % (k, ", ".join(sorted(missing[k]))))
        if orphans:
            bad += 1
            print("=== %s: ЛИШНИЕ %d ключей (код их не зовёт) ===" % (fname, len(orphans)))
            for k in orphans:
                print("   %-28s = %s" % (k, str(data[k])[:70]))
        if not missing and not orphans:
            print("=== %s: чисто - %d ключей из кода на месте, лишних нет ==="
                  % (fname, len(used)))
    if prefixes:
        print("(= динамические ключи по префиксам: %s)" % ", ".join(sorted(prefixes)))
    if dynamic:
        print("(?) не разобран аргумент tr(...) в: %s"
              % ", ".join("%s:%d" % (f, n) for f, n, _ in dynamic))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
