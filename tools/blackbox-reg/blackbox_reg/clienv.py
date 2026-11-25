"""Blackbox CLI 运行环境模拟（"下载并逆向 → 造一个能跑的沙箱 → 让它打本网关"）。

逆向结论（`@blackbox_ai/blackbox-cli` 1.0.2 + `blackbox-cli-core` 1.0.2，证据见
docs/BLACKBOX-CLI-RE.md）：

  * 配置目录 = `$HOME/.blackboxcli/`：
        settings.json          全局设置（security.auth.*）
        oauth_creds.json       设备码 OAuth 的令牌（BLACKBOX_OAUTH 用）
        google_accounts.json   Google 登录档
  * 选 provider 走 `security.auth.selectedProvider` + `selectedType`；
    `AuthType.USE_BLACKBOX_API` 的字面量是 `"blackbox-api"`。
  * 走 blackbox-api 时 baseUrl 默认 `https://api.blackbox.ai/v1`（环境变量
    `BLACKBOX_API_BASE_URL` 可覆盖），OpenAI SDK 再拼 `/chat/completions`。
  * 所以把它指向本网关只要三件事：settings.json 里换 baseUrl/apiKey/model、
    HOME 指到沙箱目录、可选再加环境变量兜底。

这个模块做的事就是"造沙箱 + 跑一次最小调用"，用来验证：
    编程工具（CLI 自己就是 agent）→ 本网关 /p/blackbox/v1 → 上游
整条链路是通的，且工具调用不会因为反代层丢字段而挂掉。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

CLI_DIR = ".blackboxcli"
# CLI 的 AuthType 枚举字面量
AUTH_TYPE_BLACKBOX_API = "blackbox-api"

DEFAULT_TOOLS_PROBE = (
    "Call the shell tool to print the current working directory, then answer with one line."
)


def settings_payload(*, base_url: str, api_key: str, model: str) -> dict[str, Any]:
    """生成 settings.json 的最小可用子集。

    CLI 读的是 `security.auth.selectedProvider/selectedType` +
    `security.auth.blackbox.{apiKey,baseUrl,model}`（见 config/auth.js 的
    `loadProviderCredentialsFromSettings`），所以这里只写这几项，别的让它用默认。
    """
    return {
        "security": {
            "auth": {
                "selectedType": AUTH_TYPE_BLACKBOX_API,
                "selectedProvider": "blackbox",
                "blackbox": {
                    "apiKey": api_key,
                    "baseUrl": base_url.rstrip("/"),
                    "model": model,
                },
            }
        },
        "general": {"previewFeatures": True},
    }


def prepare_env(
    *,
    env_dir: str,
    base_url: str,
    api_key: str,
    model: str,
    oauth_creds: dict | None = None,
) -> dict[str, Any]:
    """造一个隔离的 CLI 运行目录，返回路径与需要导出的环境变量。"""
    root = Path(env_dir).expanduser().resolve()
    home = root / "home"
    config_dir = home / CLI_DIR
    config_dir.mkdir(parents=True, exist_ok=True)

    settings_path = config_dir / "settings.json"
    settings = settings_payload(base_url=base_url, api_key=api_key, model=model)
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")

    creds_path = config_dir / "oauth_creds.json"
    if oauth_creds:
        creds_path.write_text(json.dumps(oauth_creds, ensure_ascii=False, indent=2), encoding="utf-8")

    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),          # Windows 上 homedir() 读这个
        "HOMEDRIVE": home.drive or "",
        "HOMEPATH": str(home)[len(home.drive):] if home.drive else str(home),
        "BLACKBOX_API_KEY": api_key,
        "BLACKBOX_API_BASE_URL": base_url.rstrip("/"),
        "BLACKBOX_API_MODEL": model,
        # 别让 CLI 的后台更新检查把沙箱环境搞脏
        "NO_UPDATE_NOTIFIER": "1",
        "BLACKBOX_DISABLE_UPDATE_CHECK": "1",
    }
    _chmod(settings_path, 0o600)
    if creds_path.exists():
        _chmod(creds_path, 0o600)

    return {
        "root": str(root),
        "home": str(home),
        "configDir": str(config_dir),
        "settings": str(settings_path),
        "credentials": str(creds_path) if creds_path.exists() else "",
        "workspace": str(workspace),
        "env": env,
        "settingsPreview": settings,
    }


def find_cli(bin_name: str = "blackbox") -> str:
    """找可执行的 CLI：显式二进制 → PATH → 本地安装目录 → 从下载的包里直接跑。"""
    if bin_name and os.path.sep in bin_name and Path(bin_name).exists():
        return str(Path(bin_name).resolve())
    found = shutil.which(bin_name)
    if found:
        return found
    # 官方安装脚本的默认位置
    candidates = [
        Path.home() / ".local" / "bin" / ("blackbox.cmd" if os.name == "nt" else "blackbox"),
        Path.home() / ".blackbox-cli-v2" / "blackbox.mjs",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def build_command(cli_path: str, *, prompt: str, model: str, workspace: str) -> list[str]:
    """拼一次非交互调用。

    `blackbox -p <prompt>` 是 CLI 的非交互入口（与 gemini-cli 同源），
    这里显式带 --model 与工作目录，避免它去读真实 HOME 里的旧设置。
    """
    if cli_path.lower().endswith(".mjs"):
        base = [shutil.which("node") or "node", cli_path]
    else:
        base = [cli_path]
    return base + [
        "--model",
        model,
        "--approval-mode",
        "yolo",
        "-p",
        prompt,
    ]


def run_cli(
    *,
    cli_path: str,
    env_info: dict[str, Any],
    prompt: str = DEFAULT_TOOLS_PROBE,
    model: str,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """在沙箱里跑一次 CLI，返回退出码与输出（失败也返回，不抛）。"""
    command = build_command(cli_path, prompt=prompt, model=model, workspace=env_info["workspace"])
    env = dict(os.environ)
    env.update({k: v for k, v in env_info["env"].items() if v})
    env["PWD"] = env_info["workspace"]
    try:
        proc = subprocess.run(
            command,
            cwd=env_info["workspace"],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=(os.name == "nt" and cli_path.lower().endswith(".cmd")),
        )
    except FileNotFoundError:
        return {"ok": False, "reason": "cli_not_found", "command": command}
    except subprocess.TimeoutExpired as cause:
        return {"ok": False, "reason": "timeout", "command": command, "stdout": (cause.stdout or "")[-2000:]}
    return {
        "ok": proc.returncode == 0,
        "exitCode": proc.returncode,
        "command": command,
        "stdout": (proc.stdout or "")[-4000:],
        "stderr": (proc.stderr or "")[-2000:],
    }


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        # Windows 上 chmod 只有只读位有意义，忽略
        pass


def main(argv: list[str] | None = None) -> int:
    """独立入口：`python -m blackbox_reg.clienv --gateway-url … --gateway-key …`"""
    import argparse

    parser = argparse.ArgumentParser(prog="blackbox_reg.clienv", description="Blackbox CLI 沙箱运行环境")
    parser.add_argument("--gateway-url", required=True, help="例如 http://127.0.0.1:8787/p/blackbox/v1")
    parser.add_argument("--gateway-key", required=True, help="网关 sk-dcg-… Key")
    parser.add_argument("--model", default="blackboxai/blackbox-pro")
    parser.add_argument("--env-dir", default="out/cli-env")
    parser.add_argument("--cli", default="blackbox", help="CLI 可执行文件，留空自动找")
    parser.add_argument("--run", action="store_true", help="造完环境顺手跑一次")
    parser.add_argument("--prompt", default=DEFAULT_TOOLS_PROBE)
    args = parser.parse_args(argv)

    info = prepare_env(
        env_dir=args.env_dir,
        base_url=args.gateway_url,
        api_key=args.gateway_key,
        model=args.model,
    )
    print(json.dumps({k: v for k, v in info.items() if k != "settingsPreview"}, ensure_ascii=False, indent=2))

    if not args.run:
        print("\n把上面 env 里的变量导出后运行 blackbox 即可；或加 --run 直接跑一次。")
        return 0

    cli_path = find_cli(args.cli)
    if not cli_path:
        print("找不到 blackbox 可执行文件：先跑 install.ps1，或用 --cli 指定路径。", file=sys.stderr)
        return 2
    result = run_cli(cli_path=cli_path, env_info=info, prompt=args.prompt, model=args.model)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
