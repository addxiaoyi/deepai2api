"""SQLite 落库：账号 + 代理池 + 失败分类。

代理单独一张表（`proxies`），以 `endpoint` 为主键，带 `source_email` 与健康计数 ——
下游要的是"能用的代理串"，不是"哪个账号给的那堆"。
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
    email          TEXT PRIMARY KEY,
    password       TEXT,
    access_token   TEXT,
    refresh_token  TEXT,
    trial_claimed  INTEGER,
    proxy_count    INTEGER DEFAULT 0,
    engine         TEXT,
    proxy          TEXT,
    created_at     TEXT,
    status         TEXT DEFAULT 'active',
    note           TEXT
);
CREATE TABLE IF NOT EXISTS proxies (
    endpoint       TEXT PRIMARY KEY,
    scheme         TEXT DEFAULT 'http',
    host           TEXT,
    port           INTEGER,
    username       TEXT,
    password       TEXT,
    source_email   TEXT,
    ok             INTEGER DEFAULT 0,
    failed         INTEGER DEFAULT 0,
    created_at     TEXT
);
CREATE TABLE IF NOT EXISTS failures (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    email  TEXT,
    kind   TEXT,
    reason TEXT,
    at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_proxies_source ON proxies(source_email);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def split_endpoint(endpoint: str) -> dict[str, Any]:
    """`http://u:p@h:1` -> {scheme,host,port,username,password}"""
    scheme = "http"
    rest = endpoint
    if "://" in rest:
        scheme, rest = rest.split("://", 1)
    auth = ""
    if "@" in rest:
        auth, rest = rest.rsplit("@", 1)
    username = password = ""
    if auth:
        if ":" in auth:
            username, password = auth.split(":", 1)
        else:
            username = auth
    host, _, port = rest.partition(":")
    try:
        port_num = int(port)
    except ValueError:
        port_num = 0
    return {
        "endpoint": endpoint,
        "scheme": scheme,
        "host": host,
        "port": port_num,
        "username": username,
        "password": password,
    }


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
        proxies = account.get("proxies") or []
        payload = (
            account.get("email"),
            account.get("password", ""),
            account.get("access_token", ""),
            account.get("refresh_token", ""),
            int(bool(account.get("trial_claimed"))),
            len(proxies),
            engine,
            proxy,
            _now(),
            "active",
            "",
        )
        try:
            self.db.execute(
                """INSERT INTO accounts
                   (email, password, access_token, refresh_token, trial_claimed, proxy_count,
                    engine, proxy, created_at, status, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                payload,
            )
        except sqlite3.IntegrityError:
            self.db.rollback()
            return False
        self.db.commit()
        self.add_proxies(proxies, source_email=account.get("email", ""))
        return True

    def add_proxies(self, endpoints: Iterable[str], *, source_email: str = "") -> int:
        added = 0
        for endpoint in endpoints or []:
            if not endpoint:
                continue
            parts = split_endpoint(str(endpoint))
            try:
                self.db.execute(
                    """INSERT INTO proxies (endpoint, scheme, host, port, username, password, source_email, created_at)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(endpoint) DO UPDATE SET source_email=excluded.source_email""",
                    (
                        parts["endpoint"],
                        parts["scheme"],
                        parts["host"],
                        parts["port"],
                        parts["username"],
                        parts["password"],
                        source_email,
                        _now(),
                    ),
                )
                added += 1
            except sqlite3.Error:
                continue
        self.db.commit()
        return added

    def record_proxy(self, endpoint: str, *, ok: bool) -> None:
        column = "ok" if ok else "failed"
        self.db.execute(f"UPDATE proxies SET {column} = {column} + 1 WHERE endpoint = ?", (endpoint,))
        self.db.commit()

    def record_failure(self, *, email: str, kind: str, reason: str) -> None:
        self.db.execute(
            "INSERT INTO failures (email, kind, reason, at) VALUES (?,?,?,?)",
            (email, kind or "error", (reason or "")[:600], _now()),
        )
        self.db.commit()

    # ------------------------------------------------------------------- read
    def accounts(self, limit: int = 0) -> list[dict[str, Any]]:
        sql = "SELECT * FROM accounts ORDER BY created_at DESC"
        rows = self.db.execute(sql + (" LIMIT ?" if limit else ""), ((limit,) if limit else ())).fetchall()
        return [dict(r) for r in rows]

    def proxies(self, *, limit: int = 0, source_email: str = "") -> list[str]:
        if source_email:
            rows = self.db.execute(
                "SELECT endpoint FROM proxies WHERE source_email = ? ORDER BY created_at", (source_email,)
            ).fetchall()
        else:
            sql = "SELECT endpoint FROM proxies ORDER BY created_at"
            rows = self.db.execute(sql + (" LIMIT ?" if limit else ""), ((limit,) if limit else ())).fetchall()
        return [r["endpoint"] for r in rows]

    def counts(self) -> dict[str, Any]:
        accounts = int(self.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
        proxies = int(self.db.execute("SELECT COUNT(*) FROM proxies").fetchone()[0])
        claimed = int(self.db.execute("SELECT COUNT(*) FROM accounts WHERE trial_claimed = 1").fetchone()[0])
        return {"accounts": accounts, "proxies": proxies, "trial_claimed": claimed}

    def failure_summary(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT kind, COUNT(*) AS count, MAX(reason) AS example FROM failures "
            "GROUP BY kind ORDER BY count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- exports
    def export_jsonl(self, path: str | Path) -> int:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "email": row["email"],
                "password": row["password"],
                "accessToken": row["access_token"],
                "refreshToken": row["refresh_token"],
                "trialClaimed": bool(row["trial_claimed"]),
                "proxies": self.proxies(source_email=row["email"]),
                "engine": row["engine"],
                "createdAt": row["created_at"],
            }
            for row in self.db.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()
        ]
        with target.open("w", encoding="utf-8") as handle:
            for record in rows:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return len(rows)

    def export_proxies(self, path: str | Path) -> int:
        """下游直接吃的一行一条：`http://user:pass@host:port`。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.proxies()
        target.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
        return len(rows)

    def export_csv(self, path: str | Path) -> int:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.db.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["email", "password", "trial_claimed", "proxy_count", "engine", "created_at"])
            for row in rows:
                writer.writerow(
                    [row["email"], row["password"], row["trial_claimed"], row["proxy_count"], row["engine"], row["created_at"]]
                )
        return len(rows)

    def close(self) -> None:
        self.db.close()
