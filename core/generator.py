# -*- coding: utf-8 -*-
"""
generator.py - случайная «личность» и пароли для ящиков.

Имена подбираются из пулов по локалям, чтобы ящик не выглядел как
"user12345" - для почтовиков это минус при ручной проверке.
Пароли - читаемые (слоги), но с цифрами и спецсимволом, под правила сервиса.
Локальная часть адреса всегда ASCII (умляуты и пр. транслитерируются).
"""
import json
import random
import re
import string

# --- пулы имён: (мужские, женские, фамилии) по локалям ---
NAME_POOLS = {
    "de": (
        ["Lukas", "Jonas", "Finn", "Leon", "Paul", "Erik", "Timo", "Nils", "Max", "Felix"],
        ["Mia", "Emma", "Hanna", "Lena", "Anna", "Clara", "Ida", "Nora", "Lina", "Greta"],
        ["Weber", "Schmidt", "Müller", "Fischer", "Meyer", "Wagner", "Becker", "Hoffmann",
         "Schulz", "Koch", "Richter", "Klein", "Wolf", "Neumann", "Braun", "Krüger"],
    ),
    "bg": (
        ["Ivan", "Georgi", "Dimitar", "Nikolay", "Petar", "Stoyan", "Todor", "Vasil"],
        ["Elena", "Maria", "Ivana", "Petia", "Desislava", "Ralitsa", "Gergana", "Viktoria"],
        ["Petrov", "Ivanov", "Georgiev", "Dimitrov", "Stoyanov", "Todorov", "Nikolov",
         "Vasilev", "Angelov", "Kolev"],
    ),
    "be": (
        ["Thomas", "Jean", "Pierre", "Louis", "Antoine", "Victor", "Hugo", "Lucas"],
        ["Marie", "Julie", "Camille", "Louise", "Claire", "Charlotte", "Sarah", "Emma"],
        ["Dupont", "Lambert", "Dubois", "Leroy", "Moreau", "Simon", "Laurent", "Legrand",
         "Dumont", "Fontaine"],
    ),
    "uk": (
        ["Oliver", "Jack", "Harry", "George", "Charlie", "Oscar", "James", "William"],
        ["Olivia", "Amelia", "Isla", "Ava", "Emily", "Sophie", "Grace", "Jessica"],
        ["Smith", "Jones", "Taylor", "Brown", "Wilson", "Evans", "Thomas", "Walker",
         "White", "Wright"],
    ),
}

SYLLABLES = ["ko", "mi", "ra", "no", "te", "lu", "sa", "ve", "di", "bo",
             "fe", "na", "ku", "ri", "za", "mo", "pe", "li", "du", "ka"]

# транслит для имён с диакритикой (умляуты и пр. → ascii)
_TRANSLIT = {
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
    "é": "e", "è": "e", "ê": "e", "à": "a", "â": "a",
    "ç": "c", "î": "i", "ï": "i", "ô": "o", "û": "u",
}
_TRANSLIT_RE = re.compile("|".join(map(re.escape, _TRANSLIT)))


def _ascii(s):
    return _TRANSLIT_RE.sub(lambda m: _TRANSLIT[m.group(0)], s)


def person(locale="de", rng=None):
    """Вернуть (имя, фамилия, логин-основа) - имя/фамилия в ascii-транслите."""
    rng = rng or random
    male, female, last = NAME_POOLS.get(locale, NAME_POOLS["de"])
    first = _ascii(rng.choice(male + female))
    surname = _ascii(rng.choice(last))
    login_base = f"{first.lower()}.{surname.lower()}"
    return first, surname, login_base


def password(min_len=12, special=True, rng=None):
    """Читаемый пароль: слоги + цифры + спецсимвол. Подходит для почтовиков."""
    rng = rng or random
    specs = "!@#$%^&*_-+="
    while True:
        syls = []
        total = 0
        while total < min_len - 2:
            s = rng.choice(SYLLABLES)
            if rng.random() < 0.5:
                s = s.capitalize()
            syls.append(s)
            total += len(s)
        digits = "".join(rng.choice(string.digits) for _ in range(rng.randint(2, 3)))
        base = "".join(syls)[: min_len - len(digits) - (1 if special else 0)]
        pw = base + digits
        if special:
            pw += rng.choice(specs)
        if min_len <= len(pw) <= max(24, min_len) and any(c.isdigit() for c in pw):
            return pw


def email_login(first, surname, rng=None, digits=None):
    """Упорядоченные кандидаты локальной части - СРАЗУ с цифрами.

    «Красивые» имя.фамилия без цифр почти всегда заняты на живых сервисах,
    а реалистичные адреса с годом рождения (isla.brown87 и т.п.) свободны
    и выглядят по-человечески. digits - исторический параметр, игнорируется.
    Возвращает СПИСОК кандидатов (сначала детерминированные, потом добивка)."""
    rng = rng or random
    f = first.lower()
    l = surname.lower()
    year = rng.randint(1955, 2005)
    yy = "%02d" % (year % 100)
    y4 = str(year)
    cands = [
        f"{f}.{l}{yy}",            # isla.brown87
        f"{f}.{l}{y4}",            # isla.brown1987
        f"{f}{l}{yy}",             # islabrown87
        f"{f}.{l}.{yy}",           # isla.brown.87
        f"{f[0]}.{l}{yy}",         # i.brown87
        f"{f[0]}{l}{y4}",          # ibrown1987
        f"{f}.{l}{yy}{rng.randint(1, 99):02d}",
    ]
    # добивка на случай, если всё выше занято на сервисе
    for _ in range(40):
        cands.append(f"{f}.{l}{rng.randint(1955, 2005)}{rng.randint(10, 99)}")
    return cands


def available_local_part(con, provider, first, surname, rng=None):
    """Подобрать не занятый в НАШЕЙ БД логин для провайдера.

    Идём по упорядоченному списку email_login (сразу с цифрами);
    занятость на стороне сервиса живым тестом проверяет драйвер."""
    rng = rng or random
    if provider["domains"] and provider["domains"] != "[]":
        domains = provider["domains"] if isinstance(provider["domains"], list) \
            else json.loads(provider["domains"])
    else:
        domains = [provider["name"]]
    seen = set()
    for lp in email_login(first, surname, rng):
        lp = lp.lower()
        if lp in seen:
            continue
        seen.add(lp)
        email = f"{lp}@{domains[0]}"
        existing = con.execute("SELECT 1 FROM mailboxes WHERE email=?",
                               (email,)).fetchone()
        if not existing:
            return email, lp
    return None, None
