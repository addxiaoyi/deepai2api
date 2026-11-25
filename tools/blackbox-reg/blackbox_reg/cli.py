"""命令行入口。

与网关 `src/provider-register.mjs` 的调用约定严格对齐（它就是这么拼参数的）：

    python -m blackbox_reg run --json -n 5 -c 2 \
        --mail-provider domain --mail-domains own.example.com \
        --base-url https://app.blackbox.ai --api-base https://enterprise.blackbox.ai \
        --keys-per-account 1 --captcha none --engine auto --browser-stack auto \
        --anti-ban '{...}' --key-policy '{}' \
        --push-url http://127.0.0.1:8787 --push-token <管理口令>

另外几个子命令给人工排障 / 集成用：

    doctor    依赖 / 站点 / 身份 / 邮箱 / 浏览器内核自检，不建号
    push      把 out/accounts.jsonl 批量回推进网关
    export    导出 jsonl / csv
    pool      打印本地 sqlite 统计
    cli-env   造一个隔离的 Blackbox CLI 运行环境（可顺手跑一次，验证编程工具链路）
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .config import API_BASE, APP_BASE, CONSOLE_BASE, DEFAULT_MODEL, RunConfig
from .runner import Reporter, Runner, doctor as run_doctor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blackbox_reg", description="Blackbox AI 批量注册机（协议 + 指纹浏览器）")
    parser.add_argument("--version", action="version", version=f"blackbox_reg {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="跑一轮批量注册")
    run.add_argument("-n", "--count", type=int, default=1)
    run.add_argument("-c", "--concurrency", type=int, default=2)
    run.add_argument("--engine", default="auto", choices=["auto", "protocol", "browser", "hybrid", "console"])
    run.add_argument("--browser-stack", default="auto", choices=["auto", "camoufox", "patchright", "playwright"])
    run.add_argument("--cdp", default="")
    run.add_argument("--headful", action="store_true")
    run.add_argument("--humanize", type=float, default=0.0)
    # 指纹浏览器细节（camoufox 首选）
    run.add_argument("--profile-dir", default="", help="camoufox 身份档目录（persistent_context）")
    run.add_argument("--storage-state", default="", help="playwright storage_state 落盘/复用路径")
    run.add_argument("--no-geo-align", action="store_true", help="不让时区/语言跟随出口 IP")
    run.add_argument("--no-disable-coop", action="store_true", help="保留 COOP（默认关掉以点 Turnstile）")
    run.add_argument("--mail-provider", default="domain", choices=["domain", "imap", "mailtm", "fixed"])
    run.add_argument("--mail-domains", action="append", default=[])
    run.add_argument("--email", default="")
    run.add_argument("--captcha", default="none", choices=["none", "auto", "capsolver", "2captcha", "browser", "manual"])
    run.add_argument("--captcha-key", default="")
    run.add_argument("--base-url", default=APP_BASE)
    run.add_argument("--api-base", default=API_BASE)
    run.add_argument("--console-url", default=CONSOLE_BASE)
    run.add_argument("--keys-per-account", type=int, default=1)
    run.add_argument("--model", default=DEFAULT_MODEL)
    # 身份来源
    run.add_argument("--google-pool", default="", help="Google 身份池 JSONL")
    run.add_argument("--google-cookies", default="", help="已登录 Google 的 cookies JSON")
    run.add_argument("--session-file", default="", help="已登录 app.blackbox.ai 的 cookies JSON")
    run.add_argument("--authorization-code", default="", help="手工抓到的 Google 授权码")
    # 面板 Key 接口覆盖（探测失败时用）
    run.add_argument("--keys-path", default="")
    run.add_argument("--keys-method", default="GET", choices=["GET", "POST"])
    run.add_argument("--keys-create-path", default="")
    run.add_argument("--keys-create-body", default="")
    # 其它
    run.add_argument("--no-verify", action="store_true")
    run.add_argument("--verify-timeout", type=float, default=180.0)
    run.add_argument("--no-probe", action="store_true")
    run.add_argument("--push-url", default="")
    run.add_argument("--push-token", default="")
    run.add_argument("--anti-ban", default="")
    run.add_argument("--key-policy", default="")
    run.add_argument("--proxy", action="append", default=[])
    run.add_argument("--out-dir", default="out")
    run.add_argument("--timeout", type=float, default=45.0)
    run.add_argument("--retries", type=int, default=3)
    run.add_argument("--json", action="store_true", help="stdout 输出 NDJSON 事件（网关要这个）")

    doctor = sub.add_parser("doctor", help="自检：依赖 / 站点 / 身份 / 邮箱 / 浏览器")
    doctor.add_argument("--mail-provider", default="domain")
    doctor.add_argument("--mail-domains", action="append", default=[])
    doctor.add_argument("--google-pool", default="")
    doctor.add_argument("--session-file", default="")
    doctor.add_argument("--google-cookies", default="")
    doctor.add_argument("--proxy", action="append", default=[])
    doctor.add_argument("--base-url", default=APP_BASE)
    doctor.add_argument("--api-base", default=API_BASE)

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

    cli_env = sub.add_parser("cli-env", help="造 Blackbox CLI 沙箱运行环境（可选直接跑一次）")
    cli_env.add_argument("--gateway-url", required=True, help="例如 http://127.0.0.1:8787/p/blackbox/v1")
    cli_env.add_argument("--gateway-key", required=True)
    cli_env.add_argument("--model", default=DEFAULT_MODEL)
    cli_env.add_argument("--env-dir", default="out/cli-env")
    cli_env.add_argument("--cli", default="blackbox")
    cli_env.add_argument("--run", action="store_true")
    cli_env.add_argument("--prompt", default="")

    return parser


def cmd_doctor(args) -> int:
    namespace = argparse.Namespace(
        count=1,
        concurrency=1,
        engine="protocol",
        browser_stack="auto",
        cdp="",
        headful=False,
        humanize=0,
        mail_provider=args.mail_provider,
        mail_domains=args.mail_domains,
        email="",
        captcha="none",
        captcha_key="",
        base_url=args.base_url,
        api_base=args.api_base,
        console_url=CONSOLE_BASE,
        keys_path="",
        keys_method="GET",
        keys_create_path="",
        keys_create_body="",
        model=DEFAULT_MODEL,
        keys_per_account=1,
        no_verify=False,
        verify_timeout=180.0,
        google_pool=args.google_pool,
        google_cookies=args.google_cookies,
        session_file=args.session_file,
        authorization_code="",
        push_url="",
        push_token="",
        out_dir="out",
        json=False,
        timeout=45.0,
        retries=3,
        no_probe=True,
        anti_ban="",
        key_policy="",
        proxy=args.proxy,
    )
    config = RunConfig.from_namespace(namespace)
    report = run_doctor(config)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    site = report.get("checks", {}).get("site", {})
    ok = isinstance(site, dict) and "HTTP 200" in str(site.get("providers", ""))
    return 0 if ok else 1


def cmd_run(args) -> int:
    config = RunConfig.from_namespace(args)
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


def cmd_cli_env(args) -> int:
    from . import clienv

    argv = [
        "--gateway-url",
        args.gateway_url,
        "--gateway-key",
        args.gateway_key,
        "--model",
        args.model,
        "--env-dir",
        args.env_dir,
        "--cli",
        args.cli,
    ]
    if args.run:
        argv.append("--run")
    if args.prompt:
        argv += ["--prompt", args.prompt]
    return clienv.main(argv)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "run": cmd_run,
        "doctor": cmd_doctor,
        "push": cmd_push,
        "export": cmd_export,
        "pool": cmd_pool,
        "cli-env": cmd_cli_env,
    }
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
