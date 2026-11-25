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
    uid            TEXT,
    id_token       TEXT,
    refresh_token  TEXT,
    app_check      TEXT,
    app_check_exp  REAL,
    wallet         TEXT,
    engine         TEXT,
    proxy          TEXT,
    device         TEXT,
    email_verified INTEGER,
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
CREATE TABLE IF NOT EXISTS domains (
    domain    TEXT PRIMARY KEY,
    ok        INTEGER DEFAULT 0,
    failed    INTEGER DEFAULT 0,
    cooldown_until REAL DEFAULT 0,
    note      TEXT,
    updated_at TEXT
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
            account.get("uid", ""),
            account.get("id_token", ""),
            account.get("refresh_token", ""),
            account.get("app_check", ""),
            float(account.get("app_check_exp") or 0),
            json.dumps(account.get("wallet") or {}, ensure_ascii=False),
            engine,
            proxy,
            json.dumps(account.get("device") or {}, ensure_ascii=False),
            None if account.get("email_verified") is None else int(bool(account.get("email_verified"))),
            _now(),
            "active",
            "",
        )
        try:
            self.db.execute(
                """INSERT INTO accounts
                   (email, password, name, uid, id_token, refresh_token, app_check, app_check_exp,
                    wallet, engine, proxy, device, email_verified, created_at, status, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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

    # 域名战绩：成功加分、失败扣分并进冷却，这是「防封」里最省事的一层
    def domain_ok(self, domain: str) -> None:
        self.db.execute(
            "INSERT INTO domains (domain, ok, failed, updated_at) VALUES (?, 1, 0, ?) "
            "ON CONFLICT(domain) DO UPDATE SET ok = ok + 1, updated_at = excluded.updated_at",
            (domain, _now()),
        )
        self.db.commit()

    def domain_failed(self, domain: str, cooldown: float) -> None:
        self.db.execute(
            "INSERT INTO domains (domain, ok, failed, cooldown_until, updated_at) VALUES (?, 0, 1, ?, ?) "
            "ON CONFLICT(domain) DO UPDATE SET failed = failed + 1, cooldown_until = excluded.cooldown_until, "
            "updated_at = excluded.updated_at",
            (domain, time.time() + cooldown, _now()),
        )
        self.db.commit()

    def domain_score(self, domain: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM domains WHERE domain = ?", (domain,)).fetchone()
        return dict(row) if row else {"domain": domain, "ok": 0, "failed": 0, "cooldown_until": 0}

    # ------------------------------------------------------------------- read
    def has_email(self, email: str) -> bool:
        return self.db.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone() is not None

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])

    def rows(self, limit: int = 50) -> list[dict[str, Any]]:
        cursor = self.db.execute(
            "SELECT email, name, uid, engine, email_verified, created_at FROM accounts "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cursor.fetchall()]

    def iter_accounts(self) -> Iterable[sqlite3.Row]:
        yield from self.db.execute(
            "SELECT * FROM accounts ORDER BY created_at"
        ).fetchall()

    def domain_table(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT domain, ok, failed, cooldown_until FROM domains ORDER BY ok DESC, failed ASC LIMIT ?",
            (limit,),
        ).fetchall()
        now = time.time()
        return [
            {
                "domain": r["domain"],
                "ok": r["ok"],
                "failed": r["failed"],
                "cooldown_s": max(0, int((r["cooldown_until"] or 0) - now)),
            }
            for r in rows
        ]

    def failure_summary(self, limit: int = 12) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT kind, COUNT(*) AS count, MAX(reason) AS example FROM failures "
            "GROUP BY kind ORDER BY count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- exports
    def pool_records(self) -> list[dict[str, Any]]:
        """Rows in `dcg.pool/cleverai@1` shape (gateway side adds ids and keys)."""
        out: list[dict[str, Any]] = []
        for row in self.iter_accounts():
            if not row["refresh_token"]:
                continue
            out.append(
                {
                    "email": row["email"],
                    "password": row["password"] or "",
                    "name": row["name"] or "",
                    "uid": row["uid"] or "",
                    "idToken": row["id_token"] or "",
                    "refreshToken": row["refresh_token"] or "",
                    "appCheck": row["app_check"] or "",
                    "appCheckExp": row["app_check_exp"] or 0,
                    "wallet": json.loads(row["wallet"] or "{}"),
                    "device": json.loads(row["device"] or "{}"),
                    "emailVerified": None if row["email_verified"] is None else bool(row["email_verified"]),
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

    def export_csv(self, path: str | Path) -> int:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.pool_records()
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["email", "password", "uid", "refresh_token", "engine", "created_at"])
            for row in rows:
                writer.writerow(
                    [row["email"], row["password"], row["uid"], row["refreshToken"], row["engine"], row["createdAt"]]
                )
        return len(rows)

    def close(self) -> None:
        self.db.close()
