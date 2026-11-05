# -*- coding: utf-8 -*-
"""
db.py - SQLite-хранилище менеджера почтовых ящиков.

Таблицы:
  providers         - реестр почтовых сервисов (что умеем создавать/читать)
  proxies           - список прокси пользователя (пул)
  mailboxes         - созданные ящики: логин/пароль/персона/статус
  letters           - письма, вычитанные по IMAP, с выделенным кодом
                      (это консольные команды `mail`/`codes`; в GUI IMAP нет)
  browser_profiles  - какая папка профиля браузера у какого ящика и через какой
                      прокси; папка зовётся mb<id ящика>, а НЕ по email: адрес
                      при регистрации меняется, а сессия должна пережить это

Всё стандартным sqlite3 - никаких внешних зависимостей.
"""
import datetime
import json
import os
import re
import sqlite3

# Сколько сбоев проверки подряд - и прокси выключается сам
# (включить обратно можно на вкладке «Прокси»).
PROXY_FAILS_TO_DISABLE = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,      -- 'web.de', 'mail.com', 'abv.bg', ...
    family      TEXT NOT NULL DEFAULT '',  -- 'united_internet', 'outlook', 'own'
    country     TEXT NOT NULL DEFAULT '',  -- 'DE', 'BG', 'BE', 'global', ...
    signup_url  TEXT NOT NULL DEFAULT '',
    login_url   TEXT NOT NULL DEFAULT '',
    imap_host   TEXT NOT NULL DEFAULT '',
    imap_port   INTEGER NOT NULL DEFAULT 993,
    domains     TEXT NOT NULL DEFAULT '[]',   -- json: список доменов на выбор (если есть)
    pass_min    INTEGER NOT NULL DEFAULT 8,
    pass_special INTEGER NOT NULL DEFAULT 0,  -- 1 = спецсимвол обязателен
    reg_hint    TEXT NOT NULL DEFAULT '',     -- заметка о нюансах регистрации
    driver      TEXT NOT NULL DEFAULT '',     -- 'proton' и т.п.: автопилот регистрации
    enabled     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS proxies(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL DEFAULT 'http',  -- http | socks5
    host        TEXT NOT NULL,
    port        INTEGER NOT NULL,
    login       TEXT NOT NULL DEFAULT '',
    pass        TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    fail_count  INTEGER NOT NULL DEFAULT 0,
    last_used   TEXT,
    exit_ip     TEXT NOT NULL DEFAULT '',   -- выходной адрес (из проверки живости)
    exit_country TEXT NOT NULL DEFAULT '',  -- страна прокси
    checked_at  TEXT,                       -- когда проверяли в последний раз
    https_ok    INTEGER NOT NULL DEFAULT 0, -- 1 = проверен настоящим HTTPS к Proton
                                            -- (сертификат не подменён); 0 = не проверен/не годится
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS mailboxes(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL REFERENCES providers(id),
    email       TEXT UNIQUE NOT NULL,
    login       TEXT NOT NULL DEFAULT '',
    password    TEXT NOT NULL,
    person_name TEXT NOT NULL DEFAULT '',
    backup_email TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'created',  -- created | verified | banned | error
    created_at  TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS letters(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox_id  INTEGER NOT NULL REFERENCES mailboxes(id),
    imap_uid    INTEGER,
    msg_from    TEXT NOT NULL DEFAULT '',
    subject     TEXT NOT NULL DEFAULT '',
    date        TEXT NOT NULL DEFAULT '',
    body_preview TEXT NOT NULL DEFAULT '',
    code        TEXT NOT NULL DEFAULT '',
    fetched_at  TEXT NOT NULL,
    UNIQUE(mailbox_id, imap_uid)
);

CREATE TABLE IF NOT EXISTS browser_profiles(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox_id  INTEGER NOT NULL UNIQUE REFERENCES mailboxes(id),
    profile_dir TEXT NOT NULL,
    proxy_id    INTEGER REFERENCES proxies(id),
    last_used   TEXT,
    proxy_ip    TEXT NOT NULL DEFAULT '',   -- с какого адреса выходит ЯЩИК
    proxy_country TEXT NOT NULL DEFAULT '',
    proxy_checked TEXT,
    proxy_mode  TEXT NOT NULL DEFAULT ''    -- '' авто | 'none' напрямую | 'manual' выбран вручную
);

CREATE INDEX IF NOT EXISTS idx_letters_mailbox ON letters(mailbox_id);
CREATE INDEX IF NOT EXISTS idx_mailboxes_provider ON mailboxes(provider_id);
"""


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def connect(db_path):
    """Открыть (создав при необходимости) БД и вернуть соединение."""
    d = os.path.dirname(db_path)
    if d:
        os.makedirs(d, exist_ok=True)
    # timeout + busy_timeout + WAL. Зачем: если запустить ВТОРУЮ копию
    # программы (двойной клик по exe, пока первая ещё открывается - первый
    # старт однофайловой сборки долгий), она падала с «database is locked».
    # Теперь вторая копия ЖДЁТ до 15 секунд, а не рушится трейсбеком.
    # WAL - чтобы чтения не блокировали запись (журнал, проверка прокси).
    con = sqlite3.connect(db_path, timeout=15)
    try:
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # ВНИМАНИЕ: раньше здесь на КАЖДОМ подключении сносилась колонка country
    # у proxies (ALTER TABLE ... DROP COLUMN). Убрано 11.09.2026 по отчёту QA -
    # это была мина: при возврате странового подбора (DE для web.de/gmx) колонку
    # удаляло бы сразу после создания, а «# DE» в файле прокси молча терялся.
    # Страновой подбор сейчас вне области: рабочие сервисы глобальные (proton, tuta).
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(providers)").fetchall()]
        if "driver" not in cols:
            con.execute("ALTER TABLE providers ADD COLUMN driver TEXT NOT NULL DEFAULT ''")
            con.commit()
    except Exception:
        pass
    # Поля проверки прокси: у прокси - выходной адрес/страна/когда проверяли,
    # у профиля ящика - с какого адреса выходит ИМЕННО он (для колонки в окне).
    try:
        pcols = [r[1] for r in con.execute("PRAGMA table_info(proxies)").fetchall()]
        for name, ddl in (("exit_ip", "TEXT NOT NULL DEFAULT ''"),
                          ("exit_country", "TEXT NOT NULL DEFAULT ''"),
                          ("checked_at", "TEXT"),
                          ("https_ok", "INTEGER NOT NULL DEFAULT 0")):
            if name not in pcols:
                con.execute("ALTER TABLE proxies ADD COLUMN %s %s" % (name, ddl))
        bcols = [r[1] for r in con.execute("PRAGMA table_info(browser_profiles)").fetchall()]
        for name, ddl in (("proxy_ip", "TEXT NOT NULL DEFAULT ''"),
                          ("proxy_country", "TEXT NOT NULL DEFAULT ''"),
                          ("proxy_checked", "TEXT"),
                          ("proxy_mode", "TEXT NOT NULL DEFAULT ''")):
            if name not in bcols:
                con.execute("ALTER TABLE browser_profiles ADD COLUMN %s %s" % (name, ddl))
        con.commit()
    except Exception:
        pass
    return con


# ---------- провайдеры ----------

def add_provider(con, name, family, country, signup_url, login_url,
                 imap_host, imap_port=993, domains=None, pass_min=8,
                 pass_special=0, reg_hint="", driver="", enabled=1):
    con.execute(
        """INSERT INTO providers(name, family, country, signup_url, login_url,
             imap_host, imap_port, domains, pass_min, pass_special, reg_hint, driver, enabled)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name, family, country, signup_url, login_url, imap_host, imap_port,
         json.dumps(domains or []), pass_min, pass_special, reg_hint, driver, enabled),
    )
    con.commit()


