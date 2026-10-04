"""Lazy provider adapters, with injectable factories for isolated fake runs."""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable

from .base import Adapter, AdapterError

_factories: dict[str, Callable[[], Adapter]] = {}
_lock = threading.RLock()


def register(provider: str, factory: Callable[[], Adapter]) -> None:
    """Register an adapter constructor without importing either real provider."""
    if not callable(factory):
        raise TypeError("adapter factory must be callable")
    with _lock:
        _factories[provider] = factory


def get_adapter(provider: str) -> Adapter:
    """C-12.1: construct the requested adapter only when it is needed."""
    with _lock:
        factory = _factories.get(provider)
    if factory is not None:
        return factory()
    if provider not in ("codex", "claude"):
        raise AdapterError(f"unknown provider {provider!r}", code=2)
    module = importlib.import_module(f"subfleet.adapters.{provider}")
    factory = getattr(module, f"{provider.title()}Adapter", None)
    if factory is None:
        factory = getattr(module, "get_adapter", None)
    if factory is None:
        raise AdapterError(f"{provider} adapter has no constructor", code=1)
    return factory()
