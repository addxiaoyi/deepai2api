"""Allmodels account factory: protocol engine, Turnstile, gateway pool export."""

from __future__ import annotations

from .errors import (
    Blocked,
    CaptchaRequired,
    EmailRejected,
    EmailTaken,
    NoApiKey,
    PasswordRejected,
    RateLimited,
    SignupError,
    VerificationPending,
)
from .protocol import TURNSTILE_ACTION, TURNSTILE_SITE_KEY, AllModels

__all__ = [
    "AllModels",
    "TURNSTILE_ACTION",
    "TURNSTILE_SITE_KEY",
    "SignupError",
    "EmailRejected",
    "EmailTaken",
    "PasswordRejected",
    "RateLimited",
    "Blocked",
    "CaptchaRequired",
    "VerificationPending",
    "NoApiKey",
]

__version__ = "0.1.0"
