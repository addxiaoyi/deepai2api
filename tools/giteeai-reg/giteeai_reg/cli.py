"""令牌校验 + 入池 CLI。

    python -m giteeai_reg run --tokens-file tokens.txt
    python -m giteeai_reg run --token <gitee_token>
    python -m giteeai_reg doctor
    python -m giteeai_reg keys
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

LINEKIT = Path(__file__).resolve().parents[2] / "linekit"
if str(LINEKIT) not in sys.path:
    sys.path.insert(0, str(LINEKIT))

from linekit.errors import LineError  # noqa: E402
from linekit.net import build_client, request  # noqa: E402
from linekit.push import push_account  # noqa: E402
from linekit.store import Store  # noqa: E402

from .site import API_BASE, HOME, IMAGES_PATH, MODEL, MODELS_PATH  # noqa: E402


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def verify(token: str, *, proxy: str = "", timeout: float = 30.0) -> dict[str, Any]:
    """只读校验：`GET /models`。成功即认为令牌可用且未过期。"""
    client = build_client(proxy=proxy, timeout=timeout)
    try:
        response = request(
            client, "GET", f"{API_BASE}{MODELS_PATH}", headers={"Authorization": f"Bearer {token}"}
        )
        ok = response.status_code < 400
        body = (response.text or "")[:300]
        models: list[str] = []
        if ok:
            try:
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else payload
                if isinstance(data, list):
                    models = [str((m or {}).get("id") or m) for m in data][:40]
            except ValueError:
                pass
        return {
            "ok": ok,
            "status": response.status_code,
            "models": models,
            "body": body,
            "model": MODEL,
            "imagesPath": IMAGES_PATH,
        }
    finally:
        client.close()


def cmd_run(args: argparse.Namespace) -> int:
    tokens: list[str] = []
    if args.token:
        tokens.extend(args.token)
    if args.tokens_file:
        path = Path(args.tokens_file)
        if not path.exists():
            print(json.dumps({"ok": False, "error": f"file not found: {path}"}, ensure_ascii=False))
            return 2
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                tokens.append(line)
    tokens = list(dict.fromkeys(tokens))
    if not tokens:
        print(json.dumps({"ok": False, "error": "没有令牌：用 --token 或 --tokens-file 给"}, ensure_ascii=False))
        return 2

    store = Store(Path(args.output or "out") / "accounts.sqlite3")
    proxy = (args.proxy or [""])[0] if args.proxy else ""
    created = 0
    failed = 0
    results: list[dict[str, Any]] = []
    try:
        for index, token in enumerate(tokens):
            if args.json:
                _emit({"type": "attempt", "index": index, "token": token[:8] + "…"})
            try:
                check = verify(token, proxy=proxy)
            except LineError as exc:
                failed += 1
                store.record_failure(email=f"token#{index}", kind=exc.kind, reason=str(exc))
                results.append({"index": index, "ok": False, "kind": exc.kind, "reason": str(exc)[:200]})
                if args.json:
                    _emit({"type": "failed", "index": index, "kind": exc.kind, "reason": str(exc)[:200]})
                continue
            if not check["ok"]:
                failed += 1
                store.record_failure(email=f"token#{index}", kind="blocked", reason=f"HTTP {check['status']}")
                results.append({"index": index, "ok": False, "status": check["status"], "body": check["body"][:120]})
                if args.json:
                    _emit({"type": "failed", "index": index, "kind": "blocked", "reason": f"HTTP {check['status']}"})
                continue
            record = {
                "email": f"giteeai-token-{index + 1}@local",
                "password": "",
                "name": "Gitee AI token",
                "apiKey": token,
                "keyPrefix": "",
                "emailVerified": None,
                "credits": {"models": check["models"]},
                "engine": "token-import",
            }
            added = store.add(record, engine="token-import", proxy=proxy)
            created += 1 if added else 0
            results.append({"index": index, "ok": True, "models": check["models"]})
            if args.json:
                _emit({"type": "account", "index": index, "account": {**record, "apiKey": token, "createdAt": ""}})
            if args.push_url and args.push_token:
                try:
                    push_account(args.push_url, args.push_token, record, line="giteeai")
                    if args.json:
                        _emit({"type": "pushed", "index": index})
                except LineError as exc:
                    if args.json:
                        _emit({"type": "push_error", "index": index, "message": str(exc)[:200]})
        summary = {"type": "done", "summary": {"checked": len(tokens), "created": created, "failed": failed, "results": results}}
        if args.json:
            _emit(summary)
        else:
            print(json.dumps(summary["summary"], ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0 if created else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    client = build_client(timeout=20.0)
    try:
        probe = request(client, "GET", f"{HOME}{MODELS_PATH.replace('/models', '')}/v1/models")
        report = {
            "api_base": API_BASE,
            "home": HOME,
            "model": MODEL,
            "models_status": probe.status_code,
            "models_body": (probe.text or "")[:200],
            "note": "令牌需在 gitee.com 个人设置里创建（无机器注册接口）",
        }
    except LineError as exc:
        report = {"api_base": API_BASE, "error": str(exc)}
    finally:
        client.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    store = Store(Path(args.output or "out") / "accounts.sqlite3")
    try:
        rows = store.keys()
    finally:
        store.close()
    print("\n".join(rows))
    return 0 if rows else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="giteeai_reg", description="Gitee AI 令牌校验与入池")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--output", default="out")
        p.add_argument("--proxy", action="append")
        p.add_argument("--push-url")
        p.add_argument("--push-token")
        p.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="校验令牌并入库")
    common(run)
    run.add_argument("--token", action="append")
    run.add_argument("--tokens-file")
    run.set_defaults(func=cmd_run)

    doctor = sub.add_parser("doctor", help="探针")
    common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    keys = sub.add_parser("keys", help="打印池里的令牌")
    common(keys)
    keys.set_defaults(func=cmd_keys)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except LineError as exc:
        print(json.dumps({"ok": False, "kind": exc.kind, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
