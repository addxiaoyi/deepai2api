"""CLI：run | doctor | probe | list | proxies | export | push."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .config import Config
from .errors import ProxyScrapeError
from .protocol import probe as protocol_probe
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
        "captcha_provider": args.captcha,
        "captcha_key": args.captcha_key,
        "browser_stack": args.browser_stack,
        "headless": not args.headful,
        "cdp": args.cdp,
    }
    cfg = Config.load(args.config, overrides)
    if args.count:
        cfg.count = max(1, int(args.count))
    if args.concurrency:
        cfg.concurrency = max(1, int(args.concurrency))
    if args.engine:
        cfg.engine = args.engine
    if args.proxies:
        cfg.proxies = list(args.proxies)
    if args.retry_rounds is not None:
        cfg.retry_rounds = max(0, int(args.retry_rounds))
    if args.delay:
        cfg.antiban.min_delay = float(args.delay[0])
        cfg.antiban.max_delay = float(args.delay[1])
    if args.output:
        cfg.output_dir = Path(args.output)
        cfg.db_path = cfg.output_dir / "accounts.sqlite3"
    if args.push_url:
        cfg.push_url = args.push_url
    if args.push_token:
        cfg.push_token = args.push_token
    if args.no_verify:
        cfg.verify_email = False
    if args.no_trial:
        cfg.claim_trial = False
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
        row.pop("access_token", None)
        row.pop("refresh_token", None)
    print(json.dumps({"counts": counts, "accounts": rows}, ensure_ascii=False, indent=2))
    return 0


def cmd_proxies(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    store = _store(cfg)
    try:
        rows = store.proxies(limit=args.limit)
    finally:
        store.close()
    print("\n".join(rows))
    return 0 if rows else 1


def cmd_export(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    store = _store(cfg)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = Path(args.out) if args.out else cfg.output_dir / "exports" / f"proxyscrape-{stamp}.{args.format}"
    try:
        if args.format == "proxies":
            rows = store.export_proxies(target)
        elif args.format == "jsonl":
            rows = store.export_jsonl(target)
        else:
            rows = store.export_csv(target)
    finally:
        store.close()
    print(json.dumps({"file": str(target), "rows": rows, "format": args.format}, ensure_ascii=False))
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    from .push import push_account

    cfg = _cfg(args)
    store = _store(cfg)
    pushed = 0
    try:
        for row in store.accounts():
            proxies = store.proxies(source_email=row["email"])
            push_account(
                cfg.push_url,
                cfg.push_token,
                {
                    "email": row["email"],
                    "password": row["password"],
                    "accessToken": row["access_token"],
                    "refreshToken": row["refresh_token"],
                    "trialClaimed": bool(row["trial_claimed"]),
                    "proxies": proxies,
                    "engine": row["engine"],
                    "createdAt": row["created_at"],
                },
            )
            pushed += 1
    finally:
        store.close()
    print(json.dumps({"pushed": pushed}, ensure_ascii=False))
    return 0 if pushed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="proxyscrape_reg", description="proxyscrape.com 试用注册机 + 代理池")
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-n", "--count", type=int, default=0)
        p.add_argument("-c", "--concurrency", type=int, default=0)
        p.add_argument("--engine", choices=["auto", "protocol", "browser"])
        p.add_argument("--mail-provider", choices=["imap", "fixed", "mailtm", "tempmail_lol"])
        p.add_argument("--mail-domain")
        p.add_argument("--mail-domains", nargs="+")
        p.add_argument("--mail-address")
        p.add_argument("--captcha", choices=["browser", "capsolver", "solver", "manual", "none"])
        p.add_argument("--captcha-key")
        p.add_argument("--browser-stack")
        p.add_argument("--headful", action="store_true")
        p.add_argument("--cdp", default="")
        p.add_argument("--proxy", dest="proxies", action="append")
        p.add_argument("--delay", nargs=2, type=float)
        p.add_argument("--retry-rounds", type=int, default=None)
        p.add_argument("--no-verify", action="store_true")
        p.add_argument("--no-trial", action="store_true")
        p.add_argument("--push-url")
        p.add_argument("--push-token")
        p.add_argument("--output")
        p.add_argument("--json", action="store_true")

    for name, func, help_text in (
        ("run", cmd_run, "批量注册并导出代理"),
        ("doctor", cmd_doctor, "体检：站点 / Turnstile / 号池计数"),
        ("probe", cmd_probe, "只打注册端点看门是否还在"),
    ):
        p = sub.add_parser(name, help=help_text)
        common(p)
        p.set_defaults(func=func)

    listing = sub.add_parser("list", help="列出账号（脱敏）")
    common(listing)
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(func=cmd_list)

    proxies = sub.add_parser("proxies", help="只打印代理串（一行一条）")
    common(proxies)
    proxies.add_argument("--limit", type=int, default=0)
    proxies.set_defaults(func=cmd_proxies)

    export = sub.add_parser("export", help="导出 proxies | jsonl | csv")
    common(export)
    export.add_argument("--format", choices=["proxies", "jsonl", "csv"], default="proxies")
    export.add_argument("--out")
    export.set_defaults(func=cmd_export)

    push = sub.add_parser("push", help="把账号+代理回推网关")
    common(push)
    push.set_defaults(func=cmd_push)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ProxyScrapeError as exc:
        print(json.dumps({"ok": False, "kind": exc.kind, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
