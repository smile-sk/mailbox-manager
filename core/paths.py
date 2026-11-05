# -*- coding: utf-8 -*-
"""
core/paths.py - где лежат данные, журналы, переводы и браузеры.

В исходниках всё рядом с проектом. Но в собранном .exe (PyInstaller --onefile)
код распаковывается во ВРЕМЕННУЮ папку и стирается после выхода: путь, взятый
от __file__, увёл бы базу, профили и журналы в temp - то есть данные исчезали бы
после каждого запуска. Поэтому все «свои» пути берём только отсюда:

  app_dir()      - папка рядом с .exe (в исходниках - корень проекта)
  bundle_dir()   - откуда читать ресурсы, вшитые в сборку (в исходниках = app_dir)
  data_dir()     - данные: mailboxes.db, proxies.txt, profiles/
  logs_dir()     - журналы (app.log)
  locales_dirs() - где искать переводы: рядом с программой, затем внутри сборки
  browsers_dir() - browsers/ рядом с программой (перенос Chromium без установки)
  setup_playwright_env() - сказать Playwright, откуда брать браузеры
  chromium_present()     - есть ли распакованный Chromium

Проверить сборку целиком:  MailboxManager.exe --selftest  → logs/selftest.txt
"""
import os
import sys

# Запущены как собранный exe, а не из исходников?
FROZEN = bool(getattr(sys, "frozen", False))

PROXIES_TEMPLATE = """\
# ── Прокси для mailbox_manager ────────────────────────────────────────────────
# По одному адресу в строке. Формат - как удобно:
#     host:port                      (протокол определится сам: http → socks5 → socks4)
#     http://user:pass@host:port
#     socks5://host:port
# Строки, начинающиеся с #, пропускаются.
#
# Свой список (хоть 400 строк) можно вставить прямо сюда и перезапустить
# программу - она подхватит файл сама. Либо на вкладке «Прокси» нажать
# «Импорт из файла…» и выбрать любой .txt.
#
# Живость проверяется вживую (в том числе настоящим HTTPS к Proton), мёртвые
# адреса отсеиваются, а занятые другим ящиком - не переиспользуются.
"""


def app_dir():
    """Папка, рядом с которой живут данные: папка .exe (или корень проекта)."""
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundle_dir():
    """Откуда читать вшитые в сборку ресурсы (в исходниках - сам проект)."""
    if FROZEN:
        return getattr(sys, "_MEIPASS", app_dir())
    return app_dir()


def _ensure(path):
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def data_dir():
    return _ensure(os.path.join(app_dir(), "data"))


def logs_dir():
    return _ensure(os.path.join(app_dir(), "logs"))


def profiles_dir():
    return os.path.join(data_dir(), "profiles")


def db_path():
    return os.path.join(data_dir(), "mailboxes.db")


def settings_path():
    return os.path.join(data_dir(), "settings.json")


def proxies_path():
    return os.path.join(data_dir(), "proxies.txt")


def locales_dirs():
    """Папки с переводами по порядку поиска: сначала рядом с программой (туда
    можно положить свой en.json), потом внутри сборки (если перевод вшит)."""
    out = []
    for d in (os.path.join(app_dir(), "locales"),
              os.path.join(bundle_dir(), "locales")):
        if d not in out:
            out.append(d)
    return out


def ensure_proxies_file():
    """Создать data/proxies.txt с подсказкой, если файла ещё нет: человеку
    должно быть куда вставлять свой список, не заглядывая в документацию."""
    path = proxies_path()
    if not os.path.isfile(path):
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(PROXIES_TEMPLATE)
        except OSError:
            pass
    return path


def browsers_dir():
    """Папка с браузерами рядом с программой (её можно просто перенести)."""
    return os.path.join(app_dir(), "browsers")


def _default_browsers_root():
    """Стандартный кэш Playwright, если своей папки нет."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ms-playwright")
    return os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")


def browsers_root():
    """Откуда Playwright берёт браузеры СЕЙЧАС."""
    return os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or _default_browsers_root()


def setup_playwright_env(log=None):
    """Указать Playwright, где искать браузеры (вызывать ДО запуска Chromium).

    Если рядом с программой лежит папка browsers/ - работаем из неё: тогда на
    другом ПК ничего ставить не надо, достаточно скопировать папку. Иначе
    остаётся стандартный путь Playwright (%LOCALAPPDATA%\\ms-playwright).
    Папку, заданную переменной окружения вручную, не трогаем.

    Возвращает (путь или None, откуда именно).
    """
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        return env, "задано переменной окружения"
    b = browsers_dir()
    if os.path.isdir(b) and os.listdir(b):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = b
        (log or (lambda *_a, **_k: None))(
            "[браузер] браузеры беру из папки рядом с программой: %s" % b)
        return b, "папка программы"
    return None, "стандартный кэш Playwright"


def chromium_present(root=None):
    """Есть ли в папке браузеров распакованный Chromium (chromium-XXXX)."""
    root = root or browsers_root()
    if not root or not os.path.isdir(root):
        return False
    try:
        return any(n.startswith("chromium-") for n in os.listdir(root))
    except OSError:
        return False


def headless_present(root=None):
    """Есть ли отдельный headless-браузер Playwright (chrome-headless-shell).

    В комплект для другого ПК он не входит (ещё ~270 МБ): программа работает
    видимым окном, так надёжнее и для капчи. Нужен, только если явно просят
    headless (флаг --headless в консольном режиме)."""
    root = root or browsers_root()
    if not root or not os.path.isdir(root):
        return False
    try:
        return any(n.startswith("chromium_headless_shell-") for n in os.listdir(root))
    except OSError:
        return False


def chromium_help():
    """Понятный текст, если браузера для Playwright нет."""
    return ("Не нашла Chromium, которым Playwright открывает сайты.\n\n"
            "Два пути:\n"
            "1) проще всего - скопировать папку browsers с того ПК, где "
            "программа уже работает (готовый комплект ~430 МБ), рядом с "
            "программой;\n"
            "2) либо запустить:  MailboxManager.exe --install-browser  "
            "(скачает Chromium, ~150 МБ, нужен интернет).\n\n"
            "Искала в: %s\n"
            "env BrowsersPath: %s" % (browsers_root(), browsers_dir()))
