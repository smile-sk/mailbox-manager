# -*- coding: utf-8 -*-
"""
gui.py - графический интерфейс mailbox_manager (tkinter + i18n, stdlib).

Вкладки:
  Ящики   - таблица ящиков, создание «по кнопке» (логин с цифрами + пароль),
            авторегистрация для сервисов с драйвером (Proton, Tuta) - программа сама
            заполняет форму и останавливается на капче, открытие почты в браузере
            (свой профиль + случайный прокси из пула) с подстановкой логина и пароля.
  Провайдеры - реестр сервисов: что умеем, плюс добавить / изменить / скрыть.
  Прокси  - пул: импорт из файла, добавить вручную, вкл/выкл, удалить.

Журнал - нижняя панель: всегда видна, тянется вместе с окном, делится
сашем (можно перетащить границу), свой скроллбар и кнопка «Очистить».

Локализация - locales/<code>.json (см. core/i18n.py). Пока файл один
(ru.json); положишь рядом en.json - вверху появится меню выбора языка.

Запуск:  pythonw gui.py   (или  python gui.py)
"""
import json
import os
import queue
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from core import db, providers, generator, i18n, logger, paths

DB_PATH = paths.db_path()
SETTINGS_PATH = paths.settings_path()


def tr(key, **kw):
    return i18n.tr(key, **kw)


