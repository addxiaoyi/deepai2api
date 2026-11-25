"""MoonAI 注册机 —— 协议引擎 + 指纹浏览器引擎，产物直接进 MoonAI 号池。

包结构对齐 tools/<provider>-reg 的既有约定（见 src/provider-register.mjs 的
NDJSON 事件契约）：

    cli.py      命令行入口（run / push / export / doctor / pool）
    config.py   RunConfig / AntiBanConfig —— 与网关侧 DEFAULT_ANTI_BAN 同名字段
    identity.py persona 与指纹（姓名、用户名、密码、UA、时区、语言）
    mailbox.py  域名邮箱（mail.tm / IMAP catch-all / fixed）
    net.py      httpx 客户端（代理、cookie jar、重试、退避）
    antiban.py  域名速率窗、出口串行化、抖动、失败退避
    protocol.py 注册→验证→登录→OAuth→令牌→组织→API Key（纯 HTTP）
    browser.py  同一套流程的指纹浏览器实现（camoufox / patchright / playwright / CDP）
    store.py    sqlite + jsonl 落盘
    push.py     回推网关 /api/admin/providers/moonai/pool/import
    runner.py   并发调度 + 事件流
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
