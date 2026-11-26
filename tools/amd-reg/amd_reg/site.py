"""AMD 中国区开发者云的站点事实（全部来自前端 bundle 实测）。"""

from __future__ import annotations

BASE = "https://developer.amd.com.cn"
API = f"{BASE}/api"

REFRESH = "/Auth/Refresh"
SUMMARY = "/User/GetCurrentUserDataSummary"
POINTS = "/User/GetUserPointsRecords"
ALIYUN_SCENE = "/Aliyun/GetEncryptedSceneId"
ALIYUN_VERIFY = "/Aliyun/VerifyIntelligentCaptcha"
HAS_INSTANCE = "/Experiment/HasInstance"
LAUNCH = "/Experiment/Launch"
STATUS = "/Experiment/Status"
REMOVE = "/Experiment/Remove"

SIGNUP_URL = "https://developer.amd.com/developer-program"   # AMD 统一账号注册入口（人工）
CLOUD_DOC = "https://www.amd.com/en/developer/resources/cloud-access.html"
