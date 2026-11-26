"""通用 CLI：`run | doctor | keys | list | export | push | probe | test-key`。

每条线只需要在自己的 `cli.py` 里调用 `main(SITE, sys.argv[1:])`。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .config import Config
from .errors import LineError
from .net import build_client, request
from .captcha import resolve as resolve_captcha
from .runner import Runner
from .session import Session
from .site import Site
from .store import Store


def _json_out(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _cfg(args: argparse.Namespace, site: Site) -> Config:
    overrides = {
        "mail_provider": args.mail_provider,
        "mail_domain": args.mail_domain,
        "mail_domains": args.mail_domains,
        "mail_address": args.mail_address,
    }
    cfg = Config.load(args.config, overrides)
    if site.base_url:
        cfg.base_url = args.base_url or site.base_url
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


def _captcha_hook(args: argparse.Namespace, site: Site):
    """把 `--captcha/--ticket/--randstr` 变成 Runner 用的 hook。"""
    if not site.captcha_params:
        return None
    provider = str(getattr(args, "captcha", "auto") or "auto")
    if provider == "none":
        return None
    values = {k: str(getattr(args, k, "") or "") for k in site.captcha_params}

    def hook(email: str, proxy: str) -> dict[str, str]:
        return resolve_captcha(
            site,
            provider=provider,
            values=values,
            email=email,
            proxy=proxy,
            headless=not bool(getattr(args, "headful", False)),
        )

    return hook


def cmd_run(args: argparse.Namespace, site: Site) -> int:
    cfg = _cfg(args, site)
    store = _store(cfg)
    runner = Runner(
        site,
        cfg,
        store,
        on_event=_json_out if args.json else None,
        quiet=args.json,
        captcha_hook=_captcha_hook(args, site),
    )
    try:
        summary = runner.run(cfg.count)
    finally:
        store.close()
    if not args.json:
        print(json.dumps({k: v for k, v in summary.items() if k != "accounts"}, ensure_ascii=False, indent=2))
    return 0 if summary["created"] else 1


def cmd_doctor(args: argparse.Namespace, site: Site) -> int:
    cfg = _cfg(args, site)
    store = _store(cfg)
    runner = Runner(site, cfg, store, quiet=True)
    try:
        report = runner.doctor()
    finally:
        store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_probe(args: argparse.Namespace, site: Site) -> int:
    cfg = _cfg(args, site)
    client = build_client(proxy=cfg.proxy_for(0), timeout=20.0)
    out: dict[str, Any] = {"site": site.describe()}
    try:
        for label, url in (
            ("health", site.url(site.health_path) if site.health_path else ""),
            ("session", site.url(site.session_path)),
            ("models", f"{site.api_base.rstrip('/')}/models"),
        ):
            if not url:
                continue
            try:
                response = request(client, "GET", url)
                out[label] = {"url": url, "status": response.status_code, "body": (response.text or "")[:200]}
            except LineError as exc:
                out[label] = {"url": url, "error": str(exc)[:200]}
    finally:
        client.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    models = out.get("models") or {}
    return 0 if models.get("status", 599) < 500 else 1


def cmd_list(args: argparse.Namespace, site: Site) -> int:
    cfg = _cfg(args, site)
    store = _store(cfg)
    try:
        rows = store.accounts(args.limit)
        counts = store.counts()
    finally:
        store.close()
    for row in rows:
        row.pop("password", None)
        row["api_key"] = (row.get("api_key") or "")[:12] + "…" if row.get("api_key") else ""
    print(json.dumps({"line": site.id, "counts": counts, "accounts": rows}, ensure_ascii=False, indent=2))
    return 0


def cmd_keys(args: argparse.Namespace, site: Site) -> int:
    cfg = _cfg(args, site)
    store = _store(cfg)
    try:
        rows = store.keys()
    finally:
        store.close()
    print("\n".join(rows))
    return 0 if rows else 1


def cmd_export(args: argparse.Namespace, site: Site) -> int:
    cfg = _cfg(args, site)
    store = _store(cfg)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fmt = args.format
    ext = {"keys": "txt", "jsonl": "jsonl", "csv": "csv"}[fmt]
    target = Path(args.out) if args.out else cfg.output_dir / "exports" / f"{site.id}-{stamp}.{ext}"
    try:
        if fmt == "keys":
            rows = store.export_keys(target)
        elif fmt == "jsonl":
            rows = store.export_jsonl(target)
        else:
            rows = store.export_csv(target)
    finally:
        store.close()
    print(json.dumps({"line": site.id, "file": str(target), "rows": rows, "format": fmt, "base": site.api_base}, ensure_ascii=False))
    return 0


def cmd_push(args: argparse.Namespace, site: Site) -> int:
    from .push import push_account

    cfg = _cfg(args, site)
    store = _store(cfg)
    pushed = 0
    try:
        for row in store.pool_records():
            push_account(cfg.push_url, cfg.push_token, row, line=site.id)
            pushed += 1
    finally:
        store.close()
    print(json.dumps({"line": site.id, "pushed": pushed}, ensure_ascii=False))
    return 0 if pushed else 1


def cmd_test_key(args: argparse.Namespace, site: Site) -> int:
    cfg = _cfg(args, site)
    key = str(args.key or "")
    if not key:
        store = _store(cfg)
        try:
            keys = store.keys()
        finally:
            store.close()
        key = keys[0] if keys else ""
    client = build_client(proxy=cfg.proxy_for(0), timeout=30.0)
    try:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        response = request(client, "GET", f"{site.api_base.rstrip('/')}/models", headers=headers)
        print(
            json.dumps(
                {"line": site.id, "ok": response.status_code < 400, "status": response.status_code, "body": (response.text or "")[:200]},
                ensure_ascii=False,
            )
        )
        return 0 if response.status_code < 400 else 1
    except LineError as exc:
        print(json.dumps({"line": site.id, "ok": False, "error": str(exc)[:200]}, ensure_ascii=False))
        return 1
    finally:
        client.close()


def build_parser(site: Site) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{site.id}_reg", description=f"{site.name} 注册机")
    parser.add_argument("--config")
    parser.add_argument("--base-url", default="")
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
        # 验证码门槛（Agnes = 腾讯云验证码 ticket+randstr）
        p.add_argument("--captcha", choices=["auto", "manual", "browser", "none"], default="auto")
        p.add_argument("--ticket", default="")
        p.add_argument("--randstr", default="")
        p.add_argument("--headful", action="store_true")
        p.add_argument("--push-url")
        p.add_argument("--push-token")
        p.add_argument("--output")
        p.add_argument("--json", action="store_true")

    for name, func, help_text in (
        ("run", cmd_run, "批量注册并发 Key"),
        ("doctor", cmd_doctor, "体检：站点 / 认证面 / 号池计数"),
        ("probe", cmd_probe, "只打探针（health / session / models）"),
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

    test_key = sub.add_parser("test-key", help="用池里的 Key 打一次 /models")
    common(test_key)
    test_key.add_argument("--key", default="")
    test_key.set_defaults(func=cmd_test_key)

    return parser


def main(site: Site, argv: list[str] | None = None) -> int:
    parser = build_parser(site)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args, site))
    except LineError as exc:
        print(json.dumps({"ok": False, "kind": exc.kind, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
