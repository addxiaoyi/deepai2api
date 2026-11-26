"""算力券（Lab Voucher）/ Devpoints 兑换 / 免费模型（Token Factory）。

端点全部来自 bundle 实测（`assets/points-redeem-*.js` 与主 bundle）：

    POST /api/User/GetLabVoucherExchangeRecords  {PageIndex,PageSize,Target}   兑换记录
    GET  /api/User/GetLabVoucherExchangeConfig                                兑换配置（面额/所需积分）
    POST /api/User/ExchangeLabVoucher            {...}                        兑换算力券
    GET  /api/User/GetCreativeFactoryExchangeStatus                            创意工厂兑换状态
    POST /api/User/ExchangeCreativeFactory       {...}                        创意工厂兑换
    POST /api/User/SendLabExchangePhoneCode                                   兑换前：发手机验证码
    POST /api/User/ConfirmLabExchangePhoneCode   {...}                        兑换前：验手机
    POST /api/User/SendLabExchangeEmailCode                                   兑换前：发邮箱验证码
    POST /api/User/ConfirmLabExchangeEmailCode   {...}                        兑换前：验邮箱
    GET  /api/DevpointsReward/GetActivities                                   Devpoints 活动
    POST /api/DevpointsReward/Quote              {...}                        报价（多少积分换什么）
    POST /api/DevpointsReward/Redeem             {...}                        兑换
    POST /api/DevpointsReward/Apply              {...}                        申请

免费模型（Token Factory）：前端路由 `/tokenfactory`，页面数据里有
`Access.BaseUrl.Value`（OpenAI 兼容基址）与 `Items[{Key,Label,Value}]`（计费项）。
它的**数据接口在另一个 chunk 里**，所以这里用候选探测 + 解析页面，并把结果缓存。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from linekit.errors import LineError
from linekit.net import build_client, request

from . import site

# Token Factory 的候选数据接口（按前端命名习惯猜，命中即缓存）
TOKEN_FACTORY_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("/TokenFactory/GetInfo", "GET"),
    ("/TokenFactory/GetTokenFactoryInfo", "GET"),
    ("/User/GetTokenFactoryInfo", "GET"),
    ("/User/GetTokenFactory", "GET"),
    ("/User/GetModelAccess", "GET"),
    ("/TokenFactory/GetAccess", "GET"),
    ("/User/GetFreeModelAccess", "GET"),
)


def _headers(cookie: str, token: str = "") -> dict[str, str]:
    out = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        out["Authorization"] = f"Bearer {token}"
    if cookie:
        out["Cookie"] = cookie
    return out


def _call(
    cookie: str,
    path: str,
    *,
    method: str = "GET",
    body: Any = None,
    params: dict[str, Any] | None = None,
    token: str = "",
    proxy: str = "",
    timeout: float = 30.0,
) -> Any:
    client = build_client(proxy=proxy, timeout=timeout)
    try:
        kwargs: dict[str, Any] = {"headers": _headers(cookie, token)}
        if body is not None:
            kwargs["json"] = body
        if params:
            kwargs["params"] = params
        response = request(client, method, f"{site.API}{path}", **kwargs)
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": (response.text or "")[:300]}
        if response.status_code >= 400:
            raise LineError(f"{path}: HTTP {response.status_code}", detail=payload)
        return payload
    finally:
        client.close()


def _data(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("data", "Data"):
            if key in payload:
                return payload[key]
    return payload


# ---------------------------------------------------------------- 兑换（算力券）


def voucher_config(cookie: str, *, token: str = "", proxy: str = "") -> dict[str, Any]:
    """兑换配置：面额 / 所需 Devpoints / 是否要手机或邮箱验证。"""
    payload = _call(cookie, "/User/GetLabVoucherExchangeConfig", token=token, proxy=proxy)
    data = _data(payload)
    return data if isinstance(data, dict) else {"raw": payload}


def voucher_records(cookie: str, *, target: int = 0, page_size: int = 50, token: str = "",
                    proxy: str = "") -> list[dict[str, Any]]:
    payload = _call(
        cookie,
        "/User/GetLabVoucherExchangeRecords",
        method="POST",
        body={"PageIndex": 1, "PageSize": page_size, "Target": target},
        token=token,
        proxy=proxy,
    )
    data = _data(payload)
    rows = data.get("list") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        rows = data.get("List") if isinstance(data, dict) else []
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "id": str(row.get("Id") or row.get("RecordId") or ""),
                "voucher": str(row.get("VoucherCode") or row.get("Code") or row.get("Voucher") or ""),
                "hours": row.get("Hours") or row.get("Duration") or row.get("GpuHours"),
                "points": row.get("Points") or row.get("CostPoints") or row.get("Devpoints"),
                "status": row.get("Status"),
                "createdAt": row.get("CreateTime") or row.get("CreatedAt") or "",
                "raw": row,
            }
        )
    return out


def exchange_voucher(cookie: str, *, amount: int = 1, extra: dict[str, Any] | None = None, token: str = "",
                     proxy: str = "") -> Any:
    """兑换算力券。body 以前端常见形状提交（Amount/Count/Target），站点若要求别的字段，
    `GetLabVoucherExchangeConfig` 会告诉我们，这里也支持 `extra` 透传。"""
    body: dict[str, Any] = {"Amount": amount, "Count": amount, "Num": amount}
    if extra:
        body.update(extra)
    return _call(cookie, "/User/ExchangeLabVoucher", method="POST", body=body, token=token, proxy=proxy)


def creative_status(cookie: str, *, token: str = "", proxy: str = "") -> dict[str, Any]:
    payload = _call(cookie, "/User/GetCreativeFactoryExchangeStatus", token=token, proxy=proxy)
    data = _data(payload)
    return data if isinstance(data, dict) else {"raw": payload}


def exchange_creative(cookie: str, *, extra: dict[str, Any] | None = None, token: str = "",
                      proxy: str = "") -> Any:
    return _call(cookie, "/User/ExchangeCreativeFactory", method="POST", body=extra or {}, token=token, proxy=proxy)


def devpoints_activities(cookie: str, *, token: str = "", proxy: str = "") -> list[dict[str, Any]]:
    payload = _call(cookie, "/DevpointsReward/GetActivities", token=token, proxy=proxy)
    data = _data(payload)
    rows = data if isinstance(data, list) else (data.get("list") if isinstance(data, dict) else [])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def devpoints_quote(cookie: str, *, activity_id: str = "", extra: dict[str, Any] | None = None, token: str = "",
                    proxy: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {"ActivityId": activity_id, "Id": activity_id}
    if extra:
        body.update(extra)
    payload = _call(cookie, "/DevpointsReward/Quote", method="POST", body=body, token=token, proxy=proxy)
    data = _data(payload)
    return data if isinstance(data, dict) else {"raw": payload}


def devpoints_redeem(cookie: str, *, activity_id: str = "", extra: dict[str, Any] | None = None,
                     token: str = "", proxy: str = "") -> Any:
    body: dict[str, Any] = {"ActivityId": activity_id, "Id": activity_id}
    if extra:
        body.update(extra)
    return _call(cookie, "/DevpointsReward/Redeem", method="POST", body=body, token=token, proxy=proxy)


# ------------------------------------------------------------- 免费模型（Token Factory）


def _scan_access(blob: Any) -> dict[str, Any]:
    """从任意 JSON 里挖出 `Access.BaseUrl` 与 pricing 项（前端就是这个结构）。"""
    found: dict[str, Any] = {"baseUrl": "", "items": [], "raw": None}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            access = node.get("Access") or node.get("access")
            if isinstance(access, dict):
                base = access.get("BaseUrl") or access.get("baseUrl")
                value = base.get("Value") if isinstance(base, dict) else base
                if isinstance(value, str) and value.startswith("http"):
                    found["baseUrl"] = value
                    found["raw"] = node
                items = node.get("Items") or (node.get("Pricing") or {}).get("Items") if node.get("Pricing") else node.get("Items")
                if isinstance(items, list):
                    found["items"] = [
                        {"key": str(item.get("Key") or ""), "label": str(item.get("Label") or ""), "value": item.get("Value")}
                        for item in items
                        if isinstance(item, dict)
                    ]
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(blob)
    return found


def token_factory(
    cookie: str,
    *,
    token: str = "",
    proxy: str = "",
    cache_path: str | Path | None = None,
) -> dict[str, Any]:
    """免费模型接入信息：base url + 计费项。先按缓存，再逐个试候选接口，最后解析页面。"""
    cache = Path(cache_path) if cache_path else None
    if cache and cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("baseUrl"):
                cached["from"] = "cache"
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    for path, method in TOKEN_FACTORY_CANDIDATES:
        try:
            payload = _call(cookie, path, method=method, token=token, proxy=proxy)
        except LineError:
            continue
        scanned = _scan_access(payload)
        if scanned["baseUrl"]:
            scanned["from"] = f"{method} {path}"
            if cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(scanned, ensure_ascii=False, indent=2), encoding="utf-8")
            return scanned

    # 兜底：直接把 /tokenfactory 页面扒下来找 base url
    client = build_client(proxy=proxy, timeout=30.0)
    try:
        response = request(client, "GET", f"{site.BASE}/tokenfactory", headers=_headers(cookie, token))
        text = response.text or ""
        found = re.search(r"https://[a-z0-9.\-]*(?:api|infer|llm)[a-z0-9.\-]*\.amd\.com/?[a-z0-9/_.\-]*", text, re.I)
        if found:
            result = {"baseUrl": found.group(0), "items": [], "from": "page-scan"}
            if cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result
    finally:
        client.close()
    return {"baseUrl": "", "items": [], "from": "unknown"}


# ------------------------------------------------------------------ 汇总（累加）


def account_vouchers(
    cookie: str,
    *,
    token: str = "",
    proxy: str = "",
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """一个账号的"能换多少 / 已经换到多少"：配置 + 记录 + 创意工厂状态。"""
    say = log or (lambda _m: None)
    out: dict[str, Any] = {"config": {}, "records": [], "creative": {}, "hours": 0.0, "newest": None}
    try:
        out["config"] = voucher_config(cookie, token=token, proxy=proxy)
    except LineError as exc:
        say(f"配置读取失败：{exc}")
    try:
        out["records"] = voucher_records(cookie, token=token, proxy=proxy)
    except LineError as exc:
        say(f"兑换记录读取失败：{exc}")
    try:
        out["creative"] = creative_status(cookie, token=token, proxy=proxy)
    except LineError as exc:
        say(f"创意工厂状态失败：{exc}")

    hours = 0.0
    for record in out["records"]:
        value = record.get("hours")
        if isinstance(value, (int, float)):
            hours += float(value)
        elif isinstance(value, str) and value.replace(".", "", 1).isdigit():
            hours += float(value)
    out["hours"] = round(hours, 2)
    if out["records"]:
        out["newest"] = out["records"][0]
    return out


def select_exchange_targets(config: dict[str, Any]) -> dict[str, Any]:
    """从兑换配置里读出"一次兑换要多少积分 / 给多少小时 / 上限"。"""
    out = {"cost": None, "hours": None, "limit": None, "needPhone": None, "needEmail": None, "raw": config}

    def pick(node: Any, *names: str) -> Any:
        if isinstance(node, dict):
            for name in names:
                if name in node and node[name] not in (None, ""):
                    return node[name]
        return None

    out["cost"] = pick(config, "Points", "CostPoints", "Devpoints", "Cost", "NeedPoints")
    out["hours"] = pick(config, "Hours", "Duration", "GpuHours", "VoucherHours")
    out["limit"] = pick(config, "Limit", "MaxCount", "DailyLimit", "ExchangeLimit")
    out["needPhone"] = pick(config, "NeedPhone", "RequirePhone")
    out["needEmail"] = pick(config, "NeedEmail", "RequireEmail")
    return out


def build_ledger(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把多号汇总成一份账本：每个号换到多少小时，合计多少。"""
    accounts = []
    total_hours = 0.0
    total_vouchers = 0
    for row in rows:
        hours = float(row.get("hours") or 0)
        records = row.get("records") or []
        total_hours += hours
        total_vouchers += len(records)
        accounts.append(
            {
                "email": row.get("email", ""),
                "points": row.get("points"),
                "hours": round(hours, 2),
                "vouchers": len(records),
                "codes": [r.get("voucher") for r in records if r.get("voucher")],
                "status": "ok" if row.get("ok", True) else "error",
                "error": row.get("error"),
            }
        )
    return {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "accounts": accounts,
        "totalHours": round(total_hours, 2),
        "totalVouchers": total_vouchers,
    }
