"""Deterministic merge-request risk engine.

Computes a hard-rule risk score/level from the merge request's semantic
diff, open conflicts, and cleared-field investigations — the same
explainable-scoring pattern used by ``euc/intelligence`` (weighted,
saturating component blends, evidence-linked, ``VERY_LOW..VERY_HIGH``
classification), reusing its generic scoring primitives directly rather
than re-deriving them.

This is the authority on ``risk_level``: the AI merge assessment explains
and elaborates on this baseline, it does not invent its own risk_level, and
its recommendation may only be as-or-more cautious than the baseline
recommendation computed here (see ``baseline_recommendation`` /
``RISK_LEVEL_ORDER`` in ``ai/merge_agent.py``).
"""

from __future__ import annotations

from typing import Any

from ..euc.intelligence.scoring.models import ScoreComponent, ScoreResult, classification
from ..euc.intelligence.scoring.normalization import blend, saturation_score

# Conflict types whose resolution changes calculation logic or destroys
# structure, as opposed to purely cosmetic conflicts (format/comment).
_DESTRUCTIVE_CONFLICT_TYPES = {
    "FORMULA_CONFLICT", "STRUCTURAL_CONFLICT", "SHEET_DELETE_MODIFY_CONFLICT",
    "COLUMN_DELETE_MODIFY_CONFLICT", "ROW_DELETE_MODIFY_CONFLICT",
}
_STRUCTURAL_OPERATIONS = {
    "ROW_INSERT", "ROW_DELETE", "ROW_MOVE", "COLUMN_INSERT", "COLUMN_DELETE",
    "COLUMN_MOVE", "COLUMN_RENAME", "SHEET_CREATE", "SHEET_DELETE", "SHEET_RENAME", "SHEET_MOVE",
}
_DESTRUCTIVE_STRUCTURAL_OPERATIONS = {"ROW_DELETE", "COLUMN_DELETE", "SHEET_DELETE"}

# score(0-100) classification -> the app's 4-level RISK_LEVEL enum
# (LOW/MEDIUM/HIGH/CRITICAL, used throughout MERGE_CONFLICTS/MERGE_REQUEST_AI_ASSESSMENTS).
_CLASSIFICATION_TO_RISK_LEVEL = {
    "VERY_LOW": "LOW", "LOW": "LOW", "MEDIUM": "MEDIUM", "HIGH": "HIGH", "VERY_HIGH": "CRITICAL",
}

RISK_LEVEL_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
_RECOMMENDATION_ORDER = {"APPROVE": 0, "HOLD_FOR_REVIEW": 1, "REJECT": 2}


