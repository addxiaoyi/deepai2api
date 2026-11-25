"""命令行入口：run | doctor | probe | list | export | push | keys | verify | balance。"""

from __future__ import annotations

import argparse
import json
import sys
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
        "panel_api": getattr(args, "panel_api", None),
        "api_base": getattr(args, "api_base", None),
        "keys_per_account": getattr(args, "keys_per_account", None),
        "output_dir": Path(args.out) if getattr(args, "out", None) else None,
        "mail_provider": getattr(args, "mail_provider", None),
        "mail_domain": getattr(args, "mail_domain", None),
        "captcha_provider": getattr(args, "captcha", None),
        "captcha_key": getattr(args, "captcha_key", None),
        "browser_stack": getattr(args, "browser_stack", None),
        "cdp": getattr(args, "cdp", None),
        "humanize": getattr(args, "humanize", None),
        "anti_ban": getattr(args, "anti_ban", None),
        "key_policy": getattr(args, "key_policy", None),
        "session_file": getattr(args, "session_file", None),
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
    panel = report["checks"].get("panel")
    fatal = not isinstance(panel, dict) or not panel.get("ok")
    return 1 if fatal else 0


def cmd_probe(args: argparse.Namespace) -> int:
    from .protocol import probe

    cfg = _config_from(args)
    print(
        json.dumps(
            probe(cfg.panel, panel_api=cfg.panel_api, api_base=cfg.api_base, proxy=cfg.proxies[0] if cfg.proxies else ""),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """校验 key：GET /openai/v1/models。不注册、不消耗额度。"""
    from .protocol import Novita

    cfg = _config_from(args)
    keys = _flatten(getattr(args, "key", None))
    if keys:
        emails = ["" for _ in keys]
    else:
        store = Store(cfg.db_path)
        try:
            records = store.pool_records()
        finally:
            store.close()
        keys = [row["apiKey"] for row in records]
        emails = [row["email"] for row in records]
    if not keys:
        print("没有可校验的 key（用 --key 指定，或先跑一轮 run）", file=sys.stderr)
        return 2

    results = []
    with Novita(panel=cfg.panel, panel_api=cfg.panel_api, api_base=cfg.api_base, openai_base=cfg.openai_base) as client:
        for key, email in zip(keys, emails):
            label = f"{key[:8]}…{key[-4:]}"
            try:
                results.append({"key": label, "email": email, **client.verify_key(key)})
            except SignupError as exc:
                results.append({"key": label, "email": email, "ok": False, "error": str(exc)})
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    return 0 if all(r.get("ok") for r in results) else 1


def cmd_balance(args: argparse.Namespace) -> int:
    """查余额（单位 1/10000 USD 换算成美元）。"""
    from .protocol import Novita

    cfg = _config_from(args)
    token = _flatten(getattr(args, "key", None))
    token_value = token[0] if token else ""
    with Novita(panel=cfg.panel, panel_api=cfg.panel_api, api_base=cfg.api_base, openai_base=cfg.openai_base, token=token_value) as client:
        try:
            print(json.dumps(client.balance(), ensure_ascii=False, indent=2))
            return 0
        except SignupError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 1


def cmd_list(args: argparse.Namespace) -> int:
    cfg = _config_from(args)
    store = Store(cfg.db_path)
    try:
        print(
            json.dumps(
                {
                    "total": store.count(),
                    "keys": store.key_count(),
                    "rows": store.rows(args.limit),
                    "domains": store.domain_summary(),
                    "failures": store.failure_summary(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
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
                json.dumps(
                    {"schema": "dcg.pool/novita@1", "version": 1, "provider": "novita", "accounts": rows},
                    ensure_ascii=False,
                    indent=2,
                ),
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
        print(
            "需要 --push-url 和 --push-token（或 NOVITA_GATEWAY_URL / NOVITA_GATEWAY_TOKEN）",
            file=sys.stderr,
        )
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


def cmd_keys(args: argparse.Namespace) -> int:
    cfg = _config_from(args)
    store = Store(cfg.db_path)
    try:
        rows = []
        for record in store.pool_records():
            rows.append(
                {
                    "email": record["email"],
                    "primary": f"{record['apiKey'][:8]}…{record['apiKey'][-4:]}",
                    "expireTime": record["expireTime"],
                    "keys": store.keys_for(record["email"]),
                }
            )
        print(json.dumps({"keys": rows}, ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


def cmd_import_keys(args: argparse.Namespace) -> int:
    """把外部已经生成好的 key 直接入库。"""
    cfg = _config_from(args)
    pairs = _flatten(getattr(args, "key", None))
    if not pairs:
        print("用 --key email:key 指定，可重复", file=sys.stderr)
        return 2
    store = Store(cfg.db_path)
    imported = 0
    try:
        for pair in pairs:
            if ":" not in pair:
                print(f"跳过（格式应为 email:key）：{pair[:40]}", file=sys.stderr)
                continue
            email, _, key = pair.partition(":")
            email = email.strip().lower()
            key = key.strip()
            record = {
                "email": email,
                "password": "",
                "name": email.split("@")[0],
                "api_key": key,
                "api_key_source": "manual",
                "key_prefix": key[:8],
                "key_last4": key[-4:],
                "domain": email.rsplit("@", 1)[-1] if "@" in email else "",
                "email_verified": True,
            }
            if store.add(record, engine="manual", proxy=""):
                imported += 1
                print(f"[import] {email}", flush=True)
            else:
                print(f"[import] {email} 已存在，跳过", file=sys.stderr, flush=True)
    finally:
        store.close()
    print(json.dumps({"imported": imported}, ensure_ascii=False))
    return 0 if imported else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="novita-reg",
        description="Novita AI 账号工厂：邮箱注册 + 激活 + 建 key + 号池导出。",
    )
    parser.add_argument("--config", help="YAML 配置路径（默认 ./config.yaml）")
    sub = parser.add_subparsers(dest="command", required=True)

    def shared(p: argparse.ArgumentParser) -> None:
        p.add_argument("-o", "--out", "--out-dir", dest="out", help="输出目录（默认 out；网关用 --out-dir 指到 data/<provider>-reg/）")
        p.add_argument("--proxy", action="append", help="代理，可重复或逗号分隔")
        p.add_argument("--no-proxy", action="store_true", help="忽略环境代理")
        p.add_argument("--base-url", help="面板地址（默认 https://novita.ai）")
        p.add_argument("--panel-api", help="面板 API 基址（默认 https://api-server.novita.ai）")
        p.add_argument("--api-base", help="原生 API 基址（默认 https://api.novita.ai）")
        p.add_argument("--mail-provider", choices=["imap", "domain", "tempmail_lol", "mailtm", "fixed"])
        p.add_argument("--mail-domain", help="邮箱域名")
        p.add_argument("--email", help="固定地址（plus 分号）")
        p.add_argument("--captcha", choices=["auto", "none", "capsolver", "2captcha", "browser", "manual"])
        p.add_argument("--captcha-key", help="解题平台 key（manual 模式下就是 token）")
        p.add_argument("--engine", choices=["auto", "protocol", "browser", "hybrid"], help="auto/hybrid：协议优先，被拦切浏览器；protocol 只走协议；browser 只走浏览器")
        p.add_argument("--browser-stack", choices=["auto", "camoufox", "patchright", "playwright"])
        p.add_argument("--cdp", help="接管外部指纹浏览器的 CDP 地址")
        p.add_argument("--humanize", type=float, help="打字/移动/停顿倍率（默认 1.0）")
        p.add_argument("--headful", action="store_true", help="显示浏览器窗口")
        p.add_argument("--anti-ban", help="防封策略 JSON")
        p.add_argument("--key-policy", help="key 策略 JSON（per_account/expire_time/open_model_access）")
        p.add_argument("--session-file", help="导入外部会话 cookie JSON")

    run = sub.add_parser("run", help="创建账号")
    shared(run)
    run.add_argument("-n", "--count", type=int, default=1)
    run.add_argument("-c", "--concurrency", type=int, default=2)
    run.add_argument("--attempts", type=int, default=3)
    run.add_argument("--retry-rounds", type=int, default=None)
    run.add_argument("--keys-per-account", type=int, default=None)
    run.add_argument("--no-verify", action="store_true")
    run.add_argument("--mail-domains", action="append")
    run.add_argument("--json", action="store_true", help="stdout 输出 NDJSON 事件（网关用）")
    run.add_argument("--push-url", help="网关地址；每个账号 POST 进号池")
    run.add_argument("--push-token", help="网关管理口令")
    run.set_defaults(func=cmd_run)

    doc = sub.add_parser("doctor", help="检查面板 / 邮箱 / 浏览器内核")
    shared(doc)
    doc.set_defaults(func=cmd_doctor)

    pr = sub.add_parser("probe", help="打印面板端点与 Turnstile sitekey")
    shared(pr)
    pr.set_defaults(func=cmd_probe)

    ver = sub.add_parser("verify", help="校验 key 是否可用（不注册）")
    shared(ver)
    ver.add_argument("--key", action="append", help="要校验的 key，可重复；省略则校验库里全部")
    ver.set_defaults(func=cmd_verify)

    bal = sub.add_parser("balance", help="查余额（1/10000 USD → 美元）")
    shared(bal)
    bal.add_argument("--key", action="append", help="带账单权限的 key")
    bal.set_defaults(func=cmd_balance)

    listing = sub.add_parser("list", help="查看已存账号")
    shared(listing)
    listing.add_argument("--limit", type=int, default=20)
    listing.set_defaults(func=cmd_list)

    export = sub.add_parser("export", help="导出 JSONL / CSV / pool JSON")
    shared(export)
    export.add_argument("--format", choices=["jsonl", "csv", "pool"], default="jsonl")
    export.add_argument("--file", dest="out_file")
    export.set_defaults(func=cmd_export)

    push = sub.add_parser("push", help="把库里账号全部重推到网关号池")
    shared(push)
    push.add_argument("--push-url")
    push.add_argument("--push-token")
    push.set_defaults(func=cmd_push)

    keys = sub.add_parser("keys", help="按账号列出全部 key")
    shared(keys)
    keys.set_defaults(func=cmd_keys)

    imp = sub.add_parser("import-keys", help="手工导入 email:key 对")
    shared(imp)
    imp.add_argument("--key", action="append", help="email:key，可重复")
    imp.set_defaults(func=cmd_import_keys)

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:  # pragma: no cover
        pass
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)