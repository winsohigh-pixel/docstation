from __future__ import annotations

import html
import mimetypes
import secrets
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional

from .config import StationConfig
from .db import StationDatabase
from .loggingx import Logger
from .runner import DocStationRunner
from .slots import SlotMap

SESSIONS: Dict[str, str] = {}
RUN_LOCK = threading.Lock()
RUN_THREAD: Optional[threading.Thread] = None


def esc(x) -> str:
    return html.escape(str(x or ""))


CSS = """
:root{color-scheme:dark;--bg:#111827;--panel:#172033;--panel2:#1f2a44;--txt:#eef2ff;--muted:#aab4cf;--accent:#4fd1c5;--danger:#ef4444;--ok:#22c55e;--warn:#f59e0b}
*{box-sizing:border-box} body{margin:0;background:linear-gradient(135deg,#0b1020,#151a2b);color:var(--txt);font-family:Arial,Helvetica,sans-serif;font-size:20px} a{color:var(--accent);text-decoration:none}
.header{display:flex;gap:16px;align-items:center;justify-content:space-between;padding:18px 24px;background:#0b1222;border-bottom:1px solid #31405f;position:sticky;top:0;z-index:3}.brand{font-size:30px;font-weight:800}.status{font-size:20px;color:#c8d4ef}.nav{display:flex;gap:10px;flex-wrap:wrap}.btn,button,input,select{font-size:22px;border-radius:16px;border:1px solid #3d4d70;background:#263653;color:var(--txt);padding:14px 20px;min-height:56px}.btn:hover,button:hover{background:#33466a}.danger{background:#5c1e29;border-color:#a33}.ok{background:#164d32}.warn{background:#5a4214}.wrap{padding:22px}.panel{background:rgba(23,32,51,.96);border:1px solid #31405f;border-radius:24px;padding:20px;margin-bottom:18px;box-shadow:0 12px 28px rgba(0,0,0,.25)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}.metric{background:var(--panel2);border-radius:20px;padding:18px}.metric b{font-size:36px;display:block}.muted{color:var(--muted)}
table{width:100%;border-collapse:separate;border-spacing:0 10px} th{text-align:left;color:var(--muted);font-weight:600;padding:6px 12px} td{background:#202b45;padding:14px 12px;vertical-align:middle} tr td:first-child{border-radius:16px 0 0 16px} tr td:last-child{border-radius:0 16px 16px 0}.search{display:flex;gap:12px}.search input{flex:1}.role{font-weight:700;color:#fff;background:#243b63;border-radius:999px;padding:8px 14px}.video{width:100%;max-height:72vh;background:#000;border-radius:20px;border:1px solid #3e4f74}.keypad{display:grid;grid-template-columns:repeat(3,110px);gap:12px;justify-content:center;margin-top:20px}.keypad button{font-size:34px;min-height:80px}.login{max-width:560px;margin:8vh auto}.small{font-size:16px}.chips{display:flex;gap:10px;flex-wrap:wrap}.chip{background:#24324e;border:1px solid #3d4d70;border-radius:999px;padding:8px 14px}.protected{color:#fbbf24;font-weight:800}
"""


