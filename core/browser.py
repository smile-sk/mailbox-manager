# -*- coding: utf-8 -*-
"""
browser.py - «открыть ящик в браузере».

Задел, заложенный сразу:
  • у каждого ящика СВОЯ папка профиля с именем mb<id> (данные/куки/отпечаток
    не смешиваются). Ключ - ИМЕННО id ящика, а не email: при регистрации адрес
    меняется (занятое имя → следующий кандидат), а профиль по email терялся
    вместе с сессией (к 11.09 так накопилось 13 папок-сирот);
  • прокси закрепляется ЗА ЯЩИКОМ: первый раз берётся из пула (таблица proxies)
    и запоминается в browser_profiles.proxy_id - дальше у ящика всегда один и
    тот же адрес. Разные ящики = разные адреса, но у одного ящика адрес не
    «прыгает» (менять выходной IP на каждом запуске хуже, чем не менять);
  • у каждого ящика СВОЙ отпечаток браузера (core/fingerprint.py), считается
    детерминированно от id: экран/окно, ядра, память, GPU в WebGL, шум canvas;
  • профиль подчищается до минимума: кэши сносятся (сессия и куки остаются)
    при каждом открытии и при старте программы, а с удалением ящика профиль
    сносится целиком.

Честно: это не «антидетект-браузер» и не попытка кого-то обмануть - просто
разные ящики не выглядят одним и тем же браузером.
"""
import os
import re
import shutil
import sys
import time

from core import db
from core import fingerprint
from core import paths
from core import proxy as proxy_mod

PROFILES_ROOT = paths.profiles_dir()


def profile_dir_for(key):
    """Папка профиля ящика. Ключ - id ящика (mb13), а не email: адрес при
    регистрации меняется, а профиль обязан это переживать (сессия, куки).
    Не-числовой ключ (старые вызовы) санитайзится, как раньше."""
    if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
        safe = "mb%s" % key
    else:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(key))
    return os.path.join(PROFILES_ROOT, safe)


def register_profile(con, mailbox_id, email, proxy_id=None):
    """Запомнить, что у ящика своя папка профиля и (возможно) свой прокси.
    email - человеческое имя ящика (для карточки), папка зовётся mb<id>.
    Если запись вела на старую (email) папку - ПЕРЕНОСИМ её, а не заводим
    пустую: сессия и куки остаются живыми."""
    d = profile_dir_for(mailbox_id)
    row = con.execute("SELECT profile_dir FROM browser_profiles WHERE mailbox_id=?",
                      (mailbox_id,)).fetchone()
    old = row["profile_dir"] if row is not None else None
    if old and os.path.abspath(old) != os.path.abspath(d) and os.path.isdir(old):
        try:
            os.makedirs(PROFILES_ROOT, exist_ok=True)
            if os.path.isdir(d):
                shutil.rmtree(old, ignore_errors=True)
            else:
                shutil.move(old, d)
        except Exception:
            pass
    con.execute(
        """INSERT INTO browser_profiles(mailbox_id, profile_dir, proxy_id, last_used)
           VALUES(?,?,?,?)
           ON CONFLICT(mailbox_id) DO UPDATE SET
             profile_dir=excluded.profile_dir,
             proxy_id=COALESCE(excluded.proxy_id, browser_profiles.proxy_id),
             last_used=excluded.last_used""",
        (mailbox_id, d, proxy_id, db.now()),
    )
    con.commit()
    return d


def orphan_profiles(con):
    """Имена папок профилей, на которые НЕ ссылается база (сироты: ящик удалён
    или переименован). Пустая база ящиков - возвращаем пусто: на сброшенной базе
    диск не трогаем, иначе снесло бы всё подряд."""
    try:
        if con.execute("SELECT COUNT(*) FROM mailboxes").fetchone()[0] == 0:
            return []
        known = {os.path.abspath(r["profile_dir"])
                 for r in con.execute("SELECT profile_dir FROM browser_profiles").fetchall()}
    except Exception:
        return []
    if not os.path.isdir(PROFILES_ROOT):
        return []
    out = []
    for name in sorted(os.listdir(PROFILES_ROOT)):
        full = os.path.join(PROFILES_ROOT, name)
        if os.path.isdir(full) and os.path.abspath(full) not in known:
            out.append(name)
    return out


