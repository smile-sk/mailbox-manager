# -*- coding: utf-8 -*-
"""
signup.py - автопилоты регистрации почтовых ящиков.

Первый драйвер - proton (proton.me): регистрация БЕЗ телефона и SMS.
Карта формы снята живым тестом 07-08.09.2026:
  1) выбор плана Free - кликать ТОЛЬКО по самой карточке .card-plan,
     у которой заголовок ровно «Free» (клик по тексту «Free/Бесплатно»
     может уйти в платную карточку: там бывают «free trial» и т.п.);
  2) форма аккаунта: #username (кнопка домена #select-domain),
     #password, #password-confirm - на одной странице-слайдере.
     ВАЖНО: Proton проверяет занятость имени НЕ сразу при вводе,
     а когда шаг имени активен и/или ПОСЛЕ отправки формы.
     Поэтому драйвер: пароль → имя → отправка → смотрим исход:
     окно верификации (ок) или «имя занято» (пробуем следующий вариант);
  3) окно Human Verification: путь «CAPTCHA» - паззл решает человек
     (единственное ручное действие);
  4) recovery phrase - читаем слова со страницы и сохраняем в БД;
  5) отображаемое имя + пропуск импорта;
  6) вход в ящик → статус verified.

Если адрес занят - автопилот сам пробует следующий вариант логина
(до 10), человек этого не видит.

Каждый шаг логируется. Первый живой прогон может потребовать подстройки
селекторов - правки идут по logs/app.log.
"""
import json
import os
import re
import time

from core import db, generator
from core import browser as br
from core import proxy

# маркеры текста на страницах Proton (ru/en)
_TAKEN = re.compile(
    r"already|taken|unavailable|not available|in use|used|используется|занят|занято|"
    r"недоступ|зарезервирован|адрес занят", re.I)
_RECOVERY = re.compile(r"recovery|восстанов", re.I)
_WORD_CONFIRM = re.compile(r"word\s*#?\s*(\d+)|слов[аоы]\s*№?\s*(\d+)", re.I)
_MAILBOX = re.compile(r"Входящие|Inbox|Написать|Compose|New message", re.I)
_DISPLAY_NAME = re.compile(r"display name|отображаемое имя", re.I)
_SKIP = re.compile(r"skip|пропустить|не сейчас|later|позже|не надо|no thanks|нет, спасибо|"
                   r"maybe later|not now", re.I)
_CONTINUE = re.compile(r"continue|продолжить|далее|next", re.I)
# маркер УСПЕХА отправки формы: окно Human Verification (CAPTCHA / эл. почта)
_VERIFY_OK = re.compile(r"CAPTCHA|верификац|verification|human|Эл\.?\s*почта|проверка", re.I)
_PAYMENT = re.compile(r"checkout|payment|pay\.|оплат|заплат", re.I)


