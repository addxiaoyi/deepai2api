"""Poe account factory: dual engine (in-page GraphQL protocol / Camoufox DOM).

Poe's signup, as observed on 2026-11-25:

    GET  https://poe.com/login            (Cloudflare managed challenge -> cf_clearance)
    POST /api/gql_POST  MainSignupLoginSection_sendVerificationCodeMutation_Mutation
         {emailAddress, phoneNumber: null, recaptchaToken}
         -> {"status": "user_with_confirmed_email_not_found"} + 6-digit mail
    POST /api/gql_POST  SignupOrLoginWithCodeSection_signupWithVerificationCodeMutation_Mutation
         {verificationCode, emailAddress, phoneNumber: null, subscriptions: [...]}
    -> UI then demands a phone number ("Add phone number … / Add"), i.e. SMS.

So: email leg is fully automatable, the phone leg needs an SMS provider (or it stays
`phone_required`). Both engines stop at the same place and hand the account to the pool.
"""

from __future__ import annotations

__all__ = ["Config", "Pool", "Runner", "PoeProtocol", "PoeBrowser"]

__version__ = "0.1.0"


def __getattr__(name: str):
    if name == "Config":
        from .config import Config

        return Config
    if name == "Pool":
        from .pool import Pool

        return Pool
    if name == "Runner":
        from .runner import Runner

        return Runner
    if name == "PoeProtocol":
        from .protocol import PoeProtocol

        return PoeProtocol
    if name == "PoeBrowser":
        from .browser import PoeBrowser

        return PoeBrowser
    raise AttributeError(name)
