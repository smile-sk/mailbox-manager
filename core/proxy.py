# -*- coding: utf-8 -*-
"""
proxy.py - ЖИВЫЕ прокси для ящика.

Зачем этот модуль. Раньше прокси брался из пула «как есть»: если адрес мёртв,
браузер молча не открывал сайт (или висел на белом экране), а два разных ящика
могли получить один и тот же адрес. Здесь это лечится:

  • ПРОВЕРКА перед запуском браузера: через прокси делается ОДИН HTTPS-запрос к
    НАШЕМУ сервису проверки на VPS, который называет ВЫХОДНОЙ адрес и страну.
    Результат кладётся в БД, и его видно в окне - с какого адреса выходит ящик;
  • МЁРТВЫЙ ПРОКСИ ВЫБРАСЫВАЕТСЯ: считается сбой, берётся СЛЕДУЮЩИЙ живой из
    пула. После PROXY_FAILS_TO_DISABLE сбоев прокси выключается сам (с пометкой
    в поле note), чтобы не тратить время на него каждый раз;
  • ОДИН ПРОКСИ - ОДИН ЯЩИК: адрес, закреплённый за другим ящиком, повторно не
    выдаётся; у одного ящика адрес при этом НЕ «прыгает» между запусками;
  • живых нет вообще - работаем напрямую и честно пишем это в журнал.

Проверка сделана на голых сокетах: в проекте из зависимостей только playwright,
а тащить requests/PySocks ради одного запроса не хочется. Поддерживаются
http (в том числе с логином/паролем), socks5 и socks4.

Адрес в файле БЕЗ схемы (просто «host:port») считается «авто»: при первой живой
проверке протокол определяется сам (http -> socks5 -> socks4) и запоминается.
"""
import base64
import datetime
import json
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import db

# НАШ СЕРВИС ПРОВЕРКИ - свой, на нашем VPS (tools/proxycheck_server.py, юнит
# proxycheck, наружу отдан Apache-ом как https://apiteka.pp.ua/ip).
# Почему не чужой: чужие сервисы отвечают медленно, живут с лимитами и отваливаются
# - и тогда проверка врёт, а живые прокси выглядят мёртвыми (проверено живьём
# 12.09.2026: на одном ПК 8 живых, на другом те же 487 адресов - «0 живых»).
#
# Здесь ОДИН HTTPS-запрос через прокси делает сразу две работы: называет ВЫХОДНОЙ
# АДРЕС и ДОКАЗЫВАЕТ, что прокси не подменяет сертификат (сертификат наш, настоящий:
# подделать его бесплатный прокси не может).
CHECK_IP = "example-vps"           # адрес сервиса (идём прямо на него, минуя Cloudflare)
CHECK_SNI = "apiteka.pp.ua"         # имя, сертификат которого сервис показывает
CHECK_PORT = 443
CHECK_PATH = "/ip"                  # ответ: {"ip": "1.2.3.4"}
GEO_PATH = "/geo"                   # ответ: {"ip":..., "country":..., "code":...}
CHECK_TTL = 300                     # 5 минут: свежее не перепроверяем
MAX_TRIES = 6                       # столько прокси пробуем в одной волне
PICK_BUDGET = 24                    # потолок адресов на один ящик (пул бывает 400)
TIMEOUT = 12                        # бесплатные прокси отвечают по 5-7 с - терпим
PROBE_TCP = 6                       # столько ждём TCP до самого прокси
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# Ответ НАШЕГО сервиса проверки (или Cloudflare перед ним), а не прокси. 403
# прилетает от Apache, когда запрос не прошёл ограничение «на origin только
# Cloudflare и свои»: живой случай 19.09.2026 - после включения origin-only
# (18.09) ВСЕ прокси стали «мёртвыми» с HTTP 403, живые адреса выключались,
# ручное назначение прокси снималось, ящики уезжали напрямую с домашним IP.
# 52x - Cloudflare, когда origin недоступен. Такие ответы НЕ доказывают, что
# прокси мёртв, поэтому сбоем не считаются (и адрес не снимается).
INFRA_MARKS = ("HTTP 403", "HTTP 502", "HTTP 503", "HTTP 520", "HTTP 521",
               "HTTP 522", "HTTP 523", "HTTP 524")


# ---------------------------------------------------------------- низкий уровень

def _norm(p):
    """Прокси (sqlite3.Row / dict / None) - к простому словарю."""
    if p is None:
        return None
    if isinstance(p, dict):
        d = dict(p)
    else:
        try:
            d = {k: p[k] for k in p.keys()}
        except Exception:
            return None
    try:
        port = int(d.get("port") or 0)
    except Exception:
        port = 0
    return dict(
        id=d.get("id"),
        kind=(d.get("kind") or "http").lower(),
        host=d.get("host") or "",
        port=port,
        login=d.get("login") or "",
        passw=d.get("pass") or d.get("password") or "",
    )


