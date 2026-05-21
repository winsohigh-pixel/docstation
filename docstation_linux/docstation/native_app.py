from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

import gi

# GTK3 application. On Ubuntu 26.04 GTK4 can be present and PyGObject may
# try to load Gdk 4.0 unless the namespace version is fixed before import.
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # type: ignore

from . import amba
from .config import StationConfig, resolve_relative
from .db import StationDatabase
from .loggingx import Logger
from .runner import DocStationRunner
from .slots import SlotMap, format_candidate

APP_TITLE = "DocStation"

CSS = b"""
window, dialog { background: #101827; color: #eef2ff; font-family: Sans; }
.topbar { background: #0a1020; border-bottom: 1px solid #334155; padding: 6px 14px; }
.title { font-size: 20px; font-weight: 800; color: #ffffff; }
.status { font-size: 13px; color: #cbd5e1; }
.metric { background: #1a2540; border: 1px solid #334155; border-radius: 10px; padding: 5px 12px; }
.metric-title { color: #7a8faa; font-size: 11px; }
.metric-value { color: #ffffff; font-size: 22px; font-weight: 800; }
.slot-card { background: #111827; border: 2px solid #334155; border-radius: 12px; padding: 6px; }
.slot-card-empty      { background: #111827; border-color: #334155; }
.slot-card-live       { background: #0d2535; border-color: #22d3ee; }
.slot-card-reading    { background: #0f1f3a; border-color: #60a5fa; }
.slot-card-queue      { background: #2d1f08; border-color: #f59e0b; }
.slot-card-pause      { background: #1f0a28; border-color: #c084fc; }
.slot-card-connecting { background: #1a2808; border-color: #a3e635; }
.slot-card-connected  { background: #142808; border-color: #4ade80; }
.slot-card-working    { background: #0e1c4a; border-color: #818cf8; }
.slot-card-ready      { background: #0a2214; border-color: #22c55e; }
.slot-card-error      { background: #2d0f0f; border-color: #ef4444; }
.slot-title { color: #ffffff; font-size: 18px; font-weight: 800; }
.slot-state { color: #e2e8f0; font-size: 14px; font-weight: 700; }
.slot-info { color: #aab6cc; font-size: 12px; }
.slot-label { color: #8fa1b8; font-size: 10px; font-weight: 800; }
.slot-big { color: #f8fbff; font-size: 15px; font-weight: 800; }
.slot-small { color: #b4c4d8; font-size: 11px; }
.slot-progress trough { min-height: 8px; border-radius: 6px; background: #0b111c; }
.slot-progress progress { min-height: 8px; border-radius: 6px; background: #38bdf8; }
.archive-path { color: #93c5fd; font-size: 19px; }
button { background: #263650; color: #ffffff; border: 1px solid #465774; border-radius: 15px; padding: 10px 16px; font-size: 19px; min-height: 50px; }
button:hover { background: #334764; }
button.ok { background: #14532d; }
button.warn { background: #6b4e16; }
button.danger { background: #641f2a; }
button.role { background: #1f3b63; }
button.role-active { background: #0f766e; border-color: #5eead4; }
entry, searchentry, combobox, spinbutton { background: #111827; color: #ffffff; border: 1px solid #475569; border-radius: 14px; padding: 10px; font-size: 22px; min-height: 54px; }
treeview { background: #172033; color: #eef2ff; font-size: 18px; }
treeview:selected { background: #0f766e; color: #ffffff; }
notebook tab { background: #1f2937; color: #e5e7eb; padding: 8px 20px; font-size: 16px; }
notebook tab:checked { background: #0f766e; color: #ffffff; }
.textlog { background: #050816; color: #dbeafe; font-family: Monospace; font-size: 15px; }
.small-muted { color: #94a3b8; font-size: 16px; }
"""


def apply_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def human_bytes(value: int | None) -> str:
    n = float(value or 0)
    for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
        if n < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} {unit}"
        n /= 1024
    return str(value or 0)