def migrate_profiles(con, log=None):
    """Привести профили в порядок (зовётся при старте GUI):
      1) папка каждого ящика зовётся mb<id> - старые email-папки переносим;
      2) папки без хозяина в базе - удаляем.
    Возвращает (переехало, удалено)."""
    log = log or (lambda *_a, **_k: None)
    moved = removed = 0
    try:
        rows = con.execute("SELECT id, email FROM mailboxes").fetchall()
    except Exception:
        return 0, 0
    for r in rows:
        want = profile_dir_for(r["id"])
        row = con.execute("SELECT profile_dir FROM browser_profiles WHERE mailbox_id=?",
                          (r["id"],)).fetchone()
        old = row["profile_dir"] if row is not None else None
        register_profile(con, r["id"], r["email"])
        if old and os.path.abspath(old) != os.path.abspath(want) and os.path.isdir(want):
            moved += 1
            log("[профили] ящик #%d: папка %s → %s"
                % (r["id"], os.path.basename(old), os.path.basename(want)))
    # Кэши живых профилей подчищаем при каждом старте: профиль не должен
    # вырастать до гигабайтов (сессия, куки и вход при этом остаются).
    freed = 0
    for r in rows:
        freed += prune_profile(profile_dir_for(r["id"]))
    if freed > 5 * 1024 * 1024:
        log("[профили] кэш подчищен: освобождено %.1f МБ" % (freed / 1048576.0))

    for name in orphan_profiles(con):
        try:
            shutil.rmtree(os.path.join(PROFILES_ROOT, name), ignore_errors=True)
            removed += 1
            log("[профили] удалена папка-сирота: %s" % name)
        except Exception:
            pass
    return moved, removed


# Что в профиле - мусор (кэши, GPU-хранилища, краш-дампы, телеметрия), а что
# НЕЛЬЗЯ трогать (иначе каждый вход просил бы логин заново):
#   Default/Network/Cookies, Local State, Default/Preferences,
#   Default/Local Storage, Default/Session Storage, Default/IndexedDB.
_CACHE_DIRS = (
    "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
    "GPUPersistentCache", "GraphiteDawnCache", "DawnGraphiteCache",
    "DawnWebGPUCache", "CacheStorage", "Media Cache", "CachedData",
    "component_crx_cache", "extensions_crx_cache", "Crashpad",
    "segmentation_platform", "OptimizationGuideOnDeviceModel",
    "Safe Browsing", "Variations", "MEIPreload", "Zygote",
)
_PRUNE_MIN_LOG = 5 * 1024 * 1024   # сообщать в журнал от 5 МБ


def profile_size(path):
    """Размер папки профиля в байтах."""
    total = 0
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except Exception:
                pass
    return total


def prune_profile(path, log=None):
    """Подчистить профиль до минимума. Возвращает, сколько байт освободили.
    Сносим только кэши/телеметрию - сессию и куки не трогаем."""
    log = log or (lambda *_a, **_k: None)
    if not os.path.isdir(path):
        return 0
    before = profile_size(path)
    freed = 0
    for dp, dirs, files in os.walk(path, topdown=False):
        for name in list(dirs):
            if name in _CACHE_DIRS:
                full = os.path.join(dp, name)
                try:
                    freed += profile_size(full)
                    shutil.rmtree(full, ignore_errors=True)
                except Exception:
                    pass
        for name in list(files):
            if name.startswith("BrowserMetrics") or name.endswith(".pma"):
                try:
                    freed += os.path.getsize(os.path.join(dp, name))
                    os.remove(os.path.join(dp, name))
                except Exception:
                    pass
    freed = max(freed, before - profile_size(path))
    if freed >= _PRUNE_MIN_LOG:
        log("[браузер] кэш профиля подчищен: освобождено %.1f МБ" % (freed / 1048576.0))
    return freed


