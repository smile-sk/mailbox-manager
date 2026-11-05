# -*- coding: utf-8 -*-
"""
run_gui.py - единая точка входа: обычный запуск, сборка в .exe, проверка сборки.

  python run_gui.py                     открыть программу (то же, что gui.py)
  python run_gui.py --selftest          проверить окружение/сборку и выйти
  python run_gui.py --check-browser     проверить, что Chromium запускается
  python run_gui.py --install-browser   скачать Chromium для Playwright (~150 МБ)
  python run_gui.py --cli list          командная оболочка (как app.py)

Собирается PyInstaller'ом ИМЕННО ЭТОТ файл: exe кладётся рядом, и папки
data/, logs/, locales/ оказываются рядом с exe, а не во временной папке сборки
(см. core/paths.py - иначе база и профили терялись бы при каждом запуске).

--selftest пишет отчёт в logs/selftest.txt: у exe с окном нет консоли, поэтому
результат надо куда-то положить.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def soft_stdio():
    """Консоль Windows по умолчанию cp1251 - печать отчёта с кириллицей, рамками
    и стрелками падала прямо в UnicodeEncodeError. Меняем ТОЛЬКО реакцию на
    непереводимые символы: кодировку консоли не трогаем (кириллица в cp1251
    читается нормально), а нечитаемое заменяем знаком вопроса."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def say(*parts):
    """Печать, которая не роняет программу из-за кодировки консоли."""
    text = " ".join(str(p) for p in parts)
    try:
        print(text)
    except (UnicodeEncodeError, OSError):
        try:
            buf = getattr(sys.stdout, "buffer", None)
            if buf is not None:
                buf.write((text + "\n").encode("utf-8", "replace"))
                buf.flush()
        except Exception:
            pass


def show_report(text, out):
    """В сборке без консоли отчёту некуда печататься - показываем окном.
    Флаг --no-ui окно отключает (нужно автопроверкам)."""
    if sys.stdout is not None or "--no-ui" in sys.argv:
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("MailboxManager",
                            "%s\n\nПолный отчёт:\n%s" % (text[:1500], out))
        root.destroy()
    except Exception:
        pass


