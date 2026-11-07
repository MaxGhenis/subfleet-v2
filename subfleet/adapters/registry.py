"""Lazy provider adapters, with injectable factories for isolated fake runs."""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable

from .base import Adapter, AdapterError

_factories: dict[str, Callable[[], Adapter]] = {}
_lock = threading.RLock()
#: C-12.1, C-31.1: each real provider's module and adapter class. A provider's
#: name is not always a module name (`claude-api` lives in `claude_api`).
_BUILTIN: dict[str, tuple[str, str]] = {
    "codex": ("codex", "CodexAdapter"),
    "claude": ("claude", "ClaudeAdapter"),
    "claude-api": ("claude_api", "ClaudeApiAdapter"),
}


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
    if provider not in _BUILTIN:
        raise AdapterError(f"unknown provider {provider!r}", code=2, reason="unknown-provider")
    name, constructor = _BUILTIN[provider]
    module = importlib.import_module(f"subfleet.adapters.{name}")
    factory = getattr(module, constructor, None)
    if factory is None:
        factory = getattr(module, "get_adapter", None)
    if factory is None:
        raise AdapterError(f"{provider} adapter has no constructor", code=1, reason="adapter-missing",
                           details={"provider": provider})
    return factory()