def _load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(d):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        i18n.set_lang(_load_settings().get("lang", "ru"))
        self.title(tr("app.title"))

        # размер и центр окна - чтобы влезало целиком (журнал виден сразу)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(1240, sw - 100)
        h = min(860, sh - 120)
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(940, 620)
        # иконка окна: картинка вшита в core/icon.py - в однофайловой сборке
        # никакого .ico рядом нет, файл бы не нашёлся
        try:
            from core import icon as icon_mod
            icon_mod.apply_icon(self)
        except Exception:
            pass

        # очередь сообщений из рабочих потоков -> главный (безопасный tkinter)
        self._q = queue.Queue()
        self._lang_combo = None
        self._lang_codes = []
        self._signup_running = set()   # ящики, для которых сейчас идёт авторегистрация

        self.con = db.connect(DB_PATH)
        providers.seed(self.con)

        self._build_ui()
        self._refresh_mailboxes()
        self._refresh_providers()
        self._refresh_proxies()
        self.after(150, self._drain_queue)
        self.log(tr("log.ready", n=len(db.list_mailboxes(self.con))))
        self._migrate_profiles()
        self._auto_import_proxies()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _migrate_profiles(self):
        """Профили браузера ключуются по id ящика (mb13), а не по email: адрес
        при регистрации меняется. Тут же - чистка папок-сирот. Итог в журнал."""
        try:
            from core import browser as br
            moved, removed = br.migrate_profiles(self.con)
            if moved or removed:
                self.log(tr("log.profiles_moved", moved=moved, removed=removed))
        except Exception as e:
            logger.exception("profiles migrate")
            self.log(tr("log.profiles_err", err=str(e)))

    # ------------------------------------------------------------- UI

    def _teardown_ui(self):
        """Полностью пересобрать окно (смена языка): удалить все виджеты."""
        for child in self.winfo_children():
            child.destroy()
        self._lang_combo = None

    def _build_ui(self):
        # --- верхняя строка: выбор языка (появляется, если языков >= 2) ---
        langs = i18n.available()
        self._lang_codes = sorted(langs)
        if len(self._lang_codes) >= 2:
            top = ttk.Frame(self)
            top.pack(fill="x", padx=8, pady=(6, 0))
            ttk.Label(top, text=tr("lang.label")).pack(side="left")
            names = [langs[c] for c in self._lang_codes]
            self._lang_var = tk.StringVar(value=langs[i18n.code()])
            self._lang_combo = ttk.Combobox(top, textvariable=self._lang_var,
                                            values=names, state="readonly", width=14)
            self._lang_combo.pack(side="left", padx=(4, 0))
            self._lang_combo.bind("<<ComboboxSelected>>", self._on_lang_selected)

        # --- вертикальный саш: сверху вкладки, снизу журнал (оба тянутся) ---
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=6, pady=6)

        nb = ttk.Notebook(paned)
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("Treeview", rowheight=22)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("Title.TLabel", font=("Segoe UI", 11, "bold"))

        self.tab_mail = ttk.Frame(nb)
        self.tab_prov = ttk.Frame(nb)
        self.tab_prox = ttk.Frame(nb)
        nb.add(self.tab_mail, text=tr("tab.mailboxes"))
        nb.add(self.tab_prov, text=tr("tab.providers"))
        nb.add(self.tab_prox, text=tr("tab.proxies"))

        self._build_tab_mail()
        self._build_tab_providers()
        self._build_tab_proxies()
        paned.add(nb, weight=7)

        logf = ttk.LabelFrame(paned, text=tr("log.title"))
        lbar = ttk.Frame(logf)
        lbar.pack(fill="x", padx=4, pady=(2, 0))
        ttk.Button(lbar, text=tr("log.clear"), width=10,
                   command=self._log_clear).pack(side="right")
        lfrm = ttk.Frame(logf)
        lfrm.pack(fill="both", expand=True, padx=4, pady=4)
        self.log_txt = tk.Text(lfrm, height=6, state="disabled", wrap="word",
                               font=("Consolas", 9))
        lvsb = ttk.Scrollbar(lfrm, orient="vertical", command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=lvsb.set)
        self.log_txt.pack(side="left", fill="both", expand=True)
        lvsb.pack(side="right", fill="y")
        paned.add(logf, weight=2)

    # ---------- вкладка Ящики ----------

    def _build_tab_mail(self):
        bar = ttk.Frame(self.tab_mail)
        bar.pack(fill="x", pady=(4, 2))
        ttk.Button(bar, text=tr("mb.create"), command=self._dialog_create).pack(side="left", padx=2)
        ttk.Button(bar, text=tr("mb.open_browser"), command=self._open_browser).pack(side="left", padx=2)
        ttk.Button(bar, text=tr("mb.retry_signup"),
                   command=self._retry_signup).pack(side="left", padx=2)
        ttk.Button(bar, text=tr("mb.proxy_btn"), command=self._dialog_mailbox_proxy).pack(side="left", padx=2)
        ttk.Button(bar, text=tr("mb.delete"), command=self._delete_mailbox).pack(side="left", padx=2)
        ttk.Button(bar, text="⟳", width=3, command=self._refresh_mailboxes).pack(side="left", padx=2)

        topf = ttk.Frame(self.tab_mail)
        topf.pack(fill="both", expand=True)
        cols = ("id", "email", "provider", "person", "status", "proxy", "created")
        self.tree_mb = ttk.Treeview(topf, columns=cols, show="headings", selectmode="browse")
        heads = {"id": (tr("col.id"), 40), "email": (tr("col.email"), 230),
                 "provider": (tr("col.provider"), 90), "person": (tr("col.person"), 130),
                 "status": (tr("col.status"), 95), "proxy": (tr("col.proxy"), 150),
                 "created": (tr("col.created"), 120)}
        for c, (t, w) in heads.items():
            self.tree_mb.heading(c, text=t)
            self.tree_mb.column(c, width=w, anchor="w" if c in ("email", "person") else "center",
                                stretch=(c == "email"))
        vsb = ttk.Scrollbar(topf, orient="vertical", command=self.tree_mb.yview)
        self.tree_mb.configure(yscrollcommand=vsb.set)
        self.tree_mb.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree_mb.tag_configure("verified", foreground="#2e7d32")
        self.tree_mb.tag_configure("created", foreground="#9a9a9a")
        self.tree_mb.tag_configure("banned", foreground="#c62828")
        self.tree_mb.tag_configure("error", foreground="#e65100")
        self.tree_mb.tag_configure("creating", foreground="#1565c0")
        self.tree_mb.bind("<Double-1>", lambda e: self._mailbox_doubleclick())

        tip = ttk.Label(self.tab_mail, text=tr("tab.mail_hint"), foreground="#666",
                        wraplength=760, justify="left")
        tip.pack(anchor="w", padx=6, pady=(2, 6))
    # ---------- вкладка Провайдеры ----------

    def _build_tab_providers(self):
        bar = ttk.Frame(self.tab_prov)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text=tr("prov.add"),
                   command=lambda: self._dialog_provider()).pack(side="left", padx=2)
        ttk.Button(bar, text=tr("prov.edit"),
                   command=self._edit_provider).pack(side="left", padx=2)
        ttk.Button(bar, text=tr("prov.delete"),
                   command=self._delete_provider).pack(side="left", padx=2)
        self._prov_hidden_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text=tr("prov.show_hidden"), variable=self._prov_hidden_var,
                        command=self._refresh_providers).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="⟳", width=3, command=self._refresh_providers).pack(side="left", padx=2)
        ttk.Label(bar, text=tr("prov.note"), foreground="#666").pack(side="left", padx=10)

        # Колонки IMAP больше нет: почта смотрится в браузере, IMAP нужен был
        # только консольным командам. Вместо неё «Статус» - что программа умеет.
        cols = ("name", "family", "country", "status", "signup", "hint")
        self.tree_prov = ttk.Treeview(self.tab_prov, columns=cols, show="headings",
                                      selectmode="browse")
        heads = {"name": (tr("col.provider"), 90), "family": (tr("prov.col.family"), 110),
                 "country": (tr("col.country"), 55), "status": (tr("prov.col.status"), 105),
                 "signup": (tr("prov.col.signup"), 250), "hint": (tr("prov.col.hint"), 300)}
        for c, (t, w) in heads.items():
            self.tree_prov.heading(c, text=t)
            self.tree_prov.column(c, width=w, stretch=(c in ("signup", "hint")))
        self.tree_prov.tag_configure("ready", foreground="#2e7d32")
        self.tree_prov.tag_configure("no_plan", foreground="#9a9a9a")
        self.tree_prov.bind("<Double-1>", lambda e: self._edit_provider())
        vsb = ttk.Scrollbar(self.tab_prov, orient="vertical", command=self.tree_prov.yview)
        self.tree_prov.configure(yscrollcommand=vsb.set)
        self.tree_prov.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        vsb.pack(side="right", fill="y", padx=(0, 4), pady=4)

    # ---------- вкладка Прокси ----------

    def _build_tab_proxies(self):
        # Полоса вкладки разбита на три строки: в одну кнопки пула уже не
        # влезали - на 1240 px самая правая кнопка «⟳» сжималась в 5 пикселей,
        # а подсказка о формате вообще не показывалась (живой замер 19.09.2026,
        # после добавления «Экспорт в файл…»). Теперь ни одна не режется даже
        # на минимальной ширине окна (940 px).
        bar = ttk.Frame(self.tab_prox)
        bar.pack(fill="x", pady=4)
        row_pool = ttk.Frame(bar)
        row_pool.pack(fill="x")
        row_check = ttk.Frame(bar)
        row_check.pack(fill="x")
        row_note = ttk.Frame(bar)
        row_note.pack(fill="x")
        ttk.Button(row_pool, text=tr("prox.import"), command=self._import_proxies).pack(side="left", padx=2)
        ttk.Button(row_pool, text=tr("prox.export"), command=self._export_proxies).pack(side="left", padx=2)
        ttk.Button(row_pool, text=tr("prox.add_manual"), command=self._dialog_add_proxy).pack(side="left", padx=2)
        ttk.Button(row_pool, text=tr("prox.toggle"), command=self._toggle_proxy).pack(side="left", padx=2)
        ttk.Button(row_pool, text=tr("prox.delete"), command=self._delete_proxy).pack(side="left", padx=2)
        ttk.Button(row_check, text=tr("prox.check"), command=self._check_proxies).pack(side="left", padx=2)
        ttk.Button(row_check, text=tr("prox.purge"), command=self._purge_proxies).pack(side="left", padx=2)
        self._purge_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_check, text=tr("prox.autopurge"),
                        variable=self._purge_var).pack(side="left", padx=(6, 0))
        ttk.Button(row_check, text="⟳", width=3, command=self._refresh_proxies).pack(side="left", padx=2)
        # Счётчик живых пакуется ПЕРВЫМ: если ширина впритык, режется
        # подсказка о формате (она есть и в README), а не полезная цифра.
        self._lbl_live = ttk.Label(row_note, text="", foreground="#0a7a0a")
        self._lbl_live.pack(side="left", padx=10)
        ttk.Label(row_note, text=tr("prox.note"), foreground="#666",
                  wraplength=700, justify="left").pack(side="left", padx=10)

        cols = ("id", "kind", "addr", "ip", "country", "active", "fails", "checked")
        self.tree_prox = ttk.Treeview(self.tab_prox, columns=cols, show="headings", selectmode="browse")
        heads = {"id": (tr("col.id"), 40), "kind": (tr("col.kind"), 60),
                 "addr": (tr("col.addr"), 170),
                 "ip": (tr("col.exit_ip"), 120), "country": (tr("col.country"), 90),
                 "active": (tr("col.active"), 70), "fails": (tr("col.fails"), 60),
                 "checked": (tr("col.checked"), 130)}
        for c, (t, w) in heads.items():
            self.tree_prox.heading(c, text=t)
            self.tree_prox.column(c, width=w, anchor="center" if c in ("id", "kind", "active", "fails") else "w")
        vsb = ttk.Scrollbar(self.tab_prox, orient="vertical", command=self.tree_prox.yview)
        self.tree_prox.configure(yscrollcommand=vsb.set)
        self.tree_prox.tag_configure("dead", foreground="#c62828")
        self.tree_prox.tag_configure("off", foreground="#9a9a9a")
        self.tree_prox.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        vsb.pack(side="right", fill="y", padx=(0, 4), pady=4)

    # ---------------------------------------------------------- данные

    def _selected_mailbox(self):
        sel = self.tree_mb.selection()
        if not sel:
            return None
        return db.get_mailbox(self.con, int(self.tree_mb.item(sel[0], "values")[0]))

    def _mailbox_doubleclick(self):
        mb = self._selected_mailbox()
        if mb is not None:
            self._dialog_mailbox_details(mb)

    def _status_text(self, s):
        t = tr("status." + s)
        return t if not t.startswith("status.") else s

    def _refresh_mailboxes(self, select_id=None):
        self.tree_mb.delete(*self.tree_mb.get_children())
        sel_id = None
        pmap = db.proxy_map(self.con)        # с какого адреса выходит каждый ящик
        for r in db.list_mailboxes(self.con):
            item = self.tree_mb.insert("", "end", values=(
                r["id"], r["email"], r["provider"], r["person_name"] or "",
                self._status_text(r["status"]),
                pmap.get(r["id"]) or tr("prox.direct_short"),
                (r["created_at"] or "")[:16],
            ), tags=(r["status"],))
            if select_id is not None and r["id"] == select_id:
                sel_id = item
        if sel_id:
            self.tree_mb.selection_set(sel_id)
            self.tree_mb.focus(sel_id)

    def _refresh_providers(self, select_id=None):
        """Список сервисов. «Статус» вместо прежней колонки IMAP: видно, с каким
        сервисом программа реально умеет работать (есть автопилот регистрации)."""
        self.tree_prov.delete(*self.tree_prov.get_children())
        show_hidden = bool(getattr(self, "_prov_hidden_var", None)
                           and self._prov_hidden_var.get())
        sel_item = None
        for r in db.list_providers(self.con, enabled_only=not show_hidden):
            drv = (r["driver"] or "") if "driver" in r.keys() else ""
            if not r["enabled"]:
                status = tr("prov.status.hidden")
            else:
                status = tr("prov.status.ready") if drv else tr("prov.status.noplan")
            item = self.tree_prov.insert("", "end", values=(
                r["name"], r["family"], r["country"], status,
                r["signup_url"] or "", r["reg_hint"],
            ), tags=("ready",) if drv else ("no_plan",))
            if select_id is not None and r["id"] == select_id:
                sel_item = item
        if sel_item:
            self.tree_prov.selection_set(sel_item)
            self.tree_prov.focus(sel_item)

    def _refresh_proxies(self):
        self.tree_prox.delete(*self.tree_prox.get_children())
        for p in db.list_proxies(self.con, active_only=False):
            dead = (p["fail_count"] or 0) >= 1 or not p["active"]
            self.tree_prox.insert("", "end", values=(
                p["id"], p["kind"], f"{p['host']}:{p['port']}",
                p["exit_ip"] or "-", (p["exit_country"] or "").upper() or "-",
                tr("yes") if p["active"] else tr("no"), p["fail_count"],
                (p["checked_at"] or p["last_used"] or "")[:16],
            ), tags=("dead",) if dead else ())
        self._update_live_counter()

    def _update_live_counter(self):
        """Счётчик в панели вкладки: сколько адресов живо и сколько из них
        реально годятся для Proton (прошли HTTPS-тест с проверкой сертификата).

        Второе число важнее. Бесплатный адрес может «отвечать» и при этом
        подсовывать свой сертификат - на Proton он бесполезен. Из-за разницы
        этих чисел и возникает ложное «все мёртвые»: по сокету 11 живых,
        а годных ноль."""
        try:
            live = db.live_proxy_count(self.con)
            usable = db.usable_proxy_count(self.con)
            # Возраст чисел: «живых 8» без «когда проверяли» врёт - бесплатные
            # адреса живут минутами, а проверка могла быть полчаса назад.
            mins = db.last_check_minutes(self.con)
            age = tr("prox.live_never") if mins is None else tr("prox.live_age", mins=mins)
            self._lbl_live.config(
                text="%s  ·  %s" % (tr("prox.live_n", live=live, usable=usable), age),
                foreground="#0a7a0a" if usable else ("#a05a00" if live else "#a00"))
        except Exception:
            pass

    # ---------------------------------------------------------- действия

    def _dialog_create(self):
        dlg = tk.Toplevel(self)
        dlg.title(tr("dlg.create.title"))
        # Размер окна считаем ПО СОДЕРЖИМОМУ (см. _fit в конце сборки): кнопка
        # «Создать» всегда в кадре, тянуть окно, чтобы до неё добраться, не нужно.
        dlg.resizable(True, True)      # подсказки бывают длинными - окно можно тянуть
        dlg.transient(self)
        dlg.grab_set()

        # Кнопка «Создать» живёт в ПОЛОСЕ ВНИЗУ, и полоса пакуется ПЕРВОЙ: место
        # под неё отнимается у содержимого. Раньше кнопка паковалась последней и
        # длинные подсказки сервисов сталкивали её за нижний край окна (живой
        # случай 19.09.2026: приходилось растягивать окно, чтобы нажать).
        bar = ttk.Frame(dlg)
        bar.pack(side="bottom", fill="x", pady=8)

        # Содержимое - прямо в окне, БЕЗ полосы прокрутки: раньше прокрутка
        # съедала место и всё равно заставляла растягивать окно. Теперь высота и
        # ширина окна считаются по содержимому (см. _fit ниже), поэтому кнопка
        # «Создать» всегда в кадре, а подсказки видны целиком.
        inner = ttk.Frame(dlg)
        inner.pack(side="top", fill="both", expand=True)

        placed = {"done": False}

        def _fit(*_a):
            """Подогнать окно под содержимое: ширина - по самой широкой строке,
            высота - по сумме блоков плюс полоса с кнопкой. В первый раз окно
            ещё и ставим по центру родителя, дальше положение не трогаем."""
            try:
                _fit_desc()
                dlg.update_idletasks()
                w = max(560, inner.winfo_reqwidth() + 28)
                h = inner.winfo_reqheight() + bar.winfo_reqheight() + 26
                h = min(h, max(320, dlg.winfo_screenheight() - 120))
                if placed["done"]:
                    dlg.geometry("%dx%d" % (w, h))
                else:
                    x = self.winfo_rootx() + max(0, (self.winfo_width() - w) // 2)
                    y = self.winfo_rooty() + 60
                    dlg.geometry("%dx%d+%d+%d" % (w, h, x, y))
                    placed["done"] = True
                dlg.minsize(w, h)
            except Exception:
                pass

        ttk.Label(inner, text=tr("dlg.create.service"), style="Title.TLabel").pack(anchor="w", padx=12, pady=(10, 2))
        # Работает пока ОДИН сервис - Proton: только у него есть драйвер
        # авторегистрации. Остальные показываем СЕРЫМИ и выбрать их нельзя -
        # так список не врёт, будто программа умеет их регистрировать.
        provs = sorted(db.list_providers(self.con),
                       key=lambda p: (0 if (p["driver"] or "") else 1, p["name"]))
        names = ["%s  (%s)" % (p["name"], p["country"]) for p in provs]
        ready = [bool(p["driver"] or "") for p in provs]
        state = {"i": next((i for i, ok in enumerate(ready) if ok), 0)}

        lst = tk.Listbox(inner, height=min(6, max(1, len(names))), activestyle="none",
                         exportselection=False, font=("Segoe UI", 9), relief="flat",
                         highlightthickness=0, width=34)
        for i, nm in enumerate(names):
            lst.insert("end", nm)
            if not ready[i]:
                lst.itemconfig(i, foreground="#a8a8a8")
        lst.pack(anchor="w", padx=12)
        lst.selection_set(state["i"])
        ttk.Label(inner, text=tr("dlg.create.grey_hint"), foreground="#888",
                  wraplength=460, justify="left").pack(anchor="w", padx=12, pady=(2, 0))

        # width=1: ширину берём НЕ из виджета (по умолчанию 80 символов, из-за
        # чего окно раздувалось до 710 px), а из самого окна - поле тянется
        # по нему (fill="x") и текст переносится по фактической ширине.
        desc = tk.Text(inner, width=1, height=4, wrap="word", font=("Segoe UI", 9),
                       fg="#333", bg="#f4f4f4", relief="flat", state="disabled",
                       padx=8, pady=6)
        desc.pack(fill="x", padx=12, pady=(8, 0))

        def _fit_desc():
            """Высота поля с подсказкой - по факту текста: ничего не режется и
            нет пустых строк. Окно затем подгоняется под новую высоту (_fit)."""
            try:
                if not desc.winfo_ismapped():   # окно ещё не нарисовано - не гадаем
                    return
                desc.update_idletasks()
                cnt = desc.count("1.0", "end", "displaylines")
                n = int(cnt[0]) if cnt else 4
                desc.configure(height=max(3, min(8, n)))
            except Exception:
                pass


        # Как только поле показалось, пересчитываем высоту окна по факту.
        desc.bind("<Map>", lambda _e: (_fit_desc(), _fit()))

        def _show_desc(_e=None):
            idx = state["i"]
            if idx < 0:
                return
            p = provs[idx]
            desc.config(state="normal")
            desc.delete("1.0", "end")
            drv = p["driver"] if "driver" in p.keys() else ""
            auto = tr("dlg.create.has_auto") if drv else tr("dlg.create.no_auto")
            desc.insert("1.0", "{}\n{}: {}\n{}: {}".format(
                p["reg_hint"] or "",
                tr("dlg.create.signup"), p["signup_url"] or "-",
                tr("dlg.create.auto_cap"), auto))
            desc.config(state="disabled")
            _fit_desc()
            dlg.after(40, _fit)

        def _on_pick(_e=None):
            """Выбор сервиса. Серый (без драйвера) выбрать нельзя: возвращаем
            выделение на рабочий сервис и честно пишем это в журнал."""
            sel = lst.curselection()
            if not sel:
                return
            i = sel[0]
            if not ready[i]:
                self.log(tr("dlg.create.not_ready", name=names[i]))
                lst.selection_clear(0, "end")
                lst.selection_set(state["i"])
                return
            state["i"] = i
            _show_desc()
            _update_auto()

        lst.bind("<<ListboxSelect>>", _on_pick)
        _show_desc()

        # сервисы с драйвером умеют авторегистрацию до капчи
        var_auto = tk.BooleanVar(value=False)
        chk_auto = ttk.Checkbutton(inner, text=tr("dlg.create.auto"),
                                   variable=var_auto, state="disabled")
        chk_auto.pack(anchor="w", padx=12, pady=(8, 0))

        def _update_auto(*_a):
            idx = state["i"]
            has = idx >= 0 and bool(provs[idx]["driver"])
            chk_auto.config(state="normal" if has else "disabled")
            var_auto.set(True if has else False)

        _update_auto()

        ttk.Label(inner, text=tr("dlg.create.count")).pack(anchor="w", padx=12, pady=(10, 2))
        var_n = tk.IntVar(value=1)
        ttk.Spinbox(inner, from_=1, to=50, textvariable=var_n, width=6).pack(anchor="w", padx=12)

        ttk.Label(inner, text=tr("dlg.create.note"), wraplength=470,
                  foreground="#666").pack(anchor="w", padx=12, pady=10)

        def ok():
            idx = state["i"]
            if idx < 0:
                return
            prov = provs[idx]
            if not (prov["driver"] or ""):
                messagebox.showinfo(tr("msg.browser_title"), tr("dlg.create.only_proton"))
                return
            auto = bool(prov["driver"]) and var_auto.get()
            loc_map = {"de": "de", "bg": "bg", "be": "be", "global": "uk"}.get(
                prov["country"].lower(), "uk")
            if auto:
                # авторегистрация: один ящик, программа ведёт до капчи
                first, surname, _ = generator.person(loc_map)
                email, lp = generator.available_local_part(self.con, prov, first, surname)
                if not email:
                    self.log(tr("dlg.create.no_login"))
                    dlg.destroy()
                    return
                pw = generator.password(min_len=prov["pass_min"],
                                        special=bool(prov["pass_special"]))
                mid = db.add_mailbox(self.con, prov["id"], email, pw, login=lp,
                                     person_name=f"{first} {surname}", status="creating")
                dlg.destroy()
                self._refresh_mailboxes(select_id=mid)
                self.log(tr("dlg.auto.start", email=email))
                threading.Thread(target=self._signup_worker, args=(mid,), daemon=True).start()
                return
            n = var_n.get()
            made = []
            for _ in range(n):
                first, surname, _ = generator.person(loc_map)
                email, lp = generator.available_local_part(self.con, prov, first, surname)
                if not email:
                    self.log(tr("dlg.create.no_login"))
                    break
                pw = generator.password(min_len=prov["pass_min"],
                                        special=bool(prov["pass_special"]))
                mid = db.add_mailbox(self.con, prov["id"], email, pw, login=lp,
                                     person_name=f"{first} {surname}", status="created")
                made.append(mid)
            dlg.destroy()
            self._refresh_mailboxes()
            if not made:
                return
            self.log(tr("dlg.create.done", made=len(made), name=prov["name"]))
            self._dialog_mailbox_details(db.get_mailbox(self.con, made[0]))

        _fit()          # окно по содержимому: кнопка «Создать» всегда видна

        ttk.Button(bar, text=tr("dlg.create.ok"), command=ok).pack()
        dlg.bind("<Return>", lambda e: ok())

    def _copy_text(self, text):
        try:
            self.clipboard_clear()
            self.clipboard_append(str(text))
        except Exception:
            pass

    def _dialog_mailbox_details(self, mb):
        dlg = tk.Toplevel(self)
        dlg.title(tr("dlg.mb.title", email=mb["email"]))
        dlg.geometry("580x430")
        dlg.transient(self)

        ttk.Label(dlg, text=tr("dlg.mb.title2"), style="Title.TLabel").pack(
            anchor="w", padx=12, pady=(10, 6))

        def line(label, value, copyable=False):
            f = ttk.Frame(dlg)
            f.pack(fill="x", padx=12, pady=2)
            ttk.Label(f, text=label, width=20, anchor="w").pack(side="left")
            e = ttk.Entry(f, width=42)
            e.insert(0, value or "")
            e.configure(state="readonly")
            e.pack(side="left", fill="x", expand=True, padx=(0, 6))
            if copyable:
                ttk.Button(f, text=tr("dlg.mb.copy"), width=11,
                           command=lambda v=value: self._copy_text(v)).pack(side="left")
            return e

        login = mb["login"] or mb["email"].split("@")[0]
        line(tr("dlg.mb.email"), mb["email"], copyable=True)
        line(tr("dlg.mb.login"), login, copyable=True)
        line(tr("dlg.mb.password"), mb["password"], copyable=True)
        line(tr("col.provider"), mb["provider"])
        line(tr("dlg.mb.signup_url"), mb["signup_url"] or "-")

        ttk.Label(dlg, text=tr("dlg.mb.hint"), wraplength=540, foreground="#666",
                  justify="left").pack(anchor="w", padx=12, pady=(12, 4))

        if (mb["status"] or "") in ("created", "creating"):
            ttk.Button(dlg, text=tr("mb.retry_signup"),
                       command=lambda: self._retry_signup_id(dlg, mb["id"], mb["email"])
                       ).pack(pady=(10, 0))

        btns = ttk.Frame(dlg)
        btns.pack(pady=10)
        ttk.Button(btns, text=tr("mb.open_browser"),
                   command=lambda: self._open_browser_for(mb["id"])).pack(side="left", padx=4)
        ttk.Button(btns, text=tr("mb.proxy_btn"),
                   command=lambda: self._dialog_proxy_assign(mb)).pack(side="left", padx=4)
        ttk.Button(btns, text=tr("dlg.mb.close"), command=dlg.destroy).pack(side="left", padx=4)

    def _dialog_mailbox_proxy(self):
        """Назначить прокси выбранному ящику вручную."""
        mb = self._selected_mailbox()
        if mb is None:
            messagebox.showinfo(tr("msg.browser_title"), tr("msg.mailbox_first"))
            return
        self._dialog_proxy_assign(mb)

    def _dialog_proxy_assign(self, mb):
        """Окно «Прокси для ящика»: авто / напрямую / выбрать из пула / свой
        адрес. Закреплённый адрес больше не меняется - у ящика всегда один IP,
        а разные ящики получают разные адреса."""
        dlg = tk.Toplevel(self)
        dlg.title(tr("dlg.proxy_assign.title", email=mb["email"]))
        dlg.geometry("600x560")
        dlg.minsize(520, 420)
        dlg.resizable(True, True)
        dlg.transient(self)
        dlg.grab_set()

        # Полоса кнопок внизу, пакуется первой - тот же приём, что в окне
        # «Создать ящики»: содержимое не может выдавить кнопки за край окна.
        btns = ttk.Frame(dlg)
        btns.pack(side="bottom", fill="x", pady=8)

        prof = db.get_profile(self.con, mb["id"])
        mode_now = (prof["proxy_mode"] if prof is not None and prof["proxy_mode"] else "auto")
        cur = db.proxy_map(self.con).get(mb["id"]) or tr("prox.direct_short")
        ttk.Label(dlg, text=tr("dlg.proxy_assign.now", what=cur),
                  style="Title.TLabel").pack(anchor="w", padx=12, pady=(10, 6))

        var = tk.StringVar(value=mode_now if mode_now in ("auto", "none", "manual") else "auto")
        ttk.Radiobutton(dlg, text=tr("dlg.proxy_assign.auto"), value="auto",
                        variable=var).pack(anchor="w", padx=12, pady=2)
        ttk.Radiobutton(dlg, text=tr("dlg.proxy_assign.none"), value="none",
                        variable=var).pack(anchor="w", padx=12, pady=2)
        ttk.Radiobutton(dlg, text=tr("dlg.proxy_assign.pick"), value="pick",
                        variable=var).pack(anchor="w", padx=12, pady=(8, 2))

        # пул: видно и занятые другими ящиками (их брать нельзя - иначе один
        # адрес уйдёт двум ящикам сразу)
        busy = db.assigned_proxy_ids(self.con, except_mailbox_id=mb["id"])
        tree = ttk.Treeview(dlg, columns=("id", "addr", "exit", "note"),
                            show="headings", height=7, selectmode="browse")
        for c, t, w, a in (("id", tr("col.id"), 40, "center"),
                           ("addr", tr("col.addr"), 200, "w"),
                           ("exit", tr("col.exit_ip"), 130, "w"),
                           ("note", "", 150, "w")):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor=a)
        for p in db.list_proxies(self.con, active_only=False):
            note = tr("dlg.proxy_assign.busy") if p["id"] in busy else \
                   (p["exit_country"] or "").upper()
            tree.insert("", "end", values=(p["id"], "%s://%s:%d" % (p["kind"], p["host"], p["port"]),
                                           p["exit_ip"] or "-", note))
        tree.pack(fill="both", expand=True, padx=12, pady=4)

        ttk.Radiobutton(dlg, text=tr("dlg.proxy_assign.manual"), value="manual",
                        variable=var).pack(anchor="w", padx=12, pady=(8, 2))
        e_man = ttk.Entry(dlg, width=60)
        e_man.pack(anchor="w", padx=12)
        ttk.Label(dlg, text=tr("dlg.proxy_assign.fmt"), foreground="#666",
                  wraplength=560, justify="left").pack(anchor="w", padx=12, pady=(2, 0))

        def ok():
            mode = var.get()
            pid = None
            if mode == "pick":
                sel = tree.selection()
                if not sel:
                    messagebox.showinfo(tr("msg.browser_title"), tr("dlg.proxy_assign.need_pick"))
                    return
                pid = int(tree.item(sel[0], "values")[0])
            elif mode == "manual":
                p = db.parse_proxy_line(e_man.get())
                if p is None:
                    messagebox.showerror(tr("msg.error_title"), tr("dlg.proxy_assign.bad"))
                    return
                row = db.find_proxy(self.con, p["host"], p["port"], p["login"])
                if row is not None:
                    pid = row["id"]
                else:
                    pid = db.add_proxy(self.con, p["kind"], p["host"], p["port"],
                                       p["login"], p["passw"])
            dlg.destroy()
            self._assign_proxy(mb, mode, pid)

        ttk.Button(btns, text=tr("dlg.proxy_assign.ok"), command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text=tr("dlg.mb.close"), command=dlg.destroy).pack(side="left", padx=4)

    def _assign_proxy(self, mb, mode, proxy_id=None):
        """Закрепить за ящиком выбранное (или снять закрепление). Проверку
        живости и запись делаем в отдельном потоке - окно не должно ждать."""
        def worker():
            try:
                from core import browser as br
                from core import proxy as proxy_mod
                con = db.connect(DB_PATH)
                br.register_profile(con, mb["id"], mb["email"])
                if mode == "auto":
                    db.set_profile_proxy(con, mb["id"], None, mode="auto")
                    self._q.put(("log", tr("dlg.proxy_assign.auto_done", email=mb["email"])))
                elif mode == "none":
                    db.set_profile_proxy(con, mb["id"], None, mode="none")
                    self._q.put(("log", tr("dlg.proxy_assign.none_done", email=mb["email"])))
                else:
                    row = db.get_proxy(con, proxy_id)
                    if row is None:
                        self._q.put(("log", tr("dlg.proxy_assign.bad")))
                    else:
                        res = proxy_mod.check_row(con, row, force=True,
                                                  log=lambda m: self._q.put(("log", m)))
                        db.set_profile_proxy(con, mb["id"], row["id"],
                                             res.get("ip", ""), res.get("country", ""),
                                             mode="manual")
                        self._q.put(("log", tr("dlg.proxy_assign.done", email=mb["email"],
                                               what=proxy_mod.describe(row, res))))
                con.close()
                self._q.put(("refresh_mb", None))
                self._q.put(("refresh_prox", None))
            except Exception as e:
                logger.exception("proxy assign")
                self._q.put(("log", tr("prox.check_err", err=str(e))))

        threading.Thread(target=worker, daemon=True).start()

    def _open_browser(self):
        mb = self._selected_mailbox()
        if mb is None:
            messagebox.showinfo(tr("msg.browser_title"), tr("msg.mailbox_first"))
            return
        self._open_browser_for(mb["id"])

    def _open_browser_for(self, mb_id):
        mb = db.get_mailbox(self.con, mb_id)
        self.log(tr("log.browser_start", what=tr("browser.what_login"),
                    email=mb["email"]))
        threading.Thread(target=self._browser_worker, args=(mb["id"],), daemon=True).start()

    def _browser_worker(self, mb_id):
        try:
            from core import browser as br
            con = db.connect(DB_PATH)
            mb = db.get_mailbox(con, mb_id)
            # черновик -> страница регистрации (доделать), живой -> веб-почта
            url = None
            live = mb["status"] not in ("created", "creating")
            if not live and mb["signup_url"]:
                url = mb["signup_url"]
            br.open_mailbox_browser(
                con, mb, proxy=None, headless=False,
                auto_url=url,
                wait="window",
                prefill=live,
                log=lambda m: self._q.put(("log", m)),
            )
            con.close()
            self._q.put(("refresh_mb", None))
            self._q.put(("log", tr("browser.closed")))
        except ModuleNotFoundError as e:
            logger.exception("browser: module missing")
            self._q.put(("browser_err", "missing:" + str(e)))
        except Exception as e:
            logger.exception("browser")
            self._q.put(("browser_err", str(e)))
    def _on_browser_error(self, err):
        self.log(tr("log.browser_err", err=err))
        if err.startswith("missing:"):
            msg = tr("browser.missing_playwright", name=err[8:])
        else:
            msg = tr("browser.error", err=err)
        messagebox.showerror(tr("msg.browser_title"), msg)

    def _signup_worker(self, mb_id):
        try:
            from core import signup
            con = db.connect(DB_PATH)
            ok, msg = signup.register_with_driver(
                con, mb_id, log=lambda m: self._q.put(("log", "  [регалка] " + str(m))))
            con.close()
            self._q.put(("signup_done", mb_id, ok, msg))
        except ModuleNotFoundError as e:
            logger.exception("signup: module missing")
            self._q.put(("signup_done", mb_id, False, "missing:" + str(e)))
        except Exception as e:
            logger.exception("signup")
            self._q.put(("signup_done", mb_id, False, str(e)))

    def _on_signup_done(self, mb_id, ok, msg):
        self._signup_running.discard(mb_id)
        self._refresh_mailboxes(select_id=mb_id)
        if ok:
            self.log(tr("dlg.auto.done"))
        else:
            self.log(tr("dlg.auto.failed", err=msg))
            if str(msg).startswith("missing:"):
                messagebox.showerror(tr("msg.browser_title"),
                                     tr("browser.missing_playwright", name=str(msg)[8:]))

    def _start_signup(self, mb_id, email=""):
        """Пустить авторегистрацию для черновика - первый раз или ПОВТОР.

        Повтор нужен, когда капчу не успели решить: раньше такой ящик оставался
        черновиком, и запустить авторегистрацию заново было нечем.
        """
        if mb_id in self._signup_running:
            self.log(tr("msg.retry_busy"))
            return False
        self._signup_running.add(mb_id)
        db.update_mailbox(self.con, mb_id, status="creating",
                          note="идёт авторегистрация…")
        self._refresh_mailboxes(select_id=mb_id)
        self.log(tr("dlg.auto.start", email=email or ""))
        threading.Thread(target=self._signup_worker, args=(mb_id,), daemon=True).start()
        return True

    def _retry_signup_id(self, dlg, mb_id, email=""):
        """Доделать авторегистрацию конкретного ящика (из карточки или панели)."""
        if dlg is not None:
            dlg.destroy()
        mb = db.get_mailbox(self.con, mb_id)
        prov = db.get_provider(self.con, mb["provider_id"]) if mb is not None else None
        if not (prov and (prov["driver"] or "")):
            messagebox.showinfo(tr("msg.browser_title"), tr("msg.retry_need_driver"))
            return
        self._start_signup(mb_id, email or mb["email"])

    def _retry_signup(self):
        """Панель: доделать авторегистрацию ВЫБРАННОГО черновика."""
        mb = self._selected_mailbox()
        if mb is None:
            messagebox.showinfo(tr("msg.browser_title"), tr("msg.mailbox_first"))
            return
        if (mb["status"] or "") not in ("created", "creating"):
            messagebox.showinfo(tr("msg.browser_title"), tr("msg.retry_live"))
            return
        self._retry_signup_id(None, mb["id"], mb["email"])

    def _delete_mailbox(self):
        mb = self._selected_mailbox()
        if mb is None:
            return
        if messagebox.askyesno(tr("msg.delete_title"), tr("delete.mailbox", email=mb["email"])):
            # профиль браузера сносим ВМЕСТЕ с ящиком (папка + кэш на диске),
            # иначе в data/profiles копились сироты на сотни мегабайт
            try:
                from core import browser as br
                br.remove_profile(self.con, mb["id"], log=self.log)
            except Exception:
                logger.exception("profile remove")
            db.delete_mailbox(self.con, mb["id"])
            self._refresh_mailboxes()
            self.log(tr("delete.mailbox_ok", email=mb["email"]))

    # ---------- провайдеры ----------

    def _dialog_provider(self, p=None):
        """Окно сервиса: добавить (p=None) или ИЗМЕНИТЬ (p - строка providers).
        Полей IMAP здесь нет: почта смотрится в браузере, IMAP нужен был только
        консольным командам. «Автопилот» - только чтение: драйвер это код, а не
        настройка, вписать его руками нельзя."""
        dlg = tk.Toplevel(self)
        dlg.title(tr("dlg.prov.title_edit", name=p["name"]) if p is not None
                  else tr("dlg.prov.title"))
        dlg.geometry("560x450")
        dlg.transient(self)
        dlg.grab_set()

        fields = [
            ("name", tr("dlg.prov.f_name")),
            ("family", tr("dlg.prov.f_family")),
            ("country", tr("dlg.prov.f_country")),
            ("signup_url", tr("dlg.prov.f_signup")),
            ("login_url", tr("dlg.prov.f_login")),
            ("domains", tr("dlg.prov.f_domains")),
        ]
        vals = {}
        for key, lbl in fields:
            ttk.Label(dlg, text=lbl).pack(anchor="w", padx=12, pady=(4, 0))
            e = ttk.Entry(dlg, width=62)
            e.pack(anchor="w", padx=12)
            if p is not None:
                if key == "domains":
                    try:
                        cur = ", ".join(json.loads(p["domains"] or "[]"))
                    except Exception:
                        cur = p["domains"] or ""
                else:
                    cur = p[key] or ""
                e.insert(0, cur)
            vals[key] = e
        ttk.Label(dlg, text=tr("dlg.prov.f_driver")).pack(anchor="w", padx=12, pady=(4, 0))
        e_drv = ttk.Entry(dlg, width=20)
        e_drv.insert(0, ((p["driver"] or "") if p is not None else "")
                     or tr("dlg.prov.f_driver_none"))
        e_drv.configure(state="readonly")
        e_drv.pack(anchor="w", padx=12)
        ttk.Label(dlg, text=tr("dlg.prov.pass_min")).pack(anchor="w", padx=12, pady=(4, 0))
        e_min = ttk.Entry(dlg, width=6)
        e_min.insert(0, str(p["pass_min"]) if p is not None else "8")
        e_min.pack(anchor="w", padx=12)

        def ok():
            try:
                name = vals["name"].get().strip()
                if not name:
                    messagebox.showerror(tr("msg.error_title"), tr("dlg.prov.name_required"))
                    return
                domains = [d.strip() for d in vals["domains"].get().split(",") if d.strip()] or [name]
                data = dict(
                    family=vals["family"].get().strip() or "own",
                    country=vals["country"].get().strip() or "global",
                    signup_url=vals["signup_url"].get().strip(),
                    login_url=vals["login_url"].get().strip(),
                    domains=domains,
                    pass_min=int(e_min.get() or 8),
                )
                if p is not None:
                    db.update_provider(self.con, p["id"], name=name, **data)
                    dlg.destroy()
                    self._refresh_providers(select_id=p["id"])
                    self.log(tr("prov.updated", name=name))
                else:
                    db.add_provider(self.con, name=name, imap_host="", imap_port=993,
                                    pass_special=0, reg_hint=tr("prov.manual_hint"),
                                    **data)
                    dlg.destroy()
                    self._refresh_providers()
                    self.log(tr("prov.added", name=name))
            except Exception as ex:
                msg = tr("prov.name_busy") if "UNIQUE" in str(ex) else str(ex)
                messagebox.showerror(tr("msg.error_title"), msg)

        ttk.Button(dlg, text=tr("dlg.prov.save"), command=ok).pack(pady=8)

    def _selected_provider(self):
        sel = self.tree_prov.selection()
        if not sel:
            return None
        return db.get_provider_by_name(self.con, self.tree_prov.item(sel[0], "values")[0])

    def _edit_provider(self):
        """Изменить выбранный сервис (то же по двойному клику по строке)."""
        p = self._selected_provider()
        if p is None:
            messagebox.showinfo(tr("msg.browser_title"), tr("prov.pick_first"))
            return
        self._dialog_provider(p)

    def _delete_provider(self):
        """Убрать сервис из списка.

        Сервис из реестра программы НЕ удаляем, а прячем (enabled=0): на старте
        seed() добирает реестр по имени, удалённая строка вернулась бы - и
        «удалённый» сервис появился бы снова. Добавленный руками - сносим совсем.
        """
        p = self._selected_provider()
        if p is None:
            messagebox.showinfo(tr("msg.browser_title"), tr("prov.pick_first"))
            return
        used = db.count_mailboxes_for_provider(self.con, p["id"])
        if used:
            messagebox.showinfo(tr("msg.delete_title"),
                                tr("prov.delete_busy", name=p["name"], n=used))
            return
        drv = (p["driver"] or "") if "driver" in p.keys() else ""
        ask = tr("prov.delete_driver_ask", name=p["name"]) if drv \
            else tr("prov.delete_ask", name=p["name"])
        if not messagebox.askyesno(tr("msg.delete_title"), ask):
            return
        if providers.is_registry(p["name"]):
            db.set_provider_enabled(self.con, p["id"], 0)
            self.log(tr("prov.hidden", name=p["name"]))
        else:
            db.delete_provider(self.con, p["id"])
            self.log(tr("prov.deleted", name=p["name"]))
        self._refresh_providers()

    # ---------- прокси ----------

    # Прокси: пул можно СОХРАНИТЬ в файл и забрать его на новой машине -
    # иначе после переезда приходится заново искать живые адреса и терять
    # отметки о проверках (какой у адреса выходной IP, годен ли он для Proton).

    def _export_proxies(self):
        """Сохранить пул в файл. По расширению: .json - с паролями, страной
        выхода и отметками проверки (для переноса на другую машину), .txt -
        простым списком строк «host:port:логин:пароль» для другого софта."""
        rows = db.list_proxies(self.con, active_only=False)
        if not rows:
            messagebox.showinfo(tr("prox.export_title"), tr("prox.export_none"))
            return
        if not messagebox.askyesno(tr("prox.export_title"),
                                   tr("prox.export_ask", n=len(rows))):
            return
        path = filedialog.asksaveasfilename(
            title=tr("prox.export_what"),
            initialdir=paths.data_dir(),
            initialfile="proxies_%s.json" % time.strftime("%Y%m%d_%H%M"),
            defaultextension=".json",
            filetypes=[(tr("prox.export_ft_json"), "*.json"),
                       (tr("prox.export_ft_txt"), "*.txt"),
                       ("All", "*.*")])
        if not path:
            return
        try:
            n = db.export_proxies(self.con, path)
        except Exception as e:
            logger.exception("proxy export")
            messagebox.showerror(tr("msg.error_title"),
                                 tr("prox.export_err", err=str(e)))
            return
        self.log(tr("prox.export_done", n=n, path=path))

    def _import_proxies(self):
        """Импорт пула: понимает и JSON-экспорт программы (перенос на другую
        машину), и обычный список строк - «host:port:логин:пароль», как в
        data/proxies.txt."""
        path = filedialog.askopenfilename(
            title=tr("prox.file_title"),
            filetypes=[(tr("prox.import_ft_all"), "*.json *.txt"),
                       ("JSON", "*.json"), ("Text", "*.txt"), ("All", "*.*")],
            initialdir=paths.data_dir())
        if not path:
            return
        if path.lower().endswith(".json"):
            try:
                added, upd, skipped, broken = db.import_proxies_json(self.con, path)
            except Exception as e:
                logger.exception("proxy import json")
                messagebox.showerror(tr("msg.error_title"),
                                     tr("prox.import_json_bad", err=str(e)))
                return
            self._refresh_proxies()
            self.log(tr("prox.import_json_done", added=added, upd=upd,
                        skipped=skipped, broken=broken))
            return
        added, dup, broken = db.import_proxy_file_ex(self.con, path)
        self._refresh_proxies()
        self.log(tr("prox.import_done2", added=added, dup=dup, broken=broken))
        if added == 0 and broken:
            # Пул не пополнился НИ ОДНОЙ строкой - говорим прямо и перечисляем,
            # какие форматы понимаем (иначе «импортировано 0» ни о чём не говорит)
            messagebox.showwarning(tr("prox.import_bad_title"),
                                   tr("prox.import_bad", broken=broken, path=path))


    def _dialog_add_proxy(self):
        dlg = tk.Toplevel(self)
        dlg.title(tr("dlg.prox.title"))
        dlg.geometry("480x210")
        dlg.transient(self)
        dlg.grab_set()
        ttk.Label(dlg, text=tr("dlg.prox.line")).pack(anchor="w", padx=12, pady=(10, 2))
        ttk.Label(dlg, text=tr("dlg.prox.fmt"), foreground="#666").pack(anchor="w", padx=12)
        e = ttk.Entry(dlg, width=58)
        e.pack(anchor="w", padx=12, pady=4)

        def ok():
            p = db.parse_proxy_line(e.get())
            if p is None:
                messagebox.showerror(tr("msg.error_title"), tr("dlg.prox.bad_line"))
                return
            db.add_proxy(self.con, p["kind"], p["host"], p["port"], p["login"], p["passw"])
            dlg.destroy()
            self._refresh_proxies()
            self.log(tr("prox.added", url=f"{p['kind']}://{p['host']}:{p['port']}"))

        ttk.Button(dlg, text=tr("dlg.prox.add"), command=ok).pack(pady=8)

    def _toggle_proxy(self):
        sel = self.tree_prox.selection()
        if not sel:
            return
        pid = int(self.tree_prox.item(sel[0], "values")[0])
        cur = db.get_proxy(self.con, pid)
        if cur is None:
            return
        db.set_proxy_active(self.con, pid, not cur["active"])
        self._refresh_proxies()
        if cur["active"]:
            self.log(tr("prox.toggled_off", id=pid))
        else:
            self.log(tr("prox.toggled_on", id=pid))

    def _delete_proxy(self):
        sel = self.tree_prox.selection()
        if not sel:
            return
        pid = int(self.tree_prox.item(sel[0], "values")[0])
        if messagebox.askyesno(tr("msg.delete_title"), tr("delete.proxy", id=pid)):
            db.delete_proxy(self.con, pid)
            self._refresh_proxies()

    def _check_proxies(self):
        """Проверить прокси вживую: жив ли и какой у него выходной адрес.
        Выбран один - проверяем его, ничего не выбрано - весь пул. Сеть уходит
        в отдельный поток, чтобы окно не подвисало."""
        sel = self.tree_prox.selection()
        pid = int(self.tree_prox.item(sel[0], "values")[0]) if sel else None
        purge = bool(getattr(self, "_purge_var", None) and self._purge_var.get())
        self.log(tr("prox.checking") if pid is None else tr("prox.checking_one", id=pid))

        def worker():
            try:
                from core import proxy as proxy_mod
                con = db.connect(DB_PATH)
                if pid is not None:
                    row = db.get_proxy(con, pid)
                    if row is None:
                        good, bad = 0, 0
                    else:
                        res = proxy_mod.check_row(con, row, force=True,
                                                  log=lambda m: self._q.put(("log", m)))
                        good, bad = (1, 0) if res["ok"] else (0, 1)
                else:
                    good, bad = proxy_mod.check_all(
                        con, log=lambda m: self._q.put(("log", m)),
                        purge_dead=purge)
                con.close()
                self._q.put(("log", tr("prox.check_done", good=good, bad=bad)))
                self._q.put(("refresh_prox", None))
            except Exception as e:
                logger.exception("proxy check")
                self._q.put(("log", tr("prox.check_err", err=str(e))))

        threading.Thread(target=worker, daemon=True).start()

    def _purge_proxies(self):
        """Убрать из пула все мёртвые адреса разом: после проверки их бывают
        сотни, вручную такое не вычистить. Живые и закреплённые за ящиками
        остаются. Если живых нет вовсе - сначала убеждаемся, что сеть на
        месте, и только тогда чистим (иначе одна проверка без интернета
        выкосила бы весь пул)."""
        n = db.count_dead_proxies(self.con)
        if n == 0:
            self.log(tr("prox.purge_none"))
            return
        if not messagebox.askyesno(tr("msg.delete_title"), tr("prox.purge_ask", n=n)):
            return

        def worker():
            try:
                from core import proxy as proxy_mod
                con = db.connect(DB_PATH)
                purged, reason = db.delete_dead_proxies(con)
                if purged == 0 and reason == "no_live":
                    if proxy_mod.check(None)["ok"]:
                        purged, reason = db.delete_dead_proxies(con, allow_all_dead=True)
                    else:
                        self._q.put(("log", tr("prox.purge_offline")))
                left = len(db.list_proxies(con, active_only=False))
                con.close()
                if purged:
                    self._q.put(("log", tr("prox.purge_done", n=purged, left=left)))
                self._q.put(("refresh_prox", None))
            except Exception as e:
                logger.exception("proxy purge")
                self._q.put(("log", tr("prox.check_err", err=str(e))))

        threading.Thread(target=worker, daemon=True).start()

    def _auto_import_proxies(self):
        """Пул пуст, а в data/proxies.txt есть реальные строки - забрать их.
        Файл-шаблон (одни комментарии) ничего не даёт - просто скажем об этом."""
        try:
            if db.list_proxies(self.con, active_only=False):
                return
            # файла может не быть на новом ПК - положим шаблон с подсказкой
            paths.ensure_proxies_file()
            path = paths.proxies_path()
            if not os.path.isfile(path):
                return
            with open(path, encoding="utf-8", errors="replace") as f:
                real = [ln for ln in f if ln.strip() and not ln.strip().startswith("#")]
            if not real:
                self.log(tr("prox.no_pool"))
                return
            added, skipped = db.import_proxy_file(self.con, path)
            self._refresh_proxies()
            if added:
                self.log(tr("prox.auto_import", n=added))
        except Exception:
            logger.exception("proxies autoimport")

    # ---------- язык ----------

    def _on_lang_selected(self, _event=None):
        if self._lang_combo is None:
            return
        idx = self._lang_combo.current()
        if idx < 0 or idx >= len(self._lang_codes):
            return
        code = self._lang_codes[idx]
        if code == i18n.code():
            return
        i18n.set_lang(code)
        _save_settings({"lang": i18n.code()})
        self._teardown_ui()
        self.title(tr("app.title"))
        self._build_ui()
        self._refresh_mailboxes()
        self._refresh_providers()
        self._refresh_proxies()
        self.log(tr("lang.changed", lang=i18n.lang_name(code)))

    # ---------- служебное ----------

    def log(self, msg):
        logger.log_file(str(msg))
        self.log_txt.config(state="normal")
        self.log_txt.insert("end", f"{msg}\n")
        self.log_txt.see("end")
        self.log_txt.config(state="disabled")

    def _log_clear(self):
        self.log_txt.config(state="normal")
        self.log_txt.delete("1.0", "end")
        self.log_txt.config(state="disabled")

    def _drain_queue(self):
        try:
            while True:
                item = self._q.get_nowait()
                kind = item[0]
                if kind == "log":
                    m = item[1]
                    if m.startswith("[браузер]"):
                        m = m.replace("[браузер]", "  " + tr("browser.prefix"))
                    elif m.startswith("[прокси]"):
                        m = m.replace("[прокси]", "  " + tr("proxy.prefix"))
                    self.log(m.strip())
                elif kind == "browser_err":
                    self._on_browser_error(item[1])
                elif kind == "signup_done":
                    _, mb_id, ok, msg = item
                    self._on_signup_done(mb_id, ok, msg)
                elif kind == "refresh_mb":
                    self._refresh_mailboxes()
                elif kind == "refresh_prox":
                    self._refresh_proxies()
        except queue.Empty:
            pass
        self.after(150, self._drain_queue)

    def _on_close(self):
        try:
            self.con.close()
        except Exception:
            pass
        self.destroy()


def main():
    # Где искать браузеры - решаем ДО первого запуска Chromium: в exe-сборке
    # они лежат в папке browsers/ рядом с программой, если её положили.
    paths.setup_playwright_env()
    paths.ensure_proxies_file()
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
