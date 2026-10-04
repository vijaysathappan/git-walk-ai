"""Deterministic merge-request risk scoring — the authoritative risk_level
and score behind the AI merge assessment; the AI explains this baseline, it
does not invent its own risk_level."""

from .engine import (
    RISK_LEVEL_ORDER,
    baseline_recommendation,
    deterministic_risk_level,
    evaluate_change_risk,
    evaluate_merge_risk,
)

__all__ = [
    "RISK_LEVEL_ORDER",
    "baseline_recommendation",
    "deterministic_risk_level",
    "evaluate_change_risk",
    "evaluate_merge_risk",
]
