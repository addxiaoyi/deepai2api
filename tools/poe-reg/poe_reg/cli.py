"""CLI: run | doctor | pool | export."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import Config
from .pool import Pool
from .runner import Runner


def _flatten(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def _config(args: argparse.Namespace, extra: dict | None = None) -> Config:
    overrides: dict = {
        "engine": getattr(args, "engine", None),
        "count": getattr(args, "count", None),
        "concurrency": getattr(args, "concurrency", None),
        "attempts": getattr(args, "attempts", None),
        "output_dir": Path(args.out) if getattr(args, "out", None) else None,
        "mail_provider": getattr(args, "mail_provider", None),
        "mail_domain": getattr(args, "mail_domain", None),
        "mail_domains": _flatten(getattr(args, "mail_domains", None)) or None,
        "sms_provider": getattr(args, "sms_provider", None),
    }
    if getattr(args, "headful", False):
        overrides["headless"] = False
    if getattr(args, "proxy", None):
        overrides["proxies"] = _flatten(args.proxy)
    overrides.update(extra or {})
    return Config.load(getattr(args, "config", None), overrides)


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _config(args)
    pool = Pool(cfg.db_path)

    def on_event(event: dict) -> None:
        if args.json:
            print(json.dumps(event, ensure_ascii=False), flush=True)

    try:
        runner = Runner(
            cfg,
            pool,
            on_event=on_event,
            quiet=args.json,
            push_url=args.push_url or "",
            push_token=args.push_token or "",
        )
        summary = runner.run(args.count)
        if args.json:
            return 0 if summary["created"] else 1
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["created"] else 1
    finally:
        pool.close()


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _config(args)
    report: dict = {"config": cfg.describe(), "checks": {}}
    checks = report["checks"]

    try:
        import camoufox  # noqa: F401

        checks["camoufox_python"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["camoufox_python"] = f"missing: {exc}"

    try:
        from camoufox.pkgman import installed_verstr

        checks["camoufox_browser"] = installed_verstr()
    except Exception as exc:  # noqa: BLE001
        checks["camoufox_browser"] = f"not installed: {exc}"

    from . import gql_templates

    checks["templates"] = {
        name: template["hash"][:12] + "…" for name, template in gql_templates.TEMPLATES.items()
    }

    proxy = cfg.proxies[0] if cfg.proxies else ""
    from .net import build_client, request

    client = build_client(proxy=proxy, timeout=25)
    try:
        home = request(client, "GET", "https://poe.com/", headers={"Accept": "text/html"})
        checks["poe_edge"] = f"HTTP {home.status_code} ({'cloudflare' if home.status_code == 403 else 'open'})"
        ip = request(client, "GET", "https://api.ipify.org?format=json")
        checks["egress_ip"] = ip.text[:64]
    except Exception as exc:  # noqa: BLE001
        checks["poe_edge"] = f"unreachable: {exc}"
    finally:
        client.close()

    try:
        from .mail import provider_for

        provider = provider_for(cfg.mail, proxy=proxy)
        box = provider.allocate()
        checks["mailbox"] = f"{box.provider}: {box.address}"
        provider.close()
    except Exception as exc:  # noqa: BLE001
        checks["mailbox"] = f"failed: {exc}"

    checks["sms"] = cfg.sms.provider if cfg.sms.enabled else "off (accounts stop at phone_required)"

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_pool(args: argparse.Namespace) -> int:
    cfg = _config(args)
    pool = Pool(cfg.db_path)
    try:
        if args.export:
            path = Path(args.export)
            rows = pool.export_jsonl(path)
            print(json.dumps({"ok": True, "file": str(path), "rows": rows}, ensure_ascii=False))
            return 0
        payload = {
            "total": pool.count(),
            "statuses": pool.counts_by_status(),
            "rows": pool.rows(args.limit),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        pool.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="poe-reg",
        description="Poe account factory: in-page GraphQL protocol + Camoufox DOM, independent pool.",
    )
    parser.add_argument("--config", help="YAML config path")
    sub = parser.add_subparsers(dest="command", required=True)

    def shared(p: argparse.ArgumentParser) -> None:
        p.add_argument("-o", "--out", help="output directory (default: out)")
        p.add_argument("--proxy", action="append", help="proxy, repeatable or comma separated")
        p.add_argument("--mail-provider", choices=["tempmail_lol", "mailtm", "imap", "fixed"])
        p.add_argument("--mail-domain")
        p.add_argument("--mail-domains", action="append")
        p.add_argument("--sms-provider", choices=["off", "manual", "sms_activate", "smspool"])

    run = sub.add_parser("run", help="create accounts")
    shared(run)
    run.add_argument("-n", "--count", type=int, default=1)
    run.add_argument("--engine", choices=["protocol", "browser", "auto"], default="auto")
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument("--attempts", type=int, default=2)
    run.add_argument("--headful", action="store_true")
    run.add_argument("--json", action="store_true", help="NDJSON events on stdout")
    run.add_argument("--push-url")
    run.add_argument("--push-token")
    run.set_defaults(func=cmd_run)

    doc = sub.add_parser("doctor", help="check deps, edge reachability, mailbox, templates")
    shared(doc)
    doc.set_defaults(func=cmd_doctor)

    pool = sub.add_parser("pool", help="inspect / export the Poe pool")
    shared(pool)
    pool.add_argument("--limit", type=int, default=20)
    pool.add_argument("--export", help="write JSONL to this path")
    pool.set_defaults(func=cmd_pool)

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:  # pragma: no cover
        pass
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)