def list_providers(con, enabled_only=True):
    q = "SELECT * FROM providers"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY family, name"
    return con.execute(q).fetchall()


def get_provider(con, provider_id):
    return con.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()


def get_provider_by_name(con, name):
    return con.execute("SELECT * FROM providers WHERE name=?", (name,)).fetchone()

def update_provider(con, provider_id, **fields):
    """Изменить поля сервиса. Правит только перечисленное: так правка из окна не
    затирает то, чего в окне нет (driver - это код, а не настройка)."""
    allowed = ("name", "family", "country", "signup_url", "login_url",
               "imap_host", "imap_port", "domains", "pass_min", "pass_special",
               "reg_hint", "enabled")
    sets, vals = [], []
    for col, v in fields.items():
        if col not in allowed or v is None:
            continue
        if col == "domains" and not isinstance(v, str):
            v = json.dumps(list(v or []), ensure_ascii=False)
        sets.append(col + "=?")
        vals.append(v)
    if not sets:
        return 0
    vals.append(provider_id)
    con.execute("UPDATE providers SET %s WHERE id=?" % ", ".join(sets), vals)
    con.commit()
    return len(sets)


def set_provider_enabled(con, provider_id, enabled):
    """Скрыть/показать сервис.

    Именно enabled, а НЕ DELETE: сервисы из реестра программы (core/providers.py)
    на каждом старте добираются в базу по имени - удалённая строка вернулась бы,
    и «удалённый» сервис снова оказался бы в списке. Скрытый остаётся скрытым.
    """
    con.execute("UPDATE providers SET enabled=? WHERE id=?",
                (1 if enabled else 0, provider_id))
    con.commit()


