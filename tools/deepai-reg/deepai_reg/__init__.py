"""DeepAI account factory for the gateway in this repo."""

from __future__ import annotations

__all__ = ["Config", "Store", "Runner", "DeepAI", "doctor"]

__version__ = "0.1.0"


def __getattr__(name: str):  # lazy so `doctor` works without every optional dep
    if name == "Config":
        from .config import Config

        return Config
    if name == "Store":
        from .store import Store

        return Store
    if name == "Runner":
        from .runner import Runner

        return Runner
    if name == "DeepAI":
        from .protocol import DeepAI

        return DeepAI
    if name == "doctor":
        from .runner import doctor

        return doctor
    raise AttributeError(name)
