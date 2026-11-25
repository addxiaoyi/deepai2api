"""SQLite persistence + exports in the shapes the gateway consumes."""

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
    api_key        TEXT UNIQUE,
    key_id         TEXT,
    key_prefix     TEXT,
    key_last4      TEXT,
    key_source     TEXT,
    extra_keys     TEXT,
    project_id     TEXT,
    guardrail_id   TEXT,
    organization_id TEXT,
    user_id        TEXT,
    email_verified INTEGER,
    engine         TEXT,
    proxy          TEXT,
    cookies        TEXT,
    created_at     TEXT,
    status         TEXT DEFAULT 'active',
    note           TEXT
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

    # ------------------------------------------------------------------ write
    def add(self, account: dict[str, Any], *, engine: str, proxy: str) -> bool:
        payload = (
            account.get("email"),
            account.get("password", ""),
            account.get("name", ""),
            account.get("api_key", ""),
            account.get("key_id", ""),
            account.get("key_prefix", ""),
            account.get("key_last4", ""),
            account.get("api_key_source", ""),
            json.dumps(account.get("extra_keys") or [], ensure_ascii=False),
            account.get("project_id", ""),
            account.get("guardrail_id", ""),
            account.get("organization_id", ""),
            account.get("user_id", ""),
            None if account.get("email_verified") is None else int(bool(account.get("email_verified"))),
            engine,
            proxy,
            json.dumps(account.get("cookies") or [], ensure_ascii=False),
            _now(),
            "active",
            "",
        )
        try:
            self.db.execute(
                """INSERT INTO accounts
                   (email, password, name, api_key, key_id, key_prefix, key_last4, key_source,
                    extra_keys, project_id, guardrail_id, organization_id, user_id, email_verified,
                    engine, proxy, cookies, created_at, status, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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

    def record_failure(
        self,
        *,
        email: str,
        domain: str,
        kind: str,
        reason: str,
        round_no: int = 0,
    ) -> None:
        self.db.execute(
            "INSERT INTO failures (email, domain, kind, reason, round, at) VALUES (?,?,?,?,?,?)",
            (email, domain, kind or "error", (reason or "")[:800], round_no, _now()),
        )
        self.db.commit()

    # ------------------------------------------------------------------- read
    def has_email(self, email: str) -> bool:
        return self.db.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone() is not None

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])

    def rows(self, limit: int = 50) -> list[dict[str, Any]]:
        cursor = self.db.execute(
            "SELECT email, name, api_key, key_prefix, key_source, project_id, email_verified, "
            "engine, created_at FROM accounts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cursor.fetchall()]

    def iter_accounts(self) -> Iterable[sqlite3.Row]:
        yield from self.db.execute(
            "SELECT email, password, name, api_key, key_id, key_prefix, key_last4, key_source, "
            "extra_keys, project_id, guardrail_id, organization_id, user_id, email_verified, "
            "engine, cookies, created_at FROM accounts ORDER BY created_at"
        ).fetchall()

    def failure_summary(self, limit: int = 12) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT kind, COUNT(*) AS count, MAX(reason) AS example FROM failures "
            "GROUP BY kind ORDER BY count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- exports
    def pool_records(self) -> list[dict[str, Any]]:
        """Rows in `dcg.pool/allmodels@1` shape (gateway side adds ids and keys)."""
        out: list[dict[str, Any]] = []
        for row in self.iter_accounts():
            if not row["api_key"]:
                continue
            out.append(
                {
                    "email": row["email"],
                    "password": row["password"] or "",
                    "name": row["name"] or "",
                    "apiKey": row["api_key"],
                    "keyId": row["key_id"] or "",
                    "keyPrefix": row["key_prefix"] or "",
                    "keyLast4": row["key_last4"] or "",
                    "keySource": row["key_source"] or "",
                    "extraKeys": json.loads(row["extra_keys"] or "[]"),
                    "projectId": row["project_id"] or "",
                    "guardrailId": row["guardrail_id"] or "",
                    "organizationId": row["organization_id"] or "",
                    "userId": row["user_id"] or "",
                    "emailVerified": None if row["email_verified"] is None else bool(row["email_verified"]),
                    "engine": row["engine"] or "protocol",
                    "cookies": json.loads(row["cookies"] or "[]"),
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

    def export_csv(self, path: str | Path) -> int:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.pool_records()
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["email", "password", "api_key", "key_prefix", "project_id", "guardrail_id", "created_at"]
            )
            for row in rows:
                writer.writerow(
                    [
                        row["email"],
                        row["password"],
                        row["apiKey"],
                        row["keyPrefix"],
                        row["projectId"],
                        row["guardrailId"],
                        row["createdAt"],
                    ]
                )
        return len(rows)

    def close(self) -> None:
        self.db.close()
