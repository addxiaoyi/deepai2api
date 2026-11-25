"""CLI：run | doctor | list | export | push | probe。

与 `moonai_reg` / `novita_reg` 的入口保持一致，网关的 `provider-register` 会把面板参数
翻成这些开关，所以命名不能随意改：`--json` 必须输出 NDJSON，`run` 子命令必须存在。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import RunConfig
from .errors import ZoError
from .runner import Reporter, Runner
from .store import Store


def _json_line(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _store_for(config: RunConfig) -> Store:
    return Store(str(Path(config.out_dir)))


def cmd_run(args: argparse.Namespace) -> int:
    config = RunConfig.from_namespace(args)
    store = _store_for(config)
    reporter = Reporter(json_events=bool(getattr(args, "json", False)))
    try:
        runner = Runner(config, reporter=reporter)
        summary = runner.run()
    finally:
        store.close()
    if not getattr(args, "json", False):
        printable = {k: v for k, v in (summary or {}).items() if k not in ("accounts", "items")}
        print(json.dumps(printable, ensure_ascii=False, indent=2))
    return 0 if (summary or {}).get("created") else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    config = RunConfig.from_namespace(args)
    store = _store_for(config)
    try:
        report = {
            "provider": "zo",
            "config": {
                "base_url": config.base_url,
                "api_base": config.api_base,
                "engine": config.engine,
                "mail_provider": config.mail_provider,
                "mail_domains": config.anti_ban.mail_domains,
                "captcha": config.captcha,
                "proxies": len(config.proxies),
                "out_dir": config.out_dir,
                "push": bool(config.push_url and config.push_token),
            },
            "store": store.counts(),
            "worker": {"python": sys.version.split()[0], "module": "zo_reg"},
        }
    finally:
        store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    config = RunConfig.from_namespace(args)
    store = _store_for(config)
    try:
        rows = store.accounts(limit=int(getattr(args, "limit", 50) or 50))
        counts = store.counts()
    finally:
        store.close()
    for row in rows:
        row.pop("accessToken", None)
        row.pop("refreshToken", None)
    print(json.dumps({"counts": counts, "accounts": rows}, ensure_ascii=False, indent=2))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    config = RunConfig.from_namespace(args)
    store = _store_for(config)
    try:
        path = store.export(fmt=str(args.format or "jsonl"), path=str(args.out or ""))
    finally:
        store.close()
    print(json.dumps({"file": path, "format": args.format}, ensure_ascii=False))
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    from .push import push_account

    config = RunConfig.from_namespace(args)
    store = _store_for(config)
    pushed = 0
    try:
        for row in store.accounts():
            if not row.get("apiKey"):
                continue
            push_account(
                row,
                base_url=config.push_url,
                token=config.push_token,
                mint_key=not getattr(args, "no_mint_key", False),
            )
            pushed += 1
    finally:
        store.close()
    print(json.dumps({"pushed": pushed}, ensure_ascii=False))
    return 0 if pushed else 1


def cmd_probe(args: argparse.Namespace) -> int:
    from .protocol import ZoProtocol
    from .identity import make_persona

    config = RunConfig.from_namespace(args)
    persona = make_persona(seed="probe", country=None)
    protocol = ZoProtocol(config=config, persona=persona, proxy=config.proxies[0] if config.proxies else "")
    try:
        models = protocol.list_models()
        print(json.dumps({"ok": True, "models": models[:20], "count": len(models)}, ensure_ascii=False, indent=2))
        return 0
    except ZoError as exc:
        print(json.dumps({"ok": False, "kind": getattr(exc, "kind", "error"), "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        protocol.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zo_reg", description="zo.computer 注册机（魔法链 + 指纹浏览器）")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-n", "--count", type=int, default=1)
        p.add_argument("-c", "--concurrency", type=int, default=2)
        p.add_argument("--engine", choices=["auto", "protocol", "browser", "hybrid"], default="auto")
        p.add_argument("--browser-stack", default="auto")
        p.add_argument("--headful", action="store_true")
        p.add_argument("--cdp", default="")
        p.add_argument("--humanize", type=float, default=0)
        p.add_argument("--mail-provider", default="imap")
        p.add_argument("--mail-domain", dest="mail_domains", action="append", default=[])
        p.add_argument("--email", default="")
        p.add_argument("--captcha", default="none")
        p.add_argument("--captcha-key", default="")
        p.add_argument("--base-url", default="")
        p.add_argument("--api-base", default="")
        p.add_argument("--handle-prefix", default="")
        p.add_argument("--keys-per-account", type=int, default=1)
        p.add_argument("--token-name", default="gateway")
        p.add_argument("--model", default="")
        p.add_argument("--no-verify", action="store_true")
        p.add_argument("--verify-timeout", type=float, default=240.0)
        p.add_argument("--no-probe", action="store_true")
        p.add_argument("--mcp-probe", action="store_true")
        p.add_argument("--desktop-ua", action="store_true")
        p.add_argument("--proxy", action="append", default=[])
        p.add_argument("--anti-ban", dest="anti_ban", default=None)
        p.add_argument("--key-policy", default=None)
        p.add_argument("--push-url", default="")
        p.add_argument("--push-token", default="")
        p.add_argument("--session-file", default="")
        p.add_argument("--out-dir", default="out")
        p.add_argument("--timeout", type=float, default=45.0)
        p.add_argument("--retries", type=int, default=3)
        p.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="批量注册")
    add_common(run)
    run.set_defaults(func=cmd_run)

    doctor = sub.add_parser("doctor", help="体检：配置、号池计数、worker 版本")
    add_common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    listing = sub.add_parser("list", help="列出已注册账号（脱敏）")
    add_common(listing)
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(func=cmd_list)

    export = sub.add_parser("export", help="导出 jsonl | csv")
    add_common(export)
    export.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    export.add_argument("--out", default="")
    export.set_defaults(func=cmd_export)

    push = sub.add_parser("push", help="把本地账号回推网关号池")
    add_common(push)
    push.add_argument("--no-mint-key", action="store_true")
    push.set_defaults(func=cmd_push)

    probe = sub.add_parser("probe", help="用无登录态打一次 /models/available")
    add_common(probe)
    probe.set_defaults(func=cmd_probe)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ZoError as exc:
        print(json.dumps({"ok": False, "kind": getattr(exc, "kind", "error"), "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
