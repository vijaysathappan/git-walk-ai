"""Shared change-inspection helpers used by every agentic review (merge
conflicts, merge-request risk assessment, single-commit risk review).

Extracted out of ``ai/merge_agent.py`` because none of these functions were
ever merge-request-specific — they operate on a plain ``branch_id`` and a
flat list of semantic-diff change dicts, so the merge agent and the
commit-review agent (``ai/commit_review.py``) both import the same code
instead of duplicating it.
"""

from __future__ import annotations

from typing import Any

from ..repositories.commit_store import get_cell_history

_EMPTY_VALUES = (None, "", "null", "NULL", "None")
_MAX_FIELD_INVESTIGATIONS = 8


def column_names(branch_state: dict[str, Any]) -> dict[tuple[str, str], str]:
    """Map (sheet_id, column_id) -> human-readable column name so
    explanations can say "Product Code" instead of a stable column id."""
    names: dict[tuple[str, str], str] = {}
    for sheet in branch_state.get("sheets", []) or []:
        sheet_id = sheet.get("sheet_id")
        for column in sheet.get("columns", []) or []:
            names[(sheet_id, column.get("column_id"))] = column.get("name") or column.get("column_id")
    return names


def detect_cleared_fields(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find CELL_VALUE_UPDATE changes that clear a previously non-empty
    value to empty/null — the pattern a reviewer most needs explained,
    since "why did this become blank?" is invisible in a flat diff list."""
    cleared = []
    for change in changes:
        if change.get("operation_type") != "CELL_VALUE_UPDATE":
            continue
        old_value = change.get("old_value")
        new_value = change.get("new_value")
        if old_value not in _EMPTY_VALUES and new_value in _EMPTY_VALUES:
            cleared.append(change)
    return cleared[:_MAX_FIELD_INVESTIGATIONS]


def investigate_cleared_field(
    branch_id: str, change: dict[str, Any], names: dict[tuple[str, str], str],
) -> dict[str, Any]:
    """Agentic INVESTIGATE step for one cleared cell: pull its real edit
    history so the model explains from evidence, not speculation."""
    history = get_cell_history(branch_id, change.get("sheet_id"), change.get("row_id"), change.get("column_id"))
    column_name = names.get((change.get("sheet_id"), change.get("column_id")), change.get("column_id"))
    last_change = history[0] if history else None
    return {
        "sheet_id": change.get("sheet_id"), "row_id": change.get("row_id"), "column_id": change.get("column_id"),
        "column_name": column_name, "previous_value": change.get("old_value"),
        "changed_by": last_change.get("author_email") if last_change else None,
        "changed_at": last_change.get("created_at") if last_change else None,
        "prior_edit_count": len(history),
    }
