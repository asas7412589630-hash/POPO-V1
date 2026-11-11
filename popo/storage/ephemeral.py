"""
Ephemeral mode gate.

When POPO_EPHEMERAL=1, nothing is written to disk. This module provides a
single chokepoint used by every record-writing function.
"""

from __future__ import annotations
import os
from popo.config import Config


class EphemeralGate:
    """Global gate. Callers ask instead of checking env vars directly."""

    def __init__(self, config: Config | None = None):
        self._enabled = config.ephemeral if config else False

    def is_enabled(self) -> bool:
        return self._enabled

    def check(self, operation: str) -> bool:
        """Return True if the operation is allowed to write to disk."""
        return not self._enabled

    def raise_if_blocked(self, operation: str) -> None:
        if self._enabled:
            raise RuntimeError(f"ephemeral mode blocks {operation}")


# Process-wide default. Tests override by passing explicit gate instances.
_default_gate: EphemeralGate | None = None


def get_gate(config: Config | None = None) -> EphemeralGate:
    if config is not None:
        return EphemeralGate(config)
    global _default_gate
    if _default_gate is None:
        from popo.config import get_config
        _default_gate = EphemeralGate(get_config())
    return _default_gate