def remove_profile(con, mailbox_id, log=None):
    """Удалить папку профиля ящика (зовётся при удалении ящика).
    Прокси ящика освобождается: запись профиля тоже сносится."""
    log = log or (lambda *_a, **_k: None)
    row = None
    try:
        row = con.execute("SELECT profile_dir FROM browser_profiles WHERE mailbox_id=?",
                          (mailbox_id,)).fetchone()
    except Exception:
        pass
    path = row["profile_dir"] if row is not None else profile_dir_for(mailbox_id)
    if path and os.path.isdir(path):
        size = profile_size(path)
        shutil.rmtree(path, ignore_errors=True)
        log("[браузер] папка профиля удалена (%.1f МБ)" % (size / 1048576.0))
    try:
        con.execute("DELETE FROM browser_profiles WHERE mailbox_id=?", (mailbox_id,))
        con.commit()
    except Exception:
        pass
    return path


def screen_metrics():
    """(рабочая область в DIP, масштаб DPI) - или (None, 1.0).

    Windows считает габариты ОКНА в DIP, а не в физических пикселях: при
    масштабе 125% экран 1920x1080 - это 1536x864 DIP, и окно «1920x1080»
    система просто ОБРЕЗАЕТ до рабочей области. Тогда страница рендерится
    шире окна, правая часть не видна и кликнуть по ней нельзя. Поэтому меряем
    рабочую область (без панели задач) и делим на масштаб."""
    if sys.platform != "win32":
        return None, 1.0
    try:
        import ctypes
        u = ctypes.windll.user32

        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        scale = 1.0
        try:
            dpi = int(u.GetDpiForSystem())
            if dpi > 0:
                scale = dpi / 96.0
        except Exception:
            scale = 1.0
        r = _RECT()
        if u.SystemParametersInfoW(48, 0, ctypes.byref(r), 0):   # SPI_GETWORKAREA
            w = int((r.right - r.left) / scale)
            h = int((r.bottom - r.top) / scale)
            if w > 0 and h > 0:
                return (w, h), scale
        w, h = int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
        if w > 0 and h > 0:
            return (int(w / scale), int(h / scale)), scale
    except Exception:
        pass
    return None, 1.0


def real_screen():
    """Рабочая область экрана в DIP - для отпечатка (окно не больше экрана)."""
    return screen_metrics()[0]


def _fit_viewport(page, fp, log=None):
    """Только замер для журнала - размеры НЕ меняет.

    Раньше функция «подгоняла страницу под окно» и с каждым вызовом (старт +
    каждая новая вкладка) окно ужималось: измеряли обрезанное окно и снова
    уменьшали страницу - окно съёживалось до микро-рамки в углу (13.09.2026).
    Теперь страница = окно (launch_kwargs['no_viewport'] = True), подгонять
    нечего; функция осталась, чтобы предупредить, если окно реально мало."""
    try:
        ow, oh, iw, ih = [int(v) for v in page.evaluate(
            "() => [window.outerWidth, window.outerHeight,"
            " window.innerWidth, window.innerHeight]")]
    except Exception:
        return None
    if iw < 900 or ih < 550:
        (log or (lambda *_a, **_k: None))(
            "[браузер] окно мало (%dx%d) - разверни его, часть интерфейса "
            "может быть не видна" % (iw, ih))
    return iw, ih




