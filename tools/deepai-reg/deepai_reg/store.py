"""SQLite persistence + exports in the shapes this repo already consumes."""

from __future__ import annotations

import csv
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    email        TEXT PRIMARY KEY,
    username     TEXT,
    password     TEXT,
    api_key      TEXT UNIQUE,
    user_pk      INTEGER,
    plan         TEXT,
    is_pro       INTEGER DEFAULT 0,
    engine       TEXT,
    proxy        TEXT,
    sessionid    TEXT,
    csrftoken    TEXT,
    created_at   TEXT,
    status       TEXT DEFAULT 'ok',
    note         TEXT,
    profile_json TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    email     TEXT,
    engine    TEXT,
    proxy     TEXT,
    status    TEXT,
    error     TEXT,
    at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_at ON attempts(at);
"""

GATEWAY_KEYS = ("apiKey", "username", "isPro", "email", "password", "createdAt")


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ------------------------------------------------------------------ write
    def add(self, account: dict[str, Any], *, engine: str, proxy: str) -> bool:
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        payload = (
            account.get("email"),
            account.get("username", ""),
            account.get("password", ""),
            account.get("api_key", ""),
            account.get("user_pk"),
            account.get("plan", ""),
            1 if account.get("is_pro") else 0,
            engine,
            proxy,
            account.get("sessionid", ""),
            account.get("csrftoken", ""),
            now,
            "ok",
            account.get("api_key_source", ""),
            json.dumps(account.get("profile") or {}, ensure_ascii=False),
        )
        try:
            self.db.execute(
                """INSERT INTO accounts
                   (email, username, password, api_key, user_pk, plan, is_pro, engine, proxy,
                    sessionid, csrftoken, created_at, status, note, profile_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                payload,
            )
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            self.db.rollback()
            return False

    def attempt(self, email: str, engine: str, proxy: str, status: str, error: str = "") -> None:
        self.db.execute(
            "INSERT INTO attempts (email, engine, proxy, status, error, at) VALUES (?,?,?,?,?,?)",
            (email, engine, proxy, status, error[:800], time.strftime("%Y-%m-%dT%H:%M:%S%z")),
        )
        self.db.commit()

    # ------------------------------------------------------------------- read
    def has_email(self, email: str) -> bool:
        row = self.db.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone()
        return row is not None

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])

    def counts_by_status(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) c FROM accounts GROUP BY status").fetchall()
        return {str(r["status"]): int(r["c"]) for r in rows}

    def rows(self, limit: int = 50) -> list[dict[str, Any]]:
        cursor = self.db.execute(
            "SELECT email, username, api_key, plan, is_pro, engine, created_at "
            "FROM accounts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cursor.fetchall()]

    def iter_accounts(self) -> Iterable[sqlite3.Row]:
        yield from self.db.execute(
            "SELECT email, username, password, api_key, plan, is_pro, created_at FROM accounts"
        ).fetchall()

    # ---------------------------------------------------------------- exports
    def export_jsonl(self, path: str | Path) -> int:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with target.open("w", encoding="utf-8") as handle:
            for row in self.iter_accounts():
                if not row["api_key"]:
                    continue
                record = {
                    "apiKey": row["api_key"],
                    "username": row["username"] or "",
                    "isPro": bool(row["is_pro"]),
                    "email": row["email"],
                    "password": row["password"],
                    "plan": row["plan"] or "",
                    "createdAt": row["created_at"],
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
        return written

    def export_csv(self, path: str | Path) -> int:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["email", "username", "password", "api_key", "plan", "is_pro", "created_at"])
            for row in self.iter_accounts():
                writer.writerow(
                    [
                        row["email"],
                        row["username"],
                        row["password"],
                        row["api_key"],
                        row["plan"],
                        int(bool(row["is_pro"])),
                        row["created_at"],
                    ]
                )
                written += 1
        return written

    def close(self) -> None:
        self.db.close()
