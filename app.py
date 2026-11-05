# -*- coding: utf-8 -*-
"""
app.py - командная оболочка mailbox_manager.

Команды:
  providers                  список почтовых сервисов в реестре
  create <id|name>           создать ящик: генерация персоны+пароля, сохранение в БД
  list                       мои ящики
  mail <id|email>            прочитать почту ящика по IMAP, вытащить коды
                             (только у сервисов с IMAP; у Proton его нет -
                             почта смотрится в браузере: open <id>)
  codes                      последние письма с кодами по всем ящикам
  open <id|email> [--proxy none|<id>] [--signup] [--wait enter|window]
                             открыть ящик в браузере (свой профиль + случайный прокси)
  proxy import <файл>        загрузить список прокси (по строке, # - комментарий)
  proxy list                 показать пул прокси
  proxy pick                 показать, какой прокси выпал бы сейчас

Графический интерфейс:  pythonw gui.py   (поверх этого же ядра).
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core import db
from core import paths

DATA_DIR = paths.data_dir()
DB_PATH = paths.db_path()


def get_con():
    con = db.connect(DB_PATH)
    from core import providers
    providers.seed(con)
    return con


# ---------- вывод ----------

def _p(name, val):
    print(f"  {name:<12} {val}")


def show_provider(row):
    print(f"#{row['id']} {row['name']}  [{row['family']}, {row['country']}]")
    _p("signup", row["signup_url"])
    if row["imap_host"]:
        _p("imap", f"{row['imap_host']}:{row['imap_port']}")
    else:
        _p("imap", "нет - почта смотрится в браузере (open)")
    if row["domains"] and row["domains"] != "[]":
        import json
        doms = json.loads(row["domains"])
        _p("domains", ", ".join(doms[:6]) + (" …" if len(doms) > 6 else ""))
    _p("password", f"мин. {row['pass_min']}" + (", спецсимвол" if row["pass_special"] else ""))
    if row["reg_hint"]:
        print(f"  note       {row['reg_hint']}")


def show_mailbox(row):
    print(f"#{row['id']} {row['email']}  [{row['provider']}] {row['status']}  ({row['created_at']})")
    _p("login", row["login"] or row["email"])
    _p("password", row["password"])
    if row["person_name"]:
        _p("person", row["person_name"])
    if row["backup_email"]:
        _p("backup", row["backup_email"])
    if row["note"]:
        print(f"  note       {row['note']}")


def show_letter(row):
    # row - sqlite3.Row: у него нет .get(), только доступ по ключу
    tag = f"  КОД: {row['code']}" if row["code"] else ""
    print(f"- {row['date']}  {row['msg_from']}")
    print(f"  {row['subject']}{tag}")


# ---------- команды ----------

def cmd_providers(con, args):
    for p in db.list_providers(con):
        show_provider(p)
        print()
    return 0


def cmd_create(con, args):
    from core import generator
    prov = None
    if args.what.isdigit():
        prov = db.get_provider(con, int(args.what))
    else:
        prov = db.get_provider_by_name(con, args.what.lower())
    if prov is None:
        print(f"Провайдер «{args.what}» не найден. Смотри: providers")
        return 1
    locale = args.locale or {"de": "de", "bg": "bg", "be": "be", "global": "uk"}.get(
        prov["country"].lower(), "uk")
    first, surname, _ = generator.person(locale)
    email, lp = generator.available_local_part(con, prov, first, surname)
    if not email:
        print("Не смогла подобрать свободный логин - слишком много ящиков у этого провайдера?")
        return 1
    passw = generator.password(min_len=prov["pass_min"], special=bool(prov["pass_special"]))
    mid = db.add_mailbox(con, prov["id"], email, passw, login=email.split("@")[0],
                         person_name=f"{first} {surname}", status="created")
    mb = db.get_mailbox(con, mid)
    show_mailbox(mb)
    print("\nСохранено в БД. Дальше:")
    print(f"  регистрация: python app.py open {mid} --signup   (форма регистрации)")
    if prov["imap_host"]:
        print(f"  почта:       python app.py mail {mid}            (IMAP, с кодами)")
    else:
        print(f"  почта:       python app.py open {mid}            (в браузере: IMAP нет)")
    return 0


def cmd_list(con, args):
    rows = db.list_mailboxes(con)
    if not rows:
        print("Ящиков пока нет. Первый шаг: python app.py create mail.com")
        return 0
    for r in rows:
        show_mailbox(r)
        print()
    return 0


def cmd_mail(con, args):
    mb = _resolve_mailbox(con, args.what)
    if mb is None:
        return 1
    from core import imap_reader
    try:
        added, total = imap_reader.check_mailbox(con, mb, days=args.days)
    except Exception as e:
        print(f"Ошибка чтения {mb['email']}: {e}")
        return 1
    print(f"{mb['email']}: новых {added} из {total} за {args.days} дн.")
    for row in db.list_letters(con, mb["id"], with_code_only=False, limit=args.limit):
        show_letter(row)
    return 0


def cmd_codes(con, args):
    rows = db.letters_with_codes(con, limit=args.limit)
    if not rows:
        print("Писем с кодами пока нет. Сначала: python app.py mail <id>")
        return 0
    for r in rows:
        print(f"#{r['mailbox_id']} {r['email']}: КОД {r['code']}  ({r['subject']})")
    return 0


def cmd_open(con, args):
    mb = _resolve_mailbox(con, args.what)
    if mb is None:
        return 1
    from core import browser
    proxy = None
    if args.proxy == "none":
        proxy = "none"
    elif args.proxy and args.proxy.isdigit():
        row = db.get_proxy(con, int(args.proxy))
        if row is None:
            print(f"Прокси #{args.proxy} не найден")
            return 1
        proxy = row
    url = mb["signup_url"] if args.signup else None
    # живой ящик - подставляем логин/пароль, остаётся нажать «Войти»
    live = mb["status"] not in ("created", "creating")
    browser.open_mailbox_browser(con, mb, proxy=proxy, headless=args.headless,
                                 auto_url=url, wait=args.wait,
                                 prefill=live and not args.signup)
    return 0


def cmd_proxy(con, args):
    if args.action == "list":
        rows = db.list_proxies(con)
        if not rows:
            print("Пул пуст. Добавь: python app.py proxy import data/proxies.txt")
            return 0
        for p in rows:
            auth = f"{p['login']}:***@" if p["login"] else ""
            print(f"#{p['id']} {p['kind']}://{auth}{p['host']}:{p['port']}"
                  f"  active={p['active']} fails={p['fail_count']}"
                  f"  last={p['last_used'] or '-'}")
    elif args.action == "pick":
        p = db.pick_random_proxy(con)
        if p is None:
            print("Пул пуст - сейчас браузер пошёл бы напрямую, без прокси.")
        else:
            print(f"Выпал бы прокси #{p['id']} {p['kind']}://{p['host']}:{p['port']}")
    elif args.action == "import":
        path = args.file
        if not os.path.exists(path):
            print(f"Файл не найден: {path}")
            return 1
        added, skipped = db.import_proxy_file(con, path)
        print(f"Добавлено прокси: {added}, пропущено битых строк: {skipped}")
    return 0


# ---------- помощники ----------

def _resolve_mailbox(con, what):
    if what.isdigit():
        mb = db.get_mailbox(con, int(what))
    else:
        mb = db.find_mailbox_by_email(con, what.lower())
    if mb is None:
        print(f"Ящик «{what}» не найден. Смотри: python app.py list")
    return mb


def main():
    ap = argparse.ArgumentParser(prog="mailbox_manager", description="Менеджер почтовых ящиков")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("providers", help="реестр почтовых сервисов")
    p = sub.add_parser("create", help="создать ящик (генерация+сохранение)")
    p.add_argument("what", help="id или имя провайдера, напр. 3 или mail.com")
    p.add_argument("--locale", help="локаль имени: de|bg|be|uk")
    sub.add_parser("list", help="мои ящики")
    p = sub.add_parser("mail", help="прочитать почту ящика (IMAP), достать коды")
    p.add_argument("what", help="id или email ящика")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("codes", help="последние коды по всем ящикам")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("open", help="открыть ящик в браузере (профиль+прокси из пула)")
    p.add_argument("what", help="id или email ящика")
    p.add_argument("--proxy", help="none | <id> (по умолчанию - случайный из пула)")
    p.add_argument("--signup", action="store_true", help="открыть страницу РЕГИСТРАЦИИ")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--wait", choices=["enter", "window"], default="enter",
                   help="enter=ждать Enter в консоли, window=жить пока открыто окно")
    pp = sub.add_parser("proxy", help="пул прокси")
    pp.add_argument("action", choices=["list", "pick", "import"])
    pp.add_argument("file", nargs="?", help="файл со списком для import")

    args = ap.parse_args()
    con = get_con()
    try:
        if args.cmd == "providers":
            return cmd_providers(con, args)
        if args.cmd == "create":
            return cmd_create(con, args)
        if args.cmd == "list":
            return cmd_list(con, args)
        if args.cmd == "mail":
            return cmd_mail(con, args)
        if args.cmd == "codes":
            return cmd_codes(con, args)
        if args.cmd == "open":
            return cmd_open(con, args)
        if args.cmd == "proxy":
            return cmd_proxy(con, args)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