def _numeric(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_sign_flip(old_value: Any, new_value: Any) -> bool:
    old, new = _numeric(old_value), _numeric(new_value)
    if old is None or new is None or old == 0 or new == 0:
        return False
    return (old > 0) != (new > 0)


def _is_large_swing(old_value: Any, new_value: Any, threshold: float = 2.0) -> bool:
    old, new = _numeric(old_value), _numeric(new_value)
    if old is None or new is None or old == 0:
        return False
    return abs(new - old) / abs(old) >= threshold


def deterministic_risk_level(score: float) -> str:
    return _CLASSIFICATION_TO_RISK_LEVEL[classification(score)]


def baseline_recommendation(risk_level: str, open_conflict_count: int) -> str:
    if open_conflict_count:
        return "HOLD_FOR_REVIEW"
    if risk_level in ("HIGH", "CRITICAL"):
        return "HOLD_FOR_REVIEW"
    return "APPROVE"


def evaluate_merge_risk(request: dict[str, Any], field_investigations: list[dict[str, Any]]) -> ScoreResult:
    """Thin wrapper kept for ``ai/merge_agent.py`` — merge requests always
    score CONFLICTS since a merge is exactly the situation where conflicts
    can exist."""
    return evaluate_change_risk(
        request.get("changes") or [], request.get("conflicts") or [],
        request.get("change_summary") or {}, field_investigations, include_conflicts=True,
    )


def evaluate_change_risk(
    changes: list[dict[str, Any]],
    conflicts: list[dict[str, Any]] | None = None,
    change_summary: dict[str, Any] | None = None,
    field_investigations: list[dict[str, Any]] | None = None,
    include_conflicts: bool = True,
) -> ScoreResult:
    """Deterministic risk scoring over a flat list of semantic-diff changes.

    ``include_conflicts=False`` is for scoring a single commit (which never
    has conflicts by definition) — the CONFLICTS dimension is dropped
    entirely and the remaining five weights are renormalized to sum to 1.0,
    rather than leaving 32% of the score structurally wasted at zero.
    """
    changes = changes or []
    conflicts = conflicts or []
    change_summary = change_summary or {}
    field_investigations = field_investigations or []
    open_conflicts = [item for item in conflicts if item.get("status") == "OPEN"]
    destructive_conflicts = [item for item in open_conflicts if item.get("conflict_type") in _DESTRUCTIVE_CONFLICT_TYPES]

    cleared_count = len(field_investigations)
    heavily_edited_cleared = [item for item in field_investigations if (item.get("prior_edit_count") or 0) >= 3]

    formula_changes = [item for item in changes if item.get("operation_type") == "CELL_FORMULA_UPDATE"]

    structural_changes = [item for item in changes if item.get("operation_type") in _STRUCTURAL_OPERATIONS]
    destructive_structural = [item for item in structural_changes if item.get("operation_type") in _DESTRUCTIVE_STRUCTURAL_OPERATIONS]

    value_changes = [item for item in changes if item.get("operation_type") == "CELL_VALUE_UPDATE"]
    sign_flips = [item for item in value_changes if _is_sign_flip(item.get("old_value"), item.get("new_value"))]
    large_swings = [item for item in value_changes if _is_large_swing(item.get("old_value"), item.get("new_value"))]

    total_changes = change_summary.get("total", len(changes))

    conflict_score = blend(
        (saturation_score(len(open_conflicts), 2), .55),
        (saturation_score(len(destructive_conflicts), 1), .45),
    )
    clearing_score = blend(
        (saturation_score(cleared_count, 2), .55),
        (saturation_score(len(heavily_edited_cleared), 1), .45),
    )
    formula_score = saturation_score(len(formula_changes), 4)
    structural_score = blend(
        (saturation_score(len(structural_changes), 8), .45),
        (saturation_score(len(destructive_structural), 2), .55),
    )
    anomaly_score = blend(
        (saturation_score(len(sign_flips), 1), .6),
        (saturation_score(len(large_swings), 2), .4),
    )
    volume_score = saturation_score(total_changes, 50)

    weights = {
        "CONFLICTS": .32, "DATA_CLEARING": .12, "FORMULA_CHANGES": .20,
        "STRUCTURAL_CHANGES": .16, "VALUE_ANOMALIES": .14, "CHANGE_VOLUME": .06,
    }
    if not include_conflicts:
        # A single commit never has conflicts — drop the dimension entirely
        # rather than let it always contribute exactly 0, and renormalize
        # the remaining five weights back up to sum to 1.0.
        conflicts_weight = weights.pop("CONFLICTS")
        renormalize = 1.0 / (1.0 - conflicts_weight)
        weights = {name: round(weight * renormalize, 4) for name, weight in weights.items()}
    values: dict[str, tuple[float, str, dict[str, Any]]] = {}
    if include_conflicts:
        values["CONFLICTS"] = (
            conflict_score, "Open merge conflicts, weighted higher when they touch formulas or structure.",
            {"open_conflicts": len(open_conflicts), "destructive_conflicts": len(destructive_conflicts),
             "conflict_types": sorted({item.get("conflict_type") for item in open_conflicts})},
        )
    values.update({
        "DATA_CLEARING": (
            clearing_score, "Cells cleared to empty, weighted higher for cells with a long prior edit history.",
            {"cleared_fields": cleared_count, "heavily_edited_cleared_fields": len(heavily_edited_cleared),
             "fields": [item.get("column_name") for item in field_investigations]},
        ),
        "FORMULA_CHANGES": (
            formula_score, "Formula rewrites — any change to calculation logic carries inherent risk.",
            {"formula_changes": len(formula_changes)},
        ),
        "STRUCTURAL_CHANGES": (
            structural_score, "Row/column/sheet insert, move, rename, or delete operations, weighted higher for deletes.",
            {"structural_changes": len(structural_changes), "destructive_structural_changes": len(destructive_structural)},
        ),
        "VALUE_ANOMALIES": (
            anomaly_score, "Numeric sign flips and large-magnitude swings on existing values.",
            {"sign_flips": len(sign_flips), "large_swings": len(large_swings),
             "sign_flip_cells": [f"{item.get('sheet_id')}:{item.get('row_id')}:{item.get('column_id')}" for item in sign_flips]},
        ),
        "CHANGE_VOLUME": (
            volume_score, "Total number of effective changes in this branch.",
            {"total_changes": total_changes},
        ),
    })

    # Compounding: several simultaneously-elevated dimensions (e.g. formula
    # rewrites AND structural deletes AND open conflicts, all at once) are
    # genuinely worse than a plain weighted average of independent factors
    # suggests — real incidents are rarely caused by one isolated concern.
    # Amplify each component once three or more dimensions are elevated,
    # same way a plain average would understate compounded risk.
    elevated_count = sum(1 for score, _, _ in values.values() if score >= 40)
    multiplier = min(1.5, 1.0 + max(0, elevated_count - 2) * 0.15)

    components = tuple(
        ScoreComponent(name, min(100.0, round(score * multiplier, 2)), weights[name], explanation, evidence)
        for name, (score, explanation, evidence) in values.items()
    )
    return ScoreResult("CHANGE_RISK", components, metadata={
        "open_conflict_count": len(open_conflicts),
        "cleared_field_count": cleared_count,
        "compounding_multiplier": multiplier,
        "included_conflicts_dimension": include_conflicts,
    })
