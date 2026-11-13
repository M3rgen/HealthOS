"""Public umay service modules, loaded only when requested."""

from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import (
        chat_context,
        chat_relay,
        chat_socket,
        chat_tools,
        codex_bridge_filter,
        codex_bridge_policy,
        codex_bridge_tls,
        codex_chat,
        codex_chat_thread,
        codex_health,
        codex_runtime_probe,
        codex_runtime_rpc,
        health_transfer,
    )

__all__ = [
    "chat_context",
    "chat_relay",
    "chat_socket",
    "chat_tools",
    "codex_bridge_filter",
    "codex_bridge_policy",
    "codex_bridge_tls",
    "codex_chat",
    "codex_chat_thread",
    "codex_health",
    "codex_runtime_probe",
    "codex_runtime_rpc",
    "health_transfer",
]


def __getattr__(name: str) -> ModuleType:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module
