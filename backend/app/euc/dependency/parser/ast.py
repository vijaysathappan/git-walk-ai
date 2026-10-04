"""Serializable formula AST nodes."""

from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass(frozen=True)
class ASTNode:
    kind: str
    value: Any = None
    children: tuple["ASTNode", ...] = field(default_factory=tuple)
    subtype: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.kind}
        if self.value is not None:
            payload["value"] = self.value
        if self.subtype:
            payload["subtype"] = self.subtype
        if self.children:
            payload["children"] = [child.to_dict() for child in self.children]
        return payload

    @property
    def depth(self) -> int:
        return 1 + max((child.depth for child in self.children), default=0)

    def walk(self) -> Iterator["ASTNode"]:
        yield self
        for child in self.children:
            yield from child.walk()
