"""Gitee AI 站点事实。"""

from __future__ import annotations

API_BASE = "https://ai.gitee.com/v1"
HOME = "https://ai.gitee.com"
MODEL = "z-image-turbo"
IMAGES_PATH = "/images/generations"
MODELS_PATH = "/models"
# Gitee 个人访问令牌没有统一前缀（常见 32 位十六进制），所以不做前缀校验
KEY_PREFIX = ""
