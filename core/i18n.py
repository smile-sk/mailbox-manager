# -*- coding: utf-8 -*-
"""
core/i18n.py - локализация mailbox_manager.

Формат - как в ai_ui: папка locales/, файл <code>.json - плоский словарь
"ключ": "строка" (можно с плейсхолдерами {name}) + служебный "_meta":
{"name": "Русский"} для меню выбора языка.

Правила:
  * tr("ключ") - перевод текущего языка;
  * ключа нет в текущем языке -> ищем в ru.json (язык по умолчанию),
    нет и там -> возвращаем сам ключ (видно, что строка не переведена);
  * плейсхолдеры: tr("log.ready", n=5) подставит {n}, если строка его содержит.

Добавить язык = положить рядом locales/en.json (скопировать ru.json и
перевести значения). При следующем старте программа сама увидит новый язык
и покажет его в меню выбора (вкладка сверху справа).
"""
import json
import os

from core import paths

DEFAULT = "ru"

_catalog = {}
_code = DEFAULT


def _load(code):
    """Прочитать locales/<code>.json. Ищем рядом с программой (туда можно
    положить свой перевод), затем внутри сборки - перевод может лежать в exe."""
    for d in paths.locales_dirs():
        path = os.path.join(d, code + ".json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            continue
    return None


def available():
    """{code: display_name} всех языков, что лежат в locales/."""
    out = {}
    for d in paths.locales_dirs():
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            code = fn[:-5]
            if code in out:          # первый найденный перевод и главнее
                continue
            data = _load(code)
            if data is None:
                continue
            meta = data.get("_meta") or {}
            out[code] = meta.get("name", code)
    return out


def lang_name(code):
    return available().get(code, code)


def code():
    return _code


def set_lang(code):
    global _catalog, _code
    data = _load(code)
    if data is None:
        code = DEFAULT
        data = _load(DEFAULT)
    _catalog = data or {}
    _code = code
    return _code


def tr(key, **kw):
    val = _catalog.get(key)
    if val is None and _code != DEFAULT:
        fb = _load(DEFAULT) or {}
        val = fb.get(key)
    if val is None:
        val = key
    if kw and "{" in val:
        try:
            val = val.format(**kw)
        except (KeyError, IndexError, ValueError):
            pass
    return val


set_lang(DEFAULT)
