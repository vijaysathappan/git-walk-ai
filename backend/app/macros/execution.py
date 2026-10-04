"""Resolves a SQL-fast-lane plan against a branch's live data, computing the
SemanticChange list that gets handed to the exact same commit_semantic_delta
every other Git Walk edit goes through. Read-only -- nothing here writes to
the database. The actual write, with its existing normalization/audit/
skip-if-unchanged logic, happens only when run_service hands this output to
commit_semantic_delta at confirm time.

Row/column identity math mirrors the product's own existing conversion
(see app.euc.branch_comparison.resolve_cell_identity, the ground truth this
was checked against): for a 1-based Excel address where row 1 is the
header, ROW_POSITION = excel_row - 2 and COLUMN_POSITION = excel_col - 1.
"""

from __future__ import annotations

from typing import Any

from ..excel.identity import ensure_branch_identities, sheet_table_id
from . import sql_lane
from .parser.ast import SubDecl


class ExecutionError(ValueError):
    pass


def _first_sheet_id(conn, branch_id: str) -> str:
    row = conn.execute(
        "SELECT SHEET_ID FROM BRANCH_SHEETS WHERE BRANCH_ID=? AND STATUS='ACTIVE' ORDER BY SHEET_POSITION LIMIT 1",
        (branch_id,),
    ).fetchone()
    if not row:
        raise ExecutionError("This branch has no worksheets yet.")
    return row["SHEET_ID"]


def _column_map(conn, branch_id: str, sheet_id: str) -> dict[int, dict[str, str]]:
    """1-based Excel column index -> {column_id, column_name}."""
    rows = conn.execute(
        "SELECT COLUMN_ID, COLUMN_NAME, COLUMN_POSITION FROM SHEET_COLUMNS WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE'",
        (branch_id, sheet_id),
    ).fetchall()
    return {row["COLUMN_POSITION"] + 1: {"column_id": row["COLUMN_ID"], "column_name": row["COLUMN_NAME"]} for row in rows}


def _row_map(conn, branch_id: str, sheet_id: str, excel_rows: list[int]) -> dict[int, dict[str, Any]]:
    """1-based Excel row number -> {ROW_ID (stable), PHYSICAL_ROW_ID}. Rows
    the loop bound references but that don't exist in the data are simply
    absent from the result -- not an error, just nothing to change there."""
    positions = [row - 2 for row in excel_rows]
    placeholders = ",".join("?" for _ in positions)
    rows = conn.execute(
        f"""SELECT ROW_ID, PHYSICAL_ROW_ID, ROW_POSITION FROM SHEET_ROWS
            WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE' AND ROW_POSITION IN ({placeholders})""",
        (branch_id, sheet_id, *positions),
    ).fetchall()
    by_position = {row["ROW_POSITION"]: row for row in rows}
    return {position + 2: by_position[position] for position in positions if position in by_position}


def build_change_list(
    conn, *, branch_id: str, repository_id: str, data_table_id: str, sub_ast: SubDecl,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Returns (semantic_changes, summary). Raises sql_lane.NotEligibleError
    if the macro no longer fits the fast lane (re-checked fresh here, not
    trusted from a possibly-stale extraction record) or ExecutionError for
    branch-state problems (no worksheet, unknown column)."""
    plan_ = sql_lane.plan(sub_ast)
    ensure_branch_identities(conn, branch_id, repository_id, data_table_id)
    sheet_id = _first_sheet_id(conn, branch_id)
    physical_table = sheet_table_id(conn, branch_id, sheet_id)

    columns = _column_map(conn, branch_id, sheet_id)
    needed_columns = sql_lane.referenced_columns(plan_)
    missing = [index for index in needed_columns if index not in columns]
    if missing:
        raise ExecutionError(f"Column(s) {missing} referenced by this macro do not exist on this sheet.")

    excel_rows = sql_lane.excel_rows(plan_)
    rows = _row_map(conn, branch_id, sheet_id, excel_rows)
    if not rows:
        return [], {"rows_in_range": len(excel_rows), "rows_found": 0, "change_count": 0}

    physical_row_ids = [row["PHYSICAL_ROW_ID"] for row in rows.values()]
    select_columns = ", ".join(f'"{columns[index]["column_name"]}"' for index in sorted(needed_columns))
    placeholders = ",".join("?" for _ in physical_row_ids)
    fetched = conn.execute(
        f'SELECT ROW_ID, {select_columns} FROM "{physical_table}" WHERE ROW_ID IN ({placeholders})',
        physical_row_ids,
    ).fetchall()
    by_physical_row_id = {row["ROW_ID"]: row for row in fetched}

    row_values: dict[int, dict[int, Any]] = {}
    for excel_row, identity in rows.items():
        physical = by_physical_row_id.get(identity["PHYSICAL_ROW_ID"])
        if physical is None:
            continue
        row_values[excel_row] = {index: physical[columns[index]["column_name"]] for index in needed_columns}

    computed = sql_lane.evaluate(plan_, row_values)

    changes: list[dict[str, Any]] = []
    for excel_row, new_by_column in computed.items():
        if excel_row not in row_values:
            continue
        identity = rows[excel_row]
        for column_index, new_value in new_by_column.items():
            old_value = row_values[excel_row].get(column_index)
            if old_value == new_value:
                continue
            changes.append({
                "operation_type": "CELL_VALUE_UPDATE",
                "sheet_id": sheet_id,
                "row_id": identity["ROW_ID"],
                "column_id": columns[column_index]["column_id"],
                "old_value": old_value,
                "new_value": new_value,
            })

    summary = {"rows_in_range": len(excel_rows), "rows_found": len(rows), "change_count": len(changes)}
    return changes, summary