def _read_all(sock, limit=131072):
    buf = b""
    while len(buf) < limit:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def _dechunk(data):
    out = b""
    while True:
        i = data.find(b"\r\n")
        if i < 0:
            break
        try:
            size = int(data[:i].split(b";")[0], 16)
        except Exception:
            break
        if size <= 0:
            break
        out += data[i + 2:i + 2 + size]
        data = data[i + 2 + size + 2:]
    return out


def _parse_http(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "replace").split("\r\n")
    status = 0
    if lines and lines[0].startswith("HTTP/"):
        try:
            status = int(lines[0].split()[1])
        except Exception:
            status = 0
    hdrs = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdrs[k.strip().lower()] = v.strip()
    if hdrs.get("transfer-encoding", "").lower() == "chunked":
        body = _dechunk(body)
    return status, body


def _http_get(sock, host, path, absolute=False, auth=None):
    """GET через уже открытый сокет. absolute=True - форма для http-прокси
    (запрос идёт целым адресом), иначе обычная (сокет уже ведёт к цели)."""
    target = ("http://%s%s" % (host, path)) if absolute else path
    req = ["GET %s HTTP/1.1" % target,
           "Host: %s" % host,
           "User-Agent: %s" % _UA,
           "Accept: application/json",
           "Connection: close"]
    if auth:
        req.append("Proxy-Authorization: Basic %s" % auth)
    sock.sendall(("\r\n".join(req) + "\r\n\r\n").encode())
    raw = _read_all(sock)
    if not raw:
        raise RuntimeError("пустой ответ")
    status, body = _parse_http(raw)
    if status and status != 200:
        raise RuntimeError("HTTP %d" % status)
    return body.decode("utf-8", "replace")


def _http_connect_tunnel(sock, host, port, login="", passw=""):
    """Открыть туннель CONNECT через HTTP-прокси (нужно для HTTPS)."""
    req = ["CONNECT %s:%d HTTP/1.1" % (host, port),
           "Host: %s:%d" % (host, port),
           "User-Agent: %s" % _UA,
           "Connection: keep-alive"]
    if login:
        auth = base64.b64encode(("%s:%s" % (login, passw)).encode("utf-8")).decode("ascii")
        req.append("Proxy-Authorization: Basic %s" % auth)
    sock.sendall(("\r\n".join(req) + "\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 8192:
        chunk = sock.recv(1024)
        if not chunk:
            break
        buf += chunk
    head = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if " 200" not in head:
        raise RuntimeError("HTTP-прокси не открыл туннель (%s)" % head.strip()[:40])


# (Проверка сертификата раньше жила здесь отдельной функцией _https_probe: второй
# шаг - TLS к mail.proton.me. Теперь она не нужна: HTTPS к нашему сервису проверки
# сам доказывает, что прокси не подменяет сертификат, а стоит это одного запроса
# вместо двух - на пуле в 400 адресов это разница в минуты.)


def _socks5(sock, host, port, login="", passw=""):
    """Рукопожатие SOCKS5 (без аутентификации или логин/пароль)."""
    if login:
        sock.sendall(b"\x05\x02\x00\x02")
    else:
        sock.sendall(b"\x05\x01\x00")
    resp = sock.recv(2)
    if len(resp) < 2 or resp[0] != 5:
        raise RuntimeError("SOCKS5: прокси не ответил")
    method = resp[1]
    if method == 2:
        u = login.encode("utf-8")[:255]
        w = passw.encode("utf-8")[:255]
        sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(w)]) + w)
        r = sock.recv(2)
        if len(r) < 2 or r[1] != 0:
            raise RuntimeError("SOCKS5: логин/пароль не приняты")
    elif method != 0:
        raise RuntimeError("SOCKS5: способ входа не поддержан")
    hb = host.encode("idna") if not _is_ip(host) else host.encode()
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", port))
    head = _recv_exact(sock, 4)
    if head[1] != 0:
        raise RuntimeError("SOCKS5: отказ в соединении (код %d)" % head[1])
    atyp = head[3]
    if atyp == 1:
        _recv_exact(sock, 4)
    elif atyp == 3:
        ln = _recv_exact(sock, 1)[0]
        _recv_exact(sock, ln)
    elif atyp == 4:
        _recv_exact(sock, 16)
    else:
        raise RuntimeError("SOCKS5: неизвестный тип адреса")
    _recv_exact(sock, 2)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RuntimeError("SOCKS5: соединение оборвалось")
        buf += chunk
    return buf


def _is_ip(host):
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False



# ---------------------------------------------------------------- протокол

# Какие протоколы умеем. Адрес в файле БЕЗ схемы (просто «host:port») читается
# как «auto» - вот эти три и пробуются по очереди при первой живой проверке,
# а рабочий запоминается в БД. Так не надо угадывать: часть бесплатных пулов
# отдаёт SOCKS5 на порту 1080, часть - HTTP, часть - SOCKS4.
_KINDS = ("http", "socks5", "socks4")