def delete_provider(con, provider_id):
    """Снести сервис, добавленный ВРУЧНУЮ (в реестре программы его нет)."""
    con.execute("DELETE FROM providers WHERE id=?", (provider_id,))
    con.commit()


def count_mailboxes_for_provider(con, provider_id):
    """Сколько ящиков закреплено за сервисом: сервис с ящиками убирать нельзя -
    иначе ящики остались бы без почтовика."""
    row = con.execute("SELECT COUNT(*) AS n FROM mailboxes WHERE provider_id=?",
                      (provider_id,)).fetchone()
    return int(row["n"] or 0)



# ---------- прокси ----------

def add_proxy(con, kind, host, port, login="", passw="", note=""):
    """Добавить прокси в пул. Такой же адрес (host:port + логин) дважды не пишем:
    иначе повторный импорт файла удваивал бы пул (на 400 строках это заметно).
    Возвращает id новой записи или None, если такая запись уже есть."""
    host = (host or "").strip()
    login = login or ""
    dup = con.execute(
        "SELECT id FROM proxies WHERE host=? AND port=? AND IFNULL(login,'')=?",
        (host, int(port), login),
    ).fetchone()
    if dup is not None:
        return None
    cur = con.execute(
        "INSERT INTO proxies(kind,host,port,login,pass,note) VALUES(?,?,?,?,?,?)",
        (kind, host, int(port), login, passw, note),
    )
    con.commit()
    return cur.lastrowid


def list_proxies(con, active_only=True):
    q = "SELECT * FROM proxies"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY id"
    return con.execute(q).fetchall()


def get_proxy(con, proxy_id):
    return con.execute("SELECT * FROM proxies WHERE id=?", (proxy_id,)).fetchone()


def find_proxy(con, host, port, login=""):
    """Найти прокси в пуле по адресу. Нужно ручному назначению: адрес мог уже
    быть в пуле - тогда берём существующую запись, а не плодим двойника."""
    return con.execute(
        "SELECT * FROM proxies WHERE host=? AND port=? AND IFNULL(login,'')=?",
        ((host or "").strip(), int(port), login or "")).fetchone()


def set_proxy_active(con, proxy_id, active):
    con.execute("UPDATE proxies SET active=? WHERE id=?", (1 if active else 0, proxy_id))
    con.commit()


def set_proxy_kind(con, proxy_id, kind):
    """Записать протокол, определённый живой проверкой (было «auto» - стало
    http/socks5/socks4), чтобы Playwright запускался с рабочим протоколом."""
    if kind not in ("http", "socks5", "socks4"):
        return False
    con.execute("UPDATE proxies SET kind=? WHERE id=?", (kind, proxy_id))
    con.commit()
    return True


def delete_proxy(con, proxy_id):
    con.execute("DELETE FROM proxies WHERE id=?", (proxy_id,))
    con.commit()


def count_dead_proxies(con):
    """Сколько в пуле мёртвых. Мёртвый = последняя проверка не прошла
    (fail_count>=1: при успехе счётчик обнуляется) ИЛИ адрес отключён
    автоматически после серии сбоев."""
    row = con.execute(
        "SELECT COUNT(*) AS n FROM proxies "
        "WHERE fail_count>=1 OR (active=0 AND note LIKE 'авто:%')").fetchone()
    return int(row["n"] or 0)


