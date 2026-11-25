"""SQLite persistence + exports for gab.ai accounts.

We store:
  - email/password
  - access/refresh tokens (optional; can be sensitive)
  - minted `gab_...` API key (developer key)
  - flags: email verified, plus status (developer api usable or not)

The gateway will consume pool records and mint its own `sk-dcg-...` keys.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    email           TEXT PRIMARY KEY,
    password        TEXT,
    api_key         TEXT,
    api_key_id      TEXT,
    api_key_name    TEXT,
    api_blocked     TEXT,
    access_token    TEXT,
    refresh_token   TEXT,
    expires_at      TEXT,
    email_verified  INTEGER,
    plus            INTEGER,
    engine          TEXT,
    proxy           TEXT,
    cookies         TEXT,
    created_at      TEXT,
    status          TEXT DEFAULT 'active',
    note            TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    email  TEXT,
    engine TEXT,
    proxy  TEXT,
    status TEXT,
    error  TEXT,
    at     TEXT
);
CREATE TABLE IF NOT EXISTS failures (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    email  TEXT,
    domain TEXT,
    kind   TEXT,
    reason TEXT,
    round  INTEGER DEFAULT 0,
    at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_at ON attempts(at);
CREATE INDEX IF NOT EXISTS idx_failures_at ON failures(at);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def add(self, record: dict[str, Any], *, engine: str, proxy: str) -> bool:
        payload = (
            record.get("email"),
            record.get("password", ""),
            record.get("api_key", ""),
            record.get("api_key_id", ""),
            record.get("api_key_name", ""),
            record.get("api_blocked", ""),
            record.get("access_token", ""),
            record.get("refresh_token", ""),
            record.get("expires_at", ""),
            None if record.get("email_verified") is None else int(bool(record.get("email_verified"))),
            None if record.get("plus") is None else int(bool(record.get("plus"))),
            engine,
            proxy,
            json.dumps(record.get("cookies") or [], ensure_ascii=False),
            _now(),
            "active",
            "",
        )
        try:
            self.db.execute(
                """INSERT INTO accounts
                   (email, password, api_key, api_key_id, api_key_name, api_blocked,
                    access_token, refresh_token, expires_at,
                    email_verified, plus, engine, proxy, cookies, created_at, status, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            (email, engine, proxy, status, (error or "")[:800], _now()),
        )
        self.db.commit()

    def record_failure(self, *, email: str, domain: str, kind: str, reason: str, round_no: int = 0) -> None:
        self.db.execute(
            "INSERT INTO failures (email, domain, kind, reason, round, at) VALUES (?,?,?,?,?,?)",
            (email, domain, kind or "error", (reason or "")[:800], round_no, _now()),
        )
        self.db.commit()

    def has_email(self, email: str) -> bool:
        return self.db.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone() is not None

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])

    def rows(self, limit: int = 50) -> list[dict[str, Any]]:
        cursor = self.db.execute(
            "SELECT email, api_key, api_blocked, email_verified, plus, engine, created_at FROM accounts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cursor.fetchall()]

    def iter_accounts(self) -> Iterable[sqlite3.Row]:
        yield from self.db.execute(
            "SELECT email, password, api_key, api_key_id, api_key_name, api_blocked, access_token, refresh_token, expires_at, "
            "email_verified, plus, engine, cookies, created_at, status FROM accounts ORDER BY created_at"
        ).fetchall()

    def failure_summary(self, limit: int = 12) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT kind, COUNT(*) AS count, MAX(reason) AS example FROM failures GROUP BY kind ORDER BY count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def pool_records(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in self.iter_accounts():
            if not row["api_key"]:
                continue
            out.append(
                {
                    "email": row["email"],
                    "password": row["password"] or "",
                    "apiKey": row["api_key"],
                    "apiKeyId": row["api_key_id"] or "",
                    "apiKeyName": row["api_key_name"] or "",
                    "apiBlocked": row["api_blocked"] or "",
                    "accessToken": row["access_token"] or "",
                    "refreshToken": row["refresh_token"] or "",
                    "expiresAt": row["expires_at"] or "",
                    "emailVerified": None if row["email_verified"] is None else bool(row["email_verified"]),
                    "plus": None if row["plus"] is None else bool(row["plus"]),
                    "engine": row["engine"] or "protocol",
                    "cookies": json.loads(row["cookies"] or "[]"),
                    "createdAt": row["created_at"],
                    "status": row["status"] or "active",
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

    def export_csv(self, path: str | Path) -> int:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.pool_records()
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["email", "password", "api_key", "api_blocked", "created_at"])
            for row in rows:
                writer.writerow([row["email"], row["password"], row["apiKey"], row.get("apiBlocked", ""), row["createdAt"]])
        return len(rows)

    def close(self) -> None:
        self.db.close()
