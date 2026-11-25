"""cleverai_reg — cleverai.app 批量注册机（协议引擎 + 指纹浏览器引擎）。

两套引擎通往同一个结果：

* ``protocol``：Firebase Identity Toolkit 注册 → appserver 建号，纯 HTTP。
  只借用浏览器产出的一枚 App Check 令牌（3600s 有效，整批复用）。
* ``browser``：Camoufox 在真实站点上注册，reCAPTCHA / App Check 都是原生行为，
  协议被挡时用它兜底。

产物是一条 `dcg.pool/cleverai@1` 账号记录，由网关写成号池并签发 `sk-dcg-` Key。
"""

from __future__ import annotations

__version__ = "0.1.0"

from .appcheck import APP_ID, PROJECT_ID, RECAPTCHA_SITE_KEY, AppCheckVault  # noqa: F401
from .config import Config  # noqa: F401
from .errors import SignupError  # noqa: F401
from .protocol import CleverAI, dialog_message, text_of  # noqa: F401
from .runner import Runner  # noqa: F401
from .store import Store  # noqa: F401

__all__ = [
    "__version__",
    "APP_ID",
    "PROJECT_ID",
    "RECAPTCHA_SITE_KEY",
    "AppCheckVault",
    "CleverAI",
    "Config",
    "Runner",
    "SignupError",
    "Store",
    "dialog_message",
    "text_of",
]
