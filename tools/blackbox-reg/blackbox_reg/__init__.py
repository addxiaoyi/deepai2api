"""blackbox_reg —— Blackbox AI（app.blackbox.ai）批量注册 / 建 Key worker。

对外契约（与网关 `src/provider-register.mjs` 严格对齐）：

    python -m blackbox_reg run --json -n 5 -c 2 \
        --mail-provider domain --mail-domains own.example.com \
        --base-url https://app.blackbox.ai --api-base https://enterprise.blackbox.ai \
        --keys-per-account 1 --captcha none --engine auto --browser-stack auto \
        --anti-ban '{...}' --key-policy '{}' \
        --push-url http://127.0.0.1:8787 --push-token <管理口令>

stdout 只走 NDJSON 事件（网关逐行解析），日志一律 stderr / 事件里。
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
