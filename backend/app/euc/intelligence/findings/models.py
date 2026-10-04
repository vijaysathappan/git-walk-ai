"""Stable evidence-first finding candidates."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FindingCandidate:
    rule_id: str
    category: str
    severity: str
    confidence: float
    title: str
    description: str
    remediation_code: str
    identity: str
    evidence: dict[str, Any]
    node_id: str | None = None
    sheet_id: str | None = None
    cell_address: str | None = None
    dependency_impact: dict[str, Any] = field(default_factory=dict)
