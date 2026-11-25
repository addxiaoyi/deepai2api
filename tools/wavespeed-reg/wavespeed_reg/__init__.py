"""WaveSpeedAI 注册 worker —— 协议 + 指纹浏览器双腿，域名邮箱，防封策略。

站点事实（来自官方文档，2026-11 核实）：

  * 面板 https://wavespeed.ai，登录页 /sign-in
  * **没有邮箱密码注册**，只有 Google / GitHub / AWS 三种 OAuth
  * API Key 页 /accesskey，UI 点 Generate 生成，明文只显示一次
  * 没有公开的"创建 key"HTTP 接口
  * 推理两套：
      LLM   https://llm.wavespeed.ai/v1        [OI] 兼容
      生成   https://api.wavespeed.ai/api/v3    异步任务（POST → urls.get → GET result）

所以本 worker 的"注册"由三条腿组成：

  browser  —— 指纹浏览器点 OAuth → 等回调落到 /dashboard → 去 /accesskey 生成并读明文
  session  —— 外部已经登录好的 cookie jar，直接以协议方式读 /accesskey
  manual   —— 别人已经生成的 key，只做校验（GET /v1/models 或 /api/v3/balance）

三条腿产出的记录形状完全一致，store / 号池 / 导出不需要区分来源。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