class TouchPasswordDialog(Gtk.Dialog):
    def __init__(self, parent: Optional[Gtk.Window], title: str, fields: list[str]):
        super().__init__(title=title, transient_for=parent, modal=True)
        self.set_default_size(620, 720)
        self.set_border_width(18)
        self.entries: dict[str, Gtk.Entry] = {}
        self.active_key = fields[0]

        content = self.get_content_area()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.add(outer)

        title_label = Gtk.Label(label=title)
        title_label.set_xalign(0)
        title_label.get_style_context().add_class("title")
        outer.pack_start(title_label, False, False, 0)

        for key in fields:
            label = Gtk.Label(label=key)
            label.set_xalign(0)
            outer.pack_start(label, False, False, 0)
            entry = Gtk.Entry()
            entry.set_visibility(False)
            entry.set_invisible_char("●")
            entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
            entry.connect("focus-in-event", self._entry_focus, key)
            outer.pack_start(entry, False, False, 0)
            self.entries[key] = entry

        grid = Gtk.Grid()
        grid.set_row_spacing(12)
        grid.set_column_spacing(12)
        outer.pack_start(grid, True, True, 8)
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "C", "0", "←"]
        for i, key in enumerate(keys):
            btn = Gtk.Button(label=key)
            btn.set_size_request(120, 82)
            btn.connect("clicked", self._key_clicked, key)
            grid.attach(btn, i % 3, i // 3, 1, 1)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        outer.pack_start(actions, False, False, 0)
        cancel = Gtk.Button(label="Отмена")
        cancel.get_style_context().add_class("danger")
        cancel.connect("clicked", lambda *_: self.response(Gtk.ResponseType.CANCEL))
        ok = Gtk.Button(label="OK")
        ok.get_style_context().add_class("ok")
        ok.connect("clicked", lambda *_: self.response(Gtk.ResponseType.OK))
        actions.pack_start(cancel, True, True, 0)
        actions.pack_start(ok, True, True, 0)
        self.show_all()
        self.entries[self.active_key].grab_focus()

    def _entry_focus(self, _entry: Gtk.Entry, _event: Any, key: str) -> None:
        self.active_key = key

    def _key_clicked(self, _btn: Gtk.Button, key: str) -> None:
        entry = self.entries[self.active_key]
        text = entry.get_text()
        if key == "C":
            entry.set_text("")
        elif key == "←":
            entry.set_text(text[:-1])
        else:
            entry.set_text(text + key)
        entry.grab_focus()
        entry.set_position(-1)

    def value(self, field: str) -> str:
        return self.entries[field].get_text()


class LoginWindow(Gtk.Window):
    def __init__(self, cfg: StationConfig, db: StationDatabase, on_login):
        super().__init__(title="Вход — DocStation")
        self.cfg = cfg
        self.db = db
        self.on_login = on_login
        self.role = "operator"
        self.set_default_size(720, 760)
        self.set_position(Gtk.WindowPosition.CENTER)
        if os.environ.get("DOCSTATION_WINDOWED") != "1":
            self.set_decorated(False)
            self.fullscreen()
        self.connect("destroy", Gtk.main_quit)
        apply_css()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        outer.set_margin_top(28)
        outer.set_margin_bottom(28)
        outer.set_margin_start(36)
        outer.set_margin_end(36)
        self.add(outer)

        title = Gtk.Label(label="DocStation")
        title.get_style_context().add_class("title")
        title.set_xalign(0)
        outer.pack_start(title, False, False, 0)

        hint = Gtk.Label(label="Вход в архив. Пароли по умолчанию: оператор 111, админ 888")
        hint.get_style_context().add_class("small-muted")
        hint.set_xalign(0)
        outer.pack_start(hint, False, False, 0)

        role_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        outer.pack_start(role_box, False, False, 8)
        self.op_btn = Gtk.Button(label="Оператор")
        self.adm_btn = Gtk.Button(label="Администратор")
        for b in (self.op_btn, self.adm_btn):
            b.get_style_context().add_class("role")
            role_box.pack_start(b, True, True, 0)
        self.op_btn.connect("clicked", lambda *_: self.set_role("operator"))
        self.adm_btn.connect("clicked", lambda *_: self.set_role("admin"))

        self.password = Gtk.Entry()
        self.password.set_visibility(False)
        self.password.set_invisible_char("●")
        self.password.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.password.set_placeholder_text("Пароль")
        self.password.connect("activate", lambda *_: self.try_login())
        outer.pack_start(self.password, False, False, 0)
        self.set_role("operator")

        self.error = Gtk.Label(label="")
        self.error.set_xalign(0)
        outer.pack_start(self.error, False, False, 0)

        grid = Gtk.Grid()
        grid.set_row_spacing(12)
        grid.set_column_spacing(12)
        grid.set_halign(Gtk.Align.CENTER)
        outer.pack_start(grid, True, True, 8)
        for i, key in enumerate(["1", "2", "3", "4", "5", "6", "7", "8", "9", "C", "0", "←"]):
            btn = Gtk.Button(label=key)
            btn.set_size_request(130, 86)
            btn.connect("clicked", self.key_click, key)
            grid.attach(btn, i % 3, i // 3, 1, 1)

        login = Gtk.Button(label="Войти")
        login.get_style_context().add_class("ok")
        login.connect("clicked", lambda *_: self.try_login())
        outer.pack_start(login, False, False, 0)
        self.show_all()

    def set_role(self, role: str) -> None:
        self.role = role
        if hasattr(self, "op_btn") and hasattr(self, "adm_btn"):
            self.op_btn.get_style_context().remove_class("role-active")
            self.adm_btn.get_style_context().remove_class("role-active")
            (self.op_btn if role == "operator" else self.adm_btn).get_style_context().add_class("role-active")
        if hasattr(self, "password"):
            self.password.grab_focus()

    def key_click(self, _btn: Gtk.Button, key: str) -> None:
        text = self.password.get_text()
        if key == "C":
            self.password.set_text("")
        elif key == "←":
            self.password.set_text(text[:-1])
        else:
            self.password.set_text(text + key)
        self.password.grab_focus()
        self.password.set_position(-1)

    def try_login(self) -> None:
        if self.db.check_password(self.role, self.password.get_text()):
            self.db.add_audit(self.role, "login", details="Native app login")
            self.hide()
            self.on_login(self.role)
            return
        self.error.set_markup("<span foreground='#fca5a5'>Неверный пароль</span>")
        self.password.set_text("")


class MainWindow(Gtk.Window):
    def __init__(self, cfg: StationConfig, db: StationDatabase, logger: Logger, role: str, config_path: str = "StationConfig.linux.json"):
        super().__init__(title=APP_TITLE)
        self.cfg = cfg
        self.config_path = config_path
        self.db = db
        self.logger = logger
        self.role = role
        self.runner_thread: Optional[threading.Thread] = None
        self.runner_instance: Optional[DocStationRunner] = None
        self.runner_active = False
        self.media_rows: list[Any] = []
        self.audit_rows: list[Any] = []
        self.slot_cards: dict[int, dict[str, Any]] = {}
        self.set_default_size(1920, 1080)
        self.set_position(Gtk.WindowPosition.CENTER)
        if os.environ.get("DOCSTATION_WINDOWED") != "1":
            self.set_decorated(False)
            self.fullscreen()
        self.connect("destroy", self.on_destroy)
        apply_css()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        top.get_style_context().add_class("topbar")
        root.pack_start(top, False, False, 0)

        # Brand — 5 тапов открывают сервисный режим (скрыто от оператора)
        self._service_tap_count = 0
        self._service_tap_last  = 0.0
        brand_box = Gtk.EventBox()
        brand = Gtk.Label(label="DocStation")
        brand.get_style_context().add_class("title")
        brand_box.add(brand)
        brand_box.connect("button-press-event", self._on_brand_tap)
        top.pack_start(brand_box, False, False, 0)

        sep0 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        top.pack_start(sep0, False, False, 4)

        # Inline metrics — 6 counters in topbar
        self.metrics: dict[str, Gtk.Label] = {}
        metric_names = [
            ("total",     "ВСЕГО"),
            ("queue",     "ОЧЕРЕДЬ"),
            ("connected", "ПОДКЛЮЧЕНЫ"),
            ("importing", "СКАЧИВАНИЕ"),
            ("ready",     "ГОТОВЫ"),
            ("errors",    "ОШИБКИ"),
        ]
        for key, title in metric_names:
            m = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            m.get_style_context().add_class("metric")
            tl = Gtk.Label(label=title)
            tl.get_style_context().add_class("metric-title")
            vl = Gtk.Label(label="0")
            vl.get_style_context().add_class("metric-value")
            m.pack_start(tl, False, False, 0)
            m.pack_start(vl, False, False, 0)
            self.metrics[key] = vl
            top.pack_start(m, False, False, 2)

        sep1 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        top.pack_start(sep1, False, False, 4)

        # Archive path — compact
        self.archive_path_label = Gtk.Label(label=str(resolve_relative(self.cfg.storage_root)))
        self.archive_path_label.set_xalign(0)
        self.archive_path_label.set_ellipsize(Pango.EllipsizeMode.START)
        self.archive_path_label.get_style_context().add_class("status")
        top.pack_start(self.archive_path_label, True, True, 0)

        # Role + exit
        role_label = Gtk.Label(label="Админ" if role == "admin" else "Оператор")
        role_label.get_style_context().add_class("status")
        top.pack_start(role_label, False, False, 0)
        self.header = Gtk.Label(label="")  # keep for compat with refresh_status
        exit_btn = Gtk.Button(label="Выход")
        exit_btn.get_style_context().add_class("danger")
        exit_btn.connect("clicked", lambda *_: self.close())
        top.pack_start(exit_btn, False, False, 0)
        self._top_bar = top   # saved for _build_auth_nav
        self._slot_error_since: dict[int, float] = {}   # slot → time.monotonic() when error set

        self.notebook = Gtk.Notebook()
        root.pack_start(self.notebook, True, True, 0)

        # ── Tabs visible to everyone without login ─────────────────────────
        self.build_home_tab()

        # ── Tabs requiring login (operator or admin) ───────────────────────
        self._archive_tab_idx: int = -1
        self._slots_tab_idx:   int = -1
        self._settings_tab_idx: int = -1
        self._audit_tab_idx:   int = -1
        self._password_tab_idx: int = -1
        self.build_archive_tab()
        self.build_slots_tab()
        self.build_settings_tab()
        self.build_audit_tab()
        self.build_password_tab()

        # Build nav buttons that require auth tap
        self._build_auth_nav()

        GLib.timeout_add_seconds(1, self.refresh_status)
        GLib.timeout_add_seconds(2, self.refresh_slot_cards)
        self.refresh_status()
        self.refresh_slot_cards()
        self.show_all()
        # Hide protected tabs until authenticated
        self._update_tab_visibility()
        self.start_continuous_service()

    def on_destroy(self, *_):
        try:
            if self.runner_instance is not None:
                self.runner_instance.stop()
        except Exception:
            pass
        self.db.add_audit(self.role, "logout", details="Native app exit")
        Gtk.main_quit()

    # ── Auth / role management ──────────────────────────────────────────────

    def _build_auth_nav(self) -> None:
        """Add an auth button row in the topbar so operator can unlock extra tabs."""
        # Already have topbar — inject lock button after exit_btn.
        # We re-find the topbar by looking at the first child of root.
        # Simpler: keep a reference. We patch the topbar box built in __init__.
        # The lock button is added as the last item of `top` via a stored ref.
        if hasattr(self, "_top_bar"):
            lock_btn = Gtk.Button(label="🔒")
            lock_btn.get_style_context().add_class("role")
            lock_btn.set_tooltip_text("Войти / сменить роль")
            lock_btn.connect("clicked", lambda *_: self._show_auth_dialog())
            self._top_bar.pack_start(lock_btn, False, False, 0)
            lock_btn.show()
            self._lock_btn = lock_btn

    def _show_auth_dialog(self) -> None:
        """Compact touch-friendly auth dialog: role selector + numpad."""
        dlg = Gtk.Dialog(title="Вход", transient_for=self, modal=True)
        dlg.set_default_size(520, 680)
        content = dlg.get_content_area()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_border_width(20)
        content.add(box)

        t = Gtk.Label(label="Авторизация")
        t.set_xalign(0)
        t.get_style_context().add_class("title")
        box.pack_start(t, False, False, 0)

        # Role toggle
        role_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.pack_start(role_box, False, False, 0)
        _role = ["operator"]
        op_btn = Gtk.Button(label="Оператор")
        adm_btn = Gtk.Button(label="Администратор")
        op_btn.get_style_context().add_class("role-active")
        adm_btn.get_style_context().add_class("role")

        def _set_role(r):
            _role[0] = r
            op_btn.get_style_context().remove_class("role-active")
            adm_btn.get_style_context().remove_class("role-active")
            op_btn.get_style_context().add_class("role")
            adm_btn.get_style_context().add_class("role")
            (op_btn if r == "operator" else adm_btn).get_style_context().remove_class("role")
            (op_btn if r == "operator" else adm_btn).get_style_context().add_class("role-active")

        op_btn.connect("clicked", lambda *_: _set_role("operator"))
        adm_btn.connect("clicked", lambda *_: _set_role("admin"))
        role_box.pack_start(op_btn, True, True, 0)
        role_box.pack_start(adm_btn, True, True, 0)

        pwd_entry = Gtk.Entry()
        pwd_entry.set_visibility(False)
        pwd_entry.set_invisible_char("●")
        pwd_entry.set_placeholder_text("Пароль")
        box.pack_start(pwd_entry, False, False, 0)

        err_lbl = Gtk.Label(label="")
        err_lbl.set_xalign(0)
        box.pack_start(err_lbl, False, False, 0)

        # Numpad
        grid = Gtk.Grid()
        grid.set_row_spacing(10)
        grid.set_column_spacing(10)
        box.pack_start(grid, True, True, 0)
        for i, key in enumerate(["1","2","3","4","5","6","7","8","9","C","0","←"]):
            btn = Gtk.Button(label=key)
            btn.set_size_request(110, 74)
            def _key(b, k=key):
                t = pwd_entry.get_text()
                if k == "C": pwd_entry.set_text("")
                elif k == "←": pwd_entry.set_text(t[:-1])
                else: pwd_entry.set_text(t + k)
                pwd_entry.set_position(-1)
            btn.connect("clicked", _key)
            grid.attach(btn, i % 3, i // 3, 1, 1)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.pack_start(btn_row, False, False, 0)
        cancel = Gtk.Button(label="Отмена")
        cancel.get_style_context().add_class("danger")
        cancel.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.CANCEL))
        ok_btn = Gtk.Button(label="Войти")
        ok_btn.get_style_context().add_class("ok")

        def _try_login(*_):
            role = _role[0]
            pwd  = pwd_entry.get_text()
            if self.db.check_password(role, pwd):
                self.role = role
                self.db.add_audit(role, "login", details="Touch auth dialog")
                self._update_tab_visibility()
                if hasattr(self, "_lock_btn"):
                    icon = "👤 Адм" if role == "admin" else "👤 Оп"
                    self._lock_btn.set_label(icon)
                dlg.response(Gtk.ResponseType.OK)
            else:
                err_lbl.set_markup("<span foreground='#fca5a5'>Неверный пароль</span>")
                pwd_entry.set_text("")

        ok_btn.connect("clicked", _try_login)
        pwd_entry.connect("activate", _try_login)
        btn_row.pack_start(cancel, True, True, 0)
        btn_row.pack_start(ok_btn, True, True, 0)
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    def _update_tab_visibility(self) -> None:
        """Show/hide notebook tabs based on current role."""
        role = self.role
        # Tabs and their minimum required role:
        # archive → operator+, slots_map → operator+,
        # settings → admin, audit → admin, password → operator+
        tab_roles = {
            "_archive_tab_idx":  "operator",
            "_slots_tab_idx":    "operator",
            "_settings_tab_idx": "admin",
            "_audit_tab_idx":    "admin",
            "_password_tab_idx": "operator",
        }
        for attr, req in tab_roles.items():
            idx = getattr(self, attr, -1)
            if idx < 0:
                continue
            page = self.notebook.get_nth_page(idx)
            if page is None:
                continue
            visible = (role == "admin") or (req == "operator" and role in ("operator", "admin"))
            page.set_visible(visible)
            self.notebook.get_tab_label(page).set_visible(visible)

    def add_tab(self, widget: Gtk.Widget, title: str) -> None:
        label = Gtk.Label(label=title)
        self.notebook.append_page(widget, label)
        idx = self.notebook.page_num(widget)
        # record index for visibility control
        name_map = {
            "Главная":    None,
            "Архив":      "_archive_tab_idx",
            "Ячейки":     "_slots_tab_idx",
            "Настройки":  "_settings_tab_idx",
            "Журнал":     "_audit_tab_idx",
            "Пароль":     "_password_tab_idx",
        }
        attr = name_map.get(title)
        if attr:
            setattr(self, attr, idx)



    def build_home_tab(self) -> None:
        # Slot grid fills the entire tab — counters/path live in the topbar.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_border_width(8)
        self.add_tab(box, "Главная")

        self.slot_grid = Gtk.Grid()
        self.slot_grid.set_column_homogeneous(True)
        self.slot_grid.set_row_homogeneous(True)
        self.slot_grid.set_column_spacing(8)
        self.slot_grid.set_row_spacing(8)
        box.pack_start(self.slot_grid, True, True, 0)
        self.build_slot_cards()

        # Log is intentionally not shown on the operator screen.
        # It is available in Настройки → Журнал for admin only.
        self.log_view = None


    def build_slot_cards(self) -> None:
        # Fixed touch cards from calibrated DockSlots.json. If the map is empty,
        # show 20 placeholders so the operator sees the dock layout.
        for child in self.slot_grid.get_children():
            self.slot_grid.remove(child)
        self.slot_cards.clear()
        sm = SlotMap.load(self.cfg.dock_slots_path)
        rows = sm.rows()
        if not rows:
            rows = []
            for i in range(1, 21):
                from .slots import SlotMapping
                rows.append(SlotMapping(slot=i, label=f"Ячейка {i:02d}", usb_path=""))
        for idx, row in enumerate(rows):
            event = Gtk.EventBox()
            event.set_size_request(374, 223)
            event.get_style_context().add_class("slot-card")
            event.get_style_context().add_class("slot-card-empty")

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(5)
            box.set_margin_bottom(5)
            box.set_margin_start(7)
            box.set_margin_end(7)

            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title = Gtk.Label(label=row.label)
            title.set_xalign(0)
            title.get_style_context().add_class("slot-title")
            state = Gtk.Label(label="не откалиброван" if not row.usb_path else "пусто")
            state.set_xalign(1)
            state.set_ellipsize(Pango.EllipsizeMode.END)
            state.get_style_context().add_class("slot-state")
            top.pack_start(title, True, True, 0)
            top.pack_start(state, True, True, 0)
            box.pack_start(top, False, False, 0)

            dev_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            dev_box = self._slot_value_box("КАМЕРА", "—")
            officer_box = self._slot_value_box("СОТРУДНИК", "—")
            dev_row.pack_start(dev_box["box"], True, True, 0)
            dev_row.pack_start(officer_box["box"], True, True, 0)
            box.pack_start(dev_row, False, False, 0)

            battery = Gtk.Label(label="нет устройства")
            battery.set_xalign(0)
            battery.get_style_context().add_class("slot-small")
            box.pack_start(battery, False, False, 0)

            storage_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            storage_label = Gtk.Label(label="ПАМЯТЬ")
            storage_label.set_xalign(0)
            storage_label.get_style_context().add_class("slot-label")
            storage_text = Gtk.Label(label="—")
            storage_text.set_xalign(1)
            storage_text.get_style_context().add_class("slot-small")
            storage_row.pack_start(storage_label, True, True, 0)
            storage_row.pack_start(storage_text, True, True, 0)
            box.pack_start(storage_row, False, False, 0)
            storage_bar = Gtk.ProgressBar()
            storage_bar.get_style_context().add_class("slot-progress")
            box.pack_start(storage_bar, False, False, 0)

            total_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            total_label = Gtk.Label(label="ОБЩИЙ ПРОГРЕСС")
            total_label.set_xalign(0)
            total_label.get_style_context().add_class("slot-label")
            total_text = Gtk.Label(label="—")
            total_text.set_xalign(1)
            total_text.get_style_context().add_class("slot-small")
            total_row.pack_start(total_label, True, True, 0)
            total_row.pack_start(total_text, True, True, 0)
            box.pack_start(total_row, False, False, 0)
            total_bar = Gtk.ProgressBar()
            total_bar.get_style_context().add_class("slot-progress")
            box.pack_start(total_bar, False, False, 0)

            file_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            file_label = Gtk.Label(label="ТЕКУЩИЙ ФАЙЛ")
            file_label.set_xalign(0)
            file_label.get_style_context().add_class("slot-label")
            file_text = Gtk.Label(label="")
            file_text.set_xalign(1)
            file_text.get_style_context().add_class("slot-small")
            file_row.pack_start(file_label, True, True, 0)
            file_row.pack_start(file_text, True, True, 0)
            box.pack_start(file_row, False, False, 0)
            file_bar = Gtk.ProgressBar()
            file_bar.get_style_context().add_class("slot-progress")
            box.pack_start(file_bar, False, False, 0)

            bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            speed = Gtk.Label(label="")
            speed.set_xalign(0)
            speed.get_style_context().add_class("slot-small")
            eta = Gtk.Label(label="")
            eta.set_xalign(1)
            eta.get_style_context().add_class("slot-small")
            bottom.pack_start(speed, True, True, 0)
            bottom.pack_start(eta, True, True, 0)
            box.pack_start(bottom, False, False, 0)

            # Technical USB path is intentionally not shown to the operator.
            info = Gtk.Label(label="")
            info.set_xalign(0)
            info.set_ellipsize(Pango.EllipsizeMode.END)
            info.get_style_context().add_class("slot-info")

            event.add(box)
            self.slot_grid.attach(event, idx % 5, idx // 5, 1, 1)
            self.slot_cards[row.slot] = {
                "event": event,
                "state": state,
                "device": dev_box["value"],
                "officer": officer_box["value"],
                "battery": battery,
                "storage_text": storage_text,
                "storage_bar": storage_bar,
                "total_text": total_text,
                "total_bar": total_bar,
                "file_text": file_text,
                "file_bar": file_bar,
                "speed": speed,
                "eta": eta,
                "info": info,
                "usb_path": row.usb_path,
            }
        self.slot_grid.show_all()

    def _slot_value_box(self, caption: str, value: str) -> dict[str, Any]:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        outer.set_name("slot_value_box")
        cap = Gtk.Label(label=caption)
        cap.set_xalign(0)
        cap.get_style_context().add_class("slot-label")
        val = Gtk.Label(label=value)
        val.set_xalign(0)
        val.set_ellipsize(Pango.EllipsizeMode.END)
        val.get_style_context().add_class("slot-big")
        outer.pack_start(cap, False, False, 0)
        outer.pack_start(val, False, False, 0)
        return {"box": outer, "value": val}

    @staticmethod
    def _pct(value: str) -> float:
        try:
            return max(0.0, min(100.0, float(str(value or "0").replace(",", "."))))
        except Exception:
            return 0.0

    # ── Service mode — 5 тапов по логотипу ──────────────────────────────────

    def _on_brand_tap(self, _widget, event) -> bool:
        """Считаем тапы по логотипу. 5 тапов за 4 сек → запрос сервисного PIN."""
        now = event.time / 1000.0  # GTK time in ms → sec
        if now - self._service_tap_last > 4.0:
            self._service_tap_count = 0
        self._service_tap_last = now
        self._service_tap_count += 1
        if self._service_tap_count >= 5:
            self._service_tap_count = 0
            GLib.idle_add(self._show_service_auth)
        return False

    def _show_service_auth(self) -> None:
        """PIN-диалог для входа в сервисный режим."""
        dlg = Gtk.Dialog(title="Сервисный режим", transient_for=self, modal=True)
        dlg.set_default_size(480, 560)
        content = dlg.get_content_area()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_border_width(24)
        content.add(box)

        t = Gtk.Label(label="⚙  Сервисный режим")
        t.set_xalign(0)
        t.get_style_context().add_class("title")
        box.pack_start(t, False, False, 0)

        hint = Gtk.Label(label="Для технических операций.\nВведите сервисный PIN.")
        hint.set_xalign(0)
        hint.get_style_context().add_class("small-muted")
        hint.set_line_wrap(True)
        box.pack_start(hint, False, False, 0)

        pwd = Gtk.Entry()
        pwd.set_visibility(False)
        pwd.set_invisible_char("●")
        pwd.set_placeholder_text("Сервисный PIN")
        box.pack_start(pwd, False, False, 0)

        err = Gtk.Label(label="")
        err.set_xalign(0)
        box.pack_start(err, False, False, 0)

        # Numpad
        grid = Gtk.Grid()
        grid.set_row_spacing(8)
        grid.set_column_spacing(8)
        box.pack_start(grid, True, True, 0)
        for i, key in enumerate(["1","2","3","4","5","6","7","8","9","C","0","←"]):
            btn = Gtk.Button(label=key)
            btn.set_size_request(100, 68)
            def _k(b, k=key):
                t = pwd.get_text()
                if k == "C": pwd.set_text("")
                elif k == "←": pwd.set_text(t[:-1])
                else: pwd.set_text(t + k)
                pwd.set_position(-1)
            btn.connect("clicked", _k)
            grid.attach(btn, i % 3, i // 3, 1, 1)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.pack_start(btns, False, False, 0)
        cancel = Gtk.Button(label="Отмена")
        cancel.get_style_context().add_class("danger")
        cancel.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.CANCEL))
        ok = Gtk.Button(label="Войти")
        ok.get_style_context().add_class("ok")

        def _try(*_):
            if self.db.check_password("service", pwd.get_text()):
                dlg.response(Gtk.ResponseType.OK)
            else:
                err.set_markup("<span foreground='#fca5a5'>Неверный PIN</span>")
                pwd.set_text("")

        ok.connect("clicked", _try)
        pwd.connect("activate", _try)
        btns.pack_start(cancel, True, True, 0)
        btns.pack_start(ok, True, True, 0)
        dlg.show_all()
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.OK:
            GLib.idle_add(self._open_service_panel)

    def _open_service_panel(self) -> None:
        """Диалог сервисных операций — калибровка стаканов и дисков."""
        dlg = Gtk.Dialog(title="Сервисный режим", transient_for=self, modal=True)
        dlg.set_default_size(640, 560)
        content = dlg.get_content_area()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_border_width(28)
        content.add(box)

        hdr = Gtk.Label(label="⚙  Сервисные операции")
        hdr.set_xalign(0)
        hdr.get_style_context().add_class("title")
        box.pack_start(hdr, False, False, 0)

        status_lbl = Gtk.Label(label="")
        status_lbl.set_xalign(0)
        status_lbl.get_style_context().add_class("archive-path")
        status_lbl.set_line_wrap(True)

        def _run_in_terminal(cmd: str, desc: str) -> None:
            """Запустить команду в терминале — оператор видит процесс."""
            status_lbl.set_text(f"Запущено: {desc}")
            try:
                # gnome-terminal или xterm
                for term, args in [
                    ("gnome-terminal", ["gnome-terminal", "--", "bash", "-c", f"{cmd}; echo; echo 'Нажмите Enter для закрытия'; read"]),
                    ("xterm",          ["xterm", "-e", f"bash -c '{cmd}; echo; echo Нажмите Enter; read'"]),
                    ("x-terminal-emulator", ["x-terminal-emulator", "-e", f"bash -c '{cmd}; read'"]),
                ]:
                    import shutil as _sh
                    if _sh.which(term):
                        subprocess.Popen(args)
                        return
                # Нет терминала — запускаем фоном, результат в лог
                subprocess.Popen(["bash", "-c", cmd])
                status_lbl.set_text(f"{desc} — запущено в фоне (см. лог)")
            except Exception as e:
                status_lbl.set_text(f"Ошибка запуска: {e}")

        def _btn(label: str, style: str, cmd: str, desc: str) -> Gtk.Button:
            b = Gtk.Button(label=label)
            b.get_style_context().add_class(style)
            b.connect("clicked", lambda *_: _run_in_terminal(cmd, desc))
            return b

        base = f"cd {Path(self.config_path).resolve().parent}"

        # ── Калибровка USB-стаканов ──────────────────────────────────
        sep1 = Gtk.Label(label="КАЛИБРОВКА USB-СТАКАНОВ")
        sep1.set_xalign(0)
        sep1.get_style_context().add_class("slot-label")
        box.pack_start(sep1, False, False, 4)

        cal_hint = Gtk.Label(
            label="Вставляйте рг поочерёдно в стаканы 01–20.\n"
                  "Программа определит USB-путь каждого стакана автоматически.")
        cal_hint.set_xalign(0)
        cal_hint.set_line_wrap(True)
        cal_hint.get_style_context().add_class("small-muted")
        box.pack_start(cal_hint, False, False, 0)

        cal_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.pack_start(cal_row, False, False, 0)

        for count, label in [(20, "20 стаканов"), (10, "10 стаканов"), (5, "5 стаканов")]:
            cmd = f"{base} && python3 -m docstation.main --config {self.config_path} calibrate-slots --count {count}"
            b = _btn(label, "warn", cmd, f"Калибровка {count} стаканов")
            b.set_size_request(170, 62)
            cal_row.pack_start(b, False, False, 0)

        show_slots_cmd = f"{base} && python3 -m docstation.main --config {self.config_path} show-slots"
        show_btn = _btn("Показать карту стаканов", "role", show_slots_cmd, "Карта стаканов")
        box.pack_start(show_btn, False, False, 0)

        # ── Калибровка SATA-дисков ───────────────────────────────────
        sep2 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        box.pack_start(sep2, False, False, 4)

        disk_lbl = Gtk.Label(label="КАЛИБРОВКА SATA-СЛОТОВ АРХИВА")
        disk_lbl.set_xalign(0)
        disk_lbl.get_style_context().add_class("slot-label")
        box.pack_start(disk_lbl, False, False, 4)

        disk_hint = Gtk.Label(
            label="Вставляйте диски поочерёдно в SATA-слоты.\n"
                  "Программа свяжет физический порт с номером слота.")
        disk_hint.set_xalign(0)
        disk_hint.set_line_wrap(True)
        disk_hint.get_style_context().add_class("small-muted")
        box.pack_start(disk_hint, False, False, 0)

        disk_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.pack_start(disk_row, False, False, 0)

        for count in [4, 2, 1]:
            cmd = f"{base} && python3 -m docstation.main --config {self.config_path} calibrate-storage --count {count}"
            b = _btn(f"{count} слота" if count > 1 else "1 слот", "warn", cmd, f"Калибровка {count} SATA-слотов")
            b.set_size_request(170, 62)
            disk_row.pack_start(b, False, False, 0)

        show_stor_cmd = f"{base} && python3 -m docstation.main --config {self.config_path} show-storage"
        show_stor_btn = _btn("Показать диски", "role", show_stor_cmd, "Состояние дисков")
        box.pack_start(show_stor_btn, False, False, 0)

        # ── Статус и закрытие ────────────────────────────────────────
        sep3 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        box.pack_start(sep3, False, False, 4)
        box.pack_start(status_lbl, False, False, 0)

        close_btn = Gtk.Button(label="Закрыть")
        close_btn.get_style_context().add_class("danger")
        close_btn.set_size_request(-1, 62)
        close_btn.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.CLOSE))
        box.pack_start(close_btn, False, False, 0)

        dlg.show_all()
        dlg.run()
        dlg.destroy()

    @staticmethod
    def _usb_port_has_device(usb_path: str) -> bool:
        """Проверяет физическое наличие устройства на USB-порту через sysfs.

        Работает независимо от режима устройства (Ambarella или Mass Storage),
        поэтому надёжно определяет что рг ещё стоит в стакане.

        usb_path — строка вида "1.2.3" или "ports=1.2.3" из DockSlots.json.
        """
        if not usb_path:
            return False
        clean = usb_path.replace("ports=", "").strip()
        if not clean:
            return False
        try:
            from pathlib import Path as _P
            usb_devices = _P("/sys/bus/usb/devices")
            if not usb_devices.exists():
                return False
            for dev in usb_devices.iterdir():
                devpath_f = dev / "devpath"
                if not devpath_f.exists():
                    continue
                devpath = devpath_f.read_text().strip()
                # devpath в sysfs — последний компонент, например "3" для порта "1.2.3"
                # Совпадение: точное совпадение, или наш путь заканчивается на devpath,
                # или devpath совпадает с последним сегментом нашего пути
                if devpath == clean:
                    return True
                if clean.endswith("." + devpath) or clean == devpath:
                    return True
                # Полный путь: у некоторых ядер devpath = "2.3", у других = "3"
                if "." in clean and devpath == clean.split(".")[-1]:
                    # дополнительно проверяем busnum чтобы не путать порты разных шин
                    busnum_f = dev / "busnum"
                    if busnum_f.exists():
                        bus = busnum_f.read_text().strip()
                        if clean.startswith(bus + ".") or clean == bus:
                            return True
        except Exception:
            pass
        return False

    def slot_class_for_state(self, state: str) -> str:
        st = (state or "").lower()
        # Порядок важен: более специфичные проверки — раньше
        if "ошиб" in st:
            return "slot-card-error"
        if "готов" in st:
            return "slot-card-ready"
        if "пауза" in st:                         # РАНЬШЕ скачив — "пауза перед скачиванием"
            return "slot-card-pause"
        if "скачивание" == st.strip():
            return "slot-card-working"
        if "скачив" in st and "ожидает" not in st:
            return "slot-card-working"
        if "переключ" in st or "считыв" in st:
            return "slot-card-reading"
        if "ожидает" in st:                      # "ожидает скачивания"
            return "slot-card-queue"
        if "подключение" in st:                  # "подключение к архиву"
            return "slot-card-connecting"
        if "подключён" in st:
            return "slot-card-connected"
        if "обнаруж" in st or "виден" in st:
            return "slot-card-live"
        return "slot-card-empty"

    def apply_slot_card(self, slot: int, data: dict[str, str]) -> None:
        card = self.slot_cards.get(slot)
        if not card:
            return
        state = data.get("state") or "пусто"

        # ── Auto-clear finished/error states when device is removed ─────────
        # "ошибка" → сброс через 12 сек если рг ушёл
        # "готово" → сброс только когда рг физически извлечён.
        #   Важно: после импорта рг остаётся в Mass Storage (VID:4255 не виден).
        #   Поэтому проверяем присутствие по USB-порту через sysfs,
        #   а не только через list_ambarella().
        CLEAR_AFTER_ERROR_SEC = 12
        is_error = "ошиб"  in state.lower()
        is_ready = "готов" in state.lower()
        if is_error or is_ready:
            if slot not in self._slot_error_since:
                self._slot_error_since[slot] = time.monotonic()
        else:
            self._slot_error_since.pop(slot, None)

        timeout = CLEAR_AFTER_ERROR_SEC if is_error else CLEAR_AFTER_ERROR_SEC
        if (is_error or is_ready) and (time.monotonic() - self._slot_error_since.get(slot, 0)) > timeout:
            sm = SlotMap.load(self.cfg.dock_slots_path)
            row = next((r for r in sm.rows() if r.slot == slot), None)
            if row:
                still_present = self._usb_port_has_device(row.usb_path)
                if not still_present:
                    # рг физически ушёл — сбрасываем ячейку в "пусто"
                    for key, val in [
                        ("state", "пусто"), ("device", ""), ("officer", ""),
                        ("detail", row.usb_path), ("storage", "—"),
                        ("storage_progress", "0"), ("total_progress", "0"),
                        ("file_progress", "0"), ("current_file", ""),
                        ("speed", ""), ("eta", ""),
                    ]:
                        self.db.upsert_status(f"slot_{slot}_{key}", val)
                    self._slot_error_since.pop(slot, None)
                    state = "пусто"
                    data = {"state": "пусто"}

        event = card["event"]
        ctx = event.get_style_context()
        for cls in ["slot-card-empty", "slot-card-live", "slot-card-reading", "slot-card-queue", "slot-card-pause", "slot-card-connecting", "slot-card-connected", "slot-card-working", "slot-card-ready", "slot-card-error"]:
            ctx.remove_class(cls)
        ctx.add_class(self.slot_class_for_state(state))
        card["state"].set_text(state)

        # FIX 2: при "пусто" явно обнулить все поля — иначе остаётся
        # память и прогресс вынутого регистратора
        is_empty = not state or state.strip() == "пусто"
        if is_empty:
            card["device"].set_text("—")
            card["officer"].set_text("—")
            card["battery"].set_text("нет устройства")
            card["storage_text"].set_text("—")
            card["storage_bar"].set_fraction(0)
            card["total_bar"].set_fraction(0)
            card["file_bar"].set_fraction(0)
            card["total_text"].set_text("—")
            card["file_text"].set_text("")
            card["speed"].set_text("")
            card["eta"].set_text("")
            card["info"].set_text("")
            return

        card["device"].set_text(data.get("device") or "—")
        card["officer"].set_text(data.get("officer") or "—")
        card["battery"].set_text(data.get("battery") or "регистратор / зарядка")
        card["storage_text"].set_text(data.get("storage") or "—")
        storage_pct = self._pct(data.get("storage_progress", "0"))
        card["storage_bar"].set_fraction(storage_pct / 100.0)
        total_pct = self._pct(data.get("total_progress", "0"))
        file_pct = self._pct(data.get("file_progress", "0"))
        card["total_bar"].set_fraction(total_pct / 100.0)
        card["file_bar"].set_fraction(file_pct / 100.0)
        card["total_text"].set_text(f"{int(total_pct)}%" if total_pct > 0 else "—")
        current_file = data.get("current_file") or ""
        card["file_text"].set_text(f"{int(file_pct)}%" if file_pct > 0 else "")
        card["speed"].set_text(data.get("speed") or current_file[:24])
        eta = data.get("eta") or ""
        card["eta"].set_text(("ETA " + eta) if eta else "")
        detail = data.get("detail") or ""
        card["info"].set_text(detail if "ошиб" in state.lower() else "")

    def refresh_slot_cards(self) -> bool:
        try:
            sm = SlotMap.load(self.cfg.dock_slots_path)
            # If calibration changed while the app was open, rebuild the grid.
            if sm.count() and sm.count() != len(self.slot_cards):
                self.build_slot_cards()
            st = self.db.status_dict()
            for row in sm.rows():
                data = {
                    "state": st.get(f"slot_{row.slot}_state", "пусто"),
                    "device": st.get(f"slot_{row.slot}_device", ""),
                    "officer": st.get(f"slot_{row.slot}_officer", ""),
                    "detail": st.get(f"slot_{row.slot}_detail", row.usb_path),
                    "storage": st.get(f"slot_{row.slot}_storage", "—"),
                    "storage_progress": st.get(f"slot_{row.slot}_storage_progress", "0"),
                    "total_progress": st.get(f"slot_{row.slot}_total_progress", "0"),
                    "file_progress": st.get(f"slot_{row.slot}_file_progress", "0"),
                    "current_file": st.get(f"slot_{row.slot}_current_file", ""),
                    "speed": st.get(f"slot_{row.slot}_speed", ""),
                    "eta": st.get(f"slot_{row.slot}_eta", ""),
                    "battery": st.get(f"slot_{row.slot}_battery", ""),
                }
                self.apply_slot_card(row.slot, data)

            # Idle live view: show Ambarella-mode recorders even before import starts.
            if not self.runner_active:
                try:
                    cands = amba.list_ambarella(self.cfg.vendor_vid_int, self.cfg.vendor_pid_int)
                    live_slots = set()
                    for cand in cands:
                        slot = sm.slot_for_candidate(cand)
                        if slot is not None:
                            live_slots.add(slot)
                            current_state = st.get(f"slot_{slot}_state", "")
                            if not current_state or current_state == "пусто":
                                self.apply_slot_card(slot, {"state": "обнаружен", "detail": "", "battery": "регистратор / зарядка"})
                    if cands:
                        if "total" in self.metrics:
                            self.metrics["total"].set_text(str(len(cands)))
                        if "queue" in self.metrics:
                            self.metrics["queue"].set_text(str(len(cands)))
                except Exception:
                    pass
        except Exception as exc:
            self.logger.log(f"UI slot cards error: {exc}")
        return True

    def build_archive_tab(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_border_width(18)
        self.add_tab(box, "Архив")
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.pack_start(top, False, False, 0)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Поиск по имени, регистратору, сотруднику, пути")
        self.search.connect("search-changed", lambda *_: self.refresh_archive())
        top.pack_start(self.search, True, True, 0)
        refresh = Gtk.Button(label="Обновить")
        refresh.connect("clicked", lambda *_: self.refresh_archive())
        top.pack_start(refresh, False, False, 0)

        self.media_store = Gtk.ListStore(int, str, str, str, str, str, str, str)
        self.media_tree = Gtk.TreeView(model=self.media_store)
        self.media_tree.set_headers_visible(True)
        cols = [("ID", 0, 70), ("Дата", 1, 160), ("Файл", 2, 330), ("Регистратор", 3, 140), ("Сотрудник", 4, 130), ("Размер", 5, 110), ("Защита", 6, 90), ("Путь", 7, 520)]
        for title, idx, width in cols:
            r = Gtk.CellRendererText()
            r.set_property("ellipsize", Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(title, r, text=idx)
            col.set_min_width(width)
            col.set_resizable(True)
            self.media_tree.append_column(col)
        scr = Gtk.ScrolledWindow()
        scr.add(self.media_tree)
        box.pack_start(scr, True, True, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.pack_start(actions, False, False, 0)
        open_btn = Gtk.Button(label="Открыть")
        copy_btn = Gtk.Button(label="Копировать")
        open_btn.connect("clicked", lambda *_: self.open_selected())
        copy_btn.connect("clicked", lambda *_: self.copy_selected())
        actions.pack_start(open_btn, False, False, 0)
        actions.pack_start(copy_btn, False, False, 0)
        if self.role == "admin":
            protect = Gtk.Button(label="Защита вкл/выкл")
            delete = Gtk.Button(label="Удалить")
            delete.get_style_context().add_class("danger")
            protect.connect("clicked", lambda *_: self.toggle_protect_selected())
            delete.connect("clicked", lambda *_: self.delete_selected())
            actions.pack_start(protect, False, False, 0)
            actions.pack_start(delete, False, False, 0)

    def build_slots_tab(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_border_width(18)
        self.add_tab(box, "Стаканы")
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.pack_start(actions, False, False, 0)
        refresh = Gtk.Button(label="Обновить")
        refresh.connect("clicked", lambda *_: self.refresh_slots())
        list_amba = Gtk.Button(label="Показать подключённые")
        list_amba.connect("clicked", lambda *_: self.list_amba())
        cal = Gtk.Button(label="Калибровка в терминале")
        cal.get_style_context().add_class("warn")
        cal.connect("clicked", lambda *_: self.launch_slot_calibration())
        actions.pack_start(refresh, False, False, 0)
        actions.pack_start(list_amba, False, False, 0)
        actions.pack_start(cal, False, False, 0)
        self.slots_store = Gtk.ListStore(str, str)
        tree = Gtk.TreeView(model=self.slots_store)
        for title, idx, width in [("Ячейка", 0, 160), ("USB path", 1, 300)]:
            r = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(title, r, text=idx)
            col.set_min_width(width)
            tree.append_column(col)
        scr = Gtk.ScrolledWindow()
        scr.add(tree)
        box.pack_start(scr, True, True, 0)
        self.amba_text = Gtk.TextView()
        self.amba_text.set_editable(False)
        self.amba_text.set_monospace(True)
        self.amba_text.get_style_context().add_class("textlog")
        scr2 = Gtk.ScrolledWindow()
        scr2.set_min_content_height(220)
        scr2.add(self.amba_text)
        box.pack_start(scr2, False, True, 0)

    def build_audit_tab(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_border_width(18)
        self.add_tab(box, "Журнал")
        refresh = Gtk.Button(label="Обновить журнал")
        refresh.connect("clicked", lambda *_: self.refresh_audit())
        box.pack_start(refresh, False, False, 0)
        self.audit_store = Gtk.ListStore(str, str, str, str, str, str)
        tree = Gtk.TreeView(model=self.audit_store)
        for title, idx, width in [("Время",0,170),("Роль",1,110),("Действие",2,160),("Файл",3,280),("Регистратор",4,130),("Детали",5,500)]:
            r = Gtk.CellRendererText()
            r.set_property("ellipsize", Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(title, r, text=idx)
            col.set_min_width(width)
            col.set_resizable(True)
            tree.append_column(col)
        scr = Gtk.ScrolledWindow()
        scr.add(tree)
        box.pack_start(scr, True, True, 0)

    def build_settings_tab(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_border_width(0)
        self.settings_tab = outer
        self.add_tab(outer, "Настройки")

        # Scrollable content
        scr = Gtk.ScrolledWindow()
        scr.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.pack_start(scr, True, True, 0)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_border_width(18)
        scr.add(box)

        title = Gtk.Label(label="Настройки хранения")
        title.set_xalign(0)
        title.get_style_context().add_class("title")
        box.pack_start(title, False, False, 0)

        # Папка архива фиксирована — показываем только путь
        storage_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.pack_start(storage_row, False, False, 0)
        storage_caption = Gtk.Label(label="Папка архива:")
        storage_caption.set_xalign(0)
        storage_caption.get_style_context().add_class("status")
        storage_row.pack_start(storage_caption, False, False, 0)
        self.storage_entry = Gtk.Label(label=str(resolve_relative(self.cfg.storage_root)))
        self.storage_entry.set_xalign(0)
        self.storage_entry.get_style_context().add_class("archive-path")
        storage_row.pack_start(self.storage_entry, True, True, 0)

        delay_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.pack_start(delay_row, False, False, 0)
        delay_label = Gtk.Label(label="Пауза перед переводом следующего регистратора в диск, мс")
        delay_label.set_xalign(0)
        delay_row.pack_start(delay_label, False, False, 0)
        self.delay_spin = Gtk.SpinButton()
        self.delay_spin.set_range(1000, 30000)
        self.delay_spin.set_increments(500, 1000)
        self.delay_spin.set_value(float(self.cfg.switch_to_disk_delay_ms))
        delay_row.pack_start(self.delay_spin, False, False, 0)

        parallel_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.pack_start(parallel_row, False, False, 0)
        par_label = Gtk.Label(label="Параллельных скачиваний всего")
        par_label.set_xalign(0)
        parallel_row.pack_start(par_label, False, False, 0)
        self.parallel_spin = Gtk.SpinButton()
        self.parallel_spin.set_range(1, 12)
        self.parallel_spin.set_increments(1, 1)
        self.parallel_spin.set_value(float(self.cfg.max_parallel_imports))
        parallel_row.pack_start(self.parallel_spin, False, False, 0)

        per_hub_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.pack_start(per_hub_row, False, False, 0)
        per_hub_label = Gtk.Label(label="Параллельных скачиваний на USB-ветку/хаб")
        per_hub_label.set_xalign(0)
        per_hub_row.pack_start(per_hub_label, False, False, 0)
        self.per_hub_parallel_spin = Gtk.SpinButton()
        self.per_hub_parallel_spin.set_range(1, 6)
        self.per_hub_parallel_spin.set_increments(1, 1)
        self.per_hub_parallel_spin.set_value(float(getattr(self.cfg, "max_parallel_imports_per_hub", 3)))
        per_hub_row.pack_start(self.per_hub_parallel_spin, False, False, 0)

        group_hint = Gtk.Label(label="USB-ветка определяется автоматически по topology path: например 3.* и 14.* считаются разными хабами. Лимит на хаб не перекидывается на другой хаб.")
        group_hint.set_xalign(0)
        group_hint.set_line_wrap(True)
        group_hint.get_style_context().add_class("status")
        box.pack_start(group_hint, False, False, 0)

        save = Gtk.Button(label="Сохранить настройки")
        save.get_style_context().add_class("ok")
        save.connect("clicked", lambda *_: self.save_settings())
        box.pack_start(save, False, False, 0)

        self.settings_message = Gtk.Label(label="")
        self.settings_message.set_xalign(0)
        self.settings_message.get_style_context().add_class("archive-path")
        box.pack_start(self.settings_message, False, False, 0)

        # ── Separator ──────────────────────────────────────────────────────
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        box.pack_start(sep, False, False, 4)

        # ── Storage disk slots section ─────────────────────────────────────
        self.build_storage_slots_section(box)

    def build_storage_slots_section(self, parent_box: Gtk.Box) -> None:
        """Admin section: SATA storage slot calibration and disk selection."""
        from .storage_slots import (
            StorageSlotMap, list_storage_disks, snapshot_by_path,
            wait_for_new_disk, wait_for_disk_removal
        )

        sec_title = Gtk.Label(label="Дисковые слоты архива")
        sec_title.set_xalign(0)
        sec_title.get_style_context().add_class("title")
        parent_box.pack_start(sec_title, False, False, 0)

        sec_hint = Gtk.Label(
            label="Каждый физический SATA-слот определяется по стабильному пути /dev/disk/by-path/. "
                  "После калибровки оператор видит слоты по номерам, а не /dev/sdX. "
                  "Отметьте диски галочкой чтобы включить их в ротацию архива."
        )
        sec_hint.set_xalign(0)
        sec_hint.set_line_wrap(True)
        sec_hint.get_style_context().add_class("status")
        parent_box.pack_start(sec_hint, False, False, 0)

        # ── Disk slots grid ────────────────────────────────────────────────
        # Columns: Слот | Путь by-path | Диск | Размер | Свободно | Статус | ✓ Использовать
        col_headers = ["Слот", "by-path", "Диск / серийный", "Размер", "Свободно", "Статус", "Использовать"]
        hdr_grid = Gtk.Grid()
        hdr_grid.set_column_spacing(12)
        widths = [60, 280, 260, 100, 100, 140, 160]
        for ci, (hdr, w) in enumerate(zip(col_headers, widths)):
            lbl = Gtk.Label(label=hdr)
            lbl.set_xalign(0)
            lbl.set_size_request(w, -1)
            lbl.get_style_context().add_class("slot-label")
            hdr_grid.attach(lbl, ci, 0, 1, 1)
        parent_box.pack_start(hdr_grid, False, False, 0)

        self._storage_rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        parent_box.pack_start(self._storage_rows_box, False, False, 0)

        # ── Buttons ────────────────────────────────────────────────────────
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        parent_box.pack_start(btn_row, False, False, 0)

        refresh_btn = Gtk.Button(label="🔄  Обновить состояние дисков")
        refresh_btn.connect("clicked", lambda *_: self.refresh_storage_slots())
        btn_row.pack_start(refresh_btn, False, False, 0)

        calibrate_btn = Gtk.Button(label="⚙  Калибровка слотов…")
        calibrate_btn.get_style_context().add_class("warn")
        calibrate_btn.connect("clicked", lambda *_: self.start_storage_calibration())
        btn_row.pack_start(calibrate_btn, False, False, 0)

        save_slots_btn = Gtk.Button(label="💾  Сохранить выбор дисков")
        save_slots_btn.get_style_context().add_class("ok")
        save_slots_btn.connect("clicked", lambda *_: self.save_storage_slot_selection())
        btn_row.pack_start(save_slots_btn, False, False, 0)

        self._storage_status_label = Gtk.Label(label="")
        self._storage_status_label.set_xalign(0)
        self._storage_status_label.get_style_context().add_class("archive-path")
        parent_box.pack_start(self._storage_status_label, False, False, 0)

        self._storage_slot_checks: dict[int, Gtk.CheckButton] = {}
        self.refresh_storage_slots()

    def refresh_storage_slots(self) -> None:
        """Re-read disk state and rebuild the slot rows."""
        from .storage_slots import StorageSlotMap, list_storage_disks

        slot_map = StorageSlotMap.load(getattr(self.cfg, "storage_slots_path", "StorageSlots.json"))
        disks = list_storage_disks()
        by_path_index = {d.by_path: d for d in disks}

        # clear existing rows
        for child in self._storage_rows_box.get_children():
            self._storage_rows_box.remove(child)
        self._storage_slot_checks.clear()

        widths = [60, 280, 260, 100, 100, 140, 160]

        if not slot_map.storage_slots:
            empty = Gtk.Label(label="Слоты не откалиброваны. Нажмите «Калибровка слотов…»")
            empty.set_xalign(0)
            empty.get_style_context().add_class("status")
            self._storage_rows_box.pack_start(empty, False, False, 0)
        else:
            for sm in sorted(slot_map.storage_slots, key=lambda x: x.slot):
                # find matching disk
                disk = next(
                    (d for bp, d in by_path_index.items() if sm.path_match and sm.path_match in bp),
                    None
                )

                row = Gtk.Grid()
                row.set_column_spacing(12)

                # slot number
                sl = Gtk.Label(label=f"{sm.slot:02d}")
                sl.set_xalign(0)
                sl.set_size_request(widths[0], -1)
                sl.get_style_context().add_class("slot-big")
                row.attach(sl, 0, 0, 1, 1)

                # by-path
                bp_lbl = Gtk.Label(label=sm.path_match or "—")
                bp_lbl.set_xalign(0)
                bp_lbl.set_size_request(widths[1], -1)
                bp_lbl.set_ellipsize(Pango.EllipsizeMode.START)
                bp_lbl.get_style_context().add_class("slot-small")
                row.attach(bp_lbl, 1, 0, 1, 1)

                # model / serial
                disk_lbl = Gtk.Label(label=(disk.label_short if disk else (sm.notes or "не подключён")))
                disk_lbl.set_xalign(0)
                disk_lbl.set_size_request(widths[2], -1)
                disk_lbl.set_ellipsize(Pango.EllipsizeMode.END)
                disk_lbl.get_style_context().add_class("slot-small" if disk else "slot-info")
                row.attach(disk_lbl, 2, 0, 1, 1)

                # size
                size_lbl = Gtk.Label(label=(disk.size_human if disk else "—"))
                size_lbl.set_xalign(0)
                size_lbl.set_size_request(widths[3], -1)
                size_lbl.get_style_context().add_class("slot-small")
                row.attach(size_lbl, 3, 0, 1, 1)

                # free
                free_lbl = Gtk.Label(label=(disk.free_human if (disk and disk.mountpoint) else "—"))
                free_lbl.set_xalign(0)
                free_lbl.set_size_request(widths[4], -1)
                free_lbl.get_style_context().add_class("slot-small")
                row.attach(free_lbl, 4, 0, 1, 1)

                # status
                if disk is None:
                    status_txt, status_cls = "не подключён", "slot-info"
                elif disk.mountpoint:
                    status_txt, status_cls = f"смонтирован  {disk.mountpoint}", "slot-state"
                else:
                    status_txt, status_cls = "подключён, не смонтирован", "slot-small"
                status_lbl = Gtk.Label(label=status_txt)
                status_lbl.set_xalign(0)
                status_lbl.set_size_request(widths[5], -1)
                status_lbl.set_ellipsize(Pango.EllipsizeMode.END)
                status_lbl.get_style_context().add_class(status_cls)
                row.attach(status_lbl, 5, 0, 1, 1)

                # checkbox
                chk = Gtk.CheckButton(label="")
                chk.set_active(sm.enabled)
                chk.set_size_request(widths[6], -1)
                row.attach(chk, 6, 0, 1, 1)
                self._storage_slot_checks[sm.slot] = chk

                self._storage_rows_box.pack_start(row, False, False, 0)

        self._storage_rows_box.show_all()

    def save_storage_slot_selection(self) -> None:
        """Save enabled/disabled state of storage slots."""
        from .storage_slots import StorageSlotMap

        path = getattr(self.cfg, "storage_slots_path", "StorageSlots.json")
        slot_map = StorageSlotMap.load(path)
        for sm in slot_map.storage_slots:
            chk = self._storage_slot_checks.get(sm.slot)
            if chk is not None:
                sm.enabled = chk.get_active()
        slot_map.save(path)
        self._storage_status_label.set_text("Выбор дисков сохранён.")
        self.db.add_audit(self.role, "storage_slots_saved",
                          details=f"enabled={[s.slot for s in slot_map.enabled_slots()]}")

    def start_storage_calibration(self) -> None:
        """Open the step-by-step SATA slot calibration dialog."""
        count = self._ask_slot_count()
        if count is None:
            return
        dlg = StorageCalibrationDialog(self, self.cfg, count)
        dlg.run()
        dlg.destroy()
        self.refresh_storage_slots()

    def _ask_slot_count(self) -> Optional[int]:
        dlg = Gtk.Dialog(title="Количество слотов", transient_for=self, modal=True)
        dlg.set_default_size(420, 260)
        content = dlg.get_content_area()
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        vbox.set_border_width(18)
        content.add(vbox)
        lbl = Gtk.Label(label="Сколько физических SATA-слотов калибровать?")
        lbl.set_line_wrap(True)
        lbl.get_style_context().add_class("status")
        vbox.pack_start(lbl, False, False, 0)
        spin = Gtk.SpinButton()
        spin.set_range(1, 16)
        spin.set_increments(1, 1)
        spin.set_value(4)
        vbox.pack_start(spin, False, False, 0)
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        vbox.pack_start(btn_row, False, False, 0)
        cancel = Gtk.Button(label="Отмена")
        cancel.get_style_context().add_class("danger")
        cancel.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.CANCEL))
        ok = Gtk.Button(label="Далее")
        ok.get_style_context().add_class("ok")
        ok.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.OK))
        btn_row.pack_start(cancel, True, True, 0)
        btn_row.pack_start(ok, True, True, 0)
        dlg.show_all()
        resp = dlg.run()
        count = int(spin.get_value()) if resp == Gtk.ResponseType.OK else None
        dlg.destroy()
        return count

    def save_settings(self) -> None:
        try:
            # storage_root фиксирован — не меняем
            self.cfg.switch_to_disk_delay_ms = int(self.delay_spin.get_value())
            self.cfg.max_parallel_imports = int(self.parallel_spin.get_value())
            self.cfg.max_parallel_imports_per_hub = int(self.per_hub_parallel_spin.get_value())
            self.cfg.save(self.config_path)
            self.db.add_audit(self.role, "settings_changed",
                details=f"delay={self.cfg.switch_to_disk_delay_ms}; parallel={self.cfg.max_parallel_imports}; per_hub={self.cfg.max_parallel_imports_per_hub}")
            self.settings_message.set_text("Настройки сохранены.")
        except Exception as exc:
            self.message(f"Ошибка сохранения настроек: {exc}")

    def build_password_tab(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_border_width(18)
        self.add_tab(box, "Пароль")
        info = Gtk.Label(label="Пароль меняется только для текущей роли. Админ не может сменить пароль оператора.")
        info.set_xalign(0)
        box.pack_start(info, False, False, 0)
        btn = Gtk.Button(label="Сменить мой пароль")
        btn.get_style_context().add_class("warn")
        btn.connect("clicked", lambda *_: self.change_password())
        box.pack_start(btn, False, False, 0)

    def refresh_status(self) -> bool:
        try:
            st = self.db.status_dict()

            # Пересчитываем счётчики из текущих состояний слотов —
            # накопленные значения runner'а (ready/errors) не обнуляются
            # при извлечении устройств, поэтому считаем сами.
            sm = SlotMap.load(self.cfg.dock_slots_path)
            counts = {
                "total": 0, "queue": 0, "connected": 0,
                "importing": 0, "ready": 0, "errors": 0,
            }
            for row in sm.rows():
                state = st.get(f"slot_{row.slot}_state", "пусто").strip()
                if not state or state == "пусто":
                    continue
                counts["total"] += 1
                s = state.lower()
                if "ошиб" in s:
                    counts["errors"]   += 1
                elif "готов" in s:
                    counts["ready"]    += 1
                elif "скачив" in s and "ожидает" not in s:
                    counts["importing"] += 1
                elif "ожидает" in s or "пауза" in s:
                    counts["queue"]    += 1
                elif "подключ" in s:
                    counts["connected"] += 1
                # обнаружен / считывание / переключение — тоже "всего", но без отдельного счётчика

            for key, label in self.metrics.items():
                label.set_text(str(counts.get(key, 0)))

            if hasattr(self, "archive_path_label"):
                self.archive_path_label.set_text(str(resolve_relative(self.cfg.storage_root)))
        except Exception as exc:
            self.logger.log(f"UI status error: {exc}")
        return True

    def refresh_log(self) -> bool:  # called only when log_view is set
        if not self.log_view:
            return False
        try:
            p = Path("logs/docstation_linux.log")
            if p.exists():
                text = p.read_text(encoding="utf-8", errors="replace")[-12000:]
                buf = self.log_view.get_buffer()
                buf.set_text(text)
                mark = buf.get_insert()
                self.log_view.scroll_to_mark(mark, 0.0, True, 0, 1)
        except Exception:
            pass
        return True

    def start_continuous_service(self) -> None:
        if self.runner_active:
            return
        self.runner_active = True
        self.db.add_audit(self.role, "auto_import_service_started", details="Native app continuous mode")
        self.runner_instance = DocStationRunner(self.cfg, self.db, self.logger)

        def worker():
            try:
                assert self.runner_instance is not None
                self.runner_instance.run_continuous()
            except Exception as exc:
                msg = str(exc)
                self.logger.log(f"NATIVE SERVICE ERROR: {msg}")
                GLib.idle_add(lambda m=msg: self.message(f"Ошибка службы: {m}") or False)
            finally:
                self.runner_active = False
                GLib.idle_add(self.refresh_archive)
                if self.role == "admin":
                    GLib.idle_add(self.refresh_audit)

        self.runner_thread = threading.Thread(target=worker, daemon=True)
        self.runner_thread.start()

    def start_import(self, dry_run: bool = False) -> None:
        if self.runner_active:
            self.message("Импорт уже запущен")
            return
        self.runner_active = True
        self.db.add_audit(self.role, "import_started" if not dry_run else "dry_run_started", details="Started from native app")

        def worker():
            try:
                DocStationRunner(self.cfg, self.db, self.logger).run_once(dry_run=dry_run)
            except Exception as exc:
                msg = str(exc)
                self.logger.log(f"NATIVE IMPORT ERROR: {msg}")
                GLib.idle_add(lambda m=msg: self.message(f"Ошибка импорта: {m}") or False)
            finally:
                self.runner_active = False
                GLib.idle_add(self.refresh_archive)
                if self.role == "admin":
                    GLib.idle_add(self.refresh_audit)

        self.runner_thread = threading.Thread(target=worker, daemon=True)
        self.runner_thread.start()

    def selected_media_row(self):
        sel = self.media_tree.get_selection()
        model, it = sel.get_selected()
        if it is None:
            self.message("Выбери файл в архиве")
            return None
        media_id = int(model[it][0])
        for row in self.media_rows:
            if int(row["id"]) == media_id:
                return row
        return None

    def refresh_archive(self) -> bool:
        try:
            q = self.search.get_text() if hasattr(self, "search") else ""
            self.media_rows = self.db.media_search(q, limit=500)
            self.media_store.clear()
            for r in self.media_rows:
                self.media_store.append([
                    int(r["id"]), str(r["imported_at"]), str(r["file_name"]), str(r["device_id"] or ""),
                    str(r["officer_id"] or ""), human_bytes(int(r["size_bytes"] or 0)), "🔒" if int(r["is_protected"] or 0) else "", str(r["archive_path"]),
                ])
        except Exception as exc:
            self.logger.log(f"UI archive error: {exc}")
        return True

    def open_selected(self) -> None:
        row = self.selected_media_row()
        if row is None:
            return
        path = Path(row["archive_path"])
        if not path.exists():
            self.message("Файл не найден на диске")
            return
        self.db.add_audit(self.role, "viewed", file_name=row["file_name"], archive_path=row["archive_path"], device_id=row["device_id"], officer_id=row["officer_id"])
        cmd = ["vlc", str(path)] if shutil.which("vlc") else ["xdg-open", str(path)]
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.message(f"Не удалось открыть файл: {exc}")

    def copy_selected(self) -> None:
        row = self.selected_media_row()
        if row is None:
            return
        src = Path(row["archive_path"])
        if not src.exists():
            self.message("Файл не найден на диске")
            return
        dlg = Gtk.FileChooserDialog(title="Куда скопировать файл", transient_for=self, action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Копировать", Gtk.ResponseType.OK)
        resp = dlg.run()
        folder = dlg.get_filename() if resp == Gtk.ResponseType.OK else None
        dlg.destroy()
        if not folder:
            return
        dest = Path(folder) / src.name
        try:
            if dest.exists():
                dest = self.unique_copy_path(dest)
            shutil.copy2(src, dest)
            self.db.add_audit(self.role, "copied", file_name=row["file_name"], archive_path=row["archive_path"], details=f"Copied to {dest}")
            self.message(f"Скопировано: {dest}")
        except Exception as exc:
            self.message(f"Ошибка копирования: {exc}")

    def unique_copy_path(self, path: Path) -> Path:
        stem, suffix = path.stem, path.suffix
        for i in range(2, 10000):
            c = path.with_name(f"{stem}_{i}{suffix}")
            if not c.exists():
                return c
        return path

    def toggle_protect_selected(self) -> None:
        row = self.selected_media_row()
        if row is None:
            return
        new_val = not bool(int(row["is_protected"] or 0))
        try:
            self.db.protect_media(int(row["id"]), new_val, self.role)
            self.refresh_archive()
        except Exception as exc:
            self.message(f"Ошибка защиты: {exc}")

    def delete_selected(self) -> None:
        row = self.selected_media_row()
        if row is None:
            return
        if not self.confirm(f"Удалить файл?\n{row['file_name']}"):
            return
        try:
            self.db.delete_media(int(row["id"]), self.role)
            self.refresh_archive()
        except Exception as exc:
            self.message(f"Ошибка удаления: {exc}")

    def refresh_slots(self) -> None:
        self.slots_store.clear()
        sm = SlotMap.load(self.cfg.dock_slots_path)
        for s in sm.rows():
            self.slots_store.append([s.label, s.usb_path])
        if hasattr(self, "slot_grid"):
            self.build_slot_cards()
            self.refresh_slot_cards()

    def list_amba(self) -> None:
        try:
            sm = SlotMap.load(self.cfg.dock_slots_path)
            cands = sorted(amba.list_ambarella(self.cfg.vendor_vid_int, self.cfg.vendor_pid_int), key=sm.sort_key_for_candidate)
            lines = [f"Обнаружено регистраторов в Ambarella-режиме: {len(cands)}"]
            lines.extend(f"[{i:02d}] {format_candidate(c, sm)}" for i, c in enumerate(cands, 1))
            self.amba_text.get_buffer().set_text("\n".join(lines))
        except Exception as exc:
            self.amba_text.get_buffer().set_text(f"Ошибка USB: {exc}")

    def launch_slot_calibration(self) -> None:
        script = Path("scripts/calibrate_slots.sh").resolve()
        if not script.exists():
            self.message("Скрипт калибровки не найден")
            return
        cmd = f"cd {Path.cwd()} && ./scripts/calibrate_slots.sh 20; echo; read -p 'Калибровка завершена. Нажмите Enter для закрытия...'"
        terminals = [["gnome-terminal", "--", "bash", "-lc", cmd], ["x-terminal-emulator", "-e", "bash", "-lc", cmd]]
        for tcmd in terminals:
            if shutil.which(tcmd[0]):
                subprocess.Popen(tcmd)
                return
        self.message("Не найден терминал для калибровки")

    def refresh_audit(self) -> None:
        if not hasattr(self, "audit_store"):
            return
        self.audit_store.clear()
        rows = self.db.audit_search(limit=700)
        for r in rows:
            self.audit_store.append([str(r["created_at"]), str(r["role"]), str(r["action"]), str(r["file_name"] or ""), str(r["device_id"] or ""), str(r["details"] or r["archive_path"] or "")])

    def change_password(self) -> None:
        dlg = TouchPasswordDialog(self, "Смена пароля", ["Старый пароль", "Новый пароль", "Повтор"])
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            old = dlg.value("Старый пароль")
            new = dlg.value("Новый пароль")
            repeat = dlg.value("Повтор")
            dlg.destroy()
            if new != repeat:
                self.message("Новый пароль и повтор не совпадают")
                return
            try:
                self.db.change_password(self.role, old, new)
                self.message("Пароль изменён")
            except Exception as exc:
                self.message(f"Ошибка смены пароля: {exc}")
        else:
            dlg.destroy()

    def message(self, text: str) -> None:
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.OK, text=text)
        dlg.set_default_size(520, 220)
        dlg.run()
        dlg.destroy()

    def confirm(self, text: str) -> bool:
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK_CANCEL, text=text)
        dlg.set_default_size(600, 260)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK



class StorageCalibrationDialog(Gtk.Dialog):
    """Step-by-step GTK dialog for calibrating physical SATA storage slots."""

    def __init__(self, parent: Gtk.Window, cfg, count: int):
        super().__init__(title="Калибровка дисковых слотов", transient_for=parent, modal=True)
        self.set_default_size(640, 500)
        self.cfg = cfg
        self.count = count
        self.current_slot = 1
        self.new_slots: list = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        content = self.get_content_area()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_border_width(24)
        content.add(box)

        # Title
        t = Gtk.Label(label=f"Калибровка {count} дисковых слотов")
        t.set_xalign(0)
        t.get_style_context().add_class("title")
        box.pack_start(t, False, False, 0)

        # Instruction label
        self.instr = Gtk.Label(label="")
        self.instr.set_xalign(0)
        self.instr.set_line_wrap(True)
        self.instr.get_style_context().add_class("status")
        box.pack_start(self.instr, False, False, 0)

        # Detected disk info
        self.disk_info = Gtk.Label(label="")
        self.disk_info.set_xalign(0)
        self.disk_info.get_style_context().add_class("archive-path")
        box.pack_start(self.disk_info, False, False, 0)

        # Progress bar (waiting indicator)
        self.progress = Gtk.ProgressBar()
        self.progress.set_pulse_step(0.08)
        box.pack_start(self.progress, False, False, 0)

        # Action buttons
        self.btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.pack_start(self.btn_row, False, False, 0)

        self.btn_start = Gtk.Button(label=f"Вставил диск в слот 1 → ждать обнаружения")
        self.btn_start.get_style_context().add_class("ok")
        self.btn_start.connect("clicked", self._on_start_slot)
        self.btn_row.pack_start(self.btn_start, True, True, 0)

        self.btn_skip = Gtk.Button(label="Пропустить слот")
        self.btn_skip.get_style_context().add_class("warn")
        self.btn_skip.connect("clicked", self._on_skip)
        self.btn_row.pack_start(self.btn_skip, False, False, 0)

        self.btn_close = Gtk.Button(label="Готово / Закрыть")
        self.btn_close.get_style_context().add_class("danger")
        self.btn_close.connect("clicked", lambda *_: self.response(Gtk.ResponseType.OK))
        self.btn_close.set_sensitive(False)
        self.btn_row.pack_start(self.btn_close, False, False, 0)

        self._set_slot_instruction(1)
        self.show_all()

    def _set_slot_instruction(self, slot: int) -> None:
        self.instr.set_text(
            f"Шаг {slot} из {self.count}: вставьте диск в физический слот {slot}, "
            f"затем нажмите кнопку ниже. Программа будет ждать появления нового диска."
        )
        self.btn_start.set_label(f"Вставил диск в слот {slot} → жду обнаружения")
        self.btn_start.set_sensitive(True)
        self.disk_info.set_text("")

    def _on_start_slot(self, _btn: Gtk.Button) -> None:
        self.btn_start.set_sensitive(False)
        self.btn_skip.set_sensitive(False)
        self.disk_info.set_text("Ожидание нового диска…")
        self._stop.clear()
        self._pulse_id = GLib.timeout_add(120, self._pulse_progress)
        self._thread = threading.Thread(target=self._detect_slot, daemon=True)
        self._thread.start()

    def _pulse_progress(self) -> bool:
        self.progress.pulse()
        return True  # keep pulsing until we stop it

    def _detect_slot(self) -> None:
        from .storage_slots import snapshot_by_path, wait_for_new_disk
        before = snapshot_by_path()
        disk = wait_for_new_disk(before, timeout_sec=90)
        GLib.idle_add(self._on_disk_detected, disk)

    def _on_disk_detected(self, disk) -> None:
        GLib.source_remove(self._pulse_id)
        self.progress.set_fraction(0)

        if disk is None:
            self.disk_info.set_text("Диск не обнаружен за 90 сек. Попробуйте ещё раз или пропустите слот.")
            self.btn_start.set_sensitive(True)
            self.btn_skip.set_sensitive(True)
            return

        from .storage_slots import StorageSlotMapping
        info_text = (
            f"✓  {disk.dev}  |  {disk.by_path}\n"
            f"   Модель: {disk.model or '—'}   Серийный: {disk.serial or '—'}   Размер: {disk.size_human}"
        )
        self.disk_info.set_text(info_text)

        slot_path = getattr(self.cfg, "storage_slots_path", "StorageSlots.json")
        from .storage_slots import StorageSlotMap
        slot_map = StorageSlotMap.load(slot_path)
        existing = {s.slot: s for s in slot_map.storage_slots}

        label = f"Диск {self.current_slot}"
        new_sm = StorageSlotMapping(
            slot=self.current_slot,
            label=label,
            path_match=disk.by_path,
            enabled=existing.get(self.current_slot, StorageSlotMapping(self.current_slot, label, "")).enabled,
            notes=f"{disk.model} {disk.serial} {disk.size_human}".strip(),
        )
        self.new_slots.append(new_sm)

        # Save incrementally so partial calibration is not lost
        self._save_progress()

        self.current_slot += 1
        if self.current_slot > self.count:
            self._finish()
        else:
            # Ask to remove disk, then move to next slot
            self.instr.set_text(
                f"Слот {self.current_slot - 1} сохранён. "
                f"Извлеките диск, затем перейдите к слоту {self.current_slot}."
            )
            self.btn_start.set_label(f"Извлёк диск → вставляю диск в слот {self.current_slot}")
            self.btn_start.set_sensitive(True)
            self.btn_skip.set_sensitive(True)
            self._set_slot_instruction(self.current_slot)

    def _on_skip(self, _btn: Gtk.Button) -> None:
        self.current_slot += 1
        if self.current_slot > self.count:
            self._finish()
        else:
            self._set_slot_instruction(self.current_slot)
            self.btn_skip.set_sensitive(True)

    def _save_progress(self) -> None:
        from .storage_slots import StorageSlotMap
        slot_path = getattr(self.cfg, "storage_slots_path", "StorageSlots.json")
        old = StorageSlotMap.load(slot_path)
        by_slot = {s.slot: s for s in old.storage_slots}
        for ns in self.new_slots:
            by_slot[ns.slot] = ns
        merged = StorageSlotMap(storage_slots=sorted(by_slot.values(), key=lambda x: x.slot))
        merged.save(slot_path)

    def _finish(self) -> None:
        self._save_progress()
        self.instr.set_text(f"Калибровка завершена. Сохранено {len(self.new_slots)} слотов.")
        self.disk_info.set_text("")
        self.btn_start.set_sensitive(False)
        self.btn_skip.set_sensitive(False)
        self.btn_close.set_sensitive(True)
        self.progress.set_fraction(1.0)


def run_app(config_path: str = "StationConfig.linux.json") -> int:
    cfg = StationConfig.load(config_path)
    if not Path(config_path).exists():
        cfg.save(config_path)
    db = StationDatabase(cfg.database_path)
    db.ensure_default_passwords()
    logger = Logger("logs/docstation_linux.log")
    for p in [cfg.storage_root, Path(cfg.database_path).parent, "logs", cfg.mount_root]:
        try:
            Path(p).mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass

    try:
        # App starts immediately with the slot grid — no login screen.
        # Protected tabs (archive, settings, audit) are hidden until the
        # operator taps the lock button in the topbar and enters a password.
        win = MainWindow(cfg, db, logger, role="none", config_path=config_path)
        Gtk.main()
    except Exception as exc:
        try:
            Path("logs/native_startup_error.log").write_text(str(exc), encoding="utf-8")
            logger.log(f"NATIVE STARTUP ERROR: {exc}")
        except Exception:
            pass
        raise
    try:
        logger.close()
        db.close()
    except Exception:
        pass
    return 0
