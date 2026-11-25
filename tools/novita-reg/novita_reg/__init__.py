"""Novita AI 注册 worker —— 邮箱密码流 + Turnstile，协议 + 指纹浏览器双腿。

## 站点事实（2026-11 核实，含从线上 SPA chunk 反查的真实接口）

面板 `https://novita.ai`，注册 `/user/register`，登录 `/user/login`。

前端常量（chunk 15825 / 99745）：

    I = "https://api-server.novita.ai"      ← 面板 API 基址（T({base_url: I})）
    fE = "0x4AAAAAAAaG28VfN_OxkED8"         ← Turnstile sitekey

注册调用（chunk page-97f4… SignupForm）：

    a.kz({ email, password, confirmPassword, ...Qu(),
           redirectUrl, cloudflareToken, allowNotification,
           fromInviteCode, agreementVersion })
    → POST {I}/v1/user/register

登录（chunk page-b355… LoginForm）：

    s.iD({ email, password, ...Qu(), redirectUrl, cloudflareToken,
           fromInviteCode, agreementVersion })
    → POST {I}/v1/user/login

Key 管理（chunk 15825）：

    c.eX({...})   → POST {I}/v2/user/key   （expireTime 先经 QP() 归一）
    l.Lw(query)   → GET  {I}/v2/user/key
    u._P(data)    → PUT  {I}/v2/user/key
    E.sV(id)      → DELETE {I}/v2/user/key/{id}
    → 也就是说 **面板有创建 key 的接口**，明文只在创建响应里出现一次。

推理：

    [OI] 兼容  https://api.novita.ai/openai/v1/chat/completions
    Anthropic  https://api.novita.ai/anthropic
    异步图像   POST /v3/async/txt2img → task_id → GET /v3/async/task-result
    余额       GET /openapi/v1/billing/balance/detail （单位 1/10000 USD）

## 与 WaveSpeed 的关键差别

  * 有邮箱密码注册 → 协议腿可以从零建号（只需 Turnstile token）。
  * 有创建 key 的接口 → 协议腿能拿到明文，不必依赖浏览器点 UI。
  * 有实名门槛（部分模型 403）与 voucher 风控（同设备多账号会被撤销）。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
