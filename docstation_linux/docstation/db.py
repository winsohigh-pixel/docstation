from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


PBKDF2_ITERATIONS = 180_000


def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class StationDatabase:
    """Thread-safe SQLite wrapper used by GTK UI and parallel import workers.

    One process can have many import threads. SQLite itself is fine with this,
    but a single sqlite3.Connection object must not be used concurrently.
    All access is serialized through _lock to avoid errors like:
    "cannot start a transaction within a transaction".
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=30000")
        self.init_schema()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def init_schema(self) -> None:
        with self._lock:
            c = self.conn.cursor()
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS role_passwords (
                    role TEXT PRIMARY KEY,
                    salt TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    iterations INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    file_name TEXT,
                    source_path TEXT,
                    archive_path TEXT,
                    device_id TEXT,
                    officer_id TEXT,
                    details TEXT
                );

                CREATE TABLE IF NOT EXISTS media_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    imported_at TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    archive_path TEXT NOT NULL UNIQUE,
                    source_path TEXT,
                    device_id TEXT,
                    officer_id TEXT,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT,
                    is_protected INTEGER NOT NULL DEFAULT 0,
                    deleted_at TEXT
                );

                CREATE TABLE IF NOT EXISTS station_status (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self.conn.commit()
        self.ensure_default_passwords()

    def _hash_password(self, password: str, salt: Optional[bytes] = None, iterations: int = PBKDF2_ITERATIONS) -> tuple[str, str, int]:
        salt = salt or os.urandom(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return base64.b64encode(salt).decode(), base64.b64encode(digest).decode(), iterations

    def set_password(self, role: str, password: str) -> None:
        if len(password) < 3:
            raise ValueError("Пароль должен быть минимум 3 символа")
        salt, digest, iterations = self._hash_password(password)
        with self._lock:
            self.conn.execute(
                "REPLACE INTO role_passwords(role,salt,hash,iterations,updated_at) VALUES(?,?,?,?,?)",
                (role, salt, digest, iterations, now_ts()),
            )
            self.conn.commit()
        self.add_audit(role, "password_reset", details=f"Password for role {role} was set/reset")

    def ensure_default_passwords(self) -> None:
        with self._lock:
            rows = {r[0] for r in self.conn.execute("SELECT role FROM role_passwords")}
        if "admin" not in rows:
            self.set_password("admin", "888")
        if "operator" not in rows:
            self.set_password("operator", "111")
        if "service" not in rows:
            self.set_password("service", "7777")   # сервисный PIN — только для техника

    def check_password(self, role: str, password: str) -> bool:
        with self._lock:
            row = self.conn.execute("SELECT * FROM role_passwords WHERE role=?", (role,)).fetchone()
        if not row:
            return False
        salt = base64.b64decode(row["salt"])
        _, digest, _ = self._hash_password(password, salt, int(row["iterations"]))
        return hmac_compare(digest, row["hash"])

    def change_password(self, role: str, old_password: str, new_password: str) -> None:
        if not self.check_password(role, old_password):
            raise PermissionError("Неверный старый пароль")
        self.set_password(role, new_password)
        self.add_audit(role, "password_changed", details=f"Password for role {role} was changed by itself")

    def add_audit(self, role: str, action: str, file_name: str | None = None, source_path: str | None = None,
                  archive_path: str | None = None, device_id: str | None = None, officer_id: str | None = None,
                  details: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO audit_logs(created_at,role,action,file_name,source_path,archive_path,device_id,officer_id,details)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (now_ts(), role, action, file_name, source_path, archive_path, device_id, officer_id, details),
            )
            self.conn.commit()

    def upsert_status(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute("REPLACE INTO station_status(key,value,updated_at) VALUES(?,?,?)", (key, value, now_ts()))
            self.conn.commit()

    def status_dict(self) -> Dict[str, str]:
        with self._lock:
            return {r["key"]: r["value"] for r in self.conn.execute("SELECT key,value FROM station_status")}

    def add_media(self, file_name: str, archive_path: str, source_path: str | None, device_id: str | None,
                  officer_id: str | None, size_bytes: int, sha256: str | None) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT OR IGNORE INTO media_records(imported_at,file_name,archive_path,source_path,device_id,officer_id,size_bytes,sha256)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (now_ts(), file_name, archive_path, source_path, device_id, officer_id, size_bytes, sha256),
            )
            self.conn.commit()
        self.add_audit("system", "downloaded", file_name=file_name, source_path=source_path, archive_path=archive_path,
                       device_id=device_id, officer_id=officer_id)

    def media_search(self, q: str = "", limit: int = 200) -> List[sqlite3.Row]:
        q = (q or "").strip().lower()
        with self._lock:
            if not q:
                return list(self.conn.execute(
                    "SELECT * FROM media_records WHERE deleted_at IS NULL ORDER BY imported_at DESC LIMIT ?", (limit,)))
            terms = [t for t in q.split() if t]
            sql = "SELECT * FROM media_records WHERE deleted_at IS NULL"
            args: List[Any] = []
            for term in terms:
                sql += " AND lower(coalesce(file_name,'') || ' ' || coalesce(archive_path,'') || ' ' || coalesce(device_id,'') || ' ' || coalesce(officer_id,'')) LIKE ?"
                args.append(f"%{term}%")
            sql += " ORDER BY imported_at DESC LIMIT ?"
            args.append(limit)
            return list(self.conn.execute(sql, args))

    def audit_search(self, limit: int = 500) -> List[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)))

    def protect_media(self, media_id: int, protected: bool, role: str) -> None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM media_records WHERE id=?", (media_id,)).fetchone()
            if not row:
                raise FileNotFoundError("Запись не найдена")
            self.conn.execute("UPDATE media_records SET is_protected=? WHERE id=?", (1 if protected else 0, media_id))
            self.conn.commit()
        p = Path(row["archive_path"])
        if p.exists():
            try:
                if protected:
                    p.chmod(p.stat().st_mode & ~0o222)
                else:
                    p.chmod(p.stat().st_mode | 0o200)
            except Exception:
                pass
        self.add_audit(role, "protect_on" if protected else "protect_off", file_name=row["file_name"], archive_path=row["archive_path"])

    def delete_media(self, media_id: int, role: str) -> None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM media_records WHERE id=?", (media_id,)).fetchone()
            if not row:
                raise FileNotFoundError("Запись не найдена")
            if row["is_protected"]:
                raise PermissionError("Файл защищён")
        p = Path(row["archive_path"])
        if p.exists():
            p.unlink()
        with self._lock:
            self.conn.execute("UPDATE media_records SET deleted_at=? WHERE id=?", (now_ts(), media_id))
            self.conn.commit()
        self.add_audit(role, "deleted", file_name=row["file_name"], archive_path=row["archive_path"])


def hmac_compare(a: str, b: str) -> bool:
    return hashlib.sha256(a.encode()).digest() == hashlib.sha256(b.encode()).digest()
