#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
proxycheck - НАШ сервис проверки прокси (живёт на нашем VPS example-vps).

Зачем он вместо чужого ip-api.com: нам нужен ОДИН быстрый ответ на вопрос
«с какого адреса ты пришёл?». Чужой сервис отвечает медленно, живёт с лимитами
и может отвалиться - а проверка пула на 400 адресов из-за этого врёт (мёртвыми
выглядят живые). Здесь это свой сервис: один HTTPS-запрос через прокси - и мы
знаем выходной адрес, а заодно убедились, что прокси не подменяет сертификат
(сертификат наш, настоящий, подделать его бесплатный прокси не может).

Развёрнут так:
    /opt/proxycheck/proxycheck.py   - этот файл
    systemd-юнит proxycheck         - слушает 127.0.0.1:8790
    Apache (pay.apiteka.pp.ua)      - ProxyPass отдаёт наружу по HTTPS:
                                      https://pay.apiteka.pp.ua/ip

Точки входа:
    GET /ip             -> {"ip": "1.2.3.4"}            (мгновенно, без внешних)
    GET /geo?ip=1.2.3.4 -> {"ip": ..., "country": ..., "code": ...}  (с кэшем)
    GET /ping           -> "ok"

Почему страна отдельной точкой: определять её для КАЖДОГО адреса пула (400 шт)
нельзя - у внешнего сервиса лимит, и проверка встанет. Клиент спрашивает /geo
только для тех адресов, что оказались живыми (их единицы), и кэширует у себя.
"""
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PROXYCHECK_PORT", "8790"))
DB = os.environ.get("PROXYCHECK_DB", "/opt/proxycheck/geo.db")
HIT_TTL = 30 * 86400      # удачный ответ о стране живёт месяц
MISS_TTL = 6 * 3600       # неудачный - 6 часов, чтобы не долбить сервис
UA = "proxycheck/1.0 (+mailbox_manager)"


def _con():
    con = sqlite3.connect(DB, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS geo("
                "ip TEXT PRIMARY KEY, country TEXT, code TEXT, ts REAL)")
    return con


def _cached(ip):
    try:
        con = _con()
        row = con.execute("SELECT country, code, ts FROM geo WHERE ip=?", (ip,)).fetchone()
        con.close()
    except Exception:
        return None
    if not row:
        return None
    ttl = HIT_TTL if row[0] else MISS_TTL
    if time.time() - (row[2] or 0) < ttl:
        return row[0] or "", row[1] or ""
    return None


def _store(ip, country, code):
    try:
        con = _con()
        con.execute("INSERT OR REPLACE INTO geo(ip,country,code,ts) VALUES(?,?,?,?)",
                    (ip, country or "", code or "", time.time()))
        con.commit()
        con.close()
    except Exception:
        pass


def _fetch(url, timeout=4):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _private(ip):
    return (not ip) or ip.startswith(("10.", "127.", "192.168.", "172.16.", "172.17.",
                                      "172.18.", "172.19.", "172.2", "172.30.", "172.31.",
                                      "169.254.", "::1", "fd", "fe80"))


def geo(ip):
    """Страна по адресу. Свой кэш + внешний сервис только на промахе."""
    if _private(ip):
        return "", ""
    hit = _cached(ip)
    if hit is not None:
        return hit
    country = code = ""
    try:
        d = _fetch("http://ip-api.com/json/%s?fields=status,country,countryCode" % ip)
        if d.get("status") == "success":
            country, code = d.get("country") or "", d.get("countryCode") or ""
    except Exception as e:
        sys.stderr.write("[geo] ip-api: %s\n" % e)
    if not code:
        try:
            d = _fetch("https://ipwho.is/%s" % ip)
            if d.get("success"):
                country, code = d.get("country") or "", d.get("country_code") or ""
        except Exception as e:
            sys.stderr.write("[geo] ipwho: %s\n" % e)
    _store(ip, country, code)
    return country, code


class Handler(BaseHTTPRequestHandler):
    server_version = "proxycheck/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))

    def _client_ip(self):
        """Адрес, с которого реально пришёл запрос.

        Apache, проксируя, ПРИПИСЫВАЕТ в конец X-Forwarded-For тот адрес, который
        он видит сам. Значит последний элемент - настоящий, а всё, что левее,
        мог подсунуть сам прокси (подделать выходной адрес - нам это не нужно).
        """
        xff = self.headers.get("X-Forwarded-For") or ""
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
        return self.client_address[0]

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        if path == "/ping":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/ip":
            self._send({"ip": self._client_ip(), "service": "proxycheck"})
            return
        if path == "/geo":
            q = urllib.parse.parse_qs(u.query)
            ip = (q.get("ip") or [""])[0].strip()
            country, code = geo(ip)
            self._send({"ip": ip, "country": country, "code": code})
            return
        if path == "/stats":
            try:
                con = _con()
                n = con.execute("SELECT COUNT(*) FROM geo").fetchone()[0]
                con.close()
            except Exception:
                n = -1
            self._send({"cached_ips": n})
            return
        self._send({"error": "not found"}, 404)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write("proxycheck слушает 127.0.0.1:%d, кэш %s\n" % (PORT, DB))
    srv.serve_forever()


if __name__ == "__main__":
    main()
