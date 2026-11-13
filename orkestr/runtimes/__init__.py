"""Runtime adapters registry."""
from __future__ import annotations

import importlib
import logging

from .base import RuntimeAdapter

log = logging.getLogger("orkestr.runtimes")

_ADAPTERS = (
    ("claude_code", "claude_code", "ClaudeCodeAdapter"),
    ("opencode", "opencode", "OpenCodeAdapter"),
    ("demo", "demo", "DemoAdapter"),
)


def build_adapters() -> dict[str, RuntimeAdapter]:
    """One instance per runtime id. A broken adapter is logged and skipped."""
    out: dict[str, RuntimeAdapter] = {}
    for rid, module, cls in _ADAPTERS:
        try:
            mod = importlib.import_module(f"{__name__}.{module}")
            out[rid] = getattr(mod, cls)()
        except Exception:
            log.exception("runtime adapter %s could not be loaded", rid)
    return out
