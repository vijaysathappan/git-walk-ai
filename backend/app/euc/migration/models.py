"""Shared immutable contracts for Stage 2.4 engines."""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class MigrationUnit:
    unit_id: str
    source_type: str
    source_id: str
    source_name: str
    mode: str
    difficulty: str
    weight: float
    target_type: str
    target_component: str
    rationale: str
    sheet_id: str | None = None
    domain_id: str | None = None
    parent_unit_id: str | None = None
    wave: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MigrationBlocker:
    blocker_id: str
    blocker_type: str
    category: str
    severity: str
    affected_component: str
    migration_effect: str
    remediation: str
    effort_class: str
    affected_unit_id: str | None = None
    dependency_impact: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
