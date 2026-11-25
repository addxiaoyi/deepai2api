"""Command line surface for gabai-reg: run | doctor | probe | list | export | push.

Emits NDJSON events with --json, the same way allmodels-reg does.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config
from .errors import SignupError
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
    if getattr(args, "no_api_key", False):
        overrides["create_api_key"] = False
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
    cfg = _config_from(args, {"mail_domains": _flatten(getattr(args, "mail_domains", None)) or None})
    store = Store(cfg.db_path)

    def on_event(event: dict) -> None:
        if args.json:
            print(json.dumps(event, ensure_ascii=False), flush=True)

    try:
        runner = Runner(cfg, store, on_event=on_event, quiet=args.json)
        summary = runner.run(args.count)
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
    ok = report.get("checks", {}).get("signup_page")
    return 0 if ok else 1


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
        else:
            target = Path(args.out_file or cfg.output_dir / "accounts.jsonl")
            count = store.export_jsonl(target)
        print(json.dumps({"file": str(target), "rows": count}, ensure_ascii=False))
    finally:
        store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gabai-reg", description="Bulk gab.ai account factory")
    parser.add_argument("--config", help="YAML config path")
    sub = parser.add_subparsers(dest="command", required=True)

    def shared(p: argparse.ArgumentParser) -> None:
        p.add_argument("-o", "--out", help="output directory (default: out)")
        p.add_argument("--proxy", action="append", help="proxy, repeatable or comma separated")
        p.add_argument("--no-proxy", action="store_true", help="ignore env proxies")
        p.add_argument("--base-url", help="base URL (default https://gab.ai)")
        p.add_argument("--mail-provider", choices=["tempmail_lol", "mailtm", "imap", "fixed"])
        p.add_argument("--mail-domain")
        p.add_argument("--email", help="single fixed address (uses plus-addressing)")
        p.add_argument("--captcha", choices=["none", "manual", "browser"])
        p.add_argument("--captcha-key", help="solver api key (or literal token for manual)")
        p.add_argument("--engine", choices=["auto", "protocol", "browser"], help="auto: protocol first, blocked -> browser")
        p.add_argument("--browser-stack", choices=["auto", "camoufox", "playwright"])
        p.add_argument("--cdp", help="external fingerprint browser CDP URL")
        p.add_argument("--humanize", type=float)
        p.add_argument("--headful", action="store_true")

    run = sub.add_parser("run", help="create accounts")
    shared(run)
    run.add_argument("-n", "--count", type=int, default=1)
    run.add_argument("-c", "--concurrency", type=int, default=2)
    run.add_argument("--attempts", type=int, default=3)
    run.add_argument("--retry-rounds", type=int, default=None)
    run.add_argument("--no-verify", action="store_true")
    run.add_argument("--no-api-key", action="store_true")
    run.add_argument("--mail-domains", action="append")
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=cmd_run)

    doc = sub.add_parser("doctor", help="check signup page + mailbox + deps")
    shared(doc)
    doc.set_defaults(func=cmd_doctor)

    listing = sub.add_parser("list", help="show stored accounts")
    shared(listing)
    listing.add_argument("--limit", type=int, default=20)
    listing.set_defaults(func=cmd_list)

    export = sub.add_parser("export", help="write JSONL / CSV")
    shared(export)
    export.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    export.add_argument("--file", dest="out_file")
    export.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
