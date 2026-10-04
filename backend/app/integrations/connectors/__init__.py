"""Built-in Stage 4 connector implementations."""

from .registry import connector_for
from .base import ConnectorError

__all__ = ["connector_for", "ConnectorError"]
