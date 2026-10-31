"""Adapter registry.

Adding a tool integration means writing one module and adding one line to
:data:`ADAPTERS`. Nothing else in the project needs to change.

The demo adapter is registered but is not part of the support matrix: it exists
so ``custos demo`` can exercise the whole chain with no agent installed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from .base import DOC_VERIFIED, EXPERIMENTAL, VERIFIED, Adapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .demo import DemoAdapter
from .generic import GenericAdapter
from .opencode import OpenCodeAdapter
from .pi import PiAdapter

ADAPTERS: Dict[str, Type[Adapter]] = {
    "claude_code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "opencode": OpenCodeAdapter,
    "pi": PiAdapter,
    "generic": GenericAdapter,
    "demo": DemoAdapter,
}

# Presentation order for status output: installed tools first, catch-all last.
ADAPTER_ORDER = ["claude_code", "codex", "opencode", "pi", "generic", "demo"]

# Adapters a scan should never pick up on its own. The demo adapter answers
# `detect()` with True by design (it has no store to look for), so without this
# list a plain `scan` on an empty machine would report a session that does not
# exist.
SYNTHETIC_ADAPTERS = ("demo",)

VERIFICATION_LEVELS = {
    VERIFIED: "verified against real local data",
    DOC_VERIFIED: "verified against published documentation only",
    EXPERIMENTAL: "configuration-dependent",
}


def get_adapter(name: str, config: Any = None) -> Adapter:
    """Instantiate one adapter by name."""
    key = (name or "").strip().lower().replace("-", "_")
    adapter_class = ADAPTERS.get(key)
    if adapter_class is None:
        raise KeyError(
            "unknown adapter %r (available: %s)" % (name, ", ".join(list_adapters())))
    return adapter_class(config)


def list_adapters() -> List[str]:
    """Every registered adapter name, in presentation order."""
    ordered = [name for name in ADAPTER_ORDER if name in ADAPTERS]
    extra = [name for name in ADAPTERS if name not in ADAPTER_ORDER]
    return ordered + sorted(extra)


def auto_adapter_names(config: Any = None, include_undetected: bool = False) -> List[str]:
    """Adapter names that should be used by default.

    A name is included when it is enabled in configuration and it either detects
    a local install or is the generic adapter (which has nothing to detect
    beyond its own configuration).

    ``include_undetected`` keeps adapters whose tool is missing so a scan can
    report "not installed" for each one. That is the more useful default for a
    person running ``scan`` by hand, and it is what makes an empty machine look
    diagnosed rather than merely empty.
    """
    names: List[str] = []
    for name in list_adapters():
        if name in SYNTHETIC_ADAPTERS:
            continue
        if config is not None and not config.adapter_enabled(name):
            continue
        adapter = get_adapter(name, config)
        if name == "generic" and not include_undetected and not adapter.detect():
            continue
        if include_undetected:
            names.append(name)
            continue
        try:
            if adapter.detect():
                names.append(name)
        except Exception:
            # A broken adapter must not stop the others from being scanned.
            continue
    return names


def adapter_status(config: Any = None, include_synthetic: bool = True) -> List[Dict[str, Any]]:
    """Detection and verification summary for every registered adapter."""
    rows: List[Dict[str, Any]] = []
    for name in list_adapters():
        if name in SYNTHETIC_ADAPTERS and not include_synthetic:
            continue
        adapter = get_adapter(name, config)
        try:
            detected = adapter.detect()
            error = None
        except Exception as exc:
            detected = False
            error = "%s: %s" % (type(exc).__name__, exc)
        rows.append({
            "name": name,
            "enabled": config.adapter_enabled(name) if config is not None else True,
            "detected": detected,
            "verification": adapter.VERIFICATION,
            "verification_note": adapter.VERIFICATION_NOTE,
            "capabilities": adapter.capabilities(),
            "error": error,
        })
    return rows


__all__ = [
    "ADAPTERS",
    "ADAPTER_ORDER",
    "SYNTHETIC_ADAPTERS",
    "VERIFIED",
    "DOC_VERIFIED",
    "EXPERIMENTAL",
    "Adapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "OpenCodeAdapter",
    "PiAdapter",
    "GenericAdapter",
    "DemoAdapter",
    "get_adapter",
    "list_adapters",
    "auto_adapter_names",
    "adapter_status",
    "VERIFICATION_LEVELS",
]
