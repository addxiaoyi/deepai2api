"""Camoufox 启动参数的构造与校验（独立成文件，因为这里踩过坑）。

## 实测出来的三条硬约束（camoufox 0.5.9）

本机实测（`Camoufox(**launch)` 真启动）：

  1. **`timezone=` 不是合法参数**。传了会直接
     `TypeError: BrowserType.launch() got an unexpected keyword argument 'timezone'`
     —— 不是"静默忽略"，是**启动失败**。
     时区只能通过 `geoip=<IP>` 让 camoufox 自己推导。

  2. **`os` 传多元素列表会让 fpgen 无解**：
     `os=["windows","macos"]` → `InvalidConstraints: ... yielded no possible values for "os"`
     `os="windows"` / `os=["windows"]` 都正常。
     所以每次只传一个 OS，由调用方（fingerprint_for）决定，指纹与 UA 才自洽。

  3. **`screen` 与 `window` 必须相容**：window 尺寸要落在 screen 约束范围内，
     否则同样是 `InvalidConstraints`。按平台给可行的区间。

另外两个语义选择（不是硬约束，但影响防封质量）：

  * `fingerprint_preset=True` 会改用**捆绑的真实指纹预设**（随机抽一个）。
    多账号批量跑时抽到同一个预设 = 指纹聚类，所以默认**不传**，让 fpgen 每次生成
    唯一指纹（已实测两次启动 screen 不同）。
  * `pin_cpu_cores=True` 把浏览器钉到指纹声称的核数上。关掉时 camoufox 会报**宿主**
    核数（本机 32 核，很显眼）。代价是真实 CPU 开销 + 并发启动串行化。
    默认**开启**，因为"泄露宿主核数"比"启动慢一点"危险得多。

## 为什么单独成文件

`_camoufox_launch()` 是纯函数（只依赖传入的指纹与代理），所以测试可以：
  * 断言它返回的**每个键**都在 camoufox 的合法参数白名单里（防回归）；
  * 断言不会出现 `timezone`；
  * 断言 `os` 是单值。

合法白名单从 `camoufox.utils.launch_options` 的签名里取，不手抄。
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

# 各平台的 screen 可行区间：窗口尺寸必须落在区间内，否则 fpgen 无解。
SCREEN_BOUNDS: dict[str, dict[str, int]] = {
    "windows": {"min_width": 1366, "min_height": 768, "max_width": 1920, "max_height": 1080},
    "macos": {"min_width": 1440, "min_height": 900, "max_width": 2560, "max_height": 1600},
    "linux": {"min_width": 1366, "min_height": 768, "max_width": 1920, "max_height": 1080},
}
WINDOW_SIZE: dict[str, tuple[int, int]] = {
    "windows": (1366, 768),
    "macos": (1440, 900),
    "linux": (1366, 768),
}


def normalize_os(value: str) -> str:
    """把任意写法收敛成 camoufox 认的三个值之一。"""
    text = str(value or "").strip().lower()
    if text.startswith("mac") or text == "darwin":
        return "macos"
    if text.startswith("linux"):
        return "linux"
    return "windows"


def proxy_host(proxy: str) -> str:
    """从代理 URL 里取出 host（`http://u:p@1.2.3.4:8080` → `1.2.3.4`）。"""
    if not proxy:
        return ""
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    return parsed.hostname or ""


def geoip_target(proxy: str, *, resolve: bool = True) -> str:
    """camoufox 的 `geoip` 只接受**IP 字面量**（传域名会 `InvalidIP` 硬报错）。

    所以这里把代理 host 变成 IP：
      * 已经是 IPv4/IPv6 → 直接用（这就是出口 IP，最准）；
      * 是域名 → 本地 DNS 解析一次（`socket.gethostbyname`，不发起应用的网络请求）；
      * 解析不到 / 没有代理 → 返回空串，调用方就不传 geoip。

    **绝不返回 `True`**：`geoip=True` 会让 camoufox 检测**本机**出口 IP，与代理不一致时
    时区/经纬度会比不设置更离谱。
    """
    host = proxy_host(proxy)
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not resolve:
        return ""
    try:
        return socket.gethostbyname(host)
    except OSError:
        return ""


def camoufox_legal_keys() -> set[str]:
    """camoufox 认可的 launch 参数集合（从真签名里取，不手抄）。

    camoufox 没装时返回空集合 —— 调用方应把"校验不了"当成通过（不要把测试变成
    对可选依赖的硬依赖）。
    """
    try:
        import inspect

        from camoufox import utils as cf_utils
    except Exception:  # noqa: BLE001 - 可选依赖
        return set()
    try:
        signature = inspect.signature(cf_utils.launch_options)
    except (TypeError, ValueError):
        return set()
    keys: set[str] = set()
    for name, param in signature.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        keys.add(name)
    return keys


def _screen_for(os_name: str) -> Any:
    """按平台造 Screen 约束；camoufox 没装时返回 None（调用方会跳过该键）。"""
    try:
        from camoufox.fingerprints import Screen
    except Exception:  # noqa: BLE001 - 可选依赖
        return None
    bounds = SCREEN_BOUNDS.get(os_name) or SCREEN_BOUNDS["windows"]
    return Screen(**bounds)


def _proxy_dict(proxy: str) -> dict[str, str] | None:
    if not proxy:
        return None
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    out: dict[str, str] = {"server": server}
    if parsed.username:
        out["username"] = parsed.username
    if parsed.password:
        out["password"] = parsed.password
    return out


def camoufox_launch_options(
    *,
    fingerprint: dict[str, Any],
    proxy: str = "",
    headless: bool = True,
    humanize: float = 1.0,
    locale: str = "en-US",
    geo_align: bool = True,
    use_preset: bool = False,
    pin_cpu_cores: bool = True,
    canvas_noise: bool | None = None,
) -> dict[str, Any]:
    """构造一份**可启动**的 camoufox launch 参数。

    返回的 dict 保证：
      * 没有 `timezone` 键；
      * `os` 是单值字符串；
      * `window` 落在 `screen` 区间内；
      * 所有键都在 camoufox 的合法参数里（除非 camoufox 未安装，此时只含基础键）。
    """
    os_name = normalize_os(fingerprint.get("platform") or "windows")

    options: dict[str, Any] = {
        "headless": bool(headless),
        # humanize 的语义是"光标移动的最大时长（秒）"，1.0 倍率 → 约 2.2 秒
        "humanize": round(2.2 * max(0.2, float(humanize)), 2),
        # 单值！列表会让 fpgen 在 os 维度无解
        "os": os_name,
        "locale": [fingerprint.get("locale") or locale],
        "block_webrtc": True,
        "enable_cache": True,
        "pin_cpu_cores": bool(pin_cpu_cores),
        # 身份声称的 OS/GPU 与宿主不同时 camoufox 会告警；这是刻意的。
        "i_know_what_im_doing": True,
    }

    screen = _screen_for(os_name)
    if screen is not None:
        options["screen"] = screen
        options["window"] = WINDOW_SIZE.get(os_name) or WINDOW_SIZE["windows"]

    if use_preset:
        # 想用捆绑的真实预设时才传；批量场景不建议（会抽到重复预设）
        options["fingerprint_preset"] = True

    if canvas_noise is not None:
        options["canvas_noise"] = bool(canvas_noise)

    if geo_align:
        target = geoip_target(proxy)
        if target:
            # 时区/经纬度/国家/locale 全部按出口 IP 推导 —— 这是唯一能改时区的入口
            options["geoip"] = target

    proxy_opts = _proxy_dict(proxy)
    if proxy_opts:
        options["proxy"] = proxy_opts

    return options


def describe_launch(options: dict[str, Any]) -> str:
    """给日志用的一句话摘要：内核、OS、时区来源。"""
    os_name = options.get("os", "?")
    if options.get("geoip"):
        tz = f"geoip={options['geoip']}（时区/经纬度跟随出口）"
    else:
        tz = "时区=宿主（未传 geoip：无代理或域名解析失败）"
    extras = []
    if options.get("fingerprint_preset"):
        extras.append("preset")
    if options.get("pin_cpu_cores"):
        extras.append("pin_cpu")
    return f"camoufox os={os_name} {tz}" + (f" [{','.join(extras)}]" if extras else "")