def _launch(user_data_dir, proxy_pw=None, headless=False, fp=None, log=None):
    """Внутренний запуск Playwright. Возвращает (playwright, context, page).
    fp - отпечаток из core.fingerprint (экран, окно, ориентация, GPU…)."""
    # Браузеры ищем там, где они реально лежат (рядом с программой или в кэше
    # Playwright): на другом ПК своей папки может не быть - скажем об этом
    # понятным текстом, а не «Executable doesn't exist».
    paths.setup_playwright_env(log)
    if not paths.chromium_present():
        raise RuntimeError(paths.chromium_help())
    # Headless-браузер (chrome-headless-shell) в комплект не входит: работаем
    # видимым окном. Если его попросили (--headless в консоли), а бинаря нет -
    # открываем окно, а не падаем с «Executable doesn't exist».
    if headless and not paths.headless_present():
        (log or (lambda *_a, **_k: None))(
            "[браузер] headless-браузера в комплекте нет - открываю видимым окном")
        headless = False
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as e:      # в собранной версии быть не должно
        raise RuntimeError("Playwright недоступен в этой сборке: %s" % e)

    fp = fp or {}
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    win = fp.get("window") or (None, None)
    if win[0] and win[1]:
        args.append("--window-size=%d,%d" % (win[0], win[1]))
    else:
        args.append("--start-maximized")

    launch_kwargs = dict(headless=headless, args=args)
    if fp:
        # Страница = окно: Playwright НЕ эмулирует «страницу меньше окна».
        # При эмуляции она рисуется маленьким прямоугольником в углу окна,
        # а подгонка «под окно» ужимала само окно с каждым вызовом.
        # Окно задаётся --window-size из отпечатка и влезает в рабочую
        # область, поэтому страница совпадает с ним сама.
        launch_kwargs["no_viewport"] = True
        launch_kwargs["screen"] = fp.get("screen")
        # device_scale_factor несовместим с no_viewport (Playwright ругается
        # «not supported with null viewport») - масштаб берём системный:
        # при 125% это честные 1.25, как у живого человека.
        launch_kwargs["color_scheme"] = "light"
        launch_kwargs["locale"] = (fp.get("langs") or ["ru-RU"])[0]
    if proxy_pw:
        launch_kwargs["proxy"] = proxy_pw

    p = sync_playwright().start()
    try:
        ctx = p.chromium.launch_persistent_context(user_data_dir=user_data_dir,
                                                  **launch_kwargs)
    except TypeError:
        # старый Playwright не знает параметра screen - обходимся без него
        launch_kwargs.pop("screen", None)
        if launch_kwargs.pop("no_viewport", None):
            launch_kwargs["viewport"] = fp.get("viewport")
            launch_kwargs["device_scale_factor"] = 1
        ctx = p.chromium.launch_persistent_context(user_data_dir=user_data_dir,
                                                  **launch_kwargs)
    if fp:
        try:
            ctx.add_init_script(fingerprint.init_script(fp))
        except Exception as e:
            (log or (lambda *_a, **_k: None))("[браузер] отпечаток не применён: %s" % e)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    _fit_viewport(page, fp, log)   # только замер для журнала
    return p, ctx, page


def _js_set_value(page, loc, value):
    try:
        return bool(page.evaluate("""(args) => {
            const [el, v] = args;
            const proto = el instanceof HTMLTextAreaElement
                ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }""", [loc.element_handle(), value]))
    except Exception:
        return False


_LOGIN_SELECTORS = [
    "#username", "input[name=username]", "input[autocomplete=username]",
    "input[type=email]", "input[name=login]", "input[name=email]", "#email",
]
_PASS_SELECTORS = [
    "input[type=password]", "#password", "input[name=password]",
]


