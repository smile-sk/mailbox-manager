# -*- coding: utf-8 -*-
"""
tuta.py - драйвер авторегистрации Tuta (app.tuta.com/signup).

КАРТА ПУТИ (снята живьём 13.09.2026; селекторы по data-testid, чтобы не
зависеть от языка интерфейса):

 1. ТАРИФ - карточки .buyOptionBox-v2 в порядке:
      [0] Revolutionary  3 EUR/мес
      [1] Legend         8 EUR/мес
      [2] Free           1 Гб
    ПО УМОЛЧАНИЮ Tuta держит выбранным ПЛАТНЫЙ Legend (как Proton Unlimited).
    Признак выбора - ТОЛЬКО input[type=radio].checked внутри карточки
    (+ у выбранной inline-стиль scale:1.03). Классы `cursor-pointer`/`selected`
    ВРУТ. JS-клик (.click() из скрипта) до модели React НЕ доходит -
    нужен настоящий клик мышью Playwright.

 2. ФОРМА - [data-testid="tfi:username_label"], "tfi:newPassword_label",
    "tfi:repeatedPassword_label"; домен @tutamail.com (переключается
    "btn:domain_label"); два чекбокса input.list-checkbox (БЕЗ data-testid);
    кнопка "btn:create_new_account_label".
    ВЕРДИКТЫ - ЧИСТЫЙ ТЕКСТ: по имени в div.mt-8
    («Адрес электронной почты доступен.» / «Этот адрес недоступен.»),
    по паролю вообще без класса («Пароль сгодится.» / «... недостаточно
    надёжный»). Кнопка «Создать аккаунт» активна ДАЖЕ при занятом имени -
    поэтому смотрим на ВЕРДИКТ, а не на состояние кнопки.

 3. «Готовится учётная запись …» (~30 с, клиентская генерация ключей; кнопка
    выглядит зависшей - это норма) → СОБСТВЕННАЯ КАПЧА Tuta
    ("dialog:captchaDisplay_label" - ФОТО аналоговых часов, ввод времени в
    "tfi:captcha_input", кнопка "btn:ok_action"). Решает ЧЕЛОВЕК: драйвер
    доводит до неё, пишет в журнал, поднимает окно и ждёт исчезновения диалога.

 4. RECOVERY KIT - код в "monoTextContent" (ЧЕТЫРЕ строки хекса, не слова, как
    у Proton); ДВЕ кнопки с ОДИНАКОВЫМ "btn:recoveryCode_label"
    («Скопировать код восстановления» и «Скачать PDF») - различать по тексту;
    галочка «Я понимаю риски…» БЕЗ data-testid держит кнопку
    "btn:recovery_kit_page_continue_label" («Давайте начнем») выключенной.

 5. Дальше визард ведёт на /login: «Выполняется вход …», кнопка входа названа
    САМИМ АДРЕСОМ, полей ввода на этом экране нет.

ЧЕСТНО ПРО ОГРАНИЧЕНИЯ: автопилот не решает капчу (нужно зрение) и не платит.
У Free-тарифа Tuta аккаунт УДАЛЯЕТСЯ после 6 месяцев неактивности - это
написано на самой карточке Free.
"""
import os
import re
import time

from core import db
from core import generator
from core import browser as br
from core import proxy as proxy_mod
from core import fingerprint
from core import paths
from core.signup import _body_text, _js_fill, _wait_for, _click_text, logger_exc


SIGNUP_URL = "https://app.tuta.com/signup"

# --- селекторы (значения data-testid - из живой формы 13.09.2026) ---
SEL_USER = '[data-testid="tfi:username_label"]'
SEL_PASS = '[data-testid="tfi:newPassword_label"]'
SEL_PASS2 = '[data-testid="tfi:repeatedPassword_label"]'
SEL_DOMAIN = '[data-testid="btn:domain_label"]'
SEL_CREATE = '[data-testid="btn:create_new_account_label"]'
SEL_CONTINUE = '[data-testid="btn:continue_action"]'
SEL_CAPTCHA_IMG = '[data-testid="dialog:captchaDisplay_label"]'
SEL_CAPTCHA_IN = '[data-testid="tfi:captcha_input"]'
SEL_CAPTCHA_OK = '[data-testid="btn:ok_action"]'
SEL_REC_CONT = '[data-testid="btn:recovery_kit_page_continue_label"]'
SEL_MONO = '[data-testid="monoTextContent"]'

