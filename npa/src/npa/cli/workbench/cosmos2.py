"""Compatibility alias for the capability-local Cosmos2 CLI module."""

from __future__ import annotations

import sys

from npa.workbench.cosmos import cli as _implementation

# Preserve the historical import path (including monkeypatching of private test
# seams) while keeping the implementation importable without initializing the
# entire workbench command tree inside the purpose-built GPU image.
sys.modules[__name__] = _implementation