def _fill_first(page, selectors, value):
    """Заполнить первое ПОДХОДЯЩЕЕ поле. Proton держит в DOM скрытые
    копии полей (off-screen ловушки) - поэтому перебираем все совпадения
    и берём видимое/редактируемое, а не просто .first."""
    for sel in selectors:
        try:
            loc_all = page.locator(sel)
            n = loc_all.count()
        except Exception:
            continue
        for i in range(min(n, 5)):
            loc = loc_all.nth(i)
            try:
                if not loc.is_visible() or not loc.is_editable():
                    continue
                loc.fill(value, timeout=1500)
                try:
                    if (loc.input_value() or "") == value:
                        return True
                except Exception:
                    return True
            except Exception:
                continue
    # запасной путь: нативный JS-сеттер (React-формы не всегда видят fill)
    for sel in selectors:
        try:
            loc_all = page.locator(sel)
            n = loc_all.count()
        except Exception:
            continue
        for i in range(min(n, 5)):
            loc = loc_all.nth(i)
            try:
                if loc.is_visible() and _js_set_value(page, loc, value):
                    return True
            except Exception:
                continue
    return False


# На случай, если сервис спрашивает логин и пароль разными шагами.
# «Войти» тут СОЗНАТЕЛЬНО нет: жать вход с пустым паролем нельзя.
_CONTINUE_TEXTS = ["Далее", "Продолжить", "Continue", "Next"]


def _click_continue(page):
    for t in _CONTINUE_TEXTS:
        try:
            loc = page.get_by_role("button", name=t, exact=False).first
            if loc.count() and loc.is_visible() and loc.is_enabled():
                loc.click(timeout=1500)
                return True
        except Exception:
            continue
    return False


# Каким логином входить - зависит от сервиса. Proton принимает и локальную
# часть, и адрес целиком. Tuta - ТОЛЬКО полный адрес (example@tutamail.com):
# с одной локальной частью поле подсвечивается ошибкой и вход не проходит
# (живой случай 19.09.2026 - «подставляй полностью почту, не только логин»).
_FULL_EMAIL_FAMILIES = ("tuta", "microsoft", "outlook")
_FULL_EMAIL_DOMAINS = ("tuta.com", "tutanota.com", "tutanota.de", "tuta.io",
                       "tutamail.com", "keemail.me")
_LOCAL_ONLY_FAMILIES = ("proton",)


def _login_style(mailbox):
    """'email' - входить полным адресом, 'login' - локальной частью,
    'auto' - решить по самому полю входа (type=email или подсказка с «@»)."""
    fam = (mailbox.get("family") or "").lower()
    email = mailbox.get("email") or ""
    dom = email.split("@")[-1].lower() if "@" in email else ""
    for probe in ((mailbox.get("provider") or ""), (mailbox.get("login_url") or "")):
        low = probe.lower()
        if "tuta" in low:
            fam = "tuta"
            break
        if "proton" in low:
            fam = "proton"
            break
    if fam in _FULL_EMAIL_FAMILIES or dom in _FULL_EMAIL_DOMAINS:
        return "email"
    if fam in _LOCAL_ONLY_FAMILIES:
        return "login"
    return "auto"


def _visible_login_field(page):
    """Первое ВИДИМОЕ поле логина. Proton держит в DOM скрытые копии-ловушки,
    поэтому перебираем совпадения, а не берём первое."""
    for sel in _LOGIN_SELECTORS:
        try:
            loc_all = page.locator(sel)
            n = loc_all.count()
        except Exception:
            continue
        for i in range(min(n, 5)):
            loc = loc_all.nth(i)
            try:
                if loc.is_visible() and loc.is_editable():
                    return loc
            except Exception:
                continue
    return None


def _field_wants_email(loc):
    """Поле само просит полный адрес? (type=email, подсказка вида you@… или
    name/id со словом email)."""
    try:
        typ = (loc.get_attribute("type") or "").lower()
        ph = loc.get_attribute("placeholder") or ""
        nm = "%s %s" % (loc.get_attribute("name") or "", loc.get_attribute("id") or "")
        if typ == "email" or "@" in ph or "email" in nm.lower():
            return True
    except Exception:
        pass
    return False