# вердикты Tuta - простой текст, различаем по словам.
# ВАЖНО: «доступен» входит в «недоступен» - сначала проверяем ОТРИЦАНИЕ.
_NAME_TAKEN = re.compile(r"недоступ|not available|already|занят|taken", re.I)
_NAME_OK = re.compile(r"доступ|available", re.I)
_PASS_BAD = re.compile(r"недостаточн|ненадёжн|ненадежн|not strong|weak|слаб", re.I)
_RE_LOGIN = re.compile(r"выполняется вход|logging you in|logging in", re.I)
_RE_RECOVERY = re.compile(r"код восстановлени|recovery code|recovery kit", re.I)

# тарифы: отдаём состояние карточек как есть (title + отмечено ли радио)
PLAN_JS = r"""() => [...document.querySelectorAll('.buyOptionBox-v2')].map(c => {
    const r = c.querySelector('input[type=radio]');
    const t = (c.innerText || '').replace(/\s+/g, ' ').trim();
    return { title: (t.split(' ')[0] || ''), checked: !!(r && r.checked) };
})"""


def _plan_cards(page):
    """Состояние карточек тарифов: [{title, checked}]."""
    try:
        return page.evaluate(PLAN_JS) or []
    except Exception:
        return []


def _click_testid(page, tid, timeout=8000):
    """Клик по элементу с данным data-testid. True/False."""
    try:
        page.locator('[data-testid="%s"]' % tid).first.click(timeout=timeout)
        return True
    except Exception:
        return False


def _pick_free_plan(page, log):
    """Выбрать тариф Free и ПРОВЕРИТЬ выбор.

    Тут ошибка означала бы покупку платного тарифа (Tuta по умолчанию держит
    Legend за 8 EUR/мес), поэтому проверка обязательна: клик мышью по названию
    плана + подтверждение по отмеченному радио. Не подтвердилось - возвращаем
    False, и вызывающий обязан остановиться.
    """
    if not _wait_for(page, lambda: len(_plan_cards(page)) >= 3, 30):
        log("Tuta: карточки тарифов не появились")
        return False

    cards = _plan_cards(page)
    free_i = None
    for i, c in enumerate(cards):
        if (c.get("title") or "").strip().lower().startswith("free"):
            free_i = i
            break
    if free_i is None:
        log("Tuta: карточка Free не найдена среди %d тарифов" % len(cards))
        return False
    if cards[free_i].get("checked"):
        log("Tuta: тариф Free уже выбран")
        return True

    log("Tuta: по умолчанию выбран ПЛАТНЫЙ тариф - переключаюсь на Free")
    # Реальный клик мышью: JS-клик модель React не замечает.
    clicked = False
    try:
        card = page.locator(".buyOptionBox-v2").nth(free_i)
        card.get_by_text("Free", exact=True).first.click(timeout=6000)
        clicked = True
    except Exception as e:
        log("Tuta: клик по названию Free не прошёл (%s) - пробую иначе" % e)
    if not clicked:
        try:
            page.get_by_text("Free", exact=True).first.click(timeout=6000)
            clicked = True
        except Exception as e:
            log("Tuta: не смогла кликнуть по Free: %s" % e)
    if not clicked:
        return False

    ok = _wait_for(page, lambda: bool(_plan_cards(page)[free_i].get("checked")), 10)
    if ok:
        log("Tuta: тариф Free выбран (проверено по отмеченному радио)")
    else:
        log("Tuta: тариф Free НЕ подтвердился - остановка, чтобы не улететь на платный")
    return bool(ok)


def _name_verdict(page):
    """Вердикт Tuta по имени: он лежит в div.mt-8 чистым текстом."""
    try:
        return (page.evaluate(
            "() => { const e = document.querySelector('div.mt-8');"
            " return e ? e.innerText.trim() : ''; }") or "").strip()
    except Exception:
        return ""