def _body_text(page):
    """Текст страницы: body + все iframe'ы + скрытые контейнеры ошибок Proton.

    ВАЖНО: Proton держит поле имени и его ошибки в скрытой off-screen панели
    (visibility:hidden), а body.innerText скрытые элементы НЕ включает -
    поэтому «Имя пользователя уже используется» обычным чтением не видно.
    Дополнительно читаем textContent .field-two-container напрямую.
    НО: по всему тексту страницы НЕЛЬЗЯ искать слова already/used/taken -
    там постоянно есть «Already have an account?» и т.п. (ложные «занято»).
    Вердикт по имени - только через _sample_taken()/_username_status().
    """
    texts = []
    try:
        t = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        if t.strip():
            texts.append(t)
    except Exception:
        pass
    try:
        t = page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('.field-two-container, [class*="field-two"]')
                .forEach(e => { const t = (e.textContent || '').trim(); if (t) out.push(t); });
            return out.join(' | ');
        }""") or ""
        if t.strip():
            texts.append(t)
    except Exception:
        pass
    try:
        for fr in page.frames:
            try:
                t = fr.evaluate("() => document.body ? document.body.innerText : ''") or ""
                if t.strip():
                    texts.append(t)
            except Exception:
                pass
    except Exception:
        pass
    return "\n".join(texts)


def _username_status(page):
    """Вердикт Proton по полю имени - читает СКРЫТУЮ панель напрямую
    (body.innerText её не видит). Возвращает:
      'taken' - имя занято/невалидно (есть текст ошибки);
      'ok'    - ошибки нет (свободно или проверка ещё не началась);
      ''      - панель не найдена.
    ВАЖНО: смотреть ТОЛЬКО сюда, а не на весь текст страницы."""
    try:
        return page.evaluate("""() => {
            const c = document.querySelector('.field-two-container');
            if (!c) return '';
            const t = (c.textContent || '').trim();
            const cls = ' ' + (c.className || '') + ' ';
            if (/invalid|error/i.test(cls)) {
                if (/already|taken|in use|unavailable|используется|занят|занято|недоступ|зарезервирован/i.test(t)) {
                    return 'taken';
                }
                return 'invalid';
            }
            return 'ok';
        }""") or ''
    except Exception:
        return ''


def _sample_taken(page):
    """True, если контейнер поля имени сейчас в состоянии «занято»
    (класс invalid/error + текст ошибки). Читаем ТОЛЬКО .field-two-container,
    а не весь текст страницы: там постоянно есть «Already have an account»
    и подобные нейтральные фразы со словами already/used/taken - они
    давали ложные «занято» и рушили всю регистрацию."""
    try:
        return bool(page.evaluate("""() => {
            const c = document.querySelector('.field-two-container');
            if (!c) return false;
            const t = (c.textContent || '').trim();
            const cls = ' ' + (c.className || '') + ' ';
            if (!/invalid|error/i.test(cls)) return false;
            return /already in use|already taken|address taken|not available|unavailable|used|используется|занят|занято|недоступ|зарезервирован|уже используется/i.test(t);
        }"""))
    except Exception:
        return False


def _wait_username_verdict(page, timeout=10.0):
    """Ждать вердикт Proton по имени (проверка на лету, 1-4 с после ввода).

    Не путаем СВЕЖУЮ ошибку «занято» со СТАРОЙ: после ввода нового имени
    Proton ~1 с держит предыдущий ответ (класс invalid + старый текст),
    потом сбрасывает и проверяет заново. Поэтому:
      - первые 1.2 с после ввода не оцениваем;
      - 'taken' - только если ошибка держится непрерывно >=1.8 с;
      - 'ok' - Proton снял ошибку и держит «свободно» >=1.2 с.
    Возвращает 'taken' | 'ok' | 'unknown' (unknown - отправляем форму,
    финальный вердикт вынесет _wait_submit_outcome)."""
    time.sleep(1.2)
    deadline = time.time() + timeout
    taken_since = ok_since = None
    while time.time() < deadline:
        st = _username_status(page)
        if st == "taken":
            ok_since = None
            taken_since = time.time() if taken_since is None else taken_since
            if time.time() - taken_since >= 1.8:
                return "taken"
        elif st == "ok":
            taken_since = None
            ok_since = time.time() if ok_since is None else ok_since
            if time.time() - ok_since >= 1.2:
                return "ok"
        else:
            taken_since = None
            ok_since = None
        time.sleep(0.3)
    return "unknown"


def _js_fill(page, selector, value):
    """Заполнить поле через нативный сеттер + input-событие (обходит
    «скрытые копии» полей, которыми Proton прикрывается от ботов)."""
    try:
        ok = page.evaluate("""(args) => {
            const [sel, val] = args;
            const el = document.querySelector(sel);
            if (!el) return false;
            const proto = el instanceof HTMLTextAreaElement
                ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.blur();
            return true;
        }""", [selector, value])
        return bool(ok)
    except Exception:
        return False


def _click_text(page, pattern, timeout=4000):
    """Кликнуть по КНОПКЕ/ССЫЛКЕ, чей видимый текст совпадает с pattern.
    Никогда не кликаем по произвольным div/span (это уводило на платный план)."""
    try:
        if isinstance(pattern, str):
            page.get_by_role("button", name=re.compile(re.escape(pattern), re.I)).first.click(timeout=timeout)
            return True
        page.get_by_role("button", name=pattern).first.click(timeout=timeout)
        return True
    except Exception:
        pass
    try:
        if isinstance(pattern, str):
            page.get_by_role("link", name=re.compile(re.escape(pattern), re.I)).first.click(timeout=timeout)
        else:
            page.get_by_role("link", name=pattern).first.click(timeout=timeout)
        return True
    except Exception:
        return False


def _wait_for(page, fn, timeout, step=0.3):
    """Ждать, пока fn() вернёт истину (или до таймаута)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def _extract_words(text):
    """Попытаться вытащить recovery phrase: >=20 латинских слов подряд."""
    for para in re.split(r"\n\s*\n", text):
        words = re.findall(r"[A-Za-z]{3,12}", para)
        if len(words) >= 20:
            joined = " ".join(words)
            if len(joined) <= 400:
                return words
    return []


