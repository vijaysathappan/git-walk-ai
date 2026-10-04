"""Provider-neutral connector contract and stable execution types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


class ConnectorError(RuntimeError):
    def __init__(self, code: str, message: str, *, transient: bool = False):
        self.code = code
        self.transient = transient
        super().__init__(message)


@dataclass(frozen=True)
class ConnectorRecord:
    external_type: str
    external_id: str
    source_version: str
    payload: dict[str, Any]
    source_timestamp: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorReadResult:
    records: list[ConnectorRecord]
    checkpoint: dict[str, Any]
    bytes_processed: int = 0
    warnings: tuple[str, ...] = ()


class Connector(ABC):
    connector_type = "BASE"
    capabilities: frozenset[str] = frozenset()

    def __init__(
        self,
        configuration: dict[str, Any],
        credentials: dict[str, Any] | None = None,
        object_loader: Callable[[str], bytes] | None = None,
    ):
        self.configuration = configuration
        self.credentials = credentials or {}
        self.object_loader = object_loader

    @abstractmethod
    async def test_connection(self) -> dict[str, Any]: ...

    @abstractmethod
    async def discover(self) -> dict[str, Any]: ...

    @abstractmethod
    async def read(self, request: dict[str, Any]) -> ConnectorReadResult: ...

    @abstractmethod
    async def write(self, request: dict[str, Any]) -> dict[str, Any]: ...

    async def checkpoint(self) -> dict[str, Any]:
        return {}

    async def health(self) -> dict[str, Any]:
        result = await self.test_connection()
        return {"status": "HEALTHY" if result.get("status") == "SUCCESS" else "UNHEALTHY", **result}

    def require(self, capability: str) -> None:
        if capability not in self.capabilities:
            raise ConnectorError("CAPABILITY_NOT_SUPPORTED", f"{self.connector_type} does not support {capability}")