def _fill_login(page, value):
    """Заполнить поле логина ИМЕННО этим значением (fill, при неудаче -
    нативный JS-сеттер, как в _fill_first)."""
    loc = _visible_login_field(page)
    if loc is None:
        return False
    try:
        loc.fill(value, timeout=1500)
        try:
            return (loc.input_value() or "") == value
        except Exception:
            return True
    except Exception:
        pass
    if _js_set_value(page, loc, value):
        return True
    return _fill_first(page, _LOGIN_SELECTORS, value)


def _prefill_credentials(page, mailbox, log):
    """Подставить логин и пароль, НЕ нажимая «Войти» (пользователь жмёт сам).

    Логин выбирается по сервису: Proton - локальная часть адреса, Tuta - адрес
    целиком. Для незнакомого сервиса смотрим на само поле входа.
    Ждём поля до ~20 с: SPA грузится с задержкой, Proton умеет редиректить
    с inbox на login.
    """
    email = (mailbox.get("email") or "").strip()
    local = (mailbox.get("login") or "").strip() or (email.split("@")[0] if email else "")
    password = mailbox.get("password") or ""
    style = _login_style(mailbox)
    deadline = time.time() + 20
    nudged = False
    value = None
    while time.time() < deadline:
        if value is None:
            loc = _visible_login_field(page)
            if loc is not None:
                want_email = (style == "email") or (style == "auto" and _field_wants_email(loc))
                value = email if (want_email and email) else (local or email)
                if value == email:
                    log("[браузер] логин: полный адрес %s (%s принимает только адрес "
                        "целиком)" % (email, mailbox.get("provider") or "сервис"))
                else:
                    log("[браузер] логин: %s" % value)
        ok_u = _fill_login(page, value) if value else False
        ok_p = _fill_first(page, _PASS_SELECTORS, password)
        if ok_u and ok_p:
            log("[браузер] логин и пароль подставлены - нажми «Войти»")
            return True
        # логин есть, а пароля на экране нет: возможно, это второй шаг формы
        if ok_u and not ok_p and not nudged:
            nudged = _click_continue(page)
        time.sleep(1)
    log("[браузер] автоподстановка полей не удалась (их нет или вход уже "
        "выполнен) - если надо, введи данные вручную")
    return False


# Сколько раз перезапустить браузер, если прокси не пустил (бесплатные
# адреса умирают за минуты, а сокет-проверка этого не всегда видит).
_PROXY_TRIES = 4
_GOTO_MS = 90000          # без прокси: ждём загрузку долго
_GOTO_MS_PROXY = 30000    # с прокси: не ждём минутами - лучше взять другой

# Ошибки Chromium, которые означают «не пустил ПРОКСИ», а не «сайт лежит».
_PROXY_ERR_MARKS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_SOCKS_CONNECTION_FAILED", "ERR_SOCKS_CONNECTION_HOST_UNREACHABLE",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_MANDATORY_PROXY_CONFIGURATION_FAILED",
    "ERR_TIMED_OUT", "TIMEOUT",
    # Сертификат подсунул ПРОКСИ (MITM): нормальный прокси TLS не трогает, а
    # бесплатные иногда подменяют сертификат своим. Для сайтов с HSTS (Proton)
    # это «Подключение не защищено» и на сайт не попасть. Раньше это считалось
    # «дело не в прокси» - окно открывалось с ошибкой, а адрес-вредитель
    # ОСТАВАЛСЯ закреплён за ящиком и ломал все следующие запуски (12.09.2026).
    "ERR_CERT_", "ERR_SSL_PROTOCOL_ERROR", "ERR_BAD_SSL_CLIENT_AUTH_CERT",
)

# Эти ошибки означают не «прокси умер», а «прокси подменил сертификат» (читает
# твой HTTPS) - такой адрес гасим сразу. Только ЯВНЫЕ признаки подмены: при
# сбитых часах машины Chromium тоже ругается на сертификат, но это не вина
# прокси, и гасить живые адреса за это нельзя (просто берём другой).
_CERT_ERR_MARKS = ("ERR_CERT_AUTHORITY_INVALID", "ERR_CERT_COMMON_NAME_INVALID")


