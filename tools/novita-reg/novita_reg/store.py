"""SQLite 持久化 + 导出（网关能直接吃的形状）。

与 wavespeed 的 store 相比多一张 `keys` 表：Novita 一个账号可以有多把 key
（上限 10），有效期各自独立，所以要能按账号列出全部 key。
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
    name            TEXT,
    api_key         TEXT UNIQUE,
    key_id          TEXT,
    key_prefix      TEXT,
    key_last4       TEXT,
    key_source      TEXT,
    extra_keys      TEXT,
    expire_time     TEXT,
    organization_id TEXT,
    user_id         TEXT,
    email_verified  INTEGER,
    engine          TEXT,
    domain          TEXT,
    proxy           TEXT,
    cookies         TEXT,
    created_at      TEXT,
    status          TEXT DEFAULT 'active',
    note            TEXT
);
CREATE TABLE IF NOT EXISTS keys (
    id          TEXT PRIMARY KEY,
    email       TEXT,
    api_key     TEXT,
    key_id      TEXT,
    name        TEXT,
    expire_time TEXT,
    source      TEXT,
    created_at  TEXT,
    note        TEXT
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
CREATE INDEX IF NOT EXISTS idx_accounts_domain ON accounts(domain);
CREATE INDEX IF NOT EXISTS idx_keys_email ON keys(email);
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
            account.get("expire_time", ""),
            account.get("organization_id", ""),
            account.get("user_id", ""),
            None if account.get("email_verified") is None else int(bool(account.get("email_verified"))),
            engine,
            account.get("domain", ""),
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
                    extra_keys, expire_time, organization_id, user_id, email_verified,
                    engine, domain, proxy, cookies, created_at, status, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                payload,
            )
            self.db.commit()
        except sqlite3.IntegrityError:
            self.db.rollback()
            return False

        # 主 key + 额外 key 都进 keys 表，方便按账号列出全部
        for index, extra in enumerate(account.get("extra_keys") or []):
            token = str(extra.get("key") or "")
            if token:
                self.record_key(
                    email=str(account.get("email") or ""),
                    api_key=token,
                    name=f"{account.get('email', '')}-{index + 2}",
                    expire_time=str(account.get("expire_time") or ""),
                    source=str(extra.get("source") or "created"),
                )
        primary = str(account.get("api_key") or "")
        if primary:
            self.record_key(
                email=str(account.get("email") or ""),
                api_key=primary,
                key_id=str(account.get("key_id") or ""),
                name=str(account.get("name") or ""),
                expire_time=str(account.get("expire_time") or ""),
                source=str(account.get("api_key_source") or "panel-api"),
            )
        return True

    def record_key(
        self,
        *,
        email: str,
        api_key: str,
        key_id: str = "",
        name: str = "",
        expire_time: str = "",
        source: str = "created",
    ) -> bool:
        row_id = f"{email}:{api_key[-8:]}"
        try:
            self.db.execute(
                "INSERT INTO keys (id, email, api_key, key_id, name, expire_time, source, created_at, note) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (row_id, email, api_key, key_id, name, expire_time, source, _now(), ""),
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

    def key_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM keys").fetchone()[0])

    def rows(self, limit: int = 50) -> list[dict[str, Any]]:
        cursor = self.db.execute(
            "SELECT email, name, api_key, key_prefix, key_source, domain, engine, "
            "expire_time, email_verified, created_at FROM accounts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cursor.fetchall()]

    def iter_accounts(self) -> Iterable[sqlite3.Row]:
        yield from self.db.execute(
            "SELECT email, password, name, api_key, key_id, key_prefix, key_last4, key_source, "
            "extra_keys, expire_time, organization_id, user_id, email_verified, engine, "
            "domain, cookies, created_at FROM accounts ORDER BY created_at"
        ).fetchall()

    def keys_for(self, email: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT api_key, key_id, name, expire_time, source, created_at FROM keys "
            "WHERE email = ? ORDER BY created_at",
            (email,),
        ).fetchall()
        return [
            {
                "key": row["api_key"],
                "keyId": row["key_id"] or "",
                "name": row["name"] or "",
                "expireTime": row["expire_time"] or "",
                "source": row["source"] or "",
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def failure_summary(self, limit: int = 12) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT kind, COUNT(*) AS count, MAX(reason) AS example FROM failures "
            "GROUP BY kind ORDER BY count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def domain_summary(self, limit: int = 40) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT domain, COUNT(*) AS count, "
            "SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active "
            "FROM accounts WHERE domain <> '' GROUP BY domain ORDER BY count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- exports
    def pool_records(self) -> list[dict[str, Any]]:
        """`dcg.pool/novita@1` 形状（网关侧补 id 与网关 key）。"""
        out: list[dict[str, Any]] = []
        for row in self.iter_accounts():
            if not row["api_key"]:
                continue
            extra = json.loads(row["extra_keys"] or "[]")
            # 以 keys 表为准补齐（table 里可能还有 UI 建的其他 key）
            stored = self.keys_for(row["email"])
            if stored:
                extra = [
                    {"key": k["key"], "source": k["source"] or "created", "expireTime": k["expireTime"]}
                    for k in stored
                    if k["key"] != row["api_key"]
                ]
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
                    "extraKeys": extra,
                    "expireTime": row["expire_time"] or "",
                    "organizationId": row["organization_id"] or "",
                    "userId": row["user_id"] or "",
                    "emailVerified": None if row["email_verified"] is None else bool(row["email_verified"]),
                    "engine": row["engine"] or "protocol",
                    "domain": row["domain"] or "",
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
                ["email", "password", "api_key", "key_prefix", "expire_time", "engine", "domain", "keys", "created_at"]
            )
            for row in rows:
                writer.writerow(
                    [
                        row["email"],
                        row["password"],
                        row["apiKey"],
                        row["keyPrefix"],
                        row["expireTime"],
                        row["engine"],
                        row["domain"],
                        len(row["extraKeys"]) + 1,
                        row["createdAt"],
                    ]
                )
        return len(rows)

    def close(self) -> None:
        self.db.close()