def _pass_verdict(page):
    """Вердикт по паролю - тоже текст, но без своего класса."""
    try:
        return (page.evaluate(r"""() => {
            const pat = /сгодится|надёжн|надежн|suitable|not strong|strong enough/i;
            for (const e of document.querySelectorAll('div,span,p')) {
                const t = (e.innerText || '').trim();
                if (t && t.length < 120 && e.children.length === 0 && pat.test(t)) return t;
            }
            return '';
        }""") or "").strip()
    except Exception:
        return ""


def _check_boxes(page):
    """Поставить галочки согласия. У них нет data-testid, а клик по видимому
    тексту перехватывает <label> - поэтому жмём по label/input программно.
    Возвращает число нажатых."""
    try:
        return page.evaluate(r"""() => {
            let n = 0;
            document.querySelectorAll('input.list-checkbox').forEach(cb => {
                if (cb.checked) return;
                const lab = cb.closest('label')
                    || (cb.id ? document.querySelector('label[for="' + cb.id + '"]') : null);
                if (lab) lab.click(); else cb.click();
                n++;
            });
            return n;
        }""")
    except Exception:
        return 0


def _boxes_checked(page):
    try:
        return page.evaluate(
            "() => { const a = [...document.querySelectorAll('input.list-checkbox')];"
            " return [a.filter(c => c.checked).length, a.length]; }")
    except Exception:
        return [0, 0]


def _captcha_present(page):
    """Капча Tuta на экране ПРЯМО СЕЙЧАС?

    Раньше смотрели два конкретных data-testid и только «элемент есть в DOM».
    Живой случай 19.09.2026: человек начал вводить время в капчу, драйвер счёл
    капчу пройденной, закрыл браузер и объявил ящик готовым - а регистрация не
    завершилась (вход в ящик потом не подходил). Поэтому теперь проверяем
    ВИДИМОСТЬ, а не наличие в разметке, и не залипаем на одном селекторе.
    """
    for sel in (SEL_CAPTCHA_IMG, SEL_CAPTCHA_IN):
        try:
            if page.locator(sel).first.is_visible():
                return True
        except Exception:
            pass
    try:
        return bool(page.evaluate(r"""() => {
            const shown = (e) => {
                const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                return r.width > 2 && r.height > 2
                    && s.visibility !== 'hidden' && s.display !== 'none';
            };
            for (const e of document.querySelectorAll('[data-testid]')) {
                const t = (e.getAttribute('data-testid') || '').toLowerCase();
                if (!t.includes('captcha')) continue;
                if (shown(e)) return true;
                const w = e.querySelector('input,img,canvas,svg');
                if (w && shown(w)) return true;
            }
            return false;
        }"""))
    except Exception:
        return False


def _visible_testid(page, needle):
    """Есть ли на экране ВИДИМЫЙ элемент, в data-testid которого входит needle."""
    try:
        return bool(page.evaluate(r"""(needle) => {
            const shown = (e) => {
                const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                return r.width > 2 && r.height > 2
                    && s.visibility !== 'hidden' && s.display !== 'none';
            };
            for (const e of document.querySelectorAll('[data-testid]')) {
                const t = (e.getAttribute('data-testid') || '').toLowerCase();
                if (t.includes(needle) && shown(e)) return true;
            }
            return false;
        }""", (needle or "").lower()))
    except Exception:
        return False


def _shot(page, log, tag):
    """Снимок экрана в logs/ - после разбора видно, ЧТО было на экране."""
    path = os.path.join(paths.logs_dir(),
                        "tuta_%s_%s.png" % (tag, time.strftime("%Y%m%d_%H%M%S")))
    try:
        page.screenshot(path=path, full_page=False)
        log("Tuta: снимок экрана - %s" % path)
    except Exception as e:
        log("Tuta: снимок экрана не сделался (%s)" % str(e)[:80])
    return path


def _step_now(page):
    """Что сейчас на экране: 'captcha' | 'form' | 'recovery' | 'login' | 'unknown'.

    Порядок проверок важнее всего, и первым идёт капча. Раньше шаг определялся по
    ТЕКСТУ всей страницы, и подпись галочки на форме регистрации (там есть слова
    про код восстановления) принималась за экран Recovery Kit: драйвер «завершал»
    регистрацию через секунду после нажатия «Создать аккаунт», не дождавшись
    капчи. Теперь у каждого шага - свой признак на экране, а не слово в тексте.
    """
    if _captcha_present(page):
        return "captcha"
    try:
        if page.locator(SEL_USER).first.is_visible():
            return "form"
    except Exception:
        pass
    if _visible_testid(page, "monotextcontent") or _visible_testid(page, "recovery"):
        return "recovery"
    try:
        if "/login" in (page.url or ""):
            return "login"
    except Exception:
        pass
    if _RE_LOGIN.search(_body_text(page)):
        return "login"
    return "unknown"


