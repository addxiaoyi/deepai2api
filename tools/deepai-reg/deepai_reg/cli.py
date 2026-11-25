"""Command line surface: run | doctor | mail | list | export."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .browser import _proxy_dict
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
        "output_dir": Path(args.out) if getattr(args, "out", None) else None,
        "mail_provider": getattr(args, "mail_provider", None),
        "mail_domain": getattr(args, "mail_domain", None),
    }
    if getattr(args, "email", None):
        overrides["mail_provider"] = "fixed"
        overrides["mail_address"] = args.email
    if getattr(args, "headful", False):
        overrides["headless"] = False
    if getattr(args, "no_verify", False):
        overrides["verify_email"] = False
    if getattr(args, "proxy", None):
        overrides["proxies"] = _flatten(args.proxy)
    if getattr(args, "no_proxy", False):
        overrides["proxies"] = []
    overrides.update(extra or {})
    return Config.load(getattr(args, "config", None), overrides)


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _config_from(
        args,
        {
            "mail_domains": _flatten(getattr(args, "mail_domains", None)) or None,
            "mail_auto_domains": False if getattr(args, "no_auto_domains", False) else None,
        },
    )
    store = Store(cfg.db_path)

    def on_event(event: dict) -> None:
        if args.json:
            print(json.dumps(event, ensure_ascii=False), flush=True)
        if event.get("type") != "account":
            return
        account = event["account"]
        if not (args.push_url and args.push_token):
            return
        try:
            push_account(args.push_url, args.push_token, account)
            if args.json:
                print(
                    json.dumps({"type": "pushed", "email": account["email"], "at": event.get("at")}),
                    flush=True,
                )
            else:
                print(f"[push] 已入池 {account['email']}", flush=True)
        except SignupError as exc:
            detail = {"type": "push_error", "email": account["email"], "message": str(exc)}
            if args.json:
                print(json.dumps(detail, ensure_ascii=False), flush=True)
            else:
                print(f"[push] 推送失败 {account['email']}: {exc}", file=sys.stderr, flush=True)

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
    checks = report["checks"]
    fatal = any("unreachable" in str(v) or str(v).startswith("failed") for v in checks.values())
    return 1 if fatal else 0


def cmd_mail(args: argparse.Namespace) -> int:
    from .mailbox import build_provider

    cfg = _config_from(args)
    proxy = cfg.proxies[0] if cfg.proxies else ""
    provider = build_provider(cfg.mail, proxy=proxy)
    try:
        box = provider.allocate()
        print(json.dumps({"provider": box.provider, "address": box.address}, ensure_ascii=False))
        if args.wait:
            deadline = time.time() + args.wait
            while time.time() < deadline:
                for message in provider.messages(box):
                    print(f"--- {message.sender} | {message.subject}")
                    print((message.body or message.html)[:400])
                time.sleep(6)
    finally:
        provider.close()
    return 0


def cmd_browser_check(args: argparse.Namespace) -> int:
    """Launch Camoufox with the same options the engine uses and report the identity."""
    cfg = _config_from(args)
    from camoufox.sync_api import Camoufox

    proxy = cfg.proxies[0] if cfg.proxies else ""
    launch: dict = {
        "headless": not args.headful,
        "humanize": True,
        "os": ["windows"],
        "locale": [cfg.language],
        "geoip": bool(cfg.geoip and proxy),
        "block_webrtc": True,
    }
    if proxy:
        launch["proxy"] = _proxy_dict(proxy)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    probe_js = """() => ({
      ua: navigator.userAgent,
      platform: navigator.platform,
      languages: navigator.languages,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      webdriver: navigator.webdriver,
      hardwareConcurrency: navigator.hardwareConcurrency,
      deviceMemory: navigator.deviceMemory || null,
      screen: [screen.width, screen.height, devicePixelRatio],
      canvas: (() => {
        const c = document.createElement('canvas');
        const g = c.getContext('2d');
        g.fillText('deepai-reg', 2, 12);
        return c.toDataURL().slice(-48);
      })(),
    })"""

    with Camoufox(**launch) as browser:
        contexts = list(getattr(browser, "contexts", []) or [])
        context = contexts[0] if contexts else browser.new_context()
        page = context.new_page()
        page.goto("https://deepai.org/", wait_until="domcontentloaded", timeout=90_000)
        identity = page.evaluate(probe_js)
        title = page.title()
        page.screenshot(path=str(cfg.output_dir / "browser-check.png"), full_page=False)

    print(
        json.dumps(
            {"title": title, "identity": identity, "screenshot": str(cfg.output_dir / "browser-check.png")},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = _config_from(args)
    store = Store(cfg.db_path)
    try:
        rows = store.rows(args.limit)
        print(json.dumps({"total": store.count(), "rows": rows}, ensure_ascii=False, indent=2))
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
    parser = argparse.ArgumentParser(
        prog="deepai-reg",
        description="Bulk DeepAI account factory: protocol engine, Camoufox fallback, gateway export.",
    )
    parser.add_argument("--config", help="YAML config path (default: ./config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    def shared(p: argparse.ArgumentParser) -> None:
        p.add_argument("-o", "--out", help="output directory (default: out)")
        p.add_argument("--proxy", action="append", help="proxy, repeatable or comma separated")
        p.add_argument("--no-proxy", action="store_true", help="ignore env proxies")
        p.add_argument("--mail-provider", choices=["tempmail_lol", "mailtm", "imap", "fixed"])
        p.add_argument("--mail-domain")
        p.add_argument("--email", help="single fixed address (uses plus-addressing)")

    run = sub.add_parser("run", help="create accounts")
    shared(run)
    run.add_argument("-n", "--count", type=int, default=1)
    run.add_argument("--engine", choices=["protocol", "browser", "auto"], default="auto")
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--attempts", type=int, default=3)
    run.add_argument("--headful", action="store_true", help="show the Camoufox window")
    run.add_argument("--no-verify", action="store_true", help="skip mailbox polling")
    run.add_argument("--mail-domains", action="append", help="domain rotation pool, repeatable or comma separated")
    run.add_argument("--no-auto-domains", action="store_true", help="stop learning which domains work")
    run.add_argument("--json", action="store_true", help="emit NDJSON events on stdout (used by the gateway)")
    run.add_argument("--push-url", help="gateway base URL; each new account is POSTed into its pool")
    run.add_argument("--push-token", help="gateway admin token for --push-url")
    run.set_defaults(func=cmd_run)

    doc = sub.add_parser("doctor", help="check deps, proxy egress, endpoints, mailbox")
    shared(doc)
    doc.set_defaults(func=cmd_doctor)

    mail = sub.add_parser("mail", help="allocate one mailbox (and optionally tail it)")
    shared(mail)
    mail.add_argument("--wait", type=int, default=0, help="seconds to tail the inbox")
    mail.set_defaults(func=cmd_mail)

    check = sub.add_parser("browser-check", help="launch Camoufox and print the identity it presents")
    shared(check)
    check.add_argument("--headful", action="store_true")
    check.set_defaults(func=cmd_browser_check)

    listing = sub.add_parser("list", help="show stored accounts")
    shared(listing)
    listing.add_argument("--limit", type=int, default=20)
    listing.set_defaults(func=cmd_list)

    export = sub.add_parser("export", help="write JSONL/CSV for the gateway")
    shared(export)
    export.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    export.add_argument("--file", dest="out_file")
    export.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:  # pragma: no cover - non-standard streams
        pass
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)
