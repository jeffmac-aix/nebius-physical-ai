"""Antioch control-plane integration for NPA Workbench."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .openpi_bridge import OpenPIWebsocketClient, render_stack
from .schemas import (
    CollectRequest,
    OperationRecord,
    ResumeRequest,
    SubmitRequest,
)

if TYPE_CHECKING:
    from .manager import AntiochManager

__all__ = [
    "AntiochManager",
    "CollectRequest",
    "OperationRecord",
    "OpenPIWebsocketClient",
    "ResumeRequest",
    "SubmitRequest",
    "render_stack",
]


def __getattr__(name: str) -> Any:
    """Keep the offline dataset stack optional for health/bridge-only images."""

    if name == "AntiochManager":
        from .manager import AntiochManager

        return AntiochManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
