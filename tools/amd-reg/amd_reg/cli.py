"""AMD 开发者云 CLI：cookie 导入 → 积分 → 起/停 GPU 实例 → 导出。

    python -m amd_reg import --cookie "..." --email a@b.c     # 或用 --cookies-file（一行一个）
    python -m amd_reg points
    python -m amd_reg launch --email a@b.c
    python -m amd_reg status
    python -m amd_reg remove --email a@b.c
    python -m amd_reg export --format keys
    python -m amd_reg run --json            # 给网关控制器用：等价于 import + points

cookie 怎么来：浏览器登录 developer.amd.com.cn → DevTools → Network → 任一 `/api/` 请求 →
复制请求头 `Cookie:`（或 `Authorization: Bearer …`）。这条线不做自动注册（见 site.py 说明）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

LINEKIT = Path(__file__).resolve().parents[2] / "linekit"
if str(LINEKIT) not in sys.path:
    sys.path.insert(0, str(LINEKIT))

from linekit.errors import LineError  # noqa: E402
from linekit.net import build_client, request  # noqa: E402
from linekit.store import Store  # noqa: E402

from . import courses as course_api  # noqa: E402
from . import vouchers as voucher_api  # noqa: E402
from . import site  # noqa: E402

LINE = "amd"


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _headers(cookie: str, token: str = "") -> dict[str, str]:
    out = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        out["Authorization"] = f"Bearer {token}"
    if cookie:
        out["Cookie"] = cookie
    return out


def _call(cookie: str, path: str, *, method: str = "GET", body: Any = None, token: str = "", timeout: float = 30.0,
          proxy: str = "") -> tuple[int, Any]:
    client = build_client(proxy=proxy, timeout=timeout)
    try:
        response = request(
            client,
            method,
            f"{site.API}{path}",
            headers=_headers(cookie, token),
            **({"json": body} if body is not None else {}),
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": (response.text or "")[:300]}
        return response.status_code, payload
    finally:
        client.close()


def summary(cookie: str, *, token: str = "", proxy: str = "") -> dict[str, Any]:
    """账号概览：积分 / 额度 / 是否有实例。"""
    status, payload = _call(cookie, site.SUMMARY, token=token, proxy=proxy)
    ok = status < 400
    data = payload.get("data") if isinstance(payload, dict) else None
    out: dict[str, Any] = {
        "ok": ok,
        "status": status,
        "raw": payload if not ok else None,
        "points": None,
        "nickname": "",
        "userId": "",
    }
    if ok and isinstance(data, dict):
        for field in ("points", "totalPoints", "devPoints", "score"):
            value = data.get(field)
            if isinstance(value, (int, float)):
                out["points"] = value
                break
        out["nickname"] = str(data.get("nickName") or data.get("nickname") or data.get("userName") or "")
        out["userId"] = str(data.get("userId") or data.get("id") or "")
        out["data"] = data
    has = _call(cookie, site.HAS_INSTANCE, token=token, proxy=proxy)
    out["hasInstance"] = bool((has[1] or {}).get("data")) if has[0] < 400 else None
    return out


def points(cookie: str, *, token: str = "", proxy: str = "") -> dict[str, Any]:
    status, payload = _call(cookie, site.POINTS, token=token, proxy=proxy)
    return {"ok": status < 400, "status": status, "payload": payload}


def launch(cookie: str, *, token: str = "", body: Any = None, proxy: str = "") -> dict[str, Any]:
    """起一台 GPU 实验实例。body 形参按站点实际字段传（默认空对象）。"""
    status, payload = _call(cookie, site.LAUNCH, method="POST", body=body or {}, token=token, proxy=proxy)
    return {"ok": status < 400, "status": status, "payload": payload}


def instance_status(cookie: str, *, token: str = "", proxy: str = "") -> dict[str, Any]:
    status, payload = _call(cookie, site.STATUS, token=token, proxy=proxy)
    return {"ok": status < 400, "status": status, "payload": payload}


def remove(cookie: str, *, token: str = "", proxy: str = "") -> dict[str, Any]:
    status, payload = _call(cookie, site.REMOVE, method="POST", body={}, token=token, proxy=proxy)
    return {"ok": status < 400, "status": status, "payload": payload}


def _store(path: str) -> Store:
    return Store(Path(path or "out") / "accounts.sqlite3")


def _iter_accounts(store: Store) -> list[dict[str, Any]]:
    return store.accounts()


def cmd_import(args: argparse.Namespace) -> int:
    cookies: list[tuple[str, str]] = []
    if args.cookie:
        cookies.append((args.email or f"amd-{int(time.time())}@local", args.cookie))
    if args.cookies_file:
        path = Path(args.cookies_file)
        if not path.exists():
            print(json.dumps({"ok": False, "error": f"file not found: {path}"}, ensure_ascii=False))
            return 2
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:                      # email \t cookie
                email, cookie = line.split("\t", 1)
            elif " " in line and line.split(" ", 1)[1].startswith(("_", "s", "a")):
                email, cookie = line.split(" ", 1)
            else:
                email, cookie = f"amd-{index}@local", line
            cookies.append((email.strip(), cookie.strip()))
    if not cookies:
        print(json.dumps({"ok": False, "error": "用 --cookie 或 --cookies-file（email<TAB>cookie 每行一个）"}, ensure_ascii=False))
        return 2

    store = _store(args.output)
    created = 0
    failed = 0
    results: list[dict[str, Any]] = []
    try:
        for email, cookie in cookies:
            try:
                info = summary(cookie, token=args.token or "", proxy=(args.proxy or [""])[0] if args.proxy else "")
            except LineError as exc:
                failed += 1
                store.record_failure(email=email, kind=exc.kind, reason=str(exc))
                results.append({"email": email, "ok": False, "kind": exc.kind})
                if args.json:
                    _emit({"type": "failed", "email": email, "kind": exc.kind, "reason": str(exc)[:200]})
                continue
            if not info["ok"]:
                failed += 1
                results.append({"email": email, "ok": False, "status": info["status"]})
                if args.json:
                    _emit({"type": "failed", "email": email, "kind": "blocked", "reason": f"HTTP {info['status']}"})
                continue
            record = {
                "email": email,
                "password": "",
                "name": info.get("nickname") or "AMD developer",
                "apiKey": cookie,                       # 这条线的 "key" 就是登录态 cookie
                "keyPrefix": "",
                "emailVerified": None,
                "credits": {
                    "points": info.get("points"),
                    "hasInstance": info.get("hasInstance"),
                    "userId": info.get("userId"),
                },
                "engine": "cookie-import",
            }
            added = store.add(record, engine="cookie-import", proxy="")
            created += 1 if added else 0
            results.append({"email": email, "ok": True, "points": info.get("points"), "hasInstance": info.get("hasInstance")})
            if args.json:
                _emit({"type": "account", "index": created - 1, "account": {**record, "createdAt": ""}})
        summary_out = {"type": "done", "summary": {"checked": len(cookies), "created": created, "failed": failed, "results": results}}
        if args.json:
            _emit(summary_out)
        else:
            print(json.dumps(summary_out["summary"], ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0 if created else 1


def cmd_run(args: argparse.Namespace) -> int:
    """网关控制器入口：等价 import（cookie 从 --cookie/--cookies-file 或环境变量取）。"""
    return cmd_import(args)


def cmd_points(args: argparse.Namespace) -> int:
    store = _store(args.output)
    try:
        rows = _iter_accounts(store)
    finally:
        store.close()
    out = []
    for row in rows:
        try:
            out.append({"email": row["email"], **points(row["api_key"] or "", token=args.token or "")})
        except LineError as exc:
            out.append({"email": row["email"], "ok": False, "error": str(exc)[:160]})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_action(args: argparse.Namespace, action) -> int:
    store = _store(args.output)
    try:
        rows = _iter_accounts(store)
    finally:
        store.close()
    target = [row for row in rows if not args.email or row["email"] == args.email]
    if not target:
        print(json.dumps({"ok": False, "error": "池里没有匹配的账号"}, ensure_ascii=False))
        return 1
    out = []
    for row in target:
        try:
            out.append({"email": row["email"], **action(row["api_key"] or "")})
        except LineError as exc:
            out.append({"email": row["email"], "ok": False, "error": str(exc)[:160]})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if any(item.get("ok") for item in out) else 1


def cmd_export(args: argparse.Namespace) -> int:
    store = _store(args.output)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    ext = {"keys": "txt", "jsonl": "jsonl", "csv": "csv"}[args.format]
    target = Path(args.out) if args.out else Path(args.output or "out") / "exports" / f"amd-{stamp}.{ext}"
    try:
        if args.format == "keys":
            rows = store.export_keys(target)
        elif args.format == "jsonl":
            rows = store.export_jsonl(target)
        else:
            rows = store.export_csv(target)
    finally:
        store.close()
    print(json.dumps({"line": LINE, "file": str(target), "rows": rows, "format": args.format}, ensure_ascii=False))
    return 0


def _pool_rows(store: Store, email: str = "", cookie: str = "") -> list[dict[str, Any]]:
    """取要操作的账号：给了 `--cookie` 就只用它（网关会把池里的 cookie 传进来）。"""
    if cookie:
        return [{"email": email or "cookie@local", "api_key": cookie}]
    rows = _iter_accounts(store)
    return [row for row in rows if not email or row["email"] == email]


def cmd_courses(args: argparse.Namespace) -> int:
    """列出课程 + 我们的学习进度。"""
    store = _store(args.output)
    try:
        rows = _pool_rows(store, args.email, getattr(args, "cookie", ""))
    finally:
        store.close()
    if not rows:
        print(json.dumps({"ok": False, "error": "池里没有账号"}, ensure_ascii=False))
        return 1
    out = []
    for row in rows:
        try:
            listed = course_api.list_courses(row["api_key"] or "", token=args.token or "")
            out.append({"email": row["email"], "ok": True, "courses": listed})
        except LineError as exc:
            out.append({"email": row["email"], "ok": False, "error": str(exc)[:200]})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if any(item["ok"] for item in out) else 1


def cmd_study(args: argparse.Namespace) -> int:
    """自动刷课：把课程刷到目标进度，然后看积分涨没涨。

    这是"通过审核拿 GPU"的自动化部分：AMD 的 Devpoints 主要靠学院课程 / 活动积累。
    """
    store = _store(args.output)
    try:
        rows = _pool_rows(store, args.email, getattr(args, "cookie", ""))
    finally:
        store.close()
    if not rows:
        print(json.dumps({"ok": False, "error": "池里没有账号"}, ensure_ascii=False))
        return 1

    results: list[dict[str, Any]] = []
    for row in rows:
        cookie = row["api_key"] or ""
        email = row["email"]
        try:
            before = course_api.points_delta(cookie, token=args.token or "")
            courses = course_api.list_courses(cookie, token=args.token or "")
            if args.course:
                courses = [c for c in courses if c["id"] == args.course] or [{"id": args.course, "title": args.course}]
            if not courses:
                results.append({"email": email, "ok": False, "error": "没有可刷的课程"})
                if args.json:
                    _emit({"type": "failed", "email": email, "kind": "no_course", "reason": "课程列表为空"})
                continue
            done = []
            for course in courses[: max(0, args.limit) or None]:
                if not args.force and str(course.get("studyStatus") or "") in ("1", "2", "3"):
                    continue
                info = course_api.study_course(
                    cookie,
                    course["id"],
                    token=args.token or "",
                    step=args.step,
                    target_seconds=args.target,
                    max_calls=args.max_calls,
                    pause=args.pause,
                    log=lambda m: _emit({"type": "log", "message": m}) if args.json else None,
                )
                done.append({"course": course["id"], "title": course.get("title", ""), **info})
                if args.json:
                    _emit({"type": "course", "email": email, "course": course["id"], "studyRate": info["studyRate"], "seconds": info["seconds"]})
                if args.max_courses and len(done) >= args.max_courses:
                    break
            after = course_api.points_delta(cookie, token=args.token or "")

            gained = None
            if isinstance(before.get("total"), (int, float)) and isinstance(after.get("total"), (int, float)):
                gained = after["total"] - before["total"]
            results.append({"email": email, "ok": True, "courses": done, "gained": gained, "points": after.get("total")})
            if args.json:
                _emit({"type": "done", "email": email, "courses": len(done), "gained": gained})
        except LineError as exc:
            results.append({"email": email, "ok": False, "error": str(exc)[:200]})
            if args.json:
                _emit({"type": "failed", "email": email, "kind": exc.kind, "reason": str(exc)[:200]})

    if not args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if any(item.get("ok") for item in results) else 1


def _resolve_child(cookie: str, course_id: str, child_id: str, token: str = "") -> str:
    """考试是挂在**子课程**上的：没给 --child 就取该课程第一个章节。"""
    if child_id:
        return child_id
    detail = course_api.course_detail(cookie, course_id, token=token)
    children = detail.get("CourseChildren") or detail.get("courseChildren") or []
    for child in children if isinstance(children, list) else []:
        if isinstance(child, dict) and (child.get("Id") or child.get("CourseChildId")):
            return str(child.get("Id") or child.get("CourseChildId"))
    return ""


def cmd_exam(args: argparse.Namespace) -> int:
    """考试：默认看题；`--submit` 用默认策略交卷；`--auto` 自动答题（含从判卷结果回填答案重交）。"""
    store = _store(args.output)
    try:
        rows = _pool_rows(store, args.email, getattr(args, "cookie", ""))
    finally:
        store.close()
    if not rows:
        print(json.dumps({"ok": False, "error": "池里没有账号"}, ensure_ascii=False))
        return 1

    out = []
    for row in rows:
        cookie = row["api_key"] or ""
        try:
            child_id = _resolve_child(cookie, args.course, args.child or "", token=args.token or "")
            if not child_id:
                out.append({"email": row["email"], "ok": False, "error": "找不到章节 id（用 --child 指定）"})
                continue
            if args.auto:
                info = course_api.auto_exam(
                    cookie,
                    child_id,
                    strategy=args.strategy,
                    rounds=args.rounds,
                    token=args.token or "",
                    log=lambda m: _emit({"type": "log", "message": m}) if args.json else None,
                )
                out.append({"email": row["email"], "child": child_id, **info})
                if args.json:
                    _emit({"type": "exam", "email": row["email"], "child": child_id, "passed": info.get("passed"), "rounds": info.get("rounds")})
                continue
            quiz = course_api.exam_questions(cookie, child_id, token=args.token or "")
            record = {"email": row["email"], "child": child_id, **quiz}
            if args.submit:
                answers = course_api.build_answers(quiz.get("questions") or [], args.strategy)
                record["submit"] = course_api.submit_exam(cookie, child_id, answers, token=args.token or "")
                record["result"] = course_api.exam_result(cookie, child_id, token=args.token or "")
            out.append(record)
            if args.json:
                _emit(
                    {
                        "type": "exam",
                        "email": row["email"],
                        "child": child_id,
                        "questions": len(record.get("questions") or []),
                        "submitted": bool(args.submit),
                    }
                )
        except LineError as exc:
            out.append({"email": row["email"], "ok": False, "error": str(exc)[:200]})
    if not args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if any(item.get("questions") is not None or item.get("passed") is not None for item in out) else 1


def cmd_vouchers(args: argparse.Namespace) -> int:
    """看每个号能换多少算力券、已经换了多少（含兑换码）。"""
    store = _store(args.output)
    try:
        rows = _pool_rows(store, args.email, getattr(args, "cookie", ""))
    finally:
        store.close()
    if not rows:
        print(json.dumps({"ok": False, "error": "池里没有账号"}, ensure_ascii=False))
        return 1
    out = []
    for row in rows:
        try:
            info = voucher_api.account_vouchers(row["api_key"] or "", token=args.token or "")
            out.append({"email": row["email"], "ok": True, **info, "targets": voucher_api.select_exchange_targets(info["config"])})
        except LineError as exc:
            out.append({"email": row["email"], "ok": False, "error": str(exc)[:200]})
    ledger = voucher_api.build_ledger(out)
    if args.json:
        for entry in ledger["accounts"]:
            _emit({"type": "voucher", **entry})
        _emit({"type": "done", "summary": {"hours": ledger["totalHours"], "vouchers": ledger["totalVouchers"]}})
    else:
        print(json.dumps({"ledger": ledger, "details": out}, ensure_ascii=False, indent=2))
    Path(args.output or "out").mkdir(parents=True, exist_ok=True)
    (Path(args.output or "out") / "vouchers.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def cmd_exchange(args: argparse.Namespace) -> int:
    """把积分兑换成算力券（`--all` = 每个号都换一份）。"""
    store = _store(args.output)
    try:
        rows = _pool_rows(store, args.email, getattr(args, "cookie", ""))
    finally:
        store.close()
    if not rows:
        print(json.dumps({"ok": False, "error": "池里没有账号"}, ensure_ascii=False))
        return 1
    results = []
    for row in rows:
        cookie = row["api_key"] or ""
        try:
            config = voucher_api.voucher_config(cookie, token=args.token or "")
            targets = voucher_api.select_exchange_targets(config)
            if args.dry_run:
                results.append({"email": row["email"], "ok": True, "dryRun": True, "targets": targets})
                continue
            payload = voucher_api.exchange_voucher(
                cookie,
                amount=args.amount,
                extra=json.loads(args.extra) if args.extra else None,
                token=args.token or "",
            )
            results.append({"email": row["email"], "ok": True, "targets": targets, "response": payload})
            if args.json:
                _emit({"type": "exchange", "email": row["email"], "ok": True})
        except LineError as exc:
            results.append({"email": row["email"], "ok": False, "error": str(exc)[:200]})
            if args.json:
                _emit({"type": "failed", "email": row["email"], "kind": exc.kind, "reason": str(exc)[:200]})
    if not args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if any(item.get("ok") for item in results) else 1


def cmd_models(args: argparse.Namespace) -> int:
    """免费模型（Token Factory）接入信息：base url + 计费项。"""
    store = _store(args.output)
    try:
        rows = _pool_rows(store, args.email, getattr(args, "cookie", ""))
    finally:
        store.close()
    if not rows:
        print(json.dumps({"ok": False, "error": "池里没有账号"}, ensure_ascii=False))
        return 1
    cache = Path(args.output or "out") / "tokenfactory.json"
    out = []
    for row in rows:
        info = voucher_api.token_factory(row["api_key"] or "", token=args.token or "", cache_path=cache)
        out.append({"email": row["email"], **info})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if any(item.get("baseUrl") for item in out) else 1


def cmd_consolidate(args: argparse.Namespace) -> int:
    """把多号的算力券汇总到主号（master）。

    能做两件事：
      1) 汇总账本：每个号拿到多少小时、总共有多少（写 `out/vouchers.json`）
      2) 在主号上集中兑换：`--redeem --master <email>` 会用主号的积分兑换券，
         并把其它号的兑换码列出来（若站点支持"券码输入到任意账号"，就能真正累加）
    """
    store = _store(args.output)
    try:
        rows = _pool_rows(store)
    finally:
        store.close()
    if not rows:
        print(json.dumps({"ok": False, "error": "池里没有账号"}, ensure_ascii=False))
        return 1

    rows_info = []
    for row in rows:
        try:
            info = voucher_api.account_vouchers(row["api_key"] or "", token=args.token or "")
            rows_info.append({"email": row["email"], "ok": True, **info})
        except LineError as exc:
            rows_info.append({"email": row["email"], "ok": False, "error": str(exc)[:200]})
    ledger = voucher_api.build_ledger(rows_info)

    master = args.master or (rows_info[0]["email"] if rows_info else "")
    master_row = next((row for row in rows if row["email"] == master), None)
    redeemed = None
    if args.redeem and master_row is not None:
        try:
            redeemed = voucher_api.exchange_voucher(
                master_row["api_key"] or "", amount=args.amount, token=args.token or ""
            )
        except LineError as exc:
            redeemed = {"ok": False, "error": str(exc)[:200]}

    summary = {
        "master": master,
        "totalHours": ledger["totalHours"],
        "totalVouchers": ledger["totalVouchers"],
        "accounts": ledger["accounts"],
        "redeemed": redeemed,
        "codes": [code for entry in ledger["accounts"] for code in entry.get("codes", [])],
    }
    if args.json:
        _emit({"type": "done", "summary": summary})
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    Path(args.output or "out").mkdir(parents=True, exist_ok=True)
    (Path(args.output or "out") / "vouchers.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amd_reg", description="AMD 开发者云：cookie 池 + GPU 实例")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--output", default="out")
        p.add_argument("--email", default="")
        p.add_argument("--cookie", default="")
        p.add_argument("--cookies-file", default="")
        p.add_argument("--token", default="", help="有 Bearer token 时优先用它")
        p.add_argument("--proxy", action="append")
        p.add_argument("--json", action="store_true")

    for name, func, help_text in (
        ("import", cmd_import, "导入 cookie 入池（校验 /User/GetCurrentUserDataSummary）"),
        ("run", cmd_run, "给网关用的入口（同 import）"),
        ("points", cmd_points, "查积分流水"),
        ("status", lambda a: cmd_action(a, instance_status), "查 GPU 实例状态"),
        ("launch", lambda a: cmd_action(a, launch), "起 GPU 实例"),
        ("remove", lambda a: cmd_action(a, remove), "销毁 GPU 实例（省钱）"),
    ):
        p = sub.add_parser(name, help=help_text)
        common(p)
        p.set_defaults(func=func)

    courses = sub.add_parser("courses", help="列出学院课程与进度")
    common(courses)
    courses.set_defaults(func=cmd_courses)

    study = sub.add_parser("study", help="自动刷课（拿 Devpoints → 换 GPU 小时）")
    common(study)
    study.add_argument("--course", default="", help="只刷指定课程 id")
    study.add_argument("--limit", type=int, default=0, help="最多看几门课（0=全部）")
    study.add_argument("--max-courses", type=int, default=0)
    study.add_argument("--step", type=int, default=30, help="每次上报的学习秒数（默认 30）")
    study.add_argument("--target", type=int, default=3600, help="每章节刷到的累计秒数（默认 3600）")
    study.add_argument("--max-calls", type=int, default=400)
    study.add_argument("--pause", type=float, default=1.0, help="每次上报间隔（默认 1s）")
    study.add_argument("--force", action="store_true", help="连已完成的课程也重刷")
    study.set_defaults(func=cmd_study)

    exam = sub.add_parser("exam", help="课程考试：看题 / 交卷 / 自动答题")
    common(exam)
    exam.add_argument("--course", required=True)
    exam.add_argument("--child", default="", help="子课程 id（默认取该课程第一个章节）")
    exam.add_argument("--submit", action="store_true", help="按默认策略交一次卷")
    exam.add_argument("--auto", action="store_true", help="自动答题（会读判卷结果回填正确答案再交）")
    exam.add_argument("--strategy", choices=["first", "all"], default="first")
    exam.add_argument("--rounds", type=int, default=3)
    exam.set_defaults(func=cmd_exam)

    vouchers = sub.add_parser("vouchers", help="算力券：配置 / 记录 / 合计（含兑换码）")
    common(vouchers)
    vouchers.set_defaults(func=cmd_vouchers)

    exchange = sub.add_parser("exchange", help="积分兑换算力券")
    common(exchange)
    exchange.add_argument("--amount", type=int, default=1)
    exchange.add_argument("--extra", default="", help="额外字段（JSON 字符串，透传给站点）")
    exchange.add_argument("--dry-run", action="store_true")
    exchange.set_defaults(func=cmd_exchange)

    models = sub.add_parser("models", help="免费模型（Token Factory）接入信息")
    common(models)
    models.set_defaults(func=cmd_models)

    consolidate = sub.add_parser("consolidate", help="把多号算力券汇总到主号")
    common(consolidate)
    consolidate.add_argument("--master", default="", help="主号邮箱（默认取池里第一个）")
    consolidate.add_argument("--redeem", action="store_true", help="在主号上集中兑换")
    consolidate.add_argument("--amount", type=int, default=1)
    consolidate.set_defaults(func=cmd_consolidate)

    export = sub.add_parser("export", help="导出 keys（cookie）| jsonl | csv")
    common(export)
    export.add_argument("--format", choices=["keys", "jsonl", "csv"], default="keys")
    export.add_argument("--out")
    export.set_defaults(func=cmd_export)
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