def _select_free_plan(page, log):
    """Выбрать карточку Free на странице планов Proton.

    Клик - locator.click() по САМОМУ элементу (React его принимает),
    а НЕ mouse.click по координатам: тот уходит мимо, и Proton по умолчанию
    так и держит выбранным Unlimited $9.99 - отсюда «платный тариф за $10».
    ПОСЛЕ клика обязательно проверяем aria-pressed карточки Free:
    не выбралась - пробуем снова; за 3 попытки не вышло - False
    (вызывающий код ОБЯЗАН остановиться, а не продолжать на платном)."""
    for attempt in range(3):
        try:
            # 1) найти индекс карточки Free
            idx = page.evaluate("""() => {
                const cards = [...document.querySelectorAll('.card-plan')];
                for (let i = 0; i < cards.length; i++) {
                    const t = (cards[i].innerText || '').trim();
                    const first = t.split(String.fromCharCode(10))[0].trim() || '';
                    if (/^Free$/i.test(first) || /^Бесплатно$/i.test(first)) return i;
                }
                return -1;
            }""")
            if idx is None or int(idx) < 0:
                log("Proton: карточка Free не найдена (попытка %d)" % (attempt + 1))
                time.sleep(1.5)
                continue
            # 2) клик по самому элементу (locator.click - реальный клик React)
            page.locator(".card-plan").nth(int(idx)).click(timeout=6000)
            time.sleep(2)
            # 3) ПРОВЕРКА: Free теперь выбран?
            pressed = page.evaluate("""() => {
                const cards = [...document.querySelectorAll('.card-plan')];
                for (const c of cards) {
                    const t = (c.innerText || '').trim();
                    const first = t.split(String.fromCharCode(10))[0].trim() || '';
                    if (/^Free$/i.test(first) || /^Бесплатно$/i.test(first))
                        return c.getAttribute('aria-pressed');
                }
                return null;
            }""")
            if pressed and str(pressed).strip().lower() == "true":
                log("Proton: выбран бесплатный тариф Free (aria-pressed=true, проверено)")
                return True
            log("Proton: после клика Free НЕ выбран (aria-pressed=%r) - повтор %d"
                % (pressed, attempt + 1))
        except Exception as e:
            log("Proton: клик по Free не удался (%s) - повтор %d" % (e, attempt + 1))
        time.sleep(1.5)
    log("Proton: НЕ удалось выбрать Free за 3 попытки - вызывающий код остановится")
    return False


def _fill_password_fields(page, password):
    """Заполнить поле пароля и подтверждение (оба появятся не сразу)."""
    if not _wait_for(page, lambda: page.locator("input[type=password]").count() >= 1, 30):
        return False
    page.locator("input[type=password]").nth(0).fill(password)
    _wait_for(page, lambda: page.locator("input[type=password]").count() >= 2, 8)
    pw = page.locator("input[type=password]")
    if pw.count() >= 2:
        pw.nth(1).fill(password)
    time.sleep(1.5)
    return True


def _try_login_candidate(page, login, wait=3.0):
    """Ввести логин (нативный сеттер), вернуть True, если поле найдено."""
    filled = False
    for sel in ("#username", "input[name=username]", "input[autocomplete=username]"):
        filled = _js_fill(page, sel, login)
        if filled:
            break
    if not filled:
        try:
            filled = page.evaluate("""(v) => {
                const ins = [...document.querySelectorAll('input:not([type=hidden]):not([type=password])')]
                    .filter(i => { const r = i.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
                if (!ins.length) return false;
                const el = ins[0];
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(el, v);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }""", login)
        except Exception:
            filled = False
    time.sleep(wait)
    return filled


def _has_create_btn(page):
    try:
        for pat in (re.compile(r"Create account|Начать использовать", re.I),
                    re.compile(r"Continue|Продолжить|Далее", re.I)):
            b = page.get_by_role("button", name=pat).first
            if b.count() and b.is_visible():
                return True
        return False
    except Exception:
        return False