class WebAppHandler(BaseHTTPRequestHandler):
    cfg: StationConfig
    db: StationDatabase
    logger: Logger

    def log_message(self, fmt, *args):
        self.logger.log("WEB " + fmt % args)

    def role(self) -> Optional[str]:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            if "=" not in part:
                continue
            k, v = part.strip().split("=", 1)
            if k == "dsid":
                return SESSIONS.get(v)
        return None

    def require_role(self) -> Optional[str]:
        role = self.role()
        if not role:
            self.redirect("/login")
            return None
        return role

    def send_html(self, body: str, title: str = "DocStation") -> None:
        role = self.role()
        status = self.db.status_dict().get("header", "Регистраторы: всего 0 · очередь подключения 0 · подключены 0 · скачивание 0 · готовы 0")
        nav = "" if not role else f"""
        <div class='nav'>
          <a class='btn' href='/'>Главная</a><a class='btn' href='/archive'>Архив</a>
          {"<a class='btn' href='/audit'>Журнал</a>" if role == 'admin' else ""}
          <a class='btn' href='/change-password'>Пароль</a><a class='btn danger' href='/logout'>Выход</a>
          <span class='role'>{'Админ' if role == 'admin' else 'Оператор'}</span>
        </div>"""
        page = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
        <title>{esc(title)}</title><style>{CSS}</style></head><body>
        <div class='header'><div><div class='brand'>DocStation Linux</div><div class='status'>{esc(status)}</div></div>{nav}</div>
        <div class='wrap'>{body}</div></body></html>"""
        data = page.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, loc: str) -> None:
        self.send_response(302)
        self.send_header("Location", loc)
        self.end_headers()

    def parse_post(self) -> Dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="ignore")
        return {k: v[0] if v else "" for k, v in urllib.parse.parse_qs(raw).items()}

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/login":
            return self.login_page()
        if path == "/logout":
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    if k == "dsid":
                        SESSIONS.pop(v, None)
            self.send_response(302); self.send_header("Set-Cookie", "dsid=; Max-Age=0; Path=/"); self.send_header("Location", "/login"); self.end_headers(); return
        role = self.require_role()
        if not role:
            return
        if path == "/":
            return self.index()
        if path == "/archive":
            params = urllib.parse.parse_qs(query)
            return self.archive(params.get("q", [""])[0])
        if path == "/audit" and role == "admin":
            return self.audit()
        if path == "/view":
            return self.view_file(query, role)
        if path == "/download":
            return self.download_file(query, role)
        if path == "/change-password":
            return self.change_password_page()
        self.send_error(404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/login":
            return self.login_post()
        role = self.require_role()
        if not role:
            return
        if path == "/start-import":
            return self.start_import(role)
        if path == "/protect" and role == "admin":
            data = self.parse_post(); self.db.protect_media(int(data.get("id", "0")), data.get("value") == "1", role); return self.redirect("/archive")
        if path == "/delete" and role == "admin":
            data = self.parse_post(); self.db.delete_media(int(data.get("id", "0")), role); return self.redirect("/archive")
        if path == "/change-password":
            return self.change_password_post(role)
        self.send_error(404)

    def login_page(self, error: str = ""):
        body = f"""<div class='panel login'><h1>Вход в архив</h1>{f'<p class="danger btn">{esc(error)}</p>' if error else ''}
        <form method='post' id='form'><p><select name='role'><option value='operator'>Оператор</option><option value='admin'>Администратор</option></select></p>
        <p><input id='pwd' name='password' type='password' placeholder='Пароль' autocomplete='off'></p><div class='keypad'>{self.keypad_js('pwd')}</div>
        <p><button class='ok' type='submit'>Войти</button></p><p class='muted'>По умолчанию: оператор 111, админ 888</p></form></div>"""
        self.send_html(body, "Вход")

    def keypad_js(self, target_id: str) -> str:
        keys = ["1","2","3","4","5","6","7","8","9","C","0","←"]
        html_keys = "".join(f"<button type='button' onclick=\"kp('{target_id}','{k}')\">{k}</button>" for k in keys)
        return html_keys + "<script>function kp(id,k){let e=document.getElementById(id); if(k==='C')e.value=''; else if(k==='←')e.value=e.value.slice(0,-1); else e.value+=k; e.focus();}</script>"

    def login_post(self):
        data = self.parse_post()
        role = data.get("role", "operator")
        if role not in ("operator", "admin"):
            role = "operator"
        if self.db.check_password(role, data.get("password", "")):
            sid = secrets.token_urlsafe(32)
            SESSIONS[sid] = role
            self.db.add_audit(role, "login", details="Archive login")
            self.send_response(302); self.send_header("Set-Cookie", f"dsid={sid}; HttpOnly; Path=/"); self.send_header("Location", "/"); self.end_headers(); return
        return self.login_page("Неверный пароль")

    def index(self):
        st = self.db.status_dict()
        body = f"""<div class='grid'>
        <div class='metric'><span>Всего</span><b>{esc(st.get('total','0'))}</b></div><div class='metric'><span>Очередь подключения</span><b>{esc(st.get('queue','0'))}</b></div>
        <div class='metric'><span>Подключены</span><b>{esc(st.get('connected','0'))}</b></div><div class='metric'><span>Скачивание</span><b>{esc(st.get('importing','0'))}</b></div>
        <div class='metric'><span>Готовы</span><b>{esc(st.get('ready','0'))}</b></div><div class='metric'><span>Ошибки</span><b>{esc(st.get('errors','0'))}</b></div></div>
        <div class='panel'><h2>Импорт</h2><form method='post' action='/start-import'><button class='ok'>Запустить цикл: обнаружить → очередь → диск → скачать</button></form>
        <p class='muted'>Перевод в диск идёт последовательно с задержкой {self.cfg.switch_to_disk_delay_ms//1000} сек. Копирование: {self.cfg.max_parallel_imports} всего, до {self.cfg.max_parallel_imports_per_hub} на USB-ветку.</p></div>{self.slots_panel()}"""
        self.send_html(body)


    def slots_panel(self) -> str:
        sm = SlotMap.load(self.cfg.dock_slots_path)
        rows = sm.rows()
        if not rows:
            return "<div class='panel'><h2>Стаканы</h2><p class='muted'>Карта стаканов не откалибрована. Запусти в терминале: <b>./scripts/calibrate_slots.sh</b></p></div>"
        trs = "".join(f"<tr><td>{r.slot:02d}</td><td>{esc(r.label)}</td><td><code>{esc(r.usb_path)}</code></td></tr>" for r in rows)
        return f"<div class='panel'><h2>Стаканы</h2><p class='muted'>Привязка по физическому USB topology path.</p><table><tr><th>№</th><th>Ячейка</th><th>USB path</th></tr>{trs}</table></div>"

    def start_import(self, role: str):
        global RUN_THREAD
        if not RUN_LOCK.acquire(blocking=False):
            return self.send_html("<div class='panel'><h2>Импорт уже выполняется</h2><a class='btn' href='/'>Назад</a></div>")
        def worker():
            try:
                runner = DocStationRunner(self.cfg, self.db, self.logger)
                runner.run_once(dry_run=False)
            finally:
                RUN_LOCK.release()
        RUN_THREAD = threading.Thread(target=worker, daemon=True)
        RUN_THREAD.start()
        self.db.add_audit(role, "import_started", details="Import started from web UI")
        self.redirect("/")

    def archive(self, q: str):
        rows = self.db.media_search(q, 500)
        role = self.role()
        trs = []
        for r in rows:
            prot = " 🔒" if r["is_protected"] else ""
            admin_actions = ""
            if role == "admin":
                admin_actions = f"""<form method='post' action='/protect' style='display:inline'><input type='hidden' name='id' value='{r['id']}'><input type='hidden' name='value' value='{'0' if r['is_protected'] else '1'}'><button>{'Снять защиту' if r['is_protected'] else 'Защитить'}</button></form>
                <form method='post' action='/delete' style='display:inline' onsubmit="return confirm('Удалить файл?')"><input type='hidden' name='id' value='{r['id']}'><button class='danger'>Удалить</button></form>"""
            trs.append(f"<tr><td>{esc(r['imported_at'])}</td><td><b>{esc(r['file_name'])}</b><span class='protected'>{prot}</span><br><span class='muted'>{esc(r['device_id'])} / {esc(r['officer_id'])}</span></td><td>{int(r['size_bytes'] or 0)//1024//1024} MiB</td><td><a class='btn' href='/view?id={r['id']}'>Смотреть</a> <a class='btn' href='/download?id={r['id']}'>Скачать</a> {admin_actions}</td></tr>")
        body = f"""<div class='panel'><form class='search' method='get'><input name='q' value='{esc(q)}' placeholder='Поиск по имени, регистратору, сотруднику'><button>Искать</button></form></div>
        <div class='panel'><table><tr><th>Дата</th><th>Файл</th><th>Размер</th><th>Действия</th></tr>{''.join(trs)}</table></div>"""
        self.send_html(body, "Архив")

    def audit(self):
        rows = self.db.audit_search(700)
        trs = "".join(f"<tr><td>{esc(r['created_at'])}</td><td>{esc(r['role'])}</td><td>{esc(r['action'])}</td><td>{esc(r['file_name'])}<br><span class='muted'>{esc(r['details'])}</span></td></tr>" for r in rows)
        self.send_html(f"<div class='panel'><h2>Журнал действий</h2><table><tr><th>Время</th><th>Роль</th><th>Действие</th><th>Файл/детали</th></tr>{trs}</table></div>", "Журнал")

    def row_by_id(self, id_str: str):
        return self.db.conn.execute("SELECT * FROM media_records WHERE id=? AND deleted_at IS NULL", (int(id_str or "0"),)).fetchone()

    def view_file(self, query: str, role: str):
        params = urllib.parse.parse_qs(query)
        row = self.row_by_id(params.get("id", ["0"])[0])
        if not row:
            return self.send_error(404)
        path = Path(row["archive_path"])
        self.db.add_audit(role, "viewed", file_name=row["file_name"], archive_path=row["archive_path"])
        body = f"""<div class='panel'><h2>{esc(row['file_name'])}</h2><div class='chips'><a class='btn' href='/archive'>← Архив</a><a class='btn' href='/download?id={row['id']}'>Скачать</a></div>
        <video id='v' class='video' controls src='/download?id={row['id']}&inline=1'></video>
        <div class='chips' style='margin-top:12px'><button onclick='rate(.5)'>0.5x</button><button onclick='rate(1)'>1x</button><button onclick='rate(2)'>2x</button><button onclick='rate(5)'>5x</button><button onclick='rate(10)'>10x</button><button onclick='shot()'>JPEG</button></div>
        <canvas id='c' style='display:none'></canvas><script>
        let v=document.getElementById('v'); function rate(x){{v.playbackRate=x;}}
        v.addEventListener('wheel',e=>{{ if(v.paused){{ e.preventDefault(); v.currentTime += (e.deltaY<0?1:-1)*0.04; }} }},{{passive:false}});
        function shot(){{let c=document.getElementById('c'); c.width=v.videoWidth; c.height=v.videoHeight; c.getContext('2d').drawImage(v,0,0); let a=document.createElement('a'); a.download='{esc(path.stem)}_'+Math.round(v.currentTime*1000)+'.jpg'; a.href=c.toDataURL('image/jpeg',0.92); a.click();}}
        </script></div>"""
        self.send_html(body, "Просмотр")

    def download_file(self, query: str, role: str):
        params = urllib.parse.parse_qs(query)
        row = self.row_by_id(params.get("id", ["0"])[0])
        if not row:
            return self.send_error(404)
        p = Path(row["archive_path"])
        if not p.exists():
            return self.send_error(404)
        inline = params.get("inline", ["0"])[0] == "1"
        self.db.add_audit(role, "copied", file_name=row["file_name"], archive_path=row["archive_path"], details="Downloaded/opened from archive UI")
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(p.stat().st_size))
        self.send_header("Content-Disposition", ("inline" if inline else "attachment") + f"; filename*=UTF-8''{urllib.parse.quote(p.name)}")
        self.end_headers()
        with p.open("rb") as f:
            while True:
                b = f.read(1024 * 1024)
                if not b:
                    break
                self.wfile.write(b)

    def change_password_page(self, error: str = ""):
        body = f"""<div class='panel login'><h1>Смена пароля</h1>{f'<p class="danger btn">{esc(error)}</p>' if error else ''}
        <form method='post'><input id='old' name='old' type='password' placeholder='Старый пароль'><input id='new' name='new' type='password' placeholder='Новый пароль'><input id='new2' name='new2' type='password' placeholder='Повтор'>
        <div class='keypad'>{self.keypad_js('new')}</div><p><button class='ok'>Сменить</button></p><p class='muted'>Клавиатура вводит в поле Новый пароль. Старый и повтор можно заполнить экранной клавиатурой ОС или физической.</p></form></div>"""
        self.send_html(body, "Пароль")

    def change_password_post(self, role: str):
        data = self.parse_post()
        if data.get("new") != data.get("new2"):
            return self.change_password_page("Повтор не совпадает")
        try:
            self.db.change_password(role, data.get("old", ""), data.get("new", ""))
        except Exception as exc:
            return self.change_password_page(str(exc))
        self.redirect("/")


def serve(config: StationConfig, db: StationDatabase, logger: Logger) -> None:
    WebAppHandler.cfg = config
    WebAppHandler.db = db
    WebAppHandler.logger = logger
    server = ThreadingHTTPServer((config.web_host, int(config.web_port)), WebAppHandler)
    logger.log(f"Web UI: http://{config.web_host}:{config.web_port}")
    server.serve_forever()