def _socks4(sock, host, port, login=""):
    """Рукопожатие SOCKS4/4a (логин - идентификатор пользователя)."""
    if _is_ip(host):
        body, tail = socket.inet_aton(host), b""
    else:
        # SOCKS4a: имя хоста обязано заканчиваться нулём, иначе сервер ждёт
        # остаток запроса и не отвечает (на этом «мёртвыми» выглядели живые
        # SOCKS4-адреса из пула - нашла сырым дампом 12.09.2026).
        body, tail = b"\x00\x00\x00\x01", host.encode("idna") + b"\x00"
    sock.sendall(b"\x04\x01" + struct.pack(">H", port) + body +
                 (login or "").encode("utf-8")[:255] + b"\x00" + tail)
    r = _recv_exact(sock, 8)
    if r[1] != 0x5A:
        raise RuntimeError("SOCKS4: отказ в соединении (код %d)" % r[1])


def _kinds_for(p):
    """Порядок проверки: заданный явно протокол - один, «авто» - перебор."""
    k = (p["kind"] or "").lower()
    return (k,) if k in _KINDS else _KINDS


_TLS_CTX = None


def _tls_ctx():
    """Контекст TLS С ПРОВЕРКОЙ сертификата.

    Свой список корневых сертификатов (certifi), если он есть: тогда проверка не
    зависит от хранилища Windows, которое на чужой машине бывает неполным - и
    живые прокси выглядели бы как «подменяет сертификат».
    """
    global _TLS_CTX
    if _TLS_CTX is None:
        import ssl
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:
            pass
        _TLS_CTX = ctx
    return _TLS_CTX


class ProxyMitm(Exception):
    """Прокси подсунул СВОЙ сертификат - значит читает чужой HTTPS."""


def _tls(sock, timeout):
    """Обернуть соединение в TLS и ПРОВЕРИТЬ сертификат."""
    import ssl
    sock.settimeout(timeout)
    try:
        return _tls_ctx().wrap_socket(sock, server_hostname=CHECK_SNI)
    except ssl.SSLCertVerificationError as e:
        raise ProxyMitm(str(e)[:70])
    except ssl.SSLError as e:
        raise RuntimeError("HTTPS не поднялся: %s" % str(e)[:60])


def _direct_ip(timeout=TIMEOUT):
    """Свой адрес: тот же запрос к нашему сервису, но без прокси."""
    sock = tls = None
    try:
        sock = socket.create_connection((CHECK_IP, CHECK_PORT), timeout)
        tls = _tls(sock, timeout)
        sock = None
        body = _http_get(tls, CHECK_SNI, CHECK_PATH)
        return (json.loads(body).get("ip") or "").strip()
    finally:
        for s in (sock, tls):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass


def _geo(ip):
    """Страна по выходному адресу - отдельным запросом к нашему сервису.

    Спрашиваем ТОЛЬКО про живые адреса (их единицы), поэтому лимиты сервису стран
    не грозят: пул из 400 адресов так проверяется быстро. Ответ кэшируется и у
    нашего сервиса, и в базе.
    """
    if not ip:
        return "", ""
    sock = tls = None
    try:
        sock = socket.create_connection((CHECK_IP, CHECK_PORT), PROBE_TCP)
        tls = _tls(sock, PROBE_TCP)
        sock = None
        d = json.loads(_http_get(tls, CHECK_SNI, "%s?ip=%s" % (GEO_PATH, ip)))
        return d.get("country") or "", d.get("code") or ""
    except Exception:
        return "", ""
    finally:
        for s in (sock, tls):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass


def _via(p, kind, timeout):
    """Выходной адрес, полученный ЧЕРЕЗ прокси: один HTTPS-запрос к нашему сервису.

    Заход считается удачным только если сертификат ПРОВЕРЕН и сервис назвал адрес:
    значит сразу два факта - «прокси жив» и «прокси не читает чужой HTTPS».
    """
    sock = tls = None
    try:
        sock = socket.create_connection((p["host"], p["port"]), min(timeout, PROBE_TCP))
        if kind == "socks5":
            _socks5(sock, CHECK_IP, CHECK_PORT, p["login"], p["passw"])
        elif kind == "socks4":
            _socks4(sock, CHECK_IP, CHECK_PORT, p["login"])
        else:
            _http_connect_tunnel(sock, CHECK_IP, CHECK_PORT, p["login"], p["passw"])
        tls = _tls(sock, timeout)
        sock = None
        body = _http_get(tls, CHECK_SNI, CHECK_PATH)
        ip = (json.loads(body).get("ip") or "").strip()
        if not ip:
            raise RuntimeError("сервис не назвал адрес: %s" % (body or "пусто")[:40])
        return ip
    finally:
        for s in (sock, tls):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass


def _ask(p, kind, timeout):
    """Один заход: HTTPS через прокси. Отдаёт (адрес, страна, код).

    Страну отдельным шагом добирает _geo - только для живых адресов, чтобы
    проверка пула из 400 адресов не встала на лимитах сервиса стран.
    """
    return (_via(p, kind, timeout), "", "")


