"""WaveSpeed 桌面端 / Android APK 逆向分析器（本地静态分析，不联网、不执行样本）。

## 为什么需要这一步

WaveSpeed 的网页端能力有限（只有 OAuth，没有建 key 接口），但官方确实发布了：

  * **WaveSpeed Desktop**：Windows x64 / macOS arm64+x64 / Linux AppImage+deb
  * **Android APK**：`wavespeed.ai/desktop` 与 GitHub Releases，APK 签名 SHA-256 已公示

桌面端在 Settings 里要用户粘 API key，说明它**不携带账号体系**；但它会暴露：
  * 官方 API 的确切调用方式（base URL、重试、任务轮询间隔）；
  * 可能的 additional endpoints（用量、余额、历史）；
  * Electron 情形下的 preload/IPC 契约（能否复用它的会话）。

所以分析器的目标不是"破解"，而是**把桌面端当成官方协议文档**：
从包里恢复真实端点、header 契约与轮询策略，用来校准 worker 的实现。

## 支持的三类输入

| 输入 | 处理 |
| --- | --- |
| Electron 安装包（Windows `.exe` / macOS `.zip` / Linux `.AppImage`） | 尝试定位 `resources/app.asar`，解出里面的 JS/JSON 后扫描 |
| 已解出的目录（`app.asar.unpacked/` 或手动解压结果） | 直接扫描 |
| Android `.apk` | 用 zip 读取 `classes*.dex` 与 `assets/`，做字符串与 URL 扫描 |

## 用法

    # 自动探测（有哪个分析哪个）
    python -m wavespeed_reg.re --detect

    # 指定文件或目录
    python -m wavespeed_reg.re --path "C:/Downloads/WaveSpeed-Setup.exe"
    python -m wavespeed_reg.re --path ./app.asar.unpacked --out ./out/re

    # 只导出端点清单
    python -m wavespeed_reg.re --path ./apk --only endpoints

输出是 JSON + 一份 Markdown 报告，落在 `--out`（默认 `out/re/`）。

## 边界

纯静态：不执行任何样本代码、不发起外部请求、不解密受保护资源。
遇到加壳 / 强混淆时如实报告"未能恢复"，不要臆造端点。
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------- 扫描规则

# 端点：绝对 URL 与相对 API 路径
URL_RE = re.compile(rb"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{6,200}")
API_PATH_RE = re.compile(rb'"(/(?:api|v1|v2|v3|openapi)/[A-Za-z0-9._/{}\-]{2,120})"')

# 关心的关键字（用于给命中打标签）
INTEREST = {
    "auth": (b"authorization", b"bearer", b"api-key", b"x-api-key", b"apikey"),
    "task": (b"prediction", b"/result", b"poll", b"task_id", b"taskid", b"webhook"),
    "key": (b"accesskey", b"api_key", b"apiKey", b"generate", b"apikeys"),
    "quota": (b"balance", b"credit", b"quota", b"usage"),
    "oauth": (b"oauth", b"accounts.google.com", b"github.com/login"),
    "endpoint": (b"api.wavespeed.ai", b"llm.wavespeed.ai", b"wavespeed.ai"),
}

# 噪声：这些 URL 出现在任何 Electron 包里，跟协议无关
NOISE_HOSTS = (
    "w3.org",
    "schemas.",
    "electronjs.org",
    "github.com/electron",
    "googleapis.com/oauth",
    "gstatic.com",
    "mozilla.org",
    "npmjs.com",
    "localhost:9222",
    "chrome-devtools",
)


def _tag(payload: bytes) -> list[str]:
    lowered = payload.lower()
    return [name for name, needles in INTEREST.items() if any(n.lower() in lowered for n in needles)]


def _is_noise(url: str) -> bool:
    lowered = url.lower()
    return any(host in lowered for host in NOISE_HOSTS)


# ---------------------------------------------------------------- 数据结构


@dataclass
class Finding:
    source: str
    kind: str          # url | path | string
    value: str
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "kind": self.kind, "value": self.value, "tags": self.tags}


@dataclass
class Report:
    target: str
    kind: str = "unknown"
    findings: list[Finding] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # -------------------------------------------------------------- 汇总
    def endpoints(self) -> list[str]:
        out: list[str] = []
        for finding in self.findings:
            if finding.kind not in ("url", "path"):
                continue
            if any(t in ("endpoint", "task", "key", "quota") for t in finding.tags) or "wavespeed" in finding.value:
                if finding.value not in out:
                    out.append(finding.value)
        return sorted(out)

    def by_tag(self, tag: str) -> list[str]:
        return sorted({f.value for f in self.findings if tag in f.tags})

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "kind": self.kind,
            "files": self.files,
            "notes": self.notes,
            "errors": self.errors,
            "counts": {
                "findings": len(self.findings),
                "endpoints": len(self.endpoints()),
            },
            "endpoints": self.endpoints(),
            "tags": {
                tag: self.by_tag(tag)
                for tag in ("auth", "task", "key", "quota", "oauth")
            },
            "findings": [f.as_dict() for f in self.findings],
        }

    def to_markdown(self) -> str:
        lines = [
            f"# WaveSpeed 静态分析报告：`{self.target}`",
            "",
            f"- 识别类型：**{self.kind}**",
            f"- 扫描文件：{len(self.files)} 个",
            f"- 命中：{len(self.findings)} 条（端点 {len(self.endpoints())} 个）",
            "",
        ]
        if self.notes:
            lines += ["## 说明", ""] + [f"- {note}" for note in self.notes] + [""]
        if self.errors:
            lines += ["## 异常", ""] + [f"- {err}" for err in self.errors] + [""]

        lines += ["## 端点", ""]
        endpoints = self.endpoints()
        if endpoints:
            lines += [f"- `{value}`" for value in endpoints]
        else:
            lines.append("_未恢复出端点_")
        lines.append("")

        for tag, title in (
            ("auth", "鉴权相关"),
            ("task", "任务/轮询相关"),
            ("key", "Key 相关"),
            ("quota", "额度相关"),
            ("oauth", "OAuth 相关"),
        ):
            values = self.by_tag(tag)
            if not values:
                continue
            lines += [f"## {title}", ""]
            lines += [f"- `{value}`" for value in values[:40]]
            if len(values) > 40:
                lines.append(f"- …… 另有 {len(values) - 40} 条")
            lines.append("")

        lines += [
            "## 对 worker 的影响",
            "",
            "把上面恢复出的端点与 [README](../README.md) 里记录的事实逐条对照：",
            "",
            "1. 若出现**建 key 端点**（`/accesskey` 之外的写接口）→ worker 的协议腿可以建 key，"
            "把 `wave speed_reg/browser.py` 的 `generate_key()` 换成协议调用。",
            "2. 若出现**新的推理域名** → 更新 `src/provider-catalog.mjs` 的 `apiBase` / `taskApiBase`。",
            "3. 若给出**轮询间隔与重试策略** → 校准 `protocol.py` 的 `generate()`（当前 2s 起步、"
            "逐步放宽到 10s）。",
            "4. 若桌面端只做「粘 key」→ 说明它不带账号体系，逆向对注册链路无增益，"
            "保持 OAuth-only 结论。",
            "",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------- 扫描器


def _scan_bytes(payload: bytes, source: str, report: Report, seen: set[str]) -> None:
    for match in URL_RE.finditer(payload):
        try:
            url = match.group(0).decode("utf-8", "ignore").rstrip(".,);'\"")
        except Exception:  # noqa: BLE001
            continue
        if not url or _is_noise(url) or url in seen:
            continue
        seen.add(url)
        report.findings.append(Finding(source=source, kind="url", value=url, tags=_tag(payload)))

    for match in API_PATH_RE.finditer(payload):
        try:
            path = match.group(1).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            continue
        if not path or path in seen:
            continue
        seen.add(path)
        report.findings.append(Finding(source=source, kind="path", value=path, tags=_tag(payload)))


def _iter_zip(path: Path) -> Iterable[tuple[str, bytes]]:
    """读 zip 类容器（asar 不是 zip，但 .apk / .zip / .whl 是）。"""
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if not name.lower().endswith(
                (".js", ".mjs", ".cjs", ".json", ".html", ".txt", ".xml", ".properties", ".dex", ".smali")
            ):
                continue
            try:
                yield name, archive.read(info)
            except Exception:  # noqa: BLE001 - 加密或损坏的成员
                continue


def _extract_asar(path: Path) -> tuple[list[tuple[str, bytes]], list[str]]:
    """最小 asar 读取器：header 是 pickle 风格的 JSON，文件按 offset 顺序排列。

    不追求完整实现 —— 只要能把 JS/JSON 挖出来扫描即可；遇到结构异常就报告。
    """
    notes: list[str] = []
    out: list[tuple[str, bytes]] = []
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
            if len(header) < 16:
                return out, ["asar 头不足 16 字节，可能不是 asar"]
            # 前 4 字节是 UInt32LE = 4（pickle 长度字段）
            size_pickle = int.from_bytes(header[12:16], "little")
            json_bytes = handle.read(size_pickle)
            try:
                manifest = json.loads(json_bytes.decode("utf-8", "ignore"))
            except json.JSONDecodeError:
                return out, ["asar header 不是合法 JSON，跳过"]
            base = 16 + size_pickle

            def walk(node: dict[str, Any], prefix: str) -> None:
                for name, entry in (node.get("files") or {}).items():
                    full = f"{prefix}/{name}" if prefix else name
                    if isinstance(entry, dict) and "files" in entry:
                        walk(entry, full)
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("unpacked"):
                        notes.append(f"{full} 未打包（unpacked），需从 app.asar.unpacked 单独读")
                        continue
                    if not full.lower().endswith((".js", ".mjs", ".cjs", ".json", ".html")):
                        continue
                    try:
                        offset = int(entry.get("offset", 0))
                        length = int(entry.get("size", 0))
                        handle.seek(base + offset)
                        out.append((full, handle.read(length)))
                    except Exception:  # noqa: BLE001
                        continue

            walk(manifest, "")
    except OSError as exc:
        return out, [f"读取 asar 失败：{exc}"]
    return out, notes


def _guess_kind(path: Path) -> str:
    lower = path.name.lower()
    if lower.endswith(".apk"):
        return "android-apk"
    if lower.endswith(".asar"):
        return "electron-asar"
    if lower.endswith((".exe", ".msi", ".dmg", ".appimage", ".deb", ".zip", ".tar.gz")):
        return "desktop-package"
    if path.is_dir():
        return "directory"
    return "unknown"


def analyze(path: str | Path, *, out_dir: str | Path = "out/re", max_files: int = 4000) -> Report:
    target = Path(path)
    report = Report(target=str(target), kind=_guess_kind(target))
    seen: set[str] = set()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    if not target.exists():
        report.errors.append(f"路径不存在：{target}")
        _write(report, out)
        return report

    if target.is_dir():
        count = 0
        for file in sorted(target.rglob("*")):
            if not file.is_file() or count >= max_files:
                continue
            if not file.name.lower().endswith((".js", ".mjs", ".cjs", ".json", ".html", ".dex", ".txt")):
                continue
            try:
                payload = file.read_bytes()
            except OSError as exc:
                report.errors.append(f"{file.name}: {exc}")
                continue
            report.files.append(str(file.relative_to(target)))
            _scan_bytes(payload, str(file.relative_to(target)), report, seen)
            count += 1
        if not report.files:
            report.notes.append("目录里没有可扫描的 JS/JSON/DEX；确认是否已解包")

    elif report.kind == "electron-asar":
        entries, notes = _extract_asar(target)
        report.notes.extend(notes)
        for name, payload in entries:
            report.files.append(name)
            _scan_bytes(payload, name, report, seen)

    elif report.kind == "android-apk":
        try:
            for name, payload in _iter_zip(target):
                report.files.append(name)
                _scan_bytes(payload, name, report, seen)
        except zipfile.BadZipFile as exc:
            report.errors.append(f"不是合法 zip/apk：{exc}")
        # 顺带报告签名信息（APK 签名块里的字符串能确认包名）
        try:
            raw = target.read_bytes()
            for match in re.finditer(rb"package=\"([A-Za-z0-9_.]{6,80})\"", raw):
                report.notes.append(f"AndroidManifest package: {match.group(1).decode()}")
        except OSError:
            pass

    elif report.kind == "desktop-package":
        # 安装包通常是压缩/自解压容器：先当 zip 试一次（zip/AppImage/deb 都可能可读）
        report.notes.append(
            "安装包按容器处理：能当 zip 读就直接读；读不到请先手工解包再指向解包目录"
            "（Electron 应用的目标是 resources/app.asar）"
        )
        try:
            for name, payload in _iter_zip(target):
                report.files.append(name)
                _scan_bytes(payload, name, report, seen)
        except (zipfile.BadZipFile, OSError):
            report.notes.append("该安装包不是 zip 容器（可能是 NSIS/DMG），需要外部解包工具")
    else:
        report.notes.append(f"未识别的输入类型：{report.kind}")

    if not report.findings:
        report.notes.append("没有命中任何端点：可能加壳/强混淆，或该包确实只有 UI 代码")

    _write(report, out)
    return report


def _write(report: Report, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(report.target).name)[:60] or "target"
    (out / f"{stem}.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / f"{stem}.md").write_text(report.to_markdown(), encoding="utf-8")


# ------------------------------------------------------- 运行环境模拟


def simulate_endpoints(report: Report) -> dict[str, Any]:
    """把恢复出的端点整理成"worker 该怎么配"的建议。

    这不是执行样本，而是把静态分析结果翻译成配置项 —— 也就是"模拟运行环境"：
    拿官方客户端的真实路径，填进我们的 fixture 配置里验证协议假设。

    绝对 URL 优先于相对路径：相对路径只能说明"存在这个接口"，拼不出 base。
    """
    endpoints = report.endpoints()
    absolute = [url for url in endpoints if url.startswith(("http://", "https://"))]
    relative = [url for url in endpoints if url not in absolute]

    llm: list[str] = []
    task: list[str] = []
    panel: list[str] = []
    for url in absolute:
        lowered = url.lower()
        if "llm." in lowered:
            llm.append(url)
        elif "/api/v3" in lowered or "prediction" in lowered:
            task.append(url)
        elif "wavespeed.ai" in lowered:
            panel.append(url)

    # 绝对 URL 里没有 task 基址时，用相对路径反推（/api/v3/... → https://api.wavespeed.ai/api/v3）
    if not task:
        for path in relative:
            lowered = path.lower()
            if "/api/v3" in lowered:
                task.append("https://api.wavespeed.ai/api/v3")
                break
            if "/v3/async" in lowered:
                task.append("https://api.novita.ai")
                break

    def first(candidates: list[str], fallback: str, *, strip_suffix: tuple[str, ...] = ()) -> str:
        for candidate in candidates:
            value = candidate.rstrip("/")
            for suffix in strip_suffix:
                if value.lower().endswith(suffix):
                    value = value[: -len(suffix)].rstrip("/")
            if value:
                return value
        return fallback

    return {
        "target": report.target,
        "kind": report.kind,
        "suggested_config": {
            "base_url": first(panel, "https://wavespeed.ai"),
            # LLM 基址要保留 /v1，task 基址要剥掉具体路径
            "api_base": first(llm, "https://llm.wavespeed.ai/v1"),
            "task_base": first(task, "https://api.wavespeed.ai/api/v3", strip_suffix=("/predictions",)),
        },
        "found": {"panel": panel, "llm": llm, "task": task, "relative": relative},
        "auth_hints": report.by_tag("auth"),
        "key_hints": report.by_tag("key"),
        "notes": [
            "把这个 config 粘进 tools/wavespeed-reg/config.yaml，用 --path 里的真实端点跑 doctor 验证",
            "若 key_hints 里出现写接口（例如 /accesskey/generate 这类），说明可以补一条协议建 key 的腿",
        ],
    }


def detect_candidates() -> list[Path]:
    """在常见位置找安装包（只读，不下载）。"""
    candidates: list[Path] = []
    patterns = ("*WaveSpeed*", "*wavespeed*")
    roots = [Path.home() / "Downloads", Path.home() / "Desktop", Path(os.environ.get("TEMP", "."))]
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            try:
                candidates.extend(sorted(root.glob(pattern))[:10])
            except OSError:
                continue
    return [path for path in candidates if path.is_file() or path.is_dir()]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or "-h" in args or "--help" in args:
        print(__doc__)
        return 0

    def take(flag: str, default: str = "") -> str:
        if flag in args:
            index = args.index(flag)
            if index + 1 < len(args):
                return args[index + 1]
        return default

    out_dir = take("--out", "out/re")
    only = take("--only", "")

    targets: list[Path] = []
    if "--detect" in args:
        targets = detect_candidates()
        if not targets:
            print("没有在 Downloads / Desktop / TEMP 里找到 WaveSpeed 安装包；用 --path 指定。")
            return 1
    else:
        raw = take("--path", "")
        if not raw:
            print("需要 --path <文件或目录>，或用 --detect 自动探测。")
            return 2
        targets = [Path(raw)]

    reports = []
    for target in targets:
        report = analyze(target, out_dir=out_dir)
        payload = report.to_dict()
        if only == "endpoints":
            payload = {"target": payload["target"], "endpoints": payload["endpoints"]}
        else:
            payload["simulation"] = simulate_endpoints(report)
        reports.append(payload)

    print(json.dumps(reports if len(reports) > 1 else reports[0], ensure_ascii=False, indent=2))
    return 0 if all(not r.get("errors") for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
