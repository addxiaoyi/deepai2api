"""命令行入口。

与网关 src/provider-register.mjs 的调用约定严格对齐（它就是这么拼参数的）：

    python -m moonai_reg run --json -n 5 -c 2 \
        --mail-provider imap --base-url https://auth.mooonai.com \
        --api-base https://core.mooonai.com --keys-per-account 1 \
        --captcha none --engine auto --browser-stack auto \
        --anti-ban '{...}' --key-policy '{}' \
        --push-url http://127.0.0.1:8787 --push-token <管理口令>

另外三个子命令给人工排障用：

    doctor   检查依赖 / IMAP / 站点可达性，不建号
    push     把 out/accounts.jsonl 批量回推进网关
    export   导出 jsonl / csv
    pool     打印本地 sqlite 统计
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .config import DEFAULT_MODEL, RunConfig
from .runner import Reporter, Runner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moonai_reg", description="MoonAI 批量注册机（协议 + 指纹浏览器）")
    parser.add_argument("--version", action="version", version=f"moonai_reg {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="跑一轮批量注册")
    run.add_argument("-n", "--count", type=int, default=1)
    run.add_argument("-c", "--concurrency", type=int, default=2)
    run.add_argument("--engine", default="auto", choices=["auto", "protocol", "browser", "hybrid"])
    run.add_argument("--browser-stack", default="auto", choices=["auto", "camoufox", "patchright", "playwright"])
    run.add_argument("--cdp", default="")
    run.add_argument("--headful", action="store_true")
    run.add_argument("--humanize", type=float, default=0.0)
    run.add_argument("--mail-provider", default="imap", choices=["imap", "domain", "mailtm", "fixed"])
    run.add_argument("--mail-domains", action="append", default=[])
    run.add_argument("--email", default="")
    run.add_argument("--captcha", default="none", choices=["none", "auto", "capsolver", "2captcha", "browser", "manual"])
    run.add_argument("--captcha-key", default="")
    run.add_argument("--base-url", default="https://auth.mooonai.com")
    run.add_argument("--core-url", default="https://core.mooonai.com")
    run.add_argument("--api-base", default="https://core.mooonai.com")
    run.add_argument("--keys-per-account", type=int, default=1)
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--no-verify", action="store_true")
    run.add_argument("--no-probe", action="store_true")
    run.add_argument("--push-url", default="")
    run.add_argument("--push-token", default="")
    run.add_argument("--session-file", default="")
    run.add_argument("--anti-ban", default="")
    run.add_argument("--key-policy", default="")
    run.add_argument("--proxy", action="append", default=[])
    run.add_argument("--out-dir", default="out")
    run.add_argument("--timeout", type=float, default=45.0)
    run.add_argument("--retries", type=int, default=3)
    run.add_argument("--json", action="store_true", help="stdout 输出 NDJSON 事件（网关要这个）")

    doctor = sub.add_parser("doctor", help="自检：依赖 / 站点 / IMAP")
    doctor.add_argument("--mail-provider", default="imap")
    doctor.add_argument("--mail-domains", action="append", default=[])
    doctor.add_argument("--proxy", action="append", default=[])

    push = sub.add_parser("push", help="把 out/accounts.jsonl 批量回推网关")
    push.add_argument("--push-url", required=True)
    push.add_argument("--push-token", required=True)
    push.add_argument("--file", default="")
    push.add_argument("--out-dir", default="out")
    push.add_argument("--no-mint-key", action="store_true")

    export = sub.add_parser("export", help="导出本地账号")
    export.add_argument("--format", default="jsonl", choices=["jsonl", "csv"])
    export.add_argument("--file", default="")
    export.add_argument("--out-dir", default="out")

    pool = sub.add_parser("pool", help="打印本地 sqlite 统计")
    pool.add_argument("--out-dir", default="out")

    return parser


def cmd_doctor(args) -> int:
    report: list[tuple[str, bool, str]] = []

    for module in ("httpx", "imaplib"):
        try:
            __import__(module)
            report.append((f"python:{module}", True, "ok"))
        except Exception as cause:  # noqa: BLE001
            report.append((f"python:{module}", False, str(cause)[:80]))

    try:
        from .browser import detect_stack

        stack = detect_stack("auto")
        report.append(("browser stack", True, stack))
    except Exception as cause:  # noqa: BLE001
        report.append(("browser stack", False, str(cause)[:100]))

    import httpx

    for name, url in (
        ("auth", "https://auth.mooonai.com/auth/signup/"),
        ("core", "https://core.mooonai.com/api/v1/auth/url"),
    ):
        try:
            res = httpx.get(url, timeout=20, follow_redirects=False, trust_env=False)
            report.append((f"site:{name}", res.status_code < 500, f"HTTP {res.status_code}"))
        except Exception as cause:  # noqa: BLE001
            report.append((f"site:{name}", False, str(cause)[:100]))

    if args.mail_provider in ("imap", "domain", "fixed"):
        host = os.environ.get("MOONAI_IMAP_HOST", "")
        user = os.environ.get("MOONAI_IMAP_USER", "")
        report.append(("imap:config", bool(host and user), f"{host or '(未设置)'} / {user or '-'}"))
        if host and user:
            try:
                from .mailbox import ImapMailbox

                box = ImapMailbox.from_env(f"probe@{args.mail_domains[0]}" if args.mail_domains else "probe@example.com")
                conn = box._connect()  # noqa: SLF001 - doctor 就是用来探这个的
                conn.logout()
                report.append(("imap:login", True, "ok"))
            except Exception as cause:  # noqa: BLE001
                report.append(("imap:login", False, str(cause)[:120]))

    for name, ok, detail in report:
        print(f"[{'OK ' if ok else 'FAIL'}] {name:22} {detail}")
    failed = [row for row in report if not row[1]]
    print(f"\n{len(report) - len(failed)}/{len(report)} 项通过")
    return 0 if not failed else 1


def cmd_run(args) -> int:
    config = RunConfig.from_namespace(args)
    config.probe = not getattr(args, "no_probe", False)
    reporter = Reporter(json_events=config.json_events)
    runner = Runner(config, reporter=reporter)
    try:
        summary = runner.run()
    finally:
        runner.close()
    if not config.json_events:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["created"] or not summary["failed"] else 1


def cmd_push(args) -> int:
    from .push import push_many
    from .store import Store, load_jsonl

    rows = load_jsonl(args.file) if args.file else Store(args.out_dir).accounts()
    if not rows:
        print("没有可推送的账号。")
        return 1
    result = push_many(
        rows,
        base_url=args.push_url,
        token=args.push_token,
        mint_key=not args.no_mint_key,
        log=lambda msg: print(msg),
    )
    print(json.dumps({k: v for k, v in result.items() if k != "gateway_keys"}, ensure_ascii=False))
    if result.get("gateway_keys"):
        print("网关 Key（只显示这一次）：")
        for key in result["gateway_keys"]:
            print("  " + key)
    return 0 if result["pushed"] else 1


def cmd_export(args) -> int:
    from .store import Store

    store = Store(args.out_dir)
    try:
        path = store.export(fmt=args.format, path=args.file)
    finally:
        store.close()
    print(path)
    return 0


def cmd_pool(args) -> int:
    from .store import Store

    store = Store(args.out_dir)
    try:
        print(json.dumps(store.counts(), ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "run": cmd_run,
        "doctor": cmd_doctor,
        "push": cmd_push,
        "export": cmd_export,
        "pool": cmd_pool,
    }
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
