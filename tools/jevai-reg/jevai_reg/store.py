"""SQLite 落库：账号 + Key + 失败分类。"""

from __future__ import annotations

import csv
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    email          TEXT PRIMARY KEY,
    password       TEXT,
    name           TEXT,
    user_id        TEXT,
    api_key        TEXT UNIQUE,
    key_prefix     TEXT,
    email_verified INTEGER,
    credits        TEXT,
    engine         TEXT,
    proxy          TEXT,
    created_at     TEXT,
    status         TEXT DEFAULT 'active',
    note           TEXT
);
CREATE TABLE IF NOT EXISTS failures (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    email  TEXT,
    kind   TEXT,
    reason TEXT,
    at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_failures_at ON failures(at);
"""

# 老库补列（SQLite 不支持 IF NOT EXISTS 的 ADD COLUMN，用 try 兜住）
MIGRATIONS = (
    "ALTER TABLE accounts ADD COLUMN last_checkin TEXT",
    "ALTER TABLE accounts ADD COLUMN checkin_count INTEGER DEFAULT 0",
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for statement in MIGRATIONS:
            try:
                self.db.execute(statement)
            except sqlite3.OperationalError:
                pass
        self.db.commit()

    def add(self, account: dict[str, Any], *, engine: str, proxy: str) -> bool:
        payload = (
            account.get("email"),
            account.get("password", ""),
            account.get("name", ""),
            account.get("userId", "") or account.get("user_id", ""),
            account.get("apiKey", "") or account.get("api_key", ""),
            account.get("keyPrefix", "sk-glm5-"),
            None if account.get("emailVerified") is None else int(bool(account["emailVerified"])),
            json.dumps(account.get("credits") or {}, ensure_ascii=False),
            engine,
            proxy,
            _now(),
            "active",
            "",
        )
        try:
            self.db.execute(
                """INSERT INTO accounts
                   (email, password, name, user_id, api_key, key_prefix, email_verified, credits,
                    engine, proxy, created_at, status, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                payload,
            )
        except sqlite3.IntegrityError:
            self.db.rollback()
            return False
        self.db.commit()
        return True

    def record_failure(self, *, email: str, kind: str, reason: str) -> None:
        self.db.execute(
            "INSERT INTO failures (email, kind, reason, at) VALUES (?,?,?,?)",
            (email, kind or "error", (reason or "")[:600], _now()),
        )
        self.db.commit()

    def record_checkin(self, email: str, *, credits: dict[str, Any] | None = None, when: str = "") -> None:
        """记一次签到：刷新额度快照与签到计数（用于判断"今天刷过没"）。"""
        payload = json.dumps(credits or {}, ensure_ascii=False)
        self.db.execute(
            "UPDATE accounts SET credits = ?, last_checkin = ?, "
            "checkin_count = COALESCE(checkin_count, 0) + 1 WHERE email = ?",
            (payload, when or _now(), email),
        )
        self.db.commit()

    def checkin_state(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT email, api_key, credits, last_checkin, COALESCE(checkin_count, 0) AS checkin_count "
            "FROM accounts WHERE api_key != '' ORDER BY last_checkin IS NULL DESC, last_checkin ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def accounts(self, limit: int = 0) -> list[dict[str, Any]]:
        sql = "SELECT * FROM accounts ORDER BY created_at DESC"
        rows = self.db.execute(sql + (" LIMIT ?" if limit else ""), ((limit,) if limit else ())).fetchall()
        return [dict(r) for r in rows]

    def keys(self) -> list[str]:
        rows = self.db.execute("SELECT api_key FROM accounts WHERE api_key != '' ORDER BY created_at").fetchall()
        return [r["api_key"] for r in rows]

    def counts(self) -> dict[str, Any]:
        accounts = int(self.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
        keys = int(self.db.execute("SELECT COUNT(*) FROM accounts WHERE api_key != ''").fetchone()[0])
        verified = int(self.db.execute("SELECT COUNT(*) FROM accounts WHERE email_verified = 1").fetchone()[0])
        return {"accounts": accounts, "keys": keys, "verified": verified}

    def failure_summary(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT kind, COUNT(*) AS count, MAX(reason) AS example FROM failures GROUP BY kind "
            "ORDER BY count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def pool_records(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in self.db.execute("SELECT * FROM accounts ORDER BY created_at").fetchall():
            if not row["api_key"]:
                continue
            out.append(
                {
                    "email": row["email"],
                    "password": row["password"] or "",
                    "name": row["name"] or "",
                    "userId": row["user_id"] or "",
                    "apiKey": row["api_key"],
                    "keyPrefix": row["key_prefix"] or "sk-glm5-",
                    "emailVerified": None if row["email_verified"] is None else bool(row["email_verified"]),
                    "credits": json.loads(row["credits"] or "{}"),
                    "engine": row["engine"] or "protocol",
                    "createdAt": row["created_at"],
                    "status": "active",
                }
            )
        return out

    def export_jsonl(self, path: str | Path) -> int:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.pool_records()
        with target.open("w", encoding="utf-8") as handle:
            for record in rows:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return len(rows)

    def export_keys(self, path: str | Path) -> int:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.keys()
        target.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
        return len(rows)

    def export_csv(self, path: str | Path) -> int:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.pool_records()
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["email", "name", "api_key", "email_verified", "engine", "created_at"])
            for row in rows:
                writer.writerow([row["email"], row["name"], row["apiKey"], row["emailVerified"], row["engine"], row["createdAt"]])
        return len(rows)

    def close(self) -> None:
        self.db.close()
