"""Learned mailbox-domain pool.

DeepAI keeps a disposable-domain blocklist, and the free mail APIs hand out
domains that rotate in and out of it. Rather than hard-coding a list, every
signup feeds a scorecard: a domain that produced an account stays eligible, a
domain DeepAI refused is parked. Next run starts from what is known to work.

The table lives in the same SQLite file as the accounts, so the WebUI's
"邮箱域名" panel can read it without a second store.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_domains (
    provider    TEXT NOT NULL,
    domain      TEXT NOT NULL,
    ok_count    INTEGER DEFAULT 0,
    bad_count   INTEGER DEFAULT 0,
    last_error  TEXT,
    last_ok_at  TEXT,
    updated_at  TEXT,
    PRIMARY KEY (provider, domain)
);
"""

# Two-label public suffixes we care about when reducing an address to its apex.
_TWO_LABEL = {
    "co.uk", "org.uk", "ac.uk", "com.au", "net.au", "co.nz", "co.jp", "ne.jp",
    "com.br", "com.cn", "com.hk", "com.sg", "com.tr", "co.in", "co.za",
}


def apex_domain(address: str) -> str:
    """`a.b@x.y.repairhubde.com` -> `repairhubde.com`; keeps known 2-label suffixes."""
    if "@" not in address:
        return ""
    host = address.rsplit("@", 1)[1].strip().lower().strip(".")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _TWO_LABEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


class DomainPool:
    """Scorecard over `mail_domains`; safe to share between worker threads."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ------------------------------------------------------------------ write
    def record_ok(self, provider: str, domain: str) -> None:
        if not domain:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.db.execute(
            """INSERT INTO mail_domains (provider, domain, ok_count, bad_count, last_ok_at, updated_at)
               VALUES (?,?,1,0,?,?)
               ON CONFLICT(provider, domain) DO UPDATE SET
                 ok_count = ok_count + 1, last_ok_at = excluded.last_ok_at, updated_at = excluded.updated_at""",
            (provider, domain, now, now),
        )
        self.db.commit()

    def record_bad(self, provider: str, domain: str, error: str = "") -> None:
        if not domain:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.db.execute(
            """INSERT INTO mail_domains (provider, domain, ok_count, bad_count, last_error, updated_at)
               VALUES (?,?,0,1,?,?)
               ON CONFLICT(provider, domain) DO UPDATE SET
                 bad_count = bad_count + 1, last_error = excluded.last_error, updated_at = excluded.updated_at""",
            (provider, domain, (error or "")[:300], now),
        )
        self.db.commit()

    # ------------------------------------------------------------------- read
    def rows(self, provider: str = "") -> list[dict]:
        if provider:
            cursor = self.db.execute(
                "SELECT * FROM mail_domains WHERE provider = ? ORDER BY ok_count DESC, domain", (provider,)
            )
        else:
            cursor = self.db.execute(
                "SELECT * FROM mail_domains ORDER BY ok_count DESC, domain"
            )
        return [dict(r) for r in cursor.fetchall()]

    def candidates(self, provider: str, configured: list[str]) -> list[str]:
        """Configured domains first, then learned ones, worst-scoring last."""
        known = {r["domain"]: r for r in self.rows(provider)}
        ordered: list[str] = []
        for domain in list(configured) + [d for d in known if d not in configured]:
            domain = (domain or "").strip().lower()
            if not domain or domain in ordered:
                continue
            row = known.get(domain)
            if row and row["bad_count"] > 0 and row["ok_count"] == 0:
                continue  # parked: it has only ever been refused
            ordered.append(domain)
        if ordered:
            ordered.sort(key=lambda d: -((known.get(d) or {}).get("ok_count", 0)))
            return ordered
        # Everything is parked: fall back to the configured list so a stale
        # scorecard cannot deadlock the run, and let the caller re-learn.
        return [d.strip().lower() for d in configured if d.strip()]

    def close(self) -> None:
        self.db.close()