def selftest():
    """Проверка сборки: пути, права на запись, БД, локали, Playwright, браузер."""
    lines = []

    def add(key, val):
        lines.append("%-24s %s" % (key + ":", val))

    add("когда", __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    add("frozen (это exe)", getattr(sys, "frozen", False))
    add("python", sys.version.split()[0])
    add("sys.executable", sys.executable)

    from core import paths
    add("app_dir", paths.app_dir())
    add("bundle_dir", paths.bundle_dir())
    add("data_dir", paths.data_dir())
    add("db_path", paths.db_path())
    add("logs_dir", paths.logs_dir())
    add("locales_dirs", " | ".join(paths.locales_dirs()))

    # права на запись рядом с программой (на чужом ПК папка может быть только для чтения)
    try:
        probe = os.path.join(paths.data_dir(), "_write_probe.tmp")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        add("запись в data", "ok")
    except Exception as e:
        add("запись в data", "ОШИБКА: %r" % (e,))

    try:
        from core import i18n
        add("локали найдены", ", ".join(sorted(i18n.available())) or "НЕТ (будут сырые ключи)")
        add("пример перевода", i18n.tr("app.title"))
    except Exception as e:
        add("локали", "ОШИБКА: %r" % (e,))

    try:
        from core import db, providers
        con = db.connect(paths.db_path())
        providers.seed(con)
        add("БД: ящиков", len(db.list_mailboxes(con)))
        add("БД: сервисов", len(db.list_providers(con)))
        add("БД: прокси", len(db.list_proxies(con, active_only=False)))
        con.close()
    except Exception as e:
        add("БД", "ОШИБКА: %r" % (e,))

    for mod in ("core.generator", "core.fingerprint", "core.proxy", "core.browser",
                "core.signup", "gui"):
        try:
            __import__(mod)
            add("модуль " + mod, "ok")
        except Exception as e:
            add("модуль " + mod, "ОШИБКА: %r" % (e,))

    try:
        import tkinter
        add("tkinter", tkinter.TkVersion)
    except Exception as e:
        add("tkinter", "ОШИБКА: %r" % (e,))

    try:
        import importlib.metadata as _md
        from playwright._impl._driver import compute_driver_executable
        try:
            add("playwright", _md.version("playwright"))
        except Exception:
            add("playwright", "установлен")
        res = compute_driver_executable()
        driver = res[0] if isinstance(res, (tuple, list)) else res
        add("драйвер (node)", "%s [%s]" % (driver, "есть" if os.path.isfile(str(driver)) else "НЕТ"))
    except Exception as e:
        add("playwright", "ОШИБКА: %r" % (e,))

    root, src = paths.setup_playwright_env()
    add("браузеры: путь", "%s (%s)" % (root or paths.browsers_root(), src))
    add("Chromium найден", paths.chromium_present())
    # Драйвер (node) должен не просто лежать, а РАБОТАТЬ: спрашиваем у него путь
    # к браузеру - это проверяет всю цепочку сборки, кроме самого запуска окна.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            add("chromium (через драйвер)", p.chromium.executable_path)
    except Exception as e:
        add("chromium (через драйвер)", "ОШИБКА: %r" % (e,))
    return lines


def check_browser():
    """Проверить, что Chromium из ЭТОЙ сборки реально запускается.
    Окно мелькнёт на пару секунд и закроется само."""
    import datetime
    from core import paths
    lines = []

    def add(key, val):
        lines.append("%-24s %s" % (key + ":", val))

    add("когда", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    add("frozen (это exe)", getattr(sys, "frozen", False))
    root, src = paths.setup_playwright_env()
    add("браузеры: путь", "%s (%s)" % (root or paths.browsers_root(), src))
    add("Chromium найден", paths.chromium_present())
    rc = 0
    if not paths.chromium_present():
        add("итог", "браузера нет - смотри ниже, что делать")
        lines.append("")
        lines.append(paths.chromium_help())
        rc = 1
    else:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                add("chromium exe", p.chromium.executable_path)
                # Запускаем ТАК ЖЕ, как в работе: видимым окном. Headless-бинарь
                # (ещё ~270 МБ) в комплект не входит, поэтому и проверять надо
                # тот браузер, которым реально работаем.
                b = p.chromium.launch(headless=False,
                                      args=["--no-first-run", "--no-default-browser-check"])
                add("версия браузера", b.version)
                pg = b.new_page()
                pg.goto("data:text/html,<title>ok</title><b>ok</b>")
                add("тестовая страница", (pg.title() or "") + " (загружена)" if pg.title() == "ok" else "loaded")
                b.close()
            add("итог", "браузер запускается из сборки - всё в порядке")
        except Exception as e:
            add("итог", "ОШИБКА: %r" % (e,))
            rc = 1
    text = "\n".join(lines) + "\n"
    out = os.path.join(paths.logs_dir(), "browser_check.txt")
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass
    say(text)
    say("отчёт: %s" % out)
    show_report(text, out)
    return rc


def install_browser():
    """Скачать Chromium тем драйвером, что лежит в сборке (нужен интернет)."""
    import subprocess
    from core import paths
    paths.setup_playwright_env()
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
    except Exception as e:
        say("драйвер Playwright недоступен: %r" % (e,))
        return 1
    res = compute_driver_executable()
    node, cli = (res[0], res[1]) if isinstance(res, (tuple, list)) else (res, None)
    cmd = [str(node)] + ([str(cli)] if cli else []) + ["install", "chromium"]
    say("Ставлю Chromium:", " ".join(cmd))
    try:
        rc = subprocess.call(cmd, env=get_driver_env())
    except Exception as e:
        say("не удалось запустить: %r" % (e,))
        return 1
    say("Готово, код:", rc)
    return rc


def already_running():
    """Не дать поднять вторую копию: две копии дерутся за одну базу, отсюда
    "database is locked" (окно открывается несколько секунд, и легко кликнуть ещё раз).

    Windows: именованный мьютекс - исчезает сам вместе с процессом, поэтому после
    падения или закрытия мусора не остаётся, в отличие от файла-замка.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetLastError(0)
        k.CreateMutexW(None, False, "MailboxManager_single_instance")
        return k.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return False


def warn_running():
    text = ("MailboxManager уже запущен.\n\n"
            "Окно открывается несколько секунд - подождите. "
            "Вторая копия не нужна: она мешает базе.")
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("MailboxManager", text)
        root.destroy()
    except Exception:
        say(text)


def check_service(args):
    """Видно ли НАШ сервис проверки прокси с ЭТОГО ПК.

    Зачем режим: на другом ПК «все прокси мёртвые» выглядит одинаково и когда
    пул выдохся, и когда до VPS нет маршрута (firewall/провайдер). Одна команда
    отвечает точно:

        MailboxManager.exe --check-service
        MailboxManager.exe --check-service 1.2.3.4:8080

    Второй вариант заодно проверяет конкретный адрес - тем же кодом, что и
    программа (автоопределение протокола + HTTPS-тест к Proton).
    """
    from core import proxy as proxy_mod
    from core import paths
    lines = []
    lines.append("сервис проверки: %s:%d  (имя в сертификате: %s)"
                 % (proxy_mod.CHECK_IP, proxy_mod.CHECK_PORT, proxy_mod.CHECK_SNI))
    res = proxy_mod.check(None)
    if res.get("ok"):
        cc = res.get("country") or res.get("code") or ""
        lines.append("СВЯЗЬ ЕСТЬ - этот ПК виден из интернета как %s%s"
                     % (res.get("ip", "?"), " (%s)" % cc if cc else ""))
        lines.append("время ответа: %d мс" % res.get("ms", 0))
    else:
        lines.append("СВЯЗИ НЕТ: %s" % res.get("error", "?"))
        lines.append("")
        lines.append("Значит проверка прокси на этом ПК покажет «все мёртвые» -")
        lines.append("и это будет неправдой: дело не в прокси, а в маршруте до")
        lines.append("%s. Проверь firewall/антивирус и доступ к 443." % proxy_mod.CHECK_IP)

    target = ""
    for i, a in enumerate(args):
        if a == "--check-service" and i + 1 < len(args) and not args[i + 1].startswith("-"):
            target = args[i + 1]
    if target:
        from core import db as db_mod
        p = db_mod.parse_proxy_line(target)
        lines.append("")
        if not p:
            lines.append("адрес не разобран: %s" % target)
        else:
            lines.append("--- проверяю прокси %s ---" % target)
            r2 = proxy_mod.check(p)
            if r2.get("ok"):
                lines.append("прокси ЖИВ - выходной адрес %s (%s), протокол %s, %d мс"
                             % (r2.get("ip"), r2.get("country") or "?", r2.get("kind") or "?", r2.get("ms", 0)))
                lines.append("HTTPS к Proton: %s" % ("прошёл (сертификат не подменён)"
                                                    if r2.get("tls") else "НЕ прошёл - для Proton не годится"))
            else:
                lines.append("прокси не работает: %s" % r2.get("error", "?"))

    text = "\n".join(lines)
    out = os.path.join(paths.logs_dir(), "check_service.txt")
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        out = None
    say(text)
    show_report(text, out)
    return 0


def main():
    soft_stdio()
    args = sys.argv[1:]
    if "--selftest" in args:
        from core import paths
        lines = selftest()
        out = os.path.join(paths.logs_dir(), "selftest.txt")
        text = "\n".join(lines) + "\n"
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
        say(text)
        say("отчёт: %s" % out)
        show_report(text, out)
        bad = any("ОШИБКА" in ln for ln in lines)
        return 1 if bad else 0
    if "--install-browser" in args:
        return install_browser()
    if "--check-browser" in args:
        return check_browser()
    if "--check-service" in args:
        return check_service(args)
    if "--cli" in args:
        import app
        sys.argv = ["app.py"] + args[args.index("--cli") + 1:]
        return app.main()
    if already_running():
        warn_running()
        return 0
    from gui import main as gui_main
    gui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