def _connect_via(p, timeout):
    """(адрес, страна, код, рабочий протокол).

    Прокси «auto» перебирает http -> socks5 -> socks4 ПАРАЛЛЕЛЬНО, и заход
    засчитывается ТОЛЬКО если сервис реально назвал адрес: мусор, полученный от
    чужого протокола (например, SOCKS-сервер, которому послали HTTP-запрос), -
    это не «рабочий http», а повод пробовать следующий протокол.

    Если ВСЕ попытки упали на проверке сертификата, значит прокси читает чужой
    HTTPS (MITM). Это отдельная беда: такой адрес надо выключать сразу, поэтому
    её отдаём наружу как ProxyMitm.
    """
    kinds = _kinds_for(p)
    deadline = min(timeout, 10)
    if len(kinds) == 1:
        ip, cc, code = _ask(p, kinds[0], deadline)
        if not ip:
            raise RuntimeError("%s: сервис не отдал адрес" % kinds[0])
        return ip, cc, code, kinds[0]

    errs, mitm = {}, []
    with ThreadPoolExecutor(max_workers=len(kinds)) as ex:
        futs = {ex.submit(_ask, p, k, deadline): k for k in kinds}
        try:
            for fut in as_completed(futs, timeout=deadline + 2):
                k = futs[fut]
                try:
                    ip, cc, code = fut.result()
                except ProxyMitm as e:
                    mitm.append("%s: %s" % (k, e))
                    errs[k] = "подменяет сертификат"
                    continue
                except Exception as e:
                    errs[k] = (str(e) or e.__class__.__name__)[:60]
                    continue
                if ip:
                    for other in futs:
                        other.cancel()
                    return ip, cc, code, k
                errs[k] = "сервис не отдал адрес"
        except Exception:
            pass
    if mitm and len(mitm) == len(kinds):
        raise ProxyMitm("; ".join(mitm)[:70])
    raise RuntimeError("; ".join("%s: %s" % (k, v) for k, v in errs.items())
                       or "прокси не ответил")


def pw_proxy(p, kind=None):
    """Аргумент proxy для Playwright. kind - протокол, определённый при живой
    проверке (в записи может стоять «auto» - адрес из файла без схемы)."""
    p = _norm(p)
    if p is None:
        return None
    k = (kind or p["kind"] or "http").lower()
    if k not in _KINDS:
        k = "http"
    d = {"server": "%s://%s:%d" % (k, p["host"], p["port"])}
    if p["login"]:
        d["username"] = p["login"]
        d["password"] = p["passw"]
    return d

# ---------------------------------------------------------------- проверка

def check(proxy=None, timeout=TIMEOUT):
    """Проверить прокси (None - прямое соединение). Возвращает словарь:
    ok, ip, country, code, ms, error, kind, tls.

    Через прокси делается ОДИН HTTPS-запрос к нашему сервису проверки. Он отвечает
    сразу на два вопроса: (1) живой ли адрес и с какого IP он выходит; (2) не
    подменяет ли прокси сертификат - для Proton такой бесполезен, браузер покажет
    «Подключение не защищено» и на сайт не пустит. Раньше это был второй отдельный
    шаг (TLS к mail.proton.me), теперь он не нужен: TLS и есть проверка.
    """
    p = _norm(proxy)
    res = {"ok": False, "ip": "", "country": "", "code": "", "ms": 0, "error": "",
           "kind": "", "tls": None, "infra": False}
    t0 = time.time()
    try:
        if p is None:
            res["ip"] = _direct_ip(timeout)
            res["ok"] = True
        else:
            if not p["host"] or not p["port"]:
                raise RuntimeError("в записи прокси нет адреса или порта")
            (res["ip"], res["country"], res["code"],
             res["kind"]) = _connect_via(p, timeout)
            res["tls"] = True
            res["ok"] = True
    except ProxyMitm as e:
        res["tls"] = False
        res["error"] = "подменяет сертификат (MITM): %s" % str(e)[:60]
    except socket.timeout:
        res["error"] = "таймаут"
    except ConnectionRefusedError:
        res["error"] = "отказ в соединении"
    except OSError as e:
        res["error"] = (str(e) or "сеть недоступна")[:120]
    except Exception as e:
        res["error"] = (str(e) or e.__class__.__name__)[:120]
    if not res["ok"] and any(m in (res["error"] or "") for m in INFRA_MARKS):
        # Ответил не прокси, а наш сервис проверки - это НЕ «прокси мёртв».
        res["infra"] = True
    if res["ok"] and not res["country"]:
        res["country"], res["code"] = _geo(res["ip"])
    res["ms"] = int((time.time() - t0) * 1000)
    return res


def describe(p, res=None):
    """Строка для журнала/окна: «1.2.3.4 (Germany)» либо адрес прокси."""
    p = _norm(p)
    if p is None:
        return "прямое соединение (без прокси)"
    if res and res.get("ok") and res.get("ip"):
        cc = res.get("country") or res.get("code") or ""
        return "%s (%s)" % (res["ip"], cc) if cc else res["ip"]
    return "%s://%s:%d" % (p["kind"], p["host"], p["port"])


