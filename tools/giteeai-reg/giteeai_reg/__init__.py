"""giteeai_reg —— Gitee AI（模力方舟）访问令牌的校验与入池。

为什么没有"注册"这一步：Gitee AI 的令牌必须在 gitee.com 个人设置里手动创建
（或走 OAuth 授权），没有公开的机器注册接口。所以这个 worker 做的是**批量校验 + 入池**：

    GET {api_base}/models   （只读，不消耗生图额度）
    POST {api_base}/images/generations  （真正出图，走网关反代）

产出与其它线一致：账号记录（email 留空、apiKey 存 Gitee 令牌）→ 回推网关号池。
"""

from __future__ import annotations

__version__ = "0.1.0"