def _proxy_transport_error(text):
    """Страница не загрузилась ИЗ-ЗА ПРОКСИ? (Тогда адрес меняем.)"""
    t = (text or "").upper()
    return any(m in t for m in _PROXY_ERR_MARKS)


def _proxy_cert_error(text):
    """Прокси подменил сертификат (MITM)?"""
    t = (text or "").upper()
    return any(m in t for m in _CERT_ERR_MARKS)


def _close(p, ctx):
    """Закрыть контекст и драйвер, не падая на None."""
    try:
        if ctx is not None:
            ctx.close()
    except Exception:
        pass
    try:
        if p is not None:
            p.stop()
    except Exception:
        pass


def open_mailbox_browser(con, mailbox, proxy=None, headless=False,
                         auto_url=None, wait="enter", log=print, prefill=False):
    """
    Открыть почтовый веб-интерфейс ящика в чистом профиле.
      proxy: None = случайный из пула (если пул пуст - без прокси);
             'none' = принудительно без прокси; Row = конкретный.
      wait:  'enter' - консоль ждёт Enter (старое CLI-поведение);
             'window' - жить, пока пользователь не закроет окно браузера
                        (нужно GUI/подпроцессу).
    Возвращает (прокси-строка, профиль-папка) после закрытия.
    """
    # mailbox может прийти как sqlite3.Row - у Row нет метода .get(),
    # из-за чего автоподстановка падала ('sqlite3.Row' object has no
    # attribute 'get'). Приводим к обычному словарю.
    if not isinstance(mailbox, dict):
        try:
            mailbox = dict(mailbox)
        except Exception:
            pass

    # ПРОКСИ проверяется ДО запуска браузера: мёртвый адрес = белый экран и
    # потерянное время. Профиль регистрируем первым - запись нужна, чтобы
    # закрепить живой адрес именно за этим ящиком.
    profile_dir = register_profile(con, mailbox["id"], mailbox["email"])
    # Режим прокси, выбранный вручную в окне: «без прокси» - этому ящику прокси
    # не нужен вообще, что бы ни лежало в пуле.
    if proxy is None:
        try:
            _prof = db.get_profile(con, mailbox["id"])
            if _prof is not None and _prof["proxy_mode"] == "none":
                proxy = "none"
                log("[браузер] для ящика выбран режим «без прокси» - иду напрямую")
        except Exception:
            pass
    os.makedirs(profile_dir, exist_ok=True)
    prune_profile(profile_dir, log)
    fp = fingerprint.for_mailbox(mailbox["id"], max_screen=real_screen())
    url = auto_url or mailbox["login_url"] or mailbox["provider"]
    log(f"[браузер] ящик {mailbox['email']}")
    log(f"[браузер] профиль: {profile_dir}")
    log("[браузер] отпечаток ящика: %s" % fp["label"])

    # Подбор прокси и запуск. Живой адрес проверяется ДО запуска (мёртвый - это
    # белый экран и потерянное время), но проверка на сокетах не гарантия: у
    # бесплатных прокси адрес может умереть через минуту. Поэтому если прокси
    # не пустил САМ БРАУЗЕР - помечаем сбой, снимаем адрес с ящика и берём
    # следующий живой, а не оставляем пользователя с пустым окном.
    tried_ids = []
    p = ctx = page = None
    fixed_proxy = proxy not in (None, "none")
    for attempt in range(1, _PROXY_TRIES + 1):
        if proxy == "none":
            # сознательно без прокси: снимаем прежнее закрепление, если было
            proxy_id, proxy_pw = None, None
            db.set_profile_proxy(con, mailbox["id"], None, mode="none")
            proxy_desc = proxy_mod._direct(log)
        elif proxy is None:
            # Живой прокси для ящика: свой закреплённый (он не меняется - иначе у
            # одного ящика «прыгал» бы IP), а если мёртв - следующий свободный.
            p_row, proxy_pw, proxy_desc = proxy_mod.resolve_for_mailbox(
                con, mailbox["id"], log=log, exclude_ids=tried_ids)
            proxy_id = p_row["id"] if p_row is not None else None
        else:
            # прокси задан явно (CLI/выбор вручную) - проверяем и его
            proxy_id = proxy["id"]
            proxy_pw = db.proxy_to_playwright(proxy)
            res = proxy_mod.check_row(con, proxy, log)
            proxy_desc = proxy_mod.describe(proxy, res)
            db.set_profile_proxy(con, mailbox["id"], proxy_id,
                                 res.get("ip", ""), res.get("country", ""))

        log(f"[браузер] прокси:   {proxy_desc}")
        log(f"[браузер] открываю {url}")
        log("[браузер] запускаю Chromium - первый старт занимает несколько секунд…")
        p, ctx, page = _launch(profile_dir, proxy_pw, headless=headless, fp=fp, log=log)
        fail = ""
        try:
            page.goto(url, wait_until="domcontentloaded",
                      timeout=_GOTO_MS_PROXY if proxy_pw else _GOTO_MS)
        except Exception as e:
            fail = str(e)
        if not fail:
            break
        log(f"[браузер] предупреждение: не дождалась загрузки страницы: {fail}")
        if not (proxy_pw and _proxy_transport_error(fail)):
            break                      # дело не в прокси (сайт/сеть) - не крутим
        _close(p, ctx)
        p = ctx = page = None
        if fixed_proxy:
            break                      # адрес задан вручную - менять не на что
        if proxy_id:
            tried_ids.append(proxy_id)
            cert = _proxy_cert_error(fail)
            if cert:
                log("[браузер] прокси #%s подсовывает СВОЙ сертификат (читает "
                    "HTTPS) - гашу его совсем, беру другой" % proxy_id)
            proxy_mod.mark_browser_fail(con, proxy_id, fail, log, kill=cert)
            db.set_profile_proxy(con, mailbox["id"], None)
        if attempt < _PROXY_TRIES:
            log("[браузер] прокси не пустил браузер - беру следующий живой")
            time.sleep(1)
    log("[браузер] окно открыто.")

    if prefill:
        try:
            if mailbox.get("password"):
                _prefill_credentials(page, mailbox, log)
            else:
                log("[браузер] у ящика нет сохранённого пароля - введи вручную")
        except Exception as e:
            log("[браузер] автоподстановка данных не удалась: %s" % e)

    if wait == "enter":
        try:
            if sys.stdin and sys.stdin.isatty():
                input("[браузер] Закрыть - Enter…")
        except Exception:
            pass
    else:
        # Ждём, пока пользователь не закроет все вкладки. Заодно следим за
        # окном и вкладками: система может обрезать окно, пользователь может
        # его развернуть - подгоняем страницу под реальный размер (иначе часть
        # страницы не видна и по ней нельзя кликнуть). Новые вкладки тоже
        # подгоняем и пишем в журнал, чтобы было видно, что грузится.
        seen = {}
        try:
            while ctx.pages:
                for pg in ctx.pages:
                    try:
                        m = pg.evaluate("() => [window.outerWidth, window.outerHeight]")
                        url = pg.url
                    except Exception:
                        continue
                    key = (int(m[0]), int(m[1]), url)
                    if seen.get(id(pg)) != key:
                        seen[id(pg)] = key
                        if url and url != "about:blank":
                            log("[браузер] вкладка: %s" % url)
                time.sleep(1.0)
        except Exception:
            pass

    _close(p, ctx)
    # после закрытия браузера файлы свободны - подчищаем кэш сразу,
    # чтобы профиль не копил сотни мегабайт между запусками
    prune_profile(profile_dir, log)
    return proxy_desc, profile_dir