def _await_next_step(page, log, timeout=900):
    """Ждать, пока человек пройдёт капчу, и вернуть следующий шаг.

    Возвращает 'recovery' | 'login' | 'failed' | 'timeout'.
    Железные правила (после живого разбора 19.09.2026):
      • пока капча на экране - не двигаемся дальше, сколько бы человек ни вводил;
      • следующий шаг принимаем ТОЛЬКО как положительный признак на экране: экран
        Recovery Kit (по видимому data-testid) или страница /login. Никаких
        «в тексте где-то мелькнуло слово recovery»;
      • капча была, а вернулась форма регистрации - регистрация НЕ состоялась:
        возвращаем 'failed', и вызывающий обязан сказать это вслух, а не «готово».
    """
    told = False
    seen_captcha = False
    form_back = 0
    last = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        step = _step_now(page)
        if step != last:
            log("Tuta: на экране - %s (%s)" % (step, (page.url or "")[:90]))
            last = step

        if step == "captcha":
            if not seen_captcha:
                seen_captcha = True
                _shot(page, log, "captcha")
            if not told:
                log("Tuta: НУЖЕН ЧЕЛОВЕК - капча Tuta: на картинке аналоговые часы. "
                    "Введи время (чч:мм) в поле в окне браузера и нажми «Окей». "
                    "Дальше не двинусь, пока капча на экране - вводи спокойно.")
                told = True
                try:
                    page.bring_to_front()
                except Exception:
                    pass
            time.sleep(2)
            continue

        if told:
            log("Tuta: капча с экрана ушла - смотрю, чем закончилось")
            told = False

        if step == "recovery":
            _shot(page, log, "recovery")
            return "recovery"
        if step == "login":
            _shot(page, log, "login")
            return "login"
        if step == "form" and seen_captcha:
            # капча закрылась, а форма регистрации вернулась → ввод не принят.
            # Три подряд - значит это не мигание перерисовки, а правда.
            form_back += 1
            if form_back >= 3:
                _shot(page, log, "captcha_failed")
                log("Tuta: капча НЕ принята - вернулась форма регистрации. "
                    "Регистрация не завершена.")
                return "failed"
        time.sleep(1.5)
    return "timeout"


def _grab_recovery(page, log):
    """Считать код восстановления Tuta и пройти шаг Recovery Kit.

    Код - четыре строки хекса (не слова). Кнопки «Скопировать код восстановления»
    и «Скачать PDF» имеют ОДИНАКОВЫЙ data-testid, а «Давайте начнем» держит
    выключенной галочка без data-testid.
    """
    code = ""
    for sel in (SEL_MONO, '[data-testid*="recoveryCode"]',
                '[data-testid*="recovery_code"]', 'pre', 'code'):
        try:
            loc = page.locator(sel).first
            if loc.count():
                t = (loc.inner_text(timeout=4000) or "").strip()
                if t and len(t) >= 8:
                    code = t
                    break
        except Exception:
            continue
    if not code:
        # последний шанс: блок из нескольких строк, состоящих из хекса
        try:
            code = (page.evaluate(r"""() => {
                const hex = /^[0-9A-Fa-f\s\-]+$/;
                for (const e of document.querySelectorAll('div,pre,code,span')) {
                    const t = (e.innerText || '').trim();
                    if (!t || t.length < 16 || t.length > 400) continue;
                    if (t.split('\n').length >= 2 && hex.test(t)) return t;
                }
                return '';
            }""") or "").strip()
        except Exception:
            pass

    # галочка «Я понимаю риски…» (без data-testid) - без неё кнопка выключена
    try:
        page.evaluate("() => document.querySelectorAll('input[type=checkbox]')"
                      ".forEach(cb => { if (!cb.checked) cb.click(); })")
    except Exception:
        pass
    time.sleep(0.8)

    if not _click_testid(page, "btn:recovery_kit_page_continue_label", timeout=8000):
        _click_text(page, re.compile(r"начн|начать|start|continue", re.I), timeout=4000)
    return code