def _click_submit_create(page):
    """Нажать кнопку создания аккаунта. Вернуть True, если нажали."""
    for pat in (re.compile(r"Create account|Начать использовать", re.I),
                re.compile(r"Continue|Продолжить|Далее", re.I)):
        try:
            b = page.get_by_role("button", name=pat).first
            _wait_for(page, lambda: b.count() and b.is_enabled() and b.is_visible(), 8)
            if b.count() and b.is_enabled():
                b.click(timeout=5000)
                return True
        except Exception:
            continue
    try:
        page.locator("button[type=submit]").first.click(timeout=5000)
        return True
    except Exception:
        return False


def _wait_submit_outcome(page, log):
    """Ждать исход отправки формы (до ~30 с):
    'ok' - появилось окно верификации (имя свободно);
    'taken' - Proton СТАБИЛЬНО (>=2.5 с) показывает «имя занято»;
    'unknown' - ничего не понятно (даём кандидату шанс)."""
    deadline = time.time() + 30
    taken_since = None
    while time.time() < deadline:
        time.sleep(1)
        # 1) промо-поп-ап Proton («специальное предложение», $9.99) -
        #    НЕ платный тариф, а реклама; закрываем ПЕРВЫМ делом, иначе
        #    он висит поверх формы, блокирует её и пугает «ценами»
        try:
            closed = _click_text(page, _SKIP, timeout=600)
            if closed:
                log("Proton: закрыла рекламное окно (это НЕ платный тариф - просто промо)")
                time.sleep(1.2)
        except Exception:
            pass
        # 2) стабильная ошибка занятости (только контейнер поля имени)
        if _sample_taken(page):
            if taken_since is None:
                taken_since = time.time()
            elif time.time() - taken_since >= 2.5:
                log("Proton: подтверждено «имя занято» (держится стабильно)")
                return "taken"
        else:
            taken_since = None
        # 3) окно Human Verification - успех
        try:
            txt = _body_text(page)
        except Exception:
            txt = ""
        if _VERIFY_OK.search(txt):
            log("Proton: окно верификации появилось - имя принято")
            return "ok"
        # 4) форма ушла дальше (кнопка создания исчезла) - тоже успех
        if not _has_create_btn(page):
            log("Proton: форма ушла дальше (кнопка создания исчезла)")
            return "ok"
    return "unknown"


