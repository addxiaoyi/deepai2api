"""Command line surface: run | doctor | list | export | push | probe."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import Config
from .errors import SignupError
from .push import push_account
from .runner import Runner, doctor
from .store import Store


def _flatten(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def _config_from(args: argparse.Namespace, extra: dict | None = None) -> Config:
    overrides: dict = {
        "engine": getattr(args, "engine", None),
        "count": getattr(args, "count", None),
        "concurrency": getattr(args, "concurrency", None),
        "attempts": getattr(args, "attempts", None),
        "retry_rounds": getattr(args, "retry_rounds", None),
        "base_url": getattr(args, "base_url", None),
        "keys_per_account": getattr(args, "keys_per_account", None),
        "output_dir": Path(args.out) if getattr(args, "out", None) else None,
        "mail_provider": getattr(args, "mail_provider", None),
        "mail_domain": getattr(args, "mail_domain", None),
        "captcha_provider": getattr(args, "captcha", None),
        "captcha_key": getattr(args, "captcha_key", None),
        "browser_stack": getattr(args, "browser_stack", None),
        "cdp": getattr(args, "cdp", None),
        "humanize": getattr(args, "humanize", None),
    }
    if getattr(args, "headful", False):
        overrides["headless"] = False
    if getattr(args, "email", None):
        overrides["mail_provider"] = "fixed"
        overrides["mail_address"] = args.email
    if getattr(args, "no_verify", False):
        overrides["verify_email"] = False
    if getattr(args, "proxy", None):
        overrides["proxies"] = _flatten(args.proxy)
    if getattr(args, "no_proxy", False):
        overrides["proxies"] = []
    if getattr(args, "push_url", None):
        overrides["push_url"] = args.push_url
    if getattr(args, "push_token", None):
        overrides["push_token"] = args.push_token
    overrides.update(extra or {})
    return Config.load(getattr(args, "config", None), overrides)


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _config_from(
        args,
        {"mail_domains": _flatten(getattr(args, "mail_domains", None)) or None},
    )
    store = Store(cfg.db_path)
    pushed = {"ok": 0, "failed": 0}

    def on_event(event: dict) -> None:
        if args.json:
            print(json.dumps(event, ensure_ascii=False), flush=True)
        if event.get("type") != "account":
            return
        account = event["account"]
        if not (cfg.push_url and cfg.push_token):
            return
        try:
            result = push_account(cfg.push_url, cfg.push_token, account)
            pushed["ok"] += 1
            line = {"type": "pushed", "email": account["email"], "at": event.get("at")}
            if isinstance(result, dict) and result.get("gatewayKey"):
                line["gatewayKey"] = result["gatewayKey"].get("prefix", "")
            if args.json:
                print(json.dumps(line, ensure_ascii=False), flush=True)
            else:
                print(f"[push] 已入池 {account['email']}", flush=True)
        except SignupError as exc:
            pushed["failed"] += 1
            detail = {"type": "push_error", "email": account["email"], "message": str(exc)}
            if args.json:
                print(json.dumps(detail, ensure_ascii=False), flush=True)
            else:
                print(f"[push] 推送失败 {account['email']}: {exc}", file=sys.stderr, flush=True)

    try:
        runner = Runner(cfg, store, on_event=on_event, quiet=args.json)
        summary = runner.run(args.count)
        summary["pushed"] = pushed
        if args.json:
            return 0 if summary["created"] else 1
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["created"] else 1
    finally:
        store.close()


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _config_from(args)
    report = doctor(cfg)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    dashboard = report["checks"].get("dashboard")
    fatal = not isinstance(dashboard, dict) or not dashboard.get("ok")
    return 1 if fatal else 0


def cmd_probe(args: argparse.Namespace) -> int:
    from .protocol import probe

    cfg = _config_from(args)
    print(json.dumps(probe(cfg.base_url, proxy=cfg.proxies[0] if cfg.proxies else ""), ensure_ascii=False, indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = _config_from(args)
    store = Store(cfg.db_path)
    try:
        print(json.dumps({"total": store.count(), "rows": store.rows(args.limit)}, ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cfg = _config_from(args)
    store = Store(cfg.db_path)
    try:
        if args.format == "csv":
            target = Path(args.out_file or cfg.output_dir / "accounts.csv")
            count = store.export_csv(target)
        elif args.format == "pool":
            target = Path(args.out_file or cfg.output_dir / "pool-accounts.json")
            rows = store.pool_records()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps({"version": 1, "provider": "allmodels", "accounts": rows}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            count = len(rows)
        else:
            target = Path(args.out_file or cfg.output_dir / "accounts.jsonl")
            count = store.export_jsonl(target)
        print(json.dumps({"file": str(target), "rows": count}, ensure_ascii=False))
    finally:
        store.close()
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    cfg = _config_from(args)
    if not (cfg.push_url and cfg.push_token):
        print("需要 --push-url 和 --push-token（或 ALLMODELS_GATEWAY_URL / ALLMODELS_GATEWAY_TOKEN）", file=sys.stderr)
        return 2
    store = Store(cfg.db_path)
    ok = failed = 0
    try:
        for record in store.pool_records():
            try:
                push_account(cfg.push_url, cfg.push_token, record)
                ok += 1
                print(f"[push] {record['email']} -> pool", flush=True)
            except SignupError as exc:
                failed += 1
                print(f"[push] {record['email']} failed: {exc}", file=sys.stderr, flush=True)
    finally:
        store.close()
    print(json.dumps({"pushed": ok, "failed": failed}, ensure_ascii=False))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="allmodels-reg",
        description="Bulk Allmodels dashboard account factory + gateway pool export.",
    )
    parser.add_argument("--config", help="YAML config path (default: ./config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    def shared(p: argparse.ArgumentParser) -> None:
        p.add_argument("-o", "--out", help="output directory (default: out)")
        p.add_argument("--proxy", action="append", help="proxy, repeatable or comma separated")
        p.add_argument("--no-proxy", action="store_true", help="ignore env proxies")
        p.add_argument("--base-url", help="dashboard base URL (default https://dash.allmodels.io)")
        p.add_argument("--mail-provider", choices=["tempmail_lol", "mailtm", "imap", "fixed"])
        p.add_argument("--mail-domain")
        p.add_argument("--email", help="single fixed address (uses plus-addressing)")
        p.add_argument("--captcha", choices=["none", "capsolver", "2captcha", "browser", "manual"])
        p.add_argument("--captcha-key", help="solver api key (or a literal token for --captcha manual)")
        p.add_argument("--engine", choices=["auto", "protocol", "browser"], help="auto: 协议优先，被拦切指纹浏览器")
        p.add_argument("--browser-stack", choices=["auto", "camoufox", "patchright", "playwright"])
        p.add_argument("--cdp", help="接管外部指纹浏览器的 CDP 地址，如 http://127.0.0.1:50325")
        p.add_argument("--humanize", type=float, help="打字/移动/停顿的倍率（默认 1.0）")
        p.add_argument("--headful", action="store_true", help="显示浏览器窗口")

    run = sub.add_parser("run", help="create accounts")
    shared(run)
    run.add_argument("-n", "--count", type=int, default=1)
    run.add_argument("-c", "--concurrency", type=int, default=2)
    run.add_argument("--attempts", type=int, default=3)
    run.add_argument("--retry-rounds", type=int, default=None)
    run.add_argument("--keys-per-account", type=int, default=None, help="extra API keys minted per account")
    run.add_argument("--no-verify", action="store_true", help="skip the mailbox verification loop")
    run.add_argument("--mail-domains", action="append")
    run.add_argument("--json", action="store_true", help="emit NDJSON events on stdout (used by the gateway)")
    run.add_argument("--push-url", help="gateway base URL; each account is POSTed into its pool")
    run.add_argument("--push-token", help="gateway admin token for --push-url")
    run.set_defaults(func=cmd_run)

    doc = sub.add_parser("doctor", help="check dashboard reachability, mailbox, solver")
    shared(doc)
    doc.set_defaults(func=cmd_doctor)

    pr = sub.add_parser("probe", help="print the dashboard endpoints this tool relies on")
    shared(pr)
    pr.set_defaults(func=cmd_probe)

    listing = sub.add_parser("list", help="show stored accounts")
    shared(listing)
    listing.add_argument("--limit", type=int, default=20)
    listing.set_defaults(func=cmd_list)

    export = sub.add_parser("export", help="write JSONL / CSV / pool JSON")
    shared(export)
    export.add_argument("--format", choices=["jsonl", "csv", "pool"], default="jsonl")
    export.add_argument("--file", dest="out_file")
    export.set_defaults(func=cmd_export)

    push = sub.add_parser("push", help="re-push every stored account into the gateway pool")
    shared(push)
    push.add_argument("--push-url")
    push.add_argument("--push-token")
    push.set_defaults(func=cmd_push)

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:  # pragma: no cover - non-standard streams
        pass
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)