def _fresh(row):
    """Прокси проверялся меньше CHECK_TTL назад, был жив И проходил настоящий
    HTTPS к Proton? Адрес, живой только для HTTP, нам не подходит: браузер
    через него на Proton не попадёт (подмена сертификата)."""
    try:
        if not row["checked_at"] or not row["exit_ip"]:
            return False
        if not row["https_ok"]:
            return False
        checked = datetime.datetime.fromisoformat(row["checked_at"])
        return (datetime.datetime.now() - checked).total_seconds() < CHECK_TTL
    except Exception:
        return False


def _known_good(row):
    return {"ok": True, "ip": row["exit_ip"] or "", "country": row["exit_country"] or "",
            "code": "", "ms": 0, "error": "", "cached": True, "tls": None}


def _record_fail(con, row, res, log=None):
    """Записать сбой проверки прокси. Возвращает (сбоев, выключен, подмена_сертификата).

    Подмена сертификата - это не «прокси мёртв», а «прокси читает чужой HTTPS»:
    такой адрес выключаем СРАЗУ, не дожидаясь трёх сбоев, чтобы он не вылезал
    в подборе для ящиков.
    """
    log = log or (lambda *_a, **_k: None)
    mitm = res.get("tls") is False
    fails, disabled = db.set_proxy_health(con, row["id"], ok=False,
                                          error=res.get("error", ""))
    if mitm:
        db.set_proxy_active(con, row["id"], False)
        con.execute("UPDATE proxies SET note=? WHERE id=?",
                    ("авто: подменяет сертификат (MITM)", row["id"]))
        con.commit()
        disabled = True
    return fails, disabled, mitm


def check_row(con, row, log=None, force=False):
    """Проверить прокси, помня о кэше: свежий результат не гоняем заново.
    Пишет итог в БД (адрес, страна, счётчик сбоев)."""
    log = log or (lambda *_a, **_k: None)
    if not force and _fresh(row):
        res = _known_good(row)
        log("[прокси] #%s: адрес %s - проверен только что" %
            (row["id"], describe(row, res)))
        return res
    res = check(row)
    if res["ok"]:
        db.set_proxy_health(con, row["id"], ok=True, ip=res["ip"], country=res["country"],
                            https_ok=1 if res.get("tls") else 0)
        if res.get("kind") and res["kind"] != row["kind"]:
            db.set_proxy_kind(con, row["id"], res["kind"])
            log("[прокси] #%s протокол определён сам: %s (в файле было «%s»)" %
                (row["id"], res["kind"], row["kind"] or "авто"))
        log("[прокси] #%s ок - выходной адрес %s, %d мс" %
            (row["id"], describe(row, res), res["ms"]))
    elif res.get("infra"):
        log("[прокси] #%s проверить не удалось: сервис проверки ответил «%s» - "
            "это НЕ признак мёртвого прокси, сбой не записываю" %
            (row["id"], res.get("error", "")))
    else:
        fails, disabled, mitm = _record_fail(con, row, res, log)
        if mitm:
            log("[прокси] #%s НЕ годится - %s. Выключаю его: для Proton он "
                "бесполезен." % (row["id"], res["error"]))
        elif disabled:
            log("[прокси] #%s не отвечает (%s) - сбоев %d, ОТКЛЮЧАЮ его "
                "(включить можно на вкладке «Прокси»)" % (row["id"], res["error"], fails))
        else:
            log("[прокси] #%s не отвечает (%s) - сбоев %d" % (row["id"], res["error"], fails))
    return res


def _probe_parallel(con, rows, log, workers=8):
    """Проверить ПАЧКУ адресов разом.

    Возвращает (живые [(row, res)], сколько проверок вообще не удалось сделать
    из-за нашего сервиса проверки) - второе число нужно, чтобы честно сказать
    «виноват сервис проверки», а не «весь пул мёртв».

    По одному шесть мёртвых адресов - это ~40 с ожидания перед открытием окна
    (каждый по 4-6 с). Сеть гоняем в потоках, а пишем в БД здесь, в главном
    потоке: sqlite не любит одновременные записи из разных потоков.
    """
    if not rows:
        return []
    results = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(rows))) as ex:
        futs = {ex.submit(check, r): r for r in rows}
        for fut in as_completed(futs):
            r = futs[fut]
            try:
                results[r["id"]] = fut.result()
            except Exception as e:
                results[r["id"]] = {"ok": False, "ip": "", "country": "", "code": "",
                                    "error": str(e)[:80], "ms": 0, "kind": ""}
    alive, infra = [], 0
    for r in rows:
        res = results.get(r["id"]) or {}
        if res.get("ok"):
            db.set_proxy_health(con, r["id"], ok=True, ip=res.get("ip", ""),
                                country=res.get("country", ""),
                                https_ok=1 if res.get("tls") else 0)
            if res.get("kind") and res["kind"] != r["kind"]:
                db.set_proxy_kind(con, r["id"], res["kind"])
                log("[прокси] #%s протокол определён сам: %s (в файле было «%s»)" %
                    (r["id"], res["kind"], r["kind"] or "авто"))
            log("[прокси] #%s ок - выходной адрес %s, %d мс" %
                (r["id"], describe(r, res), res.get("ms", 0)))
            alive.append((r, res))
        elif res.get("infra"):
            infra += 1
            log("[прокси] #%s проверить не удалось: сервис проверки ответил «%s» - "
                "это НЕ признак мёртвого прокси" % (r["id"], res.get("error", "")))
        else:
            fails, disabled, mitm = _record_fail(con, r, res, log)
            log("[прокси] #%s не годится (%s)%s" %
                (r["id"], res.get("error", ""),
                 " - ВЫКЛЮЧАЮ: читает HTTPS" if mitm else
                 (", ОТКЛЮЧАЮ (включить можно на вкладке «Прокси»)" if disabled else "")))
    return alive, infra


