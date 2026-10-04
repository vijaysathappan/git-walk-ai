"""Explainable scoring contracts shared by every Stage 2.3 engine."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ScoreComponent:
    dimension: str
    raw_score: float
    weight: float
    explanation: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def contribution(self) -> float:
        return round(self.raw_score * self.weight, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "score": round(self.raw_score, 2),
            "weight": self.weight,
            "contribution": self.contribution,
            "classification": classification(self.raw_score),
            "explanation": self.explanation,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ScoreResult:
    score_type: str
    components: tuple[ScoreComponent, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def score(self) -> float:
        weight = sum(item.weight for item in self.components) or 1
        return round(min(100.0, max(0.0, sum(item.contribution for item in self.components) / weight)), 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score_type": self.score_type,
            "score": self.score,
            "classification": classification(self.score),
            "components": [item.to_dict() for item in self.components],
            **self.metadata,
        }


def classification(score: float) -> str:
    if score <= 20:
        return "VERY_LOW"
    if score <= 40:
        return "LOW"
    if score <= 60:
        return "MEDIUM"
    if score <= 80:
        return "HIGH"
    return "VERY_HIGH"