def proton_signup(con, mailbox_id, log=print):
    """Автоматическая регистрация ящика Proton до капчи (и попытка дожать
    после неё). Возвращает (ok, msg)."""
    mb = db.get_mailbox(con, mailbox_id)
    prov = db.get_provider(con, mb["provider_id"])
    email0 = mb["email"]
    final_email = email0
    login0 = mb["login"] or email0.split("@")[0]
    password = mb["password"]
    person_name = mb["person_name"] or ""
    parts = person_name.split()
    first = parts[0] if parts else "Alex"
    surname = parts[-1] if len(parts) > 1 else "Smith"

    # кандидаты логинов: начальный + упорядоченный список с цифрами (если занят)
    cands, seen = [], set()
    for lp in [login0] + generator.email_login(first, surname):
        lp = lp.lower()
        if lp not in seen:
            seen.add(lp)
            cands.append(lp)
        if len(cands) >= 10:
            break

    # профиль - по id ящика (mb13): адрес при регистрации меняется (занятое
    # имя → следующий кандидат), а сессия/куки должны остаться теми же
    profile_dir = br.profile_dir_for(mailbox_id)
    os.makedirs(profile_dir, exist_ok=True)
    br.register_profile(con, mailbox_id, email0)
    # прокси для ящика: берём ЖИВОЙ (проверка + следующий свободный, если мёртв).
    # Пул пуст - идём напрямую, это нормально (не ошибка).
    prow, proxy_pw, pdesc = proxy.resolve_for_mailbox(con, mailbox_id, log=log)
    proxy_id = prow["id"] if prow is not None else None
    db.update_mailbox(con, mailbox_id, status="creating",
                      note="идёт авторегистрация…")

    log("Proton: открываю браузер (профиль ящика, прокси: %s)" % pdesc)
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    ctx = None
    try:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir, headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check",
                  "--start-maximized"],
            proxy=proxy_pw or None,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        url = prov["signup_url"] or "https://account.proton.me/mail/signup"
        log("Proton: открываю %s" % url)
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(4)

        # 1) выбор плана Free - строго по карточке Free, с проверкой выбора
        if not _select_free_plan(page, log):
            raise RuntimeError(
                "не смогла выбрать бесплатный тариф Free (Proton по умолчанию "
                "держит Unlimited $9.99) - авторегистрация остановлена, "
                "ничего не оплачено")

        # 2) ждём форму аккаунта (поле пароля); если вместо формы платёжка - стоп
        if not _wait_for(page, lambda: page.locator("input[type=password]").count() > 0, 25):
            cur = page.url
            txt = _body_text(page)
            if _PAYMENT.search(cur + " " + txt):
                raise RuntimeError(
                    "похоже, открылась платёжка платного тарифа (url=%s) - "
                    "авторегистрация остановлена, ничего не оплачено" % cur)
            raise RuntimeError("форма аккаунта не появилась (url=%s, текст: %s)"
                               % (cur, txt[:200]))
        log("Proton: форма аккаунта открыта")

        # 3) пароль+подтверждение СНАЧАЛА: Proton активирует проверку имени
        #    и шаг username только после валидного пароля
        if not _fill_password_fields(page, password):
            raise RuntimeError("не смогла заполнить пароль")
        log("Proton: пароль введён")

        # 4) цикл кандидатов: имя → вердикт Proton → если занято, следующий.
        #    Proton проверяет имя на лету (даже в скрытой панели): ждём ответ,
        #    и только свободное имя отправляем - так не тратим сабмиты.
        used_login = None
        last_err = ""
        for cand in cands:
            _try_login_candidate(page, cand, wait=0.3)
            verdict = _wait_username_verdict(page, timeout=9.0)
            if verdict == "taken":
                log("Proton: %s занят (проверка на лету) - пробую следующий" % cand)
                continue
            # пароли на всякий случай дополняем (могли сброситься)
            _fill_password_fields(page, password)
            log("Proton: пробую адрес %s - отправляю форму" % cand)
            if not _click_submit_create(page):
                last_err = "не нашла кнопку создания аккаунта"
                break
            verdict = _wait_submit_outcome(page, log)
            if verdict == "ok":
                used_login = cand
                break
            if verdict == "taken":
                log("Proton: адрес %s занят (после отправки) - пробую следующий" % cand)
                continue
            # unknown: не ошибка и не верификация - даём кандидату шанс,
            # но проверяем дальше в цикле верификации
            log("Proton: исход неясен для %s - продолжаю с ним" % cand)
            used_login = cand
            break
        if used_login is None:
            raise RuntimeError("все варианты адреса заняты (%s)" % last_err)

        try:
            doms = json.loads(prov["domains"]) if isinstance(prov["domains"], str) \
                else (prov["domains"] or [])
        except Exception:
            doms = []
        new_email = "%s@%s" % (used_login, (doms[0] if doms else "proton.me"))
        if new_email != email0:
            db.update_mailbox(con, mailbox_id, email=new_email, login=used_login)
            final_email = new_email
            log("Proton: адрес обновлён в базе: %s" % new_email)

        # возможный поп-ап «ограниченное предложение» - закрыть
        _click_text(page, _SKIP, timeout=1500)
        time.sleep(1.5)

        # 5) Human Verification: выбираем путь CAPTCHA, если окно есть
        txt = _body_text(page)
        if _VERIFY_OK.search(txt):
            _click_text(page, re.compile(r"CAPTCHA"), timeout=2500)
            log("Proton: окно верификации. Реши паззл CAPTCHA в окне - дальше я сама.")
        else:
            log("Proton: похоже, требуется подтверждение (капча/код). Реши в окне.")

        # 6) ждём завершения: recovery phrase / display name / вход в ящик
        log("Proton: жду, пока ты решаешь капчу и Proton создаёт аккаунт…")
        recovered = False
        clicked_skip = 0
        deadline = time.time() + 25 * 60
        while time.time() < deadline:
            if not ctx.pages:
                raise RuntimeError("окно браузера закрыто до завершения регистрации")
            try:
                cur = page.url
                txt = _body_text(page)
            except Exception:
                time.sleep(2)
                continue

            # если вдруг форма вернулась к ошибке занятости - поднимаем ошибку
            if _sample_taken(page) and "account" in cur:
                raise RuntimeError("Proton вернул ошибку занятости имени на позднем шаге")

            # recovery phrase: читаем слова, сохраняем, жмём дальше
            if not recovered and _RECOVERY.search(txt):
                words = _extract_words(txt)
                if len(words) >= 20:
                    db.update_mailbox(con, mailbox_id,
                                      note="recovery: " + " ".join(words))
                    log("Proton: фраза восстановления сохранена в карточку ящика (%d слов)"
                        % len(words))
                else:
                    log("Proton: экран recovery-фразы - сохрани её (нужна для "
                        "восстановления доступа), затем нажми «Продолжить»")
                recovered = True
                _click_text(page, _CONTINUE, timeout=3000)
                time.sleep(1.5)
                continue

            # подтверждение слова из фразы: «word #N» - вводим сами
            if recovered:
                m = _WORD_CONFIRM.search(txt)
                if m:
                    num = int(m.group(1) or m.group(2))
                    note = db.get_mailbox(con, mailbox_id)["note"] or ""
                    words = note.replace("recovery: ", "").split() if note.startswith("recovery:") else []
                    if words and 1 <= num <= len(words):
                        _js_fill(page, "input", words[num - 1])
                        time.sleep(0.5)
                        _click_text(page, _CONTINUE, timeout=3000)
                        log("Proton: подтвердила слово №%d" % num)
                        time.sleep(1.5)
                        continue

            # отображаемое имя
            if _DISPLAY_NAME.search(txt):
                try:
                    inp = page.locator("input[type=text]").first
                    if inp.count():
                        inp.fill(person_name)
                        _click_text(page, _CONTINUE, timeout=3000)
                        log("Proton: ввела отображаемое имя «%s»" % person_name)
                        time.sleep(2)
                        continue
                except Exception:
                    pass

            # вход в ящик?
            if "mail.proton.me" in cur and "signup" not in cur:
                log("Proton: мы в почтовом ящике (%s)" % cur)
                break
            if _MAILBOX.search(txt) and "account.proton.me" not in cur:
                log("Proton: интерфейс почты обнаружен")
                break

            # пропустить импорт/onboarding (несколько раз, но без зацикливания)
            if clicked_skip < 4 and _SKIP.search(txt):
                clicked_skip += 1
                _click_text(page, _SKIP, timeout=2500)
                time.sleep(1.5)
                continue

            time.sleep(2)
        else:
            raise RuntimeError("не дождалась завершения за 25 минут - статус оставлен черновиком")

        ok = True
        # заметка: на успехе пишем честный итог, а не «идёт авторегистрация…»
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
                note="создан автопилотом %s · фраза восстановления НЕ сохранена - "
                     "забери из Proton вручную (Настройки → Восстановление)"
                     % time.strftime("%d.%m.%Y"))
            log("Proton: ВНИМАНИЕ - recovery-фразу прочитать не удалось. "
                "Ящик работает, но фразу забери из Proton вручную!")
        log("Proton: аккаунт %s создан и работает. Пароль - в карточке ящика." % final_email)
        return True, "ok"
    except Exception as e:
        logger_exc(log, e)
        try:
            db.update_mailbox(con, mailbox_id, status="created",
                              note="авторегистрация не завершилась: %s" % e)
        except Exception:
            pass
        log("Proton: ошибка - %s. Ящик остался черновиком (можно доделать вручную)." % e)
        return False, str(e)
    finally:
        try:
            if ctx is not None:
                ctx.close()
        except Exception:
            pass
        try:
            p.stop()
        except Exception:
            pass


def logger_exc(log, e):
    try:
        from core import logger
        logger.exception("signup")
    except Exception:
        pass


def register_with_driver(con, mailbox_id, log=print):
    """Выбрать драйвер по провайдеру ящика и запустить регистрацию."""
    mb = db.get_mailbox(con, mailbox_id)
    prov = db.get_provider(con, mb["provider_id"]) if mb else None
    driver = (prov["driver"] if prov else "") or ""
    if driver == "proton":
        return proton_signup(con, mailbox_id, log=log)
    if driver == "tuta":
        # поздний импорт: tuta.py сам берёт хелперы из signup, поэтому на
        # уровне модуля импортировать нельзя (кольцо).
        from core import tuta
        return tuta.tuta_signup(con, mailbox_id, log=log)
    raise ValueError("У сервиса %s нет автопилота регистрации" % (prov["name"] if prov else "?"))