def resolve_for_mailbox(con, mailbox_id, log=None, force=False, exclude_ids=None):
    """Подобрать ящику ЖИВОЙ прокси.

    Порядок: сначала свой уже закреплённый (он не меняется - иначе у одного
    ящика «прыгал» бы IP); мёртв - освобождаем и берём следующий свободный из
    пула (проверяем до MAX_TRIES). Ничего живого нет - работаем напрямую.

    Два железных правила (после живого разбора 19.09.2026):
      • адрес, выбранный РУКОЙ в окне «Прокси для ящика» (proxy_mode='manual'),
        не снимается и не подменяется НИКОГДА - даже если проверка не прошла;
        иначе ящик тихо уезжает напрямую с домашним IP, хотя человек выбрал
        конкретный адрес;
      • ответ НАШЕГО сервиса проверки (HTTP 403/52x) - не «прокси мёртв»:
        закрепление не снимаем, сбой не записываем.

    Возвращает (строка proxies | None, аргумент proxy для Playwright, строка-описание).
    """
    log = log or (lambda *_a, **_k: None)
    skip = set(exclude_ids or ())
    prof = db.get_profile(con, mailbox_id)
    cur_id = prof["proxy_id"] if prof is not None else None
    mode = (prof["proxy_mode"] or "") if prof is not None else ""
    if cur_id and cur_id in skip:
        # адрес уже НЕ ПУСТИЛ БРАУЗЕР (хотя сокет-проверка его пропустила) -
        # снимаем закрепление и ищем другой
        log("[прокси] #%s не пустил браузер - снимаю с ящика" % cur_id)
        db.set_profile_proxy(con, mailbox_id, None)
        cur_id = None

    keep = None            # закреплённый адрес, который «не вышло проверить»
    if cur_id:
        row = db.get_proxy(con, cur_id)
        if row is None:
            cur_id = None
        elif mode == "manual":
            # Адрес выбран РУКОЙ - это указание, а не совет: не снимаем и не
            # подменяем даже при неудачной проверке. Проверка нужна лишь затем,
            # чтобы написать честную строку в журнал. Не пустит браузер - скажем
            # об этом прямо, а не поедем «напрямую» с домашним IP.
            res = check_row(con, row, log, force=force)
            if res["ok"]:
                db.set_profile_proxy(con, mailbox_id, row["id"],
                                     res.get("ip", ""), res.get("country", ""))
            else:
                log("[прокси] #%s назначен ящику ВРУЧНУЮ, но проверка не прошла "
                    "(%s) - всё равно иду через него, как выбрано вручную" %
                    (row["id"], res.get("error", "")))
            return (row, pw_proxy(row, res.get("kind") or row["kind"]),
                    describe(row, res))
        elif not row["active"]:
            log("[прокси] закреплённый #%s выключен - беру другой свободный" % cur_id)
            db.set_profile_proxy(con, mailbox_id, None)
            cur_id = None
        else:
            res = check_row(con, row, log, force=force)
            if res["ok"]:
                db.set_profile_proxy(con, mailbox_id, row["id"],
                                     res.get("ip", ""), res.get("country", ""))
                return row, pw_proxy(row, res.get("kind")), describe(row, res)
            if res.get("infra"):
                # Проверить не вышло: адрес НЕ мёртв, поэтому закрепление пока
                # не снимаем - снимем, только если найдётся заведомо живой.
                log("[прокси] #%s не проверить (%s) - оставляю закреплённым, "
                    "беру другой только если найдётся живой" %
                    (row["id"], res.get("error", "")))
                keep = row
            else:
                log("[прокси] закреплённый #%s мёртв - беру следующий живой" % row["id"])
                db.set_profile_proxy(con, mailbox_id, None)

    cands = [r for r in db.free_proxies(con, except_mailbox_id=mailbox_id)
             if r["id"] not in skip]
    if not cands:
        if keep is not None:
            log("[прокси] свободных адресов нет - оставляю за ящиком #%s (%s)" %
                (keep["id"], describe(keep)))
            return keep, pw_proxy(keep, keep["kind"]), describe(keep)
        live = len(db.list_proxies(con, active_only=True))
        log("[прокси] пул пуст - открываю напрямую, без прокси" if live == 0 else
            "[прокси] все живые прокси заняты другими ящиками - открываю напрямую")
        return None, None, _direct(log)

    # Пул бывает на 400 адресов, и у бесплатных большинство мёртвые: идём волнами
    # по MAX_TRIES, пока не найдём живого (всего не больше PICK_BUDGET адресов).
    probe, probed, alive, infra_total = cands[:PICK_BUDGET], 0, [], 0
    while probed < len(probe):
        wave = probe[probed:probed + MAX_TRIES]
        probed += len(wave)
        alive, infra = _probe_parallel(con, wave, log)
        infra_total += infra
        if alive:
            break
        if probed < len(probe):
            log("[прокси] из %d проверенных живых нет - беру следующую пачку" % probed)
    if alive:
        row, res = alive[0]
        db.set_profile_proxy(con, mailbox_id, row["id"],
                             res.get("ip", ""), res.get("country", ""))
        log("[прокси] закрепляю за ящиком #%s: %s" % (row["id"], describe(row, res)))
        return row, pw_proxy(row, res.get("kind")), describe(row, res)
    if keep is not None or (infra_total >= 2 and infra_total * 2 >= probed):
        # Ни один адрес не удалось даже ПРОВЕРИТЬ: беда не в пуле, а в нашем
        # сервисе проверки. Говорим это прямо и держим адрес за ящиком, вместо
        # того чтобы уехать напрямую с домашним IP.
        log("[прокси] прокси не проверить: %d адрес(ов) подряд получили ответ "
            "сервиса проверки. Это беда СЕРВИСА ПРОВЕРКИ, а не прокси - "
            "проверь, что origin пускает /ip, /geo, /ping (ограничение «только "
            "Cloudflare»)" % infra_total)
        row = keep or (cands[0] if cands else None)
        if row is not None:
            log("[прокси] беру адрес #%s без проверки (%s) - он хотя бы уводит "
                "трафик с домашнего IP" % (row["id"], describe(row)))
            return row, pw_proxy(row, row["kind"]), describe(row)
    if keep is not None:
        log("[прокси] живых адресов не нашла, но #%s не проверен - оставляю его "
            "за ящиком" % keep["id"])
        return keep, pw_proxy(keep, keep["kind"]), describe(keep)
    log("[прокси] живых не нашла (проверила %d из %d свободных) - открываю напрямую"
        % (probed, len(cands)))
    return None, None, _direct(log)


