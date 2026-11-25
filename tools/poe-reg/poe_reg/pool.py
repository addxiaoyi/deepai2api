"""Independent Poe pool: one row per account, cookies included.

Deliberately separate from the DeepAI pool (`data/gateway-state.json`): the two
providers have different credentials (API key vs. cookie jar), different life
cycles and different ban profiles, and mixing them would make the DeepAI
scheduler hand out a Poe cookie as if it were an api key.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          TEXT PRIMARY KEY,
    email       TEXT UNIQUE,
    status      TEXT,          -- email_verified | phone_required | active | dead
    engine      TEXT,
    proxy       TEXT,
    cookies     TEXT,          -- JSON: p-b / cf_clearance / g_state ...
    formkey     TEXT,
    phone       TEXT,
    sms_provider TEXT,
    refresh_at  TEXT,
    created_at  TEXT,
    updated_at  TEXT,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    email   TEXT,
    kind    TEXT,
    detail  TEXT,
    at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
"""

# Statuses the runner produces; anything else is operator-set.
STATUSES = ("email_verified", "phone_required", "active", "dead")


class Pool:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ------------------------------------------------------------------ write
    def upsert(self, account: dict[str, Any]) -> bool:
        email = (account.get("email") or "").strip().lower()
        if not email:
            return False
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        existing = self.db.execute("SELECT id FROM accounts WHERE email = ?", (email,)).fetchone()
        cookies = account.get("cookies")
        cookies_json = json.dumps(cookies, ensure_ascii=False) if cookies else None
        payload = (
            account.get("status") or "email_verified",
            account.get("engine") or "",
            account.get("proxy") or "",
            cookies_json,
            account.get("formkey") or "",
            account.get("phone") or "",
            account.get("sms_provider") or "",
            account.get("refresh_at") or "",
            now,
            account.get("note") or "",
        )
        if existing:
            self.db.execute(
                """UPDATE accounts SET status=?, engine=?, proxy=?, cookies=COALESCE(?, cookies),
                   formkey=CASE WHEN ?='' THEN formkey ELSE ? END,
                   phone=?, sms_provider=?, refresh_at=?, updated_at=?, note=? WHERE email=?""",
                (
                    payload[0], payload[1], payload[2], cookies_json,
                    payload[4], payload[4],
                    payload[5], payload[6], payload[7], payload[8], payload[9], email,
                ),
            )
            created = False
        else:
            self.db.execute(
                """INSERT INTO accounts (id, email, status, engine, proxy, cookies, formkey,
                   phone, sms_provider, refresh_at, updated_at, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    account.get("id") or f"poe-{int(time.time() * 1000)}-{abs(hash(email)) % 10000}",
                    email,
                )
                + (payload[0], payload[1], payload[2], cookies_json, payload[4], payload[5], payload[6], payload[7], now, payload[9], now),
            )
            created = True
        self.db.commit()
        self.event(email, "upsert", account.get("status") or "")
        return created

    def event(self, email: str, kind: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO events (email, kind, detail, at) VALUES (?,?,?,?)",
            (email, kind, detail[:600], time.strftime("%Y-%m-%dT%H:%M:%S%z")),
        )
        self.db.commit()

    # ------------------------------------------------------------------- read
    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])

    def counts_by_status(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) c FROM accounts GROUP BY status").fetchall()
        return {str(r["status"]): int(r["c"]) for r in rows}

    def rows(self, limit: int = 50) -> list[dict[str, Any]]:
        cursor = self.db.execute(
            "SELECT email, status, engine, phone, sms_provider, created_at, updated_at "
            "FROM accounts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cursor.fetchall()]

    def has(self, email: str) -> bool:
        return (
            self.db.execute("SELECT 1 FROM accounts WHERE email = ?", (email.strip().lower(),)).fetchone()
            is not None
        )

    def cookie_map(self, email: str) -> dict[str, str]:
        row = self.db.execute("SELECT cookies FROM accounts WHERE email = ?", (email,)).fetchone()
        if not row or not row["cookies"]:
            return {}
        try:
            return json.loads(row["cookies"])
        except json.JSONDecodeError:
            return {}

    def mark(self, email: str, status: str, **fields: Any) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        sets = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, time.strftime("%Y-%m-%dT%H:%M:%S%z")]
        for key, value in fields.items():
            if key in ("phone", "note", "proxy", "engine", "sms_provider", "refresh_at"):
                sets.append(f"{key} = ?")
                values.append(value)
        values.append(email)
        self.db.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE email = ?", values)
        self.db.commit()
        self.event(email, "status", status)

    # ---------------------------------------------------------------- exports
    def export_jsonl(self, path: str | Path) -> int:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.db.execute(
            "SELECT id, email, status, engine, cookies, phone, sms_provider, created_at FROM accounts"
        ).fetchall()
        written = 0
        with target.open("w", encoding="utf-8") as handle:
            for row in rows:
                record = {
                    "id": row["id"],
                    "email": row["email"],
                    "status": row["status"],
                    "engine": row["engine"],
                    "cookies": json.loads(row["cookies"]) if row["cookies"] else {},
                    "phone": row["phone"],
                    "smsProvider": row["sms_provider"],
                    "createdAt": row["created_at"],
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
        return written

    def close(self) -> None:
        self.db.close()