def live_proxy_count(con):
    """Сколько адресов ПОДТВЕРЖДЕНЫ живыми. Именно «последняя проверка прошла»
    (fail_count=0): выходной адрес у мёртвого остаётся от старого удачного
    прогона, и по одному exit_ip его живым считать нельзя."""
    row = con.execute(
        "SELECT COUNT(*) AS n FROM proxies WHERE active=1 AND exit_ip IS NOT NULL "
        "AND exit_ip<>'' AND fail_count=0").fetchone()
    return int(row["n"] or 0)


def usable_proxy_count(con):
    """Сколько адресов ГОДНЫ ДЛЯ ДЕЛА: живы И прошли настоящий HTTPS-тест.

    Важнее, чем live_proxy_count: «ответил по сокету» ещё не значит «пустит
    на Proton» - 68 из 93 типичного бесплатного пула подсовывают свой
    сертификат (MITM). Для честной картины в окне показываем ОБА числа."""
    row = con.execute(
        "SELECT COUNT(*) AS n FROM proxies WHERE active=1 AND fail_count=0 "
        "AND https_ok=1").fetchone()
    return int(row["n"] or 0)


def last_check_minutes(con):
    """Сколько МИНУТ назад пул проверялся (None - ни разу).

    Зачем: счётчик «живых» берётся из последней проверки, а бесплатные
    прокси живут минутами. Без возраста числа врут: человек видит «живых 8»
    и думает, что это прямо сейчас, хотя проверка была полчаса назад.
    """
    row = con.execute(
        "SELECT MAX(checked_at) AS t FROM proxies WHERE checked_at IS NOT NULL "
        "AND checked_at<>''").fetchone()
    t = (row["t"] or "").strip()
    if not t:
        return None
    try:
        was = datetime.datetime.fromisoformat(t)
    except Exception:
        return None
    mins = int((datetime.datetime.now() - was).total_seconds() // 60)
    return max(0, mins)


def delete_dead_proxies(con, allow_all_dead=False):
    """Удалить мёртвые адреса РАЗОМ (после проверки их бывают сотни - руками
    такое не вычистить). Заодно снимаем адрес с ящиков, иначе у ящика остался
    бы proxy_id в никуда.

    Страховка: если живых в пуле НЕТ вообще, чистку не делаем - одна пропавшая
    сеть иначе выкосила бы весь пул. Когда точно знаем, что сеть на месте
    (allow_all_dead=True), чистим и «всё мёртвое».
    Возвращает (удалено, причина_отказа)."""
    if count_dead_proxies(con) == 0:
        return 0, "none"
    if not allow_all_dead and live_proxy_count(con) == 0:
        return 0, "no_live"
    ids = [r["id"] for r in con.execute(
        "SELECT id FROM proxies WHERE fail_count>=1 "
        "OR (active=0 AND note LIKE 'авто:%')").fetchall()]
    if not ids:
        return 0, "none"
    q = ",".join("?" * len(ids))
    con.execute("UPDATE browser_profiles SET proxy_id=NULL, proxy_ip='', "
                "proxy_country='' WHERE proxy_id IN (%s)" % q, ids)
    con.execute("DELETE FROM proxies WHERE id IN (%s)" % q, ids)
    con.commit()
    return len(ids), ""


def pick_random_proxy(con, prefer_unused=True):
    """
    Выбрать случайный живой прокси из пула.
    Предпочтение - тем, что ещё ни разу не использовались (last_used IS NULL),
    затем с наименьшим числом ошибок. Возвращает строку из таблицы или None
    (пула нет).
    """
    rows = con.execute(
        "SELECT * FROM proxies WHERE active=1 "
        "ORDER BY last_used IS NOT NULL, fail_count, RANDOM()"
    ).fetchall()
    return rows[0] if rows else None

def proxy_to_playwright(p, kind=None):
    """Аргумент proxy для Playwright. kind - протокол, определённый при ЖИВОЙ
    проверке (в записи может стоять «auto» - адрес из файла без схемы)."""
    if p is None:
        return None
    k = (kind or p["kind"] or "http").lower()
    scheme = k if k in ("http", "socks5", "socks4") else "http"
    server = f"{scheme}://{p['host']}:{p['port']}"
    d = {"server": server}
    if p["login"]:
        d["username"] = p["login"]
        d["password"] = p["pass"]
    return d


def mark_proxy_used(con, proxy_id, ok=True):
    if proxy_id is None:
        return
    if ok:
        con.execute("UPDATE proxies SET last_used=?, fail_count=0 WHERE id=?",
                    (now(), proxy_id))
    else:
        con.execute("UPDATE proxies SET fail_count=fail_count+1 WHERE id=?", (proxy_id,))
    con.commit()


# ---------- проверка живости прокси (зовёт core/proxy.py) ----------

def set_proxy_health(con, proxy_id, ok, ip="", country="", error="", https_ok=0):
    """Записать результат проверки прокси. При успехе сбрасываем счётчик сбоев и
    запоминаем выходной адрес со страной; при сбое копим счётчик, и после
    PROXY_FAILS_TO_DISABLE сбоев прокси выключается сам (пометка «авто:» в note).
    Возвращает (сбоев, выключен_ли) - чтобы вызвавший мог сказать об этом в журнал."""
    row = con.execute("SELECT fail_count FROM proxies WHERE id=?", (proxy_id,)).fetchone()
    if row is None:
        return 0, False
    if ok:
        con.execute(
            """UPDATE proxies SET fail_count=0, last_used=?, exit_ip=?, exit_country=?,
                 checked_at=?, https_ok=?,
                 note=CASE WHEN note LIKE 'авто:%' THEN '' ELSE note END
               WHERE id=?""",
            (now(), ip or "", country or "", now(), 1 if https_ok else 0, proxy_id))
        con.commit()
        return 0, False
    fails = int(row["fail_count"] or 0) + 1
    disabled = fails >= PROXY_FAILS_TO_DISABLE
    con.execute(
        """UPDATE proxies SET fail_count=?, checked_at=?, https_ok=0,
             active=CASE WHEN ? THEN 0 ELSE active END,
             note=CASE WHEN ? THEN ? ELSE note END
           WHERE id=?""",
        (fails, now(), 1 if disabled else 0, 1 if disabled else 0,
         "авто: %d сбоя проверки" % fails, proxy_id))
    con.commit()
    return fails, disabled


def set_profile_proxy(con, mailbox_id, proxy_id, ip="", country="", mode=None):
    """Закрепить за ящиком прокси и запомнить, с какого адреса он выходит
    (нужно для колонки «Прокси / адрес» в главном окне).

    mode - как обходиться с прокси у этого ящика, когда его назначают вручную:
      ''       авто: живой подбирается сам (по умолчанию, ничего не меняем);
      'none'   только напрямую, без прокси (что бы ни лежало в пуле);
      'manual' адрес выбран человеком.
    mode=None - режим НЕ трогать (так зовут автоподбор и ротация прокси).
    """
    try:
        if con.execute("SELECT 1 FROM browser_profiles WHERE mailbox_id=?",
                       (mailbox_id,)).fetchone() is None:
            return False
        con.execute(
            """UPDATE browser_profiles SET proxy_id=?, proxy_ip=?, proxy_country=?,
                 proxy_checked=? WHERE mailbox_id=?""",
            (proxy_id, ip or "", country or "", now(), mailbox_id))
        if mode is not None:
            con.execute("UPDATE browser_profiles SET proxy_mode=? WHERE mailbox_id=?",
                        ("" if mode == "auto" else mode, mailbox_id))
        con.commit()
        return True
    except Exception:
        return False


def assigned_proxy_ids(con, except_mailbox_id=None):
    """id прокси, уже закреплённые за ящиками: один адрес не должен уйти двум
    ящикам сразу."""
    q = "SELECT proxy_id FROM browser_profiles WHERE proxy_id IS NOT NULL"
    params = ()
    if except_mailbox_id is not None:
        q += " AND mailbox_id<>?"
        params = (except_mailbox_id,)
    try:
        return {r["proxy_id"] for r in con.execute(q, params).fetchall()}
    except Exception:
        return set()


def free_proxies(con, except_mailbox_id=None):
    """Живые прокси, НЕ занятые другими ящиками.

    Порядок: сначала ПРОВЕРЕННЫЕ НАСТОЯЩИМ HTTPS (https_ok=1 - через них
    браузер точно попадёт на Proton), потом остальные живые, затем
    неиспользованные, дальше по числу сбоев. Так только что залитый пул
    (ещё не проверенный) тоже работает, но подбор не тратит время на адреса,
    которые подсовывают свой сертификат."""
    busy = assigned_proxy_ids(con, except_mailbox_id)
    rows = con.execute(
        "SELECT * FROM proxies WHERE active=1 "
        "ORDER BY https_ok DESC, last_used IS NOT NULL, fail_count, RANDOM()").fetchall()
    return [r for r in rows if r["id"] not in busy]


def proxy_map(con):
    """{id ящика: «DE 5.61.1.2»} - для главной таблицы: с какого адреса выходит
    каждый ящик. Ящика нет в словаре - прокси не закреплён (работает напрямую)."""
    out = {}
    try:
        rows = con.execute(
            """SELECT bp.mailbox_id, bp.proxy_id, bp.proxy_ip, bp.proxy_country,
                      p.kind, p.host, p.port, p.exit_ip
                 FROM browser_profiles bp
                 LEFT JOIN proxies p ON p.id = bp.proxy_id""").fetchall()
    except Exception:
        return out
    for r in rows:
        if not r["proxy_id"]:
            continue
        ip = r["proxy_ip"] or r["exit_ip"] or ""
        cc = (r["proxy_country"] or "").upper()
        if ip:
            out[r["mailbox_id"]] = ("%s %s" % (cc, ip)).strip()
        else:
            out[r["mailbox_id"]] = "%s://%s:%d" % (r["kind"] or "http", r["host"], r["port"])
    return out


def _is_ipv4(s):
    """Похоже ли на IPv4-адрес. Нужно, чтобы различить строки
    «ip:port:login:pass» и «login:pass:ip:port» - форматы разные."""
    o = s.split(".")
    return len(o) == 4 and all(x.isdigit() and 0 <= int(x) <= 255 for x in o)


def parse_proxy_line(line):
    """Понять строку вида:
       http://user:pass@host:port | socks5://host:port | host:port

    Адрес БЕЗ схемы читается как «auto»: реальный протокол (http / socks5 /
    socks4) определит живая проверка и запишет сюда же - угадывать не надо.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if " #" in line:
        line = line.split(" #", 1)[0].strip()
    m = re.match(r"^(https?|socks5|socks4)://(?:([^:@/]+):([^@/]*)@)?([^:/]+):(\d+)$", line)
    if m:
        scheme, login, passw, host, port = m.groups()
        kind = "http" if scheme in ("http", "https") else scheme
        return dict(kind=kind, host=host, port=int(port), login=login or "", passw=passw or "")
    m2 = re.match(r"^(?:([^:@/]+):([^@/]*)@)?([^:/]+):(\d+)$", line)
    if m2:
        login, passw, host, port = m2.groups()
        return dict(kind="auto", host=host, port=int(port), login=login or "", passw=passw or "")
    # Строки вида «ip:port:login:pass» (так их выдают прокси-магазины) и
    # обратный порядок «login:pass:ip:port». Раньше такие строки молча
    # отбрасывались - отсюда «импортировано 0» на рабочем файле.
    parts = [x.strip() for x in line.split(":")]
    if len(parts) == 4:
        a, b, c, e = parts
        if _is_ipv4(a) and b.isdigit():                     # ip:port:login:pass
            return dict(kind="auto", host=a, port=int(b), login=c, passw=e)
        if _is_ipv4(c) and e.isdigit():                     # login:pass:ip:port
            return dict(kind="auto", host=c, port=int(e), login=a, passw=b)
    # Ещё одна форма, которой выдают бесплатные пулы: «ip:port@login:pass».
    m3 = re.match(r"^([^:@/\s]+):(\d+)@([^:@/\s]*):?([^@/\s]*)$", line)
    if m3:
        host, port, login, passw = m3.groups()
        if _is_ipv4(host):
            return dict(kind="auto", host=host, port=int(port), login=login, passw=passw)
    return None


def import_proxy_file(con, path):
    """Прочитать файл со списком прокси (по одному адресу в строке).
    Пустые строки и комментарии (# в начале) пропускаются молча,
    неразобранные строки и ПОВТОРЫ (адрес уже есть в пуле) считаются
    пропущенными. Возвращает (добавлено, пропущено)."""
    added, dup, broken = import_proxy_file_ex(con, path)
    return added, dup + broken


def import_proxy_file_ex(con, path):
    """Как import_proxy_file, но с разбором причин: (добавлено, повторов, битых).

    Зачем: «импортировано 0» ничего не объяснял. Теперь видно, что именно
    случилось - например, в файле строки «ip:port:login:pass», которые старый
    разборщик не понимал и молча считал битыми, а пул оставался пустым."""
    added = dup = broken = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            p = parse_proxy_line(line)
            if p is None:
                broken += 1
                continue
            if add_proxy(con, p["kind"], p["host"], p["port"],
                         p["login"], p["passw"]) is None:
                dup += 1
                continue
            added += 1
    return added, dup, broken



# ---------- экспорт / импорт пула прокси ----------
# Зачем: пул на сотню живых адресов жалко терять при переезде на другую машину
# (или при переустановке). Экспорт - .json с полным состоянием: адрес, пароль,
# страна выхода, отметка о проверке. Импорт понимает и его, и обычный текстовый
# список (data/proxies.txt, «host:port:логин:пароль»), который разбирает
# import_proxy_file_ex.

PROXY_EXPORT_FORMAT = "mailbox_manager.proxies"
PROXY_EXPORT_VERSION = 1


def export_proxies(con, path, active_only=False):
    """Сохранить пул в файл. По расширению: .json - полное состояние (для
    переноса на другую машину), иначе - текстовый список строк для другого
    софта. Возвращает число записанных адресов."""
    rows = list_proxies(con, active_only=active_only)
    if (path or "").lower().endswith(".json"):
        data = {
            "format": PROXY_EXPORT_FORMAT,
            "version": PROXY_EXPORT_VERSION,
            "exported_at": now(),
            "count": len(rows),
            "proxies": [{
                "kind": r["kind"], "host": r["host"], "port": int(r["port"]),
                "login": r["login"] or "", "pass": r["pass"] or "",
                "note": r["note"] or "", "active": int(r["active"] or 0),
                "fail_count": int(r["fail_count"] or 0),
                "exit_ip": r["exit_ip"] or "",
                "exit_country": r["exit_country"] or "",
                "checked_at": r["checked_at"] or "",
                "https_ok": int(r["https_ok"] or 0),
            } for r in rows],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write("# mailbox_manager - пул прокси, %s, адресов: %d\n" % (now(), len(rows)))
            for r in rows:
                scheme = r["kind"] if r["kind"] in ("http", "socks5", "socks4") else "http"
                if r["login"]:
                    f.write("%s://%s:%s@%s:%d\n" % (scheme, r["login"], r["pass"] or "",
                                                     r["host"], int(r["port"])))
                else:
                    f.write("%s:%d\n" % (r["host"], int(r["port"])))
    return len(rows)


def _write_proxy_meta(con, proxy_id, it):
    """Перенести в запись сведения из файла экспорта: пароль, протокол, заметку,
    состояние вкл/выкл и результат прежней проверки."""
    con.execute(
        """UPDATE proxies SET pass=?, kind=?, note=?, active=?, fail_count=?,
                   exit_ip=?, exit_country=?, checked_at=?, https_ok=?
           WHERE id=?""",
        (str(it.get("pass") or ""), str(it.get("kind") or "auto"),
         str(it.get("note") or ""), 1 if it.get("active", 1) else 0,
         int(it.get("fail_count") or 0), str(it.get("exit_ip") or ""),
         str(it.get("exit_country") or ""), str(it.get("checked_at") or "") or None,
         1 if it.get("https_ok") else 0, proxy_id),
    )
    con.commit()


def import_proxies_json(con, path):
    """Забрать пул из JSON-экспорта.

    Повтор (host:port + логин) в пуле не размножается: запись обновляется -
    пароль, протокол и отметка о проверке переносятся из файла. Возвращает
    (добавлено, обновлено, пропущено, битых), чтобы отчёт был внятным.
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    items = data.get("proxies") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("в файле нет списка \"proxies\"")
    added = updated = skipped = broken = 0
    for it in items:
        if not isinstance(it, dict):
            broken += 1
            continue
        host = str(it.get("host") or "").strip()
        try:
            port = int(it.get("port"))
        except Exception:
            broken += 1
            continue
        if not host or not (0 < port < 65536):
            broken += 1
            continue
        login = str(it.get("login") or "")
        row = find_proxy(con, host, port, login)
        if row is not None:
            _write_proxy_meta(con, row["id"], it)
            updated += 1
            continue
        pid = add_proxy(con, str(it.get("kind") or "auto"), host, port, login,
                        str(it.get("pass") or ""), str(it.get("note") or ""))
        if pid is None:
            skipped += 1
            continue
        _write_proxy_meta(con, pid, it)
        added += 1
    return added, updated, skipped, broken


# ---------- ящики ----------

def add_mailbox(con, provider_id, email, password, login="", person_name="",
                backup_email="", status="created", note=""):
    cur = con.execute(
        """INSERT INTO mailboxes(provider_id, email, login, password, person_name,
             backup_email, status, created_at, note)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (provider_id, email, login, password, person_name, backup_email,
         status, now(), note),
    )
    con.commit()
    return cur.lastrowid


_MB_SELECT = """SELECT m.*, p.name AS provider, p.family, p.country AS provider_country, p.login_url, p.signup_url,
                       p.imap_host, p.imap_port
                FROM mailboxes m JOIN providers p ON p.id = m.provider_id"""


def list_mailboxes(con, provider_id=None):
    q = _MB_SELECT
    if provider_id:
        q += " WHERE m.provider_id=?"
        return con.execute(q, (provider_id,)).fetchall()
    q += " ORDER BY m.id DESC"
    return con.execute(q).fetchall()


def get_mailbox(con, mailbox_id):
    return con.execute(_MB_SELECT + " WHERE m.id=?", (mailbox_id,)).fetchone()


def find_mailbox_by_email(con, email):
    return con.execute(_MB_SELECT + " WHERE m.email=?", (email,)).fetchone()


def update_mailbox_status(con, mailbox_id, status, note=None):
    if note is None:
        con.execute("UPDATE mailboxes SET status=? WHERE id=?", (status, mailbox_id))
    else:
        con.execute("UPDATE mailboxes SET status=?, note=? WHERE id=?", (status, note, mailbox_id))
    con.commit()


def update_mailbox(con, mailbox_id, email=None, login=None, password=None,
                   person_name=None, backup_email=None, status=None, note=None):
    """Обновить поля ящика (None = не трогать). Нужно автопилоту: адрес
    меняется при занятости, пароль - когда сервис (Tuta) счёл наш слабым,
    в заметку копим recovery phrase и пр."""
    sets, vals = [], []
    for col, v in (("email", email), ("login", login), ("password", password),
                   ("person_name", person_name), ("backup_email", backup_email),
                   ("status", status), ("note", note)):
        if v is not None:
            sets.append(f"{col}=?")
            vals.append(v)
    if not sets:
        return
    vals.append(mailbox_id)
    con.execute(f"UPDATE mailboxes SET {', '.join(sets)} WHERE id=?", vals)
    con.commit()


def get_profile(con, mailbox_id):
    """Запись профиля браузера ящика (папка + закреплённый за ним прокси) или None."""
    try:
        return con.execute("SELECT * FROM browser_profiles WHERE mailbox_id=?",
                           (mailbox_id,)).fetchone()
    except Exception:
        return None


def delete_mailbox(con, mailbox_id):
    """Удалить ящик, его письма и запись профиля браузера.
    Папку профиля на диске сносит core.browser.remove_profile (её зовёт GUI) -
    иначе после удаления ящиков оставались сироты на сотни мегабайт."""
    con.execute("DELETE FROM letters WHERE mailbox_id=?", (mailbox_id,))
    con.execute("DELETE FROM browser_profiles WHERE mailbox_id=?", (mailbox_id,))
    con.execute("DELETE FROM mailboxes WHERE id=?", (mailbox_id,))
    con.commit()


# ---------- письма ----------

def save_letter(con, mailbox_id, uid, msg_from, subject, date, preview, code):
    con.execute(
        """INSERT OR REPLACE INTO letters(mailbox_id, imap_uid, msg_from, subject,
             date, body_preview, code, fetched_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (mailbox_id, uid, msg_from, subject, date, preview, code, now()),
    )
    con.commit()


def list_letters(con, mailbox_id, with_code_only=False, limit=200):
    q = "SELECT * FROM letters WHERE mailbox_id=?"
    if with_code_only:
        q += " AND code != ''"
    q += " ORDER BY date DESC LIMIT ?"
    return con.execute(q, (mailbox_id, limit)).fetchall()


def letters_with_codes(con, limit=50):
    return con.execute(
        "SELECT l.*, m.email FROM letters l JOIN mailboxes m ON m.id=l.mailbox_id "
        "WHERE l.code != '' ORDER BY l.fetched_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def get_letter(con, letter_id):
    return con.execute("SELECT * FROM letters WHERE id=?", (letter_id,)).fetchone()