def mark_browser_fail(con, proxy_id, error="", log=None, kill=False):
    """Прокси НЕ ПУСТИЛ САМ БРАУЗЕР (а сокет-проверка его пропустила).

    Так бывает с бесплатными прокси: адрес отвечает на проверку, а через
    минуту Chromium через него уже не соединяется (проверено живьём
    12.09.2026). Считаем это сбоем: адрес уходит вниз списка, а после
    PROXY_FAILS_TO_DISABLE сбоев отключается.

    kill=True - адрес не просто сбойный, а ВРЕДНЫЙ: он подменяет сертификат
    (то есть влезает в чужой HTTPS). Такой гасим сразу, не дожидаясь трёх сбоев.
    """
    log = log or (lambda *_a, **_k: None)
    if not proxy_id:
        return
    fails, disabled = db.set_proxy_health(con, proxy_id, ok=False, error=error)
    if kill:
        db.set_proxy_active(con, proxy_id, False)
        con.execute("UPDATE proxies SET note=? WHERE id=?",
                    ("авто: подменяет сертификат (MITM)", proxy_id))
        con.commit()
        disabled = True
    log("[прокси] #%s не пустил браузер (%s) - сбоев %d%s" %
        (proxy_id, (error or "ошибка загрузки")[:80], fails,
         ", ОТКЛЮЧАЮ" if disabled else ""))


def _direct(log):
    """Прямое соединение: покажем свой настоящий адрес - для наглядности."""
    res = check(None)
    if res["ok"]:
        log("[прокси] прямое соединение: твой адрес %s%s" %
            (res["ip"], " (%s)" % res["country"] if res["country"] else ""))
        return "прямое (%s)" % res["ip"]
    return "прямое (без прокси)"


