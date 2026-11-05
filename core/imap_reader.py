# -*- coding: utf-8 -*-
"""
imap_reader.py - чтение писем по IMAP и вытаскивание кода подтверждения.

Поток: подключиться к imap_host ящика → забрать письма за N дней →
для каждого вытащить текст → найти код (regex) → сохранить в БД.
Письма читаются по UID (а не по sequence number), чтобы imap_uid был
стабилен между сессиями и UNIQUE(mailbox_id, imap_uid) имел смысл.
"""
import datetime
import html as _html
import imaplib
import re

from email.header import decode_header, make_header
from email import policy
from email.parser import BytesParser

# --- паттерны кодов подтверждения (рус/англ и просто «голые» коды) ---
_CODE_KEYWORDS = (
    r"verification|verify|confirm(?:ation)?|activation|activate|"
    r"one[- ]time|otp|security code|access code|login code|"
    r"код подтвержд|код для входа|код активац|код доступа|проверочный код|"
    r"одноразов|подтверждени|код"
)
_CODE_RE = re.compile(
    rf"(?:{_CODE_KEYWORDS})\D{{0,60}}?\b(\d{{4,8}})\b", re.I
)
# запасной: 5-6-значное число отдельной строкой или в скобках/кавычках
_FALLBACK_RE = re.compile(r"(?<![\d.])\b(\d{5,6})\b(?![\d.])")


def _dec_header(s):
    """'=?utf-8?B?...?=' и прочее → строка."""
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return s or ""


def _body_text(raw):
    """Достать текст письма (plain; если нет - html без тегов)."""
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception:
        return ""
    parts = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        ct = part.get_content_type()
        try:
            payload = part.get_content()
        except Exception:
            try:
                payload = part.get_payload(decode=True)
                if payload:
                    payload = payload.decode(part.get_content_charset() or "utf-8", "replace")
                else:
                    payload = ""
            except Exception:
                payload = ""
        if not isinstance(payload, str):
            payload = str(payload)
        if ct == "text/plain":
            parts.append(payload)
        elif ct == "text/html":
            txt = re.sub(r"<style[\s\S]*?</style>|<script[\s\S]*?</script>", " ", payload)
            txt = re.sub(r"<[^>]+>", " ", txt)
            parts.append(_html.unescape(txt))
    return "\n".join(parts)


def extract_code(subject, body):
    """Вернуть найденный код или ''."""
    text = f"{subject}\n{body}"
    m = _CODE_RE.search(text)
    if m:
        return m.group(1)
    m2 = _FALLBACK_RE.search(body or "")
    return m2.group(1) if m2 else ""


def fetch_letters(host, user, password, days=7, limit=25, port=993):
    """
    Вернуть список писем: [(uid, from, subject, date, text, code)].
    Кидает исключение с человеческим сообщением при проблемах.
    Читает по UID - стабильный идентификатор письма между сессиями.
    """
    since = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%d-%b-%Y")
    try:
        imap = imaplib.IMAP4_SSL(host, port, timeout=30)
    except Exception as e:
        raise ConnectionError(f"Не могу подключиться к {host}: {e}")
    try:
        try:
            imap.login(user, password)
        except imaplib.IMAP4.error as e:
            raise PermissionError(f"Логин отклонён для {user}@{host}: {e}")
        imap.select("INBOX")
        typ, data = imap.uid("search", None, f"(SINCE {since})")
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        uids = uids[-limit:]
        out = []
        for uid in uids:
            typ, msg_data = imap.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            raw = msg_data[0][1]
            try:
                msg = BytesParser(policy=policy.default).parsebytes(raw)
            except Exception:
                continue
            subj = _dec_header(msg.get("Subject"))
            frm = _dec_header(msg.get("From"))
            date = msg.get("Date") or ""
            body = _body_text(raw)
            code = extract_code(subj, body)
            out.append((int(uid), frm, subj, date, body[:2000], code))
        imap.logout()
        return out
    finally:
        try:
            imap.shutdown()
        except Exception:
            pass


def check_mailbox(con, mailbox, days=7, limit=25):
    """
    Прочитать почту ящика (Row из БД) и сохранить письма в таблицу letters.
    Возвращает (новых_писем, всего_прочитано).
    """
    from core import db
    if not mailbox["imap_host"]:
        raise ValueError(f"У провайдера {mailbox['provider']} не задан imap_host")
    login = mailbox["login"] or mailbox["email"]
    letters = fetch_letters(mailbox["imap_host"], login, mailbox["password"],
                            days=days, limit=limit, port=mailbox["imap_port"] or 993)
    added = 0
    for uid, frm, subj, date, body, code in letters:
        cur = con.execute(
            "SELECT 1 FROM letters WHERE mailbox_id=? AND imap_uid=?",
            (mailbox["id"], uid),
        ).fetchone()
        if not cur:
            added += 1
        db.save_letter(con, mailbox["id"], uid, frm, subj, date, body, code)
    return added, len(letters)
