"""CLI：run | doctor | probe | list | keys | export | push | test-key。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .config import Config
from .errors import JevError
from .protocol import API_BASE, Jev, find_keys, probe as protocol_probe
from .runner import Runner
from .store import Store


def _json_out(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _cfg(args: argparse.Namespace) -> Config:
    overrides = {
        "mail_provider": args.mail_provider,
        "mail_domain": args.mail_domain,
        "mail_domains": args.mail_domains,
        "mail_address": args.mail_address,
    }
    cfg = Config.load(args.config, overrides)
    if args.count:
        cfg.count = max(1, int(args.count))
    if args.concurrency:
        cfg.concurrency = max(1, int(args.concurrency))
    if args.proxies:
        cfg.proxies = list(args.proxies)
    if args.retry_rounds is not None:
        cfg.retry_rounds = max(0, int(args.retry_rounds))
    if args.output:
        cfg.output_dir = Path(args.output)
        cfg.db_path = cfg.output_dir / "accounts.sqlite3"
    if args.push_url:
        cfg.push_url = args.push_url
    if args.push_token:
        cfg.push_token = args.push_token
    if args.key_name:
        cfg.key_name = args.key_name
    if args.no_verify:
        cfg.verify_email = False
    if args.no_checkin:
        cfg.checkin = False
    return cfg


def _store(cfg: Config) -> Store:
    return Store(cfg.db_path or cfg.output_dir / "accounts.sqlite3")


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    store = _store(cfg)
    runner = Runner(cfg, store, on_event=_json_out if args.json else None, quiet=args.json)
    try:
        summary = runner.run(cfg.count)
    finally:
        store.close()
    if not args.json:
        print(json.dumps({k: v for k, v in summary.items() if k != "accounts"}, ensure_ascii=False, indent=2))
    return 0 if summary["created"] else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    store = _store(cfg)
    runner = Runner(cfg, store, quiet=True)
    try:
        report = runner.doctor()
    finally:
        store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    print(json.dumps(protocol_probe(proxy=cfg.proxy_for(0)), ensure_ascii=False, indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    store = _store(cfg)
    try:
        rows = store.accounts(args.limit)
        counts = store.counts()
    finally:
        store.close()
    for row in rows:
        row.pop("password", None)
        row["api_key"] = (row.get("api_key") or "")[:12] + "…" if row.get("api_key") else ""
    print(json.dumps({"counts": counts, "accounts": rows}, ensure_ascii=False, indent=2))
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    store = _store(cfg)
    try:
        rows = store.keys()
    finally:
        store.close()
    print("\n".join(rows))
    return 0 if rows else 1


def cmd_export(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    store = _store(cfg)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fmt = args.format
    ext = {"keys": "txt", "jsonl": "jsonl", "csv": "csv"}[fmt]
    target = Path(args.out) if args.out else cfg.output_dir / "exports" / f"jevai-{stamp}.{ext}"
    try:
        if fmt == "keys":
            rows = store.export_keys(target)
        elif fmt == "jsonl":
            rows = store.export_jsonl(target)
        else:
            rows = store.export_csv(target)
    finally:
        store.close()
    print(json.dumps({"file": str(target), "rows": rows, "format": fmt, "base": API_BASE}, ensure_ascii=False))
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    from .push import push_account

    cfg = _cfg(args)
    store = _store(cfg)
    pushed = 0
    try:
        for row in store.pool_records():
            push_account(cfg.push_url, cfg.push_token, row)
            pushed += 1
    finally:
        store.close()
    print(json.dumps({"pushed": pushed}, ensure_ascii=False))
    return 0 if pushed else 1


def cmd_test_key(args: argparse.Namespace) -> int:
    """用一把 Key 打 `/api/v1/models`，确认还能用。"""
    import httpx

    key = str(args.key or "")
    if not key:
        cfg = _cfg(args)
        store = _store(cfg)
        try:
            keys = store.keys()
        finally:
            store.close()
        key = keys[0] if keys else ""
    if not key:
        print(json.dumps({"ok": False, "error": "no key in pool"}, ensure_ascii=False))
        return 1
    try:
        response = httpx.get(
            f"{API_BASE}/models", headers={"Authorization": f"Bearer {key}"}, timeout=30, trust_env=False
        )
        head = (response.text or "")[:200]
        print(json.dumps({"ok": response.status_code < 400, "status": response.status_code, "body": head}, ensure_ascii=False))
        return 0 if response.status_code < 400 else 1
    except httpx.HTTPError as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False))
        return 1



def cmd_checkin(args: argparse.Namespace) -> int:
    """把池子里每个账号挨个签到：这是"免费额度"的主要来源。

    流程：sign-in（拿新 session）→ 读签到前额度 → POST /api/user/checkin →
    读签到后额度 → 落库。用 `--only-today` 跳过今天已经签过的。
    """
    cfg = _cfg(args)
    store = _store(cfg)
    rows = store.checkin_state()
    if args.limit:
        rows = rows[: int(args.limit)]
    today = time.strftime("%Y-%m-%d")
    done = 0
    gained_total = 0.0
    results: list[dict[str, Any]] = []
    try:
        for row in rows:
            email = row["email"]
            if args.only_today and str(row.get("last_checkin") or "").startswith(today):
                results.append({"email": email, "skipped": "already-checked-in-today"})
                continue
            account = next((a for a in store.accounts() if a["email"] == email), None)
            if not account or not account.get("password"):
                results.append({"email": email, "error": "no-password-stored"})
                continue
            proxy = cfg.proxy_for(done)
            client = None
            try:
                client = Jev(proxy=proxy, timeout=cfg.timeout)
                client.login(email, account["password"])
                before = _credit_value(client.credit_status() or client.credits())
                payload = client.checkin()
                after = _credit_value(client.credit_status() or client.credits())
                gained = round(after - before, 3)
                gained_total += max(0.0, gained)
                store.record_checkin(email, credits={"credits": after, "gained": gained})
                done += 1
                results.append({"email": email, "ok": True, "before": before, "after": after, "gained": gained, "raw": payload})
                if args.json:
                    _json_out({"type": "checkin", "email": email, "gained": gained, "after": after})
                else:
                    print(f"  {email}: +{gained} -> {after}")
            except JevError as exc:
                results.append({"email": email, "error": exc.kind, "reason": str(exc)[:200]})
                if args.json:
                    _json_out({"type": "checkin_failed", "email": email, "kind": exc.kind, "reason": str(exc)[:200]})
            finally:
                if client is not None:
                    client.close()
            time.sleep(max(0.0, args.pause))
    finally:
        store.close()
    summary = {"checked": done, "gained": round(gained_total, 3), "results": results}
    if args.json:
        _json_out({"type": "done", "summary": summary})
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if done else 1


def _credit_value(payload: dict[str, Any]) -> float:
    """从各形状里抠出"当前额度"这一个数（没有就 0）。"""
    if not isinstance(payload, dict):
        return 0.0
    for field in ("credits", "remainingCredits", "remaining", "balance", "credit", "total"):
        value = payload.get(field)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):
            for inner in ("remainingCredits", "remaining", "balance", "credits", "total"):
                deep = value.get(inner)
                if isinstance(deep, (int, float)):
                    return float(deep)
    for key in ("remainingCredits", "remaining", "balance"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jevai_reg", description="jev-ai.org 注册机 + Key 池")
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-n", "--count", type=int, default=0)
        p.add_argument("-c", "--concurrency", type=int, default=0)
        p.add_argument("--mail-provider", choices=["imap", "fixed", "mailtm", "tempmail_lol"])
        p.add_argument("--mail-domain")
        p.add_argument("--mail-domains", nargs="+")
        p.add_argument("--mail-address")
        p.add_argument("--key-name")
        p.add_argument("--proxy", dest="proxies", action="append")
        p.add_argument("--retry-rounds", type=int, default=None)
        p.add_argument("--no-verify", action="store_true")
        p.add_argument("--no-checkin", action="store_true")
        p.add_argument("--push-url")
        p.add_argument("--push-token")
        p.add_argument("--output")
        p.add_argument("--json", action="store_true")

    for name, func, help_text in (
        ("run", cmd_run, "批量注册并建 Key"),
        ("doctor", cmd_doctor, "体检：站点 / 认证面 / 号池计数 / Key 端点缓存"),
        ("probe", cmd_probe, "只打认证与模型 API 探针"),
    ):
        p = sub.add_parser(name, help=help_text)
        common(p)
        p.set_defaults(func=func)

    listing = sub.add_parser("list", help="列出账号（脱敏）")
    common(listing)
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(func=cmd_list)

    keys = sub.add_parser("keys", help="打印池里的 Key")
    common(keys)
    keys.set_defaults(func=cmd_keys)

    export = sub.add_parser("export", help="导出 keys | jsonl | csv")
    common(export)
    export.add_argument("--format", choices=["keys", "jsonl", "csv"], default="keys")
    export.add_argument("--out")
    export.set_defaults(func=cmd_export)

    push = sub.add_parser("push", help="回推网关号池")
    common(push)
    push.set_defaults(func=cmd_push)

    checkin = sub.add_parser("checkin", help="给池里每个账号签到（免费额度的主要来源）")
    common(checkin)
    checkin.add_argument("--only-today", action="store_true", help="跳过今天已签到的")
    checkin.add_argument("--limit", type=int, default=0)
    checkin.add_argument("--pause", type=float, default=3.0, help="每个账号之间的间隔秒数")
    checkin.set_defaults(func=cmd_checkin)

    test_key = sub.add_parser("test-key", help="用池里的 Key 打一次 /api/v1/models")
    common(test_key)
    test_key.add_argument("--key", default="")
    test_key.set_defaults(func=cmd_test_key)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except JevError as exc:
        print(json.dumps({"ok": False, "kind": exc.kind, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
