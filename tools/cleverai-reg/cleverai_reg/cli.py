"""CLI: run | doctor | probe | list | domains | export | push | chat."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .appcheck import AppCheckVault, probe as appcheck_probe
from .browser import available_stacks
from .config import Config
from .errors import SignupError
from .protocol import CleverAI, dialog_message, probe as protocol_probe, text_of
from .runner import Runner
from .store import Store


def _json_out(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _config(args: argparse.Namespace, overrides: dict[str, Any] | None = None) -> Config:
    extra = dict(overrides or {})
    if getattr(args, "captcha_key", None):
        extra["captcha_key"] = args.captcha_key
    if getattr(args, "captcha", None):
        extra["appcheck_provider"] = args.captcha
    if getattr(args, "appcheck_token", None):
        extra["appcheck_token"] = args.appcheck_token
    if getattr(args, "mail_provider", None):
        extra["mail_provider"] = args.mail_provider
    if getattr(args, "mail_domain", None):
        extra["mail_domain"] = args.mail_domain
    if getattr(args, "mail_domains", None):
        extra["mail_domains"] = args.mail_domains
    if getattr(args, "mail_address", None):
        extra["mail_address"] = args.mail_address
    if getattr(args, "engine", None):
        extra["engine"] = args.engine
    if getattr(args, "browser_stack", None):
        extra["browser_stack"] = args.browser_stack
    if getattr(args, "headful", False):
        extra["headless"] = False
    if getattr(args, "cdp", None):
        extra["cdp"] = args.cdp
    if getattr(args, "humanize", None):
        extra["humanize"] = args.humanize
    if getattr(args, "chat_engine", None):
        extra["chat_engine"] = args.chat_engine
    if getattr(args, "base_url", None):
        extra["base_url"] = args.base_url
    cfg = Config.load(getattr(args, "config", None), extra)
    if getattr(args, "count", None):
        cfg.count = max(1, int(args.count))
    if getattr(args, "concurrency", None):
        cfg.concurrency = max(1, int(args.concurrency))
    if getattr(args, "retry_rounds", None) is not None:
        cfg.retry_rounds = max(0, int(args.retry_rounds))
    if getattr(args, "proxies", None):
        cfg.proxies = list(args.proxies)
    if getattr(args, "push_url", None):
        cfg.push_url = args.push_url
    if getattr(args, "push_token", None):
        cfg.push_token = args.push_token
    if getattr(args, "output", None):
        cfg.output_dir = Path(args.output)
        cfg.db_path = cfg.output_dir / "accounts.sqlite3"
    if getattr(args, "delay", None):
        cfg.antiban.min_delay = float(args.delay[0])
        cfg.antiban.max_delay = float(args.delay[1])
    if getattr(args, "no_verify", False):
        cfg.verify_email = False
    if getattr(args, "no_warmup", False):
        cfg.antiban.warmup = False
    return cfg


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _config(args)
    store = Store(cfg.db_path or cfg.output_dir / "accounts.sqlite3")
    runner = Runner(cfg, store, on_event=_json_out if args.json else None, quiet=args.json)
    try:
        summary = runner.run(cfg.count)
    finally:
        store.close()
    if not args.json:
        print(json.dumps({k: v for k, v in summary.items() if k != "accounts"}, ensure_ascii=False, indent=2))
    return 0 if summary["created"] else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _config(args)
    store = Store(cfg.db_path or cfg.output_dir / "accounts.sqlite3")
    runner = Runner(cfg, store, quiet=True)
    try:
        report = runner.doctor()
    finally:
        store.close()
    report["browser_available"] = available_stacks()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = bool(report.get("protocol", {}).get("backend", "").startswith("HTTP"))
    return 0 if ok else 1


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = _config(args)
    out = {
        "protocol": protocol_probe(proxy=cfg.proxy_for(0)),
        "appcheck": appcheck_probe(proxy=cfg.proxy_for(0), token=args.appcheck_token or ""),
    }
    if args.with_browser:
        vault = AppCheckVault(
            provider="browser",
            proxy=cfg.proxy_for(0),
            headless=not args.headful,
        )
        try:
            token = vault.get()
            out["browser_appcheck"] = {"ok": True, "len": len(token)}
        except SignupError as exc:
            out["browser_appcheck"] = {"ok": False, "error": f"{exc.kind}: {exc}"[:200]}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = _config(args)
    store = Store(cfg.db_path or cfg.output_dir / "accounts.sqlite3")
    try:
        rows = store.rows(args.limit)
        print(json.dumps({"total": store.count(), "accounts": rows}, ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


def cmd_domains(args: argparse.Namespace) -> int:
    cfg = _config(args)
    store = Store(cfg.db_path or cfg.output_dir / "accounts.sqlite3")
    try:
        print(
            json.dumps(
                {"domains": store.domain_table(args.limit), "failures": store.failure_summary()},
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        store.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cfg = _config(args)
    store = Store(cfg.db_path or cfg.output_dir / "accounts.sqlite3")
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_dir = cfg.output_dir / "exports"
        if args.format == "csv":
            target = Path(args.out) if args.out else out_dir / f"cleverai-{stamp}.csv"
            count = store.export_csv(target)
        elif args.format == "pool":
            target = Path(args.out) if args.out else out_dir / f"cleverai-pool-{stamp}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(
                    {
                        "schema": "dcg.pool/cleverai@1",
                        "version": 1,
                        "provider": "cleverai",
                        "accounts": store.pool_records(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            count = len(store.pool_records())
        else:
            target = Path(args.out) if args.out else out_dir / f"cleverai-{stamp}.jsonl"
            count = store.export_jsonl(target)
        print(json.dumps({"file": str(target), "rows": count, "format": args.format}, ensure_ascii=False))
    finally:
        store.close()
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    from .push import push_account

    cfg = _config(args)
    store = Store(cfg.db_path or cfg.output_dir / "accounts.sqlite3")
    pushed = 0
    try:
        for record in store.pool_records():
            result = push_account(cfg.push_url, cfg.push_token, record)
            pushed += 1
            key = (result.get("gatewayKey") or {}).get("prefix", "")
            print(f"  + {record['email']} {key}")
    finally:
        store.close()
    print(json.dumps({"pushed": pushed}, ensure_ascii=False))
    return 0 if pushed else 1


def cmd_chat(args: argparse.Namespace) -> int:
    """用池里某个账号跑一次对话，验证 key 是否还能用。"""
    cfg = _config(args)
    store = Store(cfg.db_path or cfg.output_dir / "accounts.sqlite3")
    try:
        records = store.pool_records()
    finally:
        store.close()
    if not records:
        print("号池是空的：先跑 register 或 import", file=sys.stderr)
        return 2
    record = next((r for r in records if r["email"] == args.email), None) or records[0]
    with CleverAI(proxy=cfg.proxy_for(0)) as client:
        client.refresh_token = record["refreshToken"]
        client.uid = record["uid"]
        client.email = record["email"]
        client.refresh()
        events = client.chat(
            [dialog_message(args.prompt)],
            engine=args.chat_engine or cfg.chat_engine,
            wallet=record.get("wallet") or {},
            cost=2,
        )
        out = []
        for event in events:
            out.append(text_of(event))
            if event.get("finishReason") in ("stop", "error"):
                break
    print("".join(out) or "(empty)")
    return 0


def cmd_appcheck(args: argparse.Namespace) -> int:
    """单独取一枚 App Check 令牌，方便手动写进 config / 环境变量。"""
    cfg = _config(args)
    vault = AppCheckVault(
        provider=cfg.appcheck.provider,
        api_key=cfg.appcheck.api_key,
        proxy=cfg.proxy_for(0),
        headless=cfg.browser.headless,
    )
    try:
        token = vault.get()
    except SignupError as exc:
        print(json.dumps({"ok": False, "error": f"{exc.kind}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "token": token, "stats": vault.stats}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cleverai_reg", description="cleverai.app 注册机（协议 + 指纹浏览器）")
    parser.add_argument("--config", help="YAML 配置文件")
    parser.add_argument("--base-url", dest="base_url", help="站点地址，默认 https://cleverai.app")
    parser.add_argument("--captcha", help="App Check 令牌来源：browser | capsolver | manual")
    parser.add_argument("--captcha-key", dest="captcha_key", help="capsolver 的 clientKey")
    parser.add_argument("--appcheck-token", dest="appcheck_token", help="直接复用一枚 App Check JWT")
    parser.add_argument("--headful", action="store_true", help="有头浏览器（调试用）")
    parser.add_argument("--cdp", help="接管外部指纹浏览器的 CDP 地址")
    parser.add_argument("--humanize", type=float, help="拟人化倍率")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="批量注册")
    run.add_argument("-n", "--count", type=int, default=1, help="数量")
    run.add_argument("-c", "--concurrency", type=int, default=2, help="并发")
    run.add_argument("--engine", choices=["auto", "protocol", "browser"], help="注册引擎")
    run.add_argument("--browser-stack")
    run.add_argument("--mail-provider", choices=["imap", "fixed", "mailtm", "tempmail_lol"])
    run.add_argument("--mail-domain")
    run.add_argument("--mail-domains", nargs="+")
    run.add_argument("--mail-address")
    run.add_argument("--chat-engine", dest="chat_engine", help="预热/探测用的引擎 id")
    run.add_argument("--proxy", dest="proxies", action="append", help="代理，可重复")
    run.add_argument("--delay", nargs=2, type=float, help="间隔区间，如 --delay 25 70")
    run.add_argument("--retry-rounds", dest="retry_rounds", type=int)
    run.add_argument("--no-verify", action="store_true", help="跳过邮箱验证")
    run.add_argument("--no-warmup", action="store_true", help="跳过注册后预热对话")
    run.add_argument("--push-url")
    run.add_argument("--push-token")
    run.add_argument("--output", help="输出目录")
    run.add_argument("--json", action="store_true", help="NDJSON 事件流")
    run.set_defaults(func=cmd_run)

    doctor = sub.add_parser("doctor", help="体检：后端、App Check、浏览器、域名战绩")
    doctor.set_defaults(func=cmd_doctor)

    probe = sub.add_parser("probe", help="只探测协议与 App Check 交换端点")
    probe.add_argument("--with-browser", action="store_true")
    probe.set_defaults(func=cmd_probe)

    listing = sub.add_parser("list", help="列出已注册账号")
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(func=cmd_list)

    domains = sub.add_parser("domains", help="域名战绩与失败分类")
    domains.add_argument("--limit", type=int, default=20)
    domains.set_defaults(func=cmd_domains)

    export = sub.add_parser("export", help="导出 jsonl | csv | pool")
    export.add_argument("--format", choices=["jsonl", "csv", "pool"], default="jsonl")
    export.add_argument("--out")
    export.set_defaults(func=cmd_export)

    push = sub.add_parser("push", help="把本地账号回推网关号池")
    push.add_argument("--push-url", dest="push_url")
    push.add_argument("--push-token", dest="push_token")
    push.set_defaults(func=cmd_push)

    chat = sub.add_parser("chat", help="用池里的账号跑一次对话")
    chat.add_argument("--email")
    chat.add_argument("--prompt", default="Say hello in one short sentence.")
    chat.add_argument("--chat-engine", dest="chat_engine")
    chat.set_defaults(func=cmd_chat)

    appcheck = sub.add_parser("appcheck", help="取一枚 App Check 令牌")
    appcheck.set_defaults(func=cmd_appcheck)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SignupError as exc:
        print(json.dumps({"ok": False, "kind": exc.kind, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
