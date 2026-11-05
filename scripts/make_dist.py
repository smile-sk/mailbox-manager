# -*- coding: utf-8 -*-
"""
make_dist.py - собрать папку, которую можно просто скопировать на другой ПК.

    python scripts/make_dist.py                  папка dist/MailboxManager
    python scripts/make_dist.py --browsers       ещё и браузеры (~430 МБ)
    python scripts/make_dist.py --out D:\\tmp\\d    куда положить

Что кладётся в папку:
    MailboxManager.exe     сама программа (должна быть собрана заранее)
    locales/ru.json        тексты интерфейса (рядом с exe - можно править)
    data/proxies.txt       шаблон: куда вставлять свой список прокси
    README.txt             инструкция по эксплуатации
    browsers/              (по флагу) Chromium, чтобы ничего не качать
"""
import argparse
import glob
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import paths  # noqa: E402

README_FILE = os.path.join(ROOT, "scripts", "README.dist.txt")


def read_readme():
    """Текст README для папки переноса: единый источник scripts/README.dist.txt."""
    with open(README_FILE, encoding="utf-8") as f:   # universal newlines -> \n
        return f.read()


def copy_browsers(dst):
    """Скопировать Chromium/ffmpeg/winldd из кэша Playwright в browsers/."""
    src = paths._default_browsers_root()
    if not os.path.isdir(src):
        print("Кэш Playwright не найден: %s - браузеры не копирую" % src)
        return 0
    keep = ("chromium-", "ffmpeg-", "winldd-")   # headless_shell не нужен: окно видимое
    total = 0
    for name in sorted(os.listdir(src)):
        if not name.startswith(keep):
            continue
        s, d = os.path.join(src, name), os.path.join(dst, "browsers", name)
        if os.path.isdir(d):
            print("уже есть: %s" % name)
            continue
        print("копирую %s …" % name)
        shutil.copytree(s, d)
        total += 1
    print("скопировано папок браузеров: %d" % total)
    return total


def main():
    ap = argparse.ArgumentParser(description="собрать папку для другого ПК")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--name", default="MailboxManager")
    ap.add_argument("--exe", default=os.path.join(ROOT, "MailboxManager.exe"))
    ap.add_argument("--browsers", action="store_true",
                    help="скопировать браузеры Playwright (~430 МБ)")
    a = ap.parse_args()

    if not os.path.isfile(a.exe):
        print("Нет собранного exe: %s\nСначала собери его (compile_exe → run_gui.py)." % a.exe)
        return 1
    dst = os.path.join(a.out, a.name)
    os.makedirs(dst, exist_ok=True)

    shutil.copy2(a.exe, os.path.join(dst, os.path.basename(a.exe)))
    print("exe: %.1f МБ" % (os.path.getsize(a.exe) / 1048576.0))

    loc = os.path.join(dst, "locales")
    os.makedirs(loc, exist_ok=True)
    for d in paths.locales_dirs():
        if not os.path.isdir(d):
            continue
        for f in glob.glob(os.path.join(d, "*.json")):
            shutil.copy2(f, os.path.join(loc, os.path.basename(f)))
    print("локали: %s" % ", ".join(sorted(os.path.basename(p) for p in glob.glob(os.path.join(loc, "*.json")))))

    data = os.path.join(dst, "data")
    os.makedirs(data, exist_ok=True)
    pfile = os.path.join(data, "proxies.txt")
    if not os.path.isfile(pfile):
        with open(pfile, "w", encoding="utf-8", newline="\n") as f:
            f.write(paths.PROXIES_TEMPLATE)

    legacy = os.path.join(dst, "ПРОЧТИ-МЕНЯ.txt")
    if os.path.isfile(legacy):
        os.remove(legacy)                      # прежнее имя, до переименования
    with open(os.path.join(dst, "README.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(read_readme())

    if a.browsers:
        copy_browsers(dst)

    size = 0
    for dp, _dn, fn in os.walk(dst):
        for f in fn:
            try:
                size += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    print("\nПапка готова: %s   (%.0f МБ)" % (dst, size / 1048576.0))
    print("Копируй её на другой ПК целиком и запускай %s.exe" % a.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