def check_all(con, log=None, limit=None, workers=None, purge_dead=False):
    """Проверить весь пул (кнопка «Проверить» на вкладке «Прокси»).
    purge_dead=True - после проверки сразу убрать мёртвые из пула (иначе на
    400 адресах руками чистить нечего делать).
    Возвращает (живых, мёртвых).

    Проверка идёт ПАРАЛЛЕЛЬНО (по умолчанию 8 адресов разом): по одному пул из
    14 адресов проверялся бы минутами. Сеть - в потоках, запись в БД - тут, в
    главном потоке: sqlite не любит одновременные записи из разных потоков.
    """
    log = log or (lambda *_a, **_k: None)
    rows = db.list_proxies(con, active_only=False)
    if limit:
        rows = rows[:limit]
    if not rows:
        log("[прокси] пул пуст - проверять нечего")
        return 0, 0
    # Сначала - связь с НАШИМ сервисом проверки. Если его не видно (чужой ПК за
    # блокировкой, нет маршрута до VPS), то ЛЮБОЙ прокси покажется мёртвым, и это
    # будет неправдой: именно так на другом ПК вышло «0 живых» при живом интернете.
    probe = check(None)
    if not probe["ok"]:
        log("[прокси] НЕТ связи с сервисом проверки %s (%s) - проверять нечем. "
            "Дело не в прокси: проверь доступ к VPS." % (CHECK_IP, probe.get("error", "")))
        return 0, 0
    log("[прокси] сервис проверки отвечает - мой адрес %s%s" %
        (probe["ip"], " (%s)" % probe["country"] if probe["country"] else ""))
    # Пул на 400 адресов и 8 потоков - это ~5 минут ожидания (замерено 12.09.2026).
    # Проверка чисто сетевая, поэтому на большом пуле потоков больше.
    if workers is None:
        workers = min(32, max(8, len(rows) // 4))
    log("[прокси] проверяю %d адрес(ов) в %d поток(ах) - это займёт минуту-другую"
        % (len(rows), workers))

    results = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(rows))) as ex:
        futs = {ex.submit(check, row): row for row in rows}
        for fut in as_completed(futs):
            row = futs[fut]
            try:
                results[row["id"]] = fut.result()
            except Exception as e:
                results[row["id"]] = {"ok": False, "ip": "", "country": "",
                                      "code": "", "error": str(e)[:80], "ms": 0,
                                      "kind": ""}

    good = bad = 0
    quiet = len(rows) > 40        # большой пул: по каждой мёртвой строке не пишем
    step = max(1, len(rows) // 10) if quiet else 0
    for i, row in enumerate(rows, 1):
        res = results.get(row["id"]) or {}
        if res.get("ok"):
            db.set_proxy_health(con, row["id"], ok=True, ip=res.get("ip", ""),
                                country=res.get("country", ""),
                                https_ok=1 if res.get("tls") else 0)
            if res.get("kind") and res["kind"] != row["kind"]:
                db.set_proxy_kind(con, row["id"], res["kind"])
                log("[прокси] #%s протокол определён сам: %s (в файле было «%s»)" %
                    (row["id"], res["kind"], row["kind"] or "авто"))
            log("[прокси] #%s ок - выходной адрес %s, %d мс" %
                (row["id"], describe(row, res), res.get("ms", 0)))
            good += 1
        else:
            fails, disabled, mitm = _record_fail(con, row, res, log)
            if mitm:
                log("[прокси] #%s не годится - %s. Выключен: для Proton "
                    "бесполезен." % (row["id"], res.get("error", "")))
            elif not quiet or disabled:
                log("[прокси] #%s не отвечает (%s) - сбоев %d%s" %
                    (row["id"], res.get("error", ""), fails,
                     ", ОТКЛЮЧАЮ (включить можно на вкладке «Прокси»)" if disabled else ""))
            bad += 1
        if step and i % step == 0:
            log("[прокси] проверено %d/%d - живых %d" % (i, len(rows), good))
    log("[прокси] проверка пула: живых %d, мёртвых %d" % (good, bad))

    if purge_dead:
        # Чистим мёртвые разом. Если живых не нашлось вовсе - сначала убедимся,
        # что сеть вообще есть: иначе одна проверка без интернета выкосила бы
        # весь пул подчистую.
        allow_all = False
        if good == 0:
            # Живых нет вовсе. Причины две: пропала сеть ИЛИ пул разом выдохся
            # (бесплатные адреса живут минутами). Галочку «удалять мёртвые после
            # проверки» человек ставит осознанно, поэтому при ЖИВОЙ сети чистку
            # ВЫПОЛНЯЕМ (пул всегда возвращается повторным импортом data/proxies.txt).
            # А при МЁРТВОЙ сети - пропускаем: иначе одна проверка без интернета
            # выкосила бы весь пул подчистую. Раньше чистка не срабатывала и при
            # живой сети - из-за этого галочка выглядела «нерабочей».
            if not check(None)["ok"]:
                log("[прокси] живых нет и сети нет - чистку мёртвых пропускаю")
                return good, bad
            allow_all = True
            log("[прокси] живых нет вовсе (пул разом выдохся), но сеть есть - "
                "убираю мёртвых, как просили галочкой")
        purged, reason = db.delete_dead_proxies(con, allow_all_dead=allow_all)
        if purged:
            left = len(db.list_proxies(con, active_only=False))
            log("[прокси] мёртвые убраны из пула: %d, осталось %d" % (purged, left))
    return good, bad
