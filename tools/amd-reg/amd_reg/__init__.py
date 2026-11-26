"""AMD AI 开发者计划（developer.amd.com.cn）—— 账号 / 积分 / GPU 实验实例。

实测（2026-11）：中国区前端 SPA 的接口基址是 `https://developer.amd.com.cn/api`，
bundle 里出现的相关端点：

    鉴权      POST /api/Auth/Refresh            （token 续期）
    用户      GET  /api/User/GetCurrentUserDataSummary   （积分/额度汇总）
              GET  /api/User/GetUserPointsRecords        （积分流水 = Devpoints）
    验证码    GET  /api/Aliyun/GetEncryptedSceneId       （阿里云智能验证 sceneId）
              POST /api/Aliyun/VerifyIntelligentCaptcha  （交验证结果）
    GPU 实例  GET  /api/Experiment/HasInstance
              POST /api/Experiment/Launch        ← 起实例（"嫖 GPU"就是这一步）
              GET  /api/Experiment/Status
              POST /api/Experiment/Remove

注意：**注册/登录不在这个 bundle 里** —— 它走 AMD 统一账号（developer.amd.com / account.amd.com），
本机无法凭空注册（而且要手机号 + 阿里云智能验证 + 额度审批）。所以这条线的自动化边界是：

    账号获取（人工/浏览器） → cookie/token 导入入池 → 积分查询 → 起/停 GPU 实例 → 导出
"""

from __future__ import annotations

__version__ = "0.1.0"