def tuta_signup(con, mailbox_id, log=print):
    """Авторегистрация ящика Tuta до конца, кроме капчи (её решает человек).

    Возвращает (ok, msg) - как proton_signup, чтобы вызывающий код не различал
    драйверы.
    """
    mb = db.get_mailbox(con, mailbox_id)
    if mb is None:
        return False, "ящик не найден"
    prov = db.get_provider(con, mb["provider_id"])
    email0 = mb["email"] or ""
    login0 = mb["login"] or email0.split("@")[0]
    password = mb["password"]
    person_name = mb["person_name"] or ""
    parts = person_name.split()
    first = parts[0] if parts else "Alex"
    surname = parts[-1] if len(parts) > 1 else "Smith"

    # кандидаты логинов: базовый + упорядоченные с цифрами (занятое имя → следующий)
    cands, seen = [], set()
    base = re.sub(r"[^a-z0-9._]", "", (login0 or "").lower())
    for lp in ([base] if base else []) + list(generator.email_login(first, surname)):
        lp = (lp or "").lower()
        if lp and lp not in seen:
            seen.add(lp)
            cands.append(lp)
        if len(cands) >= 10:
            break
    if not cands:
        cands = ["mail%d" % mailbox_id]

    # профиль - по id ящика: адрес при регистрации меняется (занятое имя →
    # следующий кандидат), а куки/сессия должны остаться те же.
    profile_dir = br.profile_dir_for(mailbox_id)
    os.makedirs(profile_dir, exist_ok=True)
    br.register_profile(con, mailbox_id, email0)
    # живой прокси для ящика (проверка + следующий свободный, если мёртв);
    # пул пуст - идём напрямую, это нормально.
    _prow, proxy_pw, pdesc = proxy_mod.resolve_for_mailbox(con, mailbox_id, log=log)
    db.update_mailbox(con, mailbox_id, status="creating",
                      note="идёт авторегистрация Tuta…")

    fp = fingerprint.for_mailbox(mailbox_id, max_screen=br.real_screen())
    log("Tuta: открываю браузер (профиль ящика, прокси: %s)" % pdesc)
    log("[браузер] отпечаток ящика: %s" % fp["label"])

    p = ctx = None
    try:
        # Тем же путём, что и обычное открытие ящика: отпечаток, прокси,
        # готовый размер окна (страница = окно).
        p, ctx, page = br._launch(profile_dir, proxy_pw=proxy_pw,
                                  headless=False, fp=fp, log=log)
        url = (prov["signup_url"] if prov else "") or SIGNUP_URL
        log("Tuta: открываю %s" % url)
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(4)

        # 1) тариф Free - строго с проверкой (по умолчанию у Tuta ПЛАТНЫЙ)
        if not _pick_free_plan(page, log):
            raise RuntimeError(
                "не смогла выбрать бесплатный тариф Free (Tuta по умолчанию "
                "держит платный Legend 8 EUR/мес) - авторегистрация остановлена, "
                "ничего не оплачено")

        # «Продолжить» → форма аккаунта
        if not _click_testid(page, "btn:continue_action"):
            _click_text(page, re.compile(r"продолжить|continue|weiter|далее", re.I))
        if not _wait_for(page, lambda: page.locator(SEL_USER).count() > 0, 30):
            raise RuntimeError("форма аккаунта не появилась (url=%s): %s"
                               % (page.url, _body_text(page)[:200]))
        log("Tuta: форма аккаунта открыта")

        # 2) имя + пароль. Вердикты - текст, поэтому читаем их, а не кнопку.
        used_login = None
        for lp in cands:
            _js_fill(page, SEL_USER, lp)
            _js_fill(page, SEL_PASS, password)
            _js_fill(page, SEL_PASS2, password)
            time.sleep(1.5)

            v = _name_verdict(page)
            if _NAME_TAKEN.search(v):
                log("Tuta: имя «%s» занято (%s) - пробую следующее" % (lp, v))
                continue
            if not _NAME_OK.search(v):
                log("Tuta: вердикт по имени «%s» неясен: %s" % (lp, v or "нет текста"))
                continue
            pv = _pass_verdict(page)
            if pv and _PASS_BAD.search(pv):
                # Tuta's own strength meter rejected the card's password. Do not
                # die: make a stronger one (with a special char), write it INTO
                # THE CARD (the card is the source of truth for login) and refill.
                stronger = None
                for _ in range(2):
                    cand = generator.password(max(12, len(password or "")), True)
                    _js_fill(page, SEL_PASS, cand)
                    _js_fill(page, SEL_PASS2, cand)
                    time.sleep(1.5)
                    pv2 = _pass_verdict(page)
                    if not (pv2 and _PASS_BAD.search(pv2)):
                        stronger = cand
                        break
                if not stronger:
                    raise RuntimeError("Tuta не приняла пароль даже после смены (%s)" % pv)
                password = stronger
                db.update_mailbox(con, mailbox_id, password=stronger)
                log("Tuta: пароль из карточки Tuta сочла слабым - придумала крепче "
                    "и сохранила в карточку ящика")
            used_login = lp
            log("Tuta: имя «%s» свободно (%s)" % (lp, v))
            break
        if not used_login:
            raise RuntimeError("не нашла свободное имя (проверила %d вариантов)"
                               % len(cands))

        new_email = used_login + "@tutamail.com"
        db.update_mailbox(con, mailbox_id, email=new_email, login=used_login)

        # 3) галочки согласия и «Создать аккаунт»
        n = _check_boxes(page)
        got, total = _boxes_checked(page)
        log("Tuta: галочки согласия: отмечено %d из %d (нажала %d)"
            % (got, total, n))
        if total and got < total:
            raise RuntimeError("не удалось отметить все галочки согласия (%d из %d)"
                               % (got, total))
        time.sleep(0.6)
        if not _click_testid(page, "btn:create_new_account_label"):
            raise RuntimeError("не нашла кнопку «Создать аккаунт»")
        log("Tuta: нажала «Создать аккаунт» - идёт клиентская генерация ключей "
            "(~30 с), кнопка кажется зависшей, это норма")

        # 4) ждём капчу (её проходит человек) и следующий шаг
        step = _await_next_step(page, log, timeout=15 * 60)
        code = ""
        if step == "failed":
            raise RuntimeError(
                "капча Tuta не пройдена - регистрация НЕ завершена, ящик не создан. "
                "Нажми «Доделать авторегистрацию» - пройду всё заново; капчу решай "
                "спокойно: пока она на экране, дальше я не двинусь")
        if step == "recovery":
            code = _grab_recovery(page, log)
            if code:
                db.update_mailbox(con, mailbox_id,
                                  note="recovery: " + code.replace("\n", " "))
                log("Tuta: код восстановления сохранён в карточку ящика")
                time.sleep(2)
            else:
                log("Tuta: экран Recovery Kit - код прочитать не удалось, забери "
                    "вручную (кнопки «Скопировать код восстановления» / «Скачать PDF»)")
        elif step == "timeout":
            raise RuntimeError("не дождалась следующего шага за 15 минут")

        # 5) итог: заметка честная, статус - verified
        cur_note = ""
        try:
            cur_note = db.get_mailbox(con, mailbox_id)["note"] or ""
        except Exception:
            pass
        if cur_note.startswith("recovery:"):
            db.update_mailbox(con, mailbox_id, status="verified",
                              note=cur_note + " · создан автопилотом "
                              + time.strftime("%d.%m.%Y"))
        else:
            db.update_mailbox(
                con, mailbox_id, status="verified",
                note="создан автопилотом %s · код восстановления НЕ сохранён - "
                     "забери вручную (Tuta → Recovery Kit)"
                     % time.strftime("%d.%m.%Y"))
            log("Tuta: ВНИМАНИЕ - код восстановления не сохранён. Ящик работает, "
                "но код лучше забрать вручную!")
        log("Tuta: аккаунт %s готов. Пароль - в карточке ящика." % new_email)
        return True, "ok"
    except Exception as e:
        logger_exc(log, e)
        try:
            db.update_mailbox(con, mailbox_id, status="created",
                              note="авторегистрация не завершилась: %s" % e)
        except Exception:
            pass
        log("Tuta: ошибка - %s. Ящик остался черновиком (можно доделать вручную)." % e)
        return False, str(e)
    finally:
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
