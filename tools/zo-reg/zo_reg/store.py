"""落盘：sqlite（去重 / 断点续跑 / 失败分布）+ jsonl（喂网关 / 人看）。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    email           TEXT PRIMARY KEY,
    handle          TEXT,
    name            TEXT,
    api_key         TEXT,
    key_id          TEXT,
    access_token    TEXT,
    refresh_token   TEXT,
    access_expires  INTEGER,
    engine          TEXT,
    domain          TEXT,
    proxy           TEXT,
    status          TEXT,
    note            TEXT,
    payload         TEXT,
    created_at      TEXT
);
CREATE TABLE IF NOT EXISTS failures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT,
    kind        TEXT,
    reason      TEXT,
    domain      TEXT,
    engine      TEXT,
    at          TEXT
);
CREATE TABLE IF NOT EXISTS domains (
    domain      TEXT PRIMARY KEY,
    ok          INTEGER DEFAULT 0,
    bad         INTEGER DEFAULT 0,
    updated_at  TEXT
);
"""


class Store:
    def __init__(self, out_dir: str):
        self.out_dir = os.path.abspath(out_dir)
        os.makedirs(self.out_dir, exist_ok=True)
        self.db_path = os.path.join(self.out_dir, "accounts.sqlite3")
        self.jsonl_path = os.path.join(self.out_dir, "accounts.jsonl")
        self.csv_path = os.path.join(self.out_dir, "accounts.csv")
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ 写
    def upsert(self, record: dict) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO accounts (email, handle, name, api_key, key_id, access_token,
                    refresh_token, access_expires, engine, domain, proxy, status, note, payload, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(email) DO UPDATE SET
                    handle=excluded.handle, name=excluded.name,
                    api_key=excluded.api_key, key_id=excluded.key_id,
                    access_token=excluded.access_token, refresh_token=excluded.refresh_token,
                    access_expires=excluded.access_expires, engine=excluded.engine,
                    domain=excluded.domain, proxy=excluded.proxy, status=excluded.status,
                    note=excluded.note, payload=excluded.payload
                """,
                (
                    record.get("email", ""),
                    record.get("handle", ""),
                    record.get("name", ""),
                    record.get("apiKey", ""),
                    record.get("keyId", ""),
                    record.get("accessToken", ""),
                    record.get("refreshToken", ""),
                    int(record.get("accessTokenExpiresAt") or 0),
                    record.get("engine", "protocol"),
                    record.get("domain", ""),
                    record.get("proxy", ""),
                    record.get("status", "active"),
                    record.get("note", ""),
                    json.dumps(record, ensure_ascii=False),
                    record.get("createdAt") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
            )
            self._conn.commit()
        self._append_jsonl(record)

    def record_failure(self, *, email: str, kind: str, reason: str, domain: str = "", engine: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO failures (email, kind, reason, domain, engine, at) VALUES (?,?,?,?,?,?)",
                (email, kind, reason[:400], domain, engine, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            )
            self._conn.commit()

    def record_domain(self, domain: str, *, ok: bool) -> None:
        if not domain:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO domains (domain, ok, bad, updated_at) VALUES (?,?,?,?)
                ON CONFLICT(domain) DO UPDATE SET
                    ok = ok + excluded.ok, bad = bad + excluded.bad, updated_at = excluded.updated_at
                """,
                (domain, 1 if ok else 0, 0 if ok else 1, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            )
            self._conn.commit()

    def _append_jsonl(self, record: dict) -> None:
        os.makedirs(self.out_dir, exist_ok=True)
        with open(self.jsonl_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ 读
    def seen_emails(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT email FROM accounts WHERE api_key != '' OR access_token != ''"
            ).fetchall()
        return {row[0] for row in rows}

    def accounts(self, limit: int = 0) -> list[dict]:
        sql = "SELECT payload FROM accounts ORDER BY created_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        out = []
        for (payload,) in rows:
            try:
                out.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
        return out

    def counts(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            with_key = self._conn.execute("SELECT COUNT(*) FROM accounts WHERE api_key != ''").fetchone()[0]
            with_token = self._conn.execute("SELECT COUNT(*) FROM accounts WHERE access_token != ''").fetchone()[0]
            failures = self._conn.execute("SELECT kind, COUNT(*) FROM failures GROUP BY kind").fetchall()
        return {
            "accounts": total,
            "with_api_key": with_key,
            "with_access_token": with_token,
            "failures": {kind: count for kind, count in failures},
        }

    def export(self, *, fmt: str = "jsonl", path: str = "") -> str:
        rows = self.accounts()
        target = path or (self.jsonl_path if fmt == "jsonl" else self.csv_path)
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
        if fmt == "csv":
            header = ["email", "handle", "api_key", "access_token", "engine", "domain", "status", "created_at"]
            lines = [",".join(header)]
            for row in rows:
                values = [
                    str(row.get(k, ""))
                    for k in ("email", "handle", "apiKey", "accessToken", "engine", "domain", "status", "createdAt")
                ]
                lines.append(",".join('"' + v.replace('"', '""') + '"' for v in values))
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        else:
            with open(target, "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return target

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def load_jsonl(path: str) -> list[dict]:
    rows: list[dict] = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def records_from_dataclasses(items: Iterable) -> list[dict]:
    out = []
    for item in items:
        if isinstance(item, dict):
            out.append(item)
        elif hasattr(item, "to_record"):
            out.append(item.to_record())
        else:
            out.append(asdict(item))
    return out