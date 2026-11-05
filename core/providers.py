# -*- coding: utf-8 -*-
"""
providers.py - реестр почтовых сервисов, на которые целится менеджер.

Отбор (поиск 2026-09): без SMS при регистрации, с IMAP, «живые» сервисы.
Семейство United Internet даёт сразу несколько марок с одного движка регистрации:
больше вариантов с одного IP, чем у одиночного сервиса.

ЧЕСТНО О ГРАНИЦАХ ЗНАНИЯ: точные URL страниц регистрации и состав доменов
почтовики меняют. Ниже - проверенные в поиске хосты IMAP и главные страницы,
полный список доменов и актуальные формы уточняются на этапе обкатки драйвера
(этап 2), это нормальный процесс, а не галлюцинация.
"""
from core import db

# поля: name, family, country, signup_url, login_url, imap_host,
#       domains(образцовые), pass_min, pass_special, reg_hint
REGISTRY = [
    # --- United Internet: один движок, четыре флага ---
    dict(name="web.de",  family="united_internet", country="DE",
         signup_url="https://registrierung.web.de/",
         login_url="https://www.web.de/",
         imap_host="imap.web.de",
         domains=["web.de"],
         pass_min=8, pass_special=0,
         reg_hint="Крупнейший немецкий почтовик. Регистрация работает только с IP Германии/Австрии/Швейцарии - с другого IP откажет (проверено 07.09). Без SMS; 1 ГБ."),
    dict(name="gmx",     family="united_internet", country="DE",
         signup_url="https://registrierung.gmx.net/",
         login_url="https://www.gmx.net/",
         imap_host="imap.gmx.net",
         domains=["gmx.de", "gmx.net", "gmx.at", "gmx.ch"],
         pass_min=8, pass_special=0,
         reg_hint="Домен на выбор: DE/AT/CH. Регистрация только с IP немецкоязычного региона. Без SMS, 65 ГБ."),
    dict(name="mail.com", family="united_internet", country="global",
         signup_url="https://signup.mail.com/",
         login_url="https://www.mail.com/int/",
         imap_host="imap.mail.com",
         # образец реальных доменов; полный список (~200) даёт сайт при регистрации
         domains=["mail.com", "email.com", "dr.com", "europe.com", "usa.com",
                  "consultant.com", "engineer.com", "journalist.com",
                  "accountant.com", "scientist.com", "teacher.com", "chef.net"],
         pass_min=8, pass_special=0,
         reg_hint="~200 доменов на выбор - разводит ящики с одного IP лучше всех."),
    dict(name="abv.bg",  family="united_internet", country="BG",
         signup_url="https://www.abv.bg/",
         login_url="https://www.abv.bg/",
         imap_host="imap.abv.bg",
         domains=["abv.bg"],
         pass_min=6, pass_special=0,
         reg_hint="Болгария (та же семья United Internet). Без SMS; при отказе - нужен болгарский IP."),
    dict(name="mail.bg", family="united_internet", country="BG",
         signup_url="https://www.mail.bg/",
         login_url="https://www.mail.bg/",
         imap_host="imap.mail.bg",
         domains=["mail.bg"],
         pass_min=6, pass_special=0,
         reg_hint="Болгария, брат abv.bg. Без SMS; при отказе - нужен болгарский IP."),

    # --- отдельные ---
    dict(name="mailfence", family="own", country="BE",
         signup_url="https://www.mailfence.com/",
         login_url="https://www.mailfence.com/",
         imap_host="imap.mailfence.com",
         domains=["mailfence.com"],
         pass_min=8, pass_special=0,
         reg_hint="Бельгия, приватность. Просит существующий email (не SMS). Всего 500 МБ."),
    dict(name="outlook", family="microsoft", country="global",
         signup_url="https://signup.live.com/signup?lic=1",
         login_url="https://outlook.live.com/mail/",
         imap_host="outlook.office365.com",
         domains=["outlook.com", "hotmail.com", "live.com"],
         pass_min=10, pass_special=0,
         reg_hint="Часто можно без телефона (кнопка skip). На «подозрительном» IP просит номер."),

    dict(name="proton", family="proton", country="global",
         signup_url="https://account.proton.me/mail/signup",
         login_url="https://mail.proton.me/u/0/inbox",
         imap_host="",  # на бесплатном тарифе IMAP нет - почта смотрится в браузере
         domains=["proton.me", "protonmail.com", "pm.me"],
         pass_min=12, pass_special=1,
         driver="proton",
         reg_hint="Без телефона и SMS (проверено живьём 07.09). Верификация - капча или код на существующий email. IMAP на бесплатном нет: почта - в браузере. Авторегистрация: ты решаешь только капчу."),

    dict(name="tuta", family="tuta", country="global",
         signup_url="https://app.tuta.com/signup",
         login_url="https://app.tuta.com/login",
         imap_host="",  # Tuta не отдаёт IMAP: почта только в её клиенте/вебе
         domains=["tutamail.com", "tuta.com", "tutanota.com", "keemail.me"],
         pass_min=12, pass_special=1,
         driver="tuta",
         reg_hint="Без телефона и SMS. Своя капча (фото аналоговых часов) - решает ЧЕЛОВЕК, автопилот доводит и ждёт. Тариф Free выбирается явно: по умолчанию Tuta держит платный Legend 8 EUR/мес. IMAP нет - почта в вебе. ВНИМАНИЕ: Free-аккаунт удаляется после 6 месяцев неактивности."),
]


REGISTRY_NAMES = tuple(p["name"] for p in REGISTRY)


def is_registry(name):
    """Сервис из реестра программы? Такие из базы НЕ удаляем, а прячем
    (providers.enabled=0): seed() вернул бы удалённую строку на старте."""
    return (name or "") in REGISTRY_NAMES


def seed(con):
    """Дополнить реестр провайдеров недостающими записями (по имени).
    Безопасен при повторном запуске и не затирает ручные правки.
    Скрытые человеком (enabled=0) обратно НЕ включаются: enabled меняет
    только человек, seed туда не лезет."""
    for p in REGISTRY:
        if db.get_provider_by_name(con, p["name"]) is None:
            db.add_provider(
                con,
                name=p["name"], family=p["family"], country=p["country"],
                signup_url=p["signup_url"], login_url=p["login_url"],
                imap_host=p["imap_host"], domains=p["domains"],
                pass_min=p["pass_min"], pass_special=p["pass_special"],
                reg_hint=p["reg_hint"], driver=p.get("driver", ""),
            )
    # догонка: у существующих строк реестра добираем появившийся драйвер
    # (только пустой driver - ручные правки не затираем)
    for p in REGISTRY:
        if p.get("driver"):
            con.execute("UPDATE providers SET driver=? WHERE name=? AND driver=''",
                        (p["driver"], p["name"]))
    con.commit()
