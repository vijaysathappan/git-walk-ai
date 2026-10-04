"""Three-way semantic merge for stable Excel workbook identities."""

import copy
from typing import Any


def _by_id(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {item[key]: item for item in items}


def _conflict_key(
    conflict_type: str, sheet_id: str | None, row_id: str | None,
    column_id: str | None, property_name: str,
) -> tuple[str, str | None, str | None, str | None, str]:
    return conflict_type, sheet_id, row_id, column_id, property_name


def _choose(
    base: Any,
    main: Any,
    branch: Any,
    *,
    conflict_type: str,
    sheet_id: str | None,
    row_id: str | None = None,
    column_id: str | None = None,
    property_name: str,
    conflicts: list[dict[str, Any]],
    resolutions: dict[tuple, Any],
) -> Any:
    if branch == base:
        return copy.deepcopy(main)
    if main == base:
        return copy.deepcopy(branch)
    if main == branch:
        return copy.deepcopy(main)
    key = _conflict_key(
        conflict_type, sheet_id, row_id, column_id, property_name
    )
    if key in resolutions:
        return copy.deepcopy(resolutions[key])
    conflicts.append(
        {
            "conflict_type": conflict_type,
            "sheet_id": sheet_id,
            "row_id": row_id,
            "column_id": column_id,
            "base_state": {"property": property_name, "value": base},
            "main_state": {"property": property_name, "value": main},
            "branch_state": {"property": property_name, "value": branch},
        }
    )
    return copy.deepcopy(main)


def _object_changed(item: dict[str, Any] | None, base: dict[str, Any], kind: str) -> bool:
    if item is None:
        return True
    if kind == "sheet":
        def content(sheet: dict[str, Any]) -> dict[str, Any]:
            return {
                "name": sheet.get("name"),
                "columns": {
                    column["column_id"]: {
                        "name": column.get("name"),
                        "data_type": column.get("data_type"),
                    }
                    for column in sheet.get("columns", [])
                },
                "rows": {
                    row["row_id"]: {
                        key: row.get(key, {})
                        for key in ("values", "formulas", "styles", "comments")
                    }
                    for row in sheet.get("rows", [])
                },
            }
        return content(item) != content(base)
    if kind == "column":
        return any(item.get(key) != base.get(key) for key in ("name", "data_type"))
    return any(
        item.get(key, {}) != base.get(key, {})
        for key in ("values", "formulas", "styles", "comments")
    )


def _merge_cell_maps(
    base_row: dict[str, Any],
    main_row: dict[str, Any],
    branch_row: dict[str, Any],
    sheet_id: str,
    row_id: str,
    conflicts: list[dict[str, Any]],
    resolutions: dict[tuple, Any],
) -> dict[str, dict[str, Any]]:
    output = {"values": {}, "formulas": {}, "styles": {}, "comments": {}}
    map_specs = (
        ("values", "CELL_VALUE_CONFLICT"),
        ("formulas", "FORMULA_CONFLICT"),
        ("styles", "CELL_FORMAT_CONFLICT"),
        ("comments", "CELL_COMMENT_CONFLICT"),
    )
    for property_name, conflict_type in map_specs:
        base_map = base_row.get(property_name, {})
        main_map = main_row.get(property_name, {})
        branch_map = branch_row.get(property_name, {})
        for column_id in set(base_map) | set(main_map) | set(branch_map):
            value = _choose(
                base_map.get(column_id), main_map.get(column_id), branch_map.get(column_id),
                conflict_type=conflict_type, sheet_id=sheet_id, row_id=row_id,
                column_id=column_id, property_name=property_name,
                conflicts=conflicts, resolutions=resolutions,
            )
            if value is not None:
                output[property_name][column_id] = value
    return output


def three_way_merge(
    base: dict[str, Any],
    main: dict[str, Any],
    branch: dict[str, Any],
    resolutions: dict[tuple, Any] | None = None,
) -> dict[str, Any]:
    """Merge BASE, protected MAIN, and a USER branch without using cell addresses."""
    resolutions = resolutions or {}
    conflicts: list[dict[str, Any]] = []
    merged = {
        "repository_id": main.get("repository_id") or branch.get("repository_id"),
        "branch_id": main.get("branch_id"),
        "head_commit_id": main.get("head_commit_id"),
        "sheets": [],
    }
    base_sheets = _by_id(base.get("sheets", []), "sheet_id")
    main_sheets = _by_id(main.get("sheets", []), "sheet_id")
    branch_sheets = _by_id(branch.get("sheets", []), "sheet_id")
    for sheet_id in set(base_sheets) | set(main_sheets) | set(branch_sheets):
        base_sheet = base_sheets.get(sheet_id)
        main_sheet = main_sheets.get(sheet_id)
        branch_sheet = branch_sheets.get(sheet_id)
        if base_sheet is None:
            if main_sheet is None:
                merged["sheets"].append(copy.deepcopy(branch_sheet))
            elif branch_sheet is None or main_sheet == branch_sheet:
                merged["sheets"].append(copy.deepcopy(main_sheet))
            else:
                selected = _choose(
                    None, main_sheet, branch_sheet,
                    conflict_type="STRUCTURAL_CONFLICT", sheet_id=sheet_id,
                    property_name="sheet", conflicts=conflicts, resolutions=resolutions,
                )
                if selected is not None:
                    merged["sheets"].append(selected)
            continue
        if main_sheet is None or branch_sheet is None:
            survivor = branch_sheet if main_sheet is None else main_sheet
            if survivor is None:
                continue
            if not _object_changed(survivor, base_sheet, "sheet"):
                continue
            selected = _choose(
                base_sheet, main_sheet, branch_sheet,
                conflict_type="SHEET_DELETE_MODIFY_CONFLICT", sheet_id=sheet_id,
                property_name="sheet", conflicts=conflicts, resolutions=resolutions,
            )
            if selected is not None:
                merged["sheets"].append(selected)
            continue

        merged_sheet = {
            "sheet_id": sheet_id,
            "name": _choose(
                base_sheet.get("name"), main_sheet.get("name"), branch_sheet.get("name"),
                conflict_type="SHEET_RENAME_CONFLICT", sheet_id=sheet_id,
                property_name="name", conflicts=conflicts, resolutions=resolutions,
            ),
            "position": _choose(
                base_sheet.get("position"), main_sheet.get("position"), branch_sheet.get("position"),
                conflict_type="SHEET_MOVE_CONFLICT", sheet_id=sheet_id,
                property_name="position", conflicts=conflicts, resolutions=resolutions,
            ),
            "columns": [],
            "rows": [],
        }
        base_columns = _by_id(base_sheet.get("columns", []), "column_id")
        main_columns = _by_id(main_sheet.get("columns", []), "column_id")
        branch_columns = _by_id(branch_sheet.get("columns", []), "column_id")
        for column_id in set(base_columns) | set(main_columns) | set(branch_columns):
            base_column = base_columns.get(column_id)
            main_column = main_columns.get(column_id)
            branch_column = branch_columns.get(column_id)
            if base_column is None:
                selected = main_column if branch_column is None else branch_column if main_column is None else _choose(
                    None, main_column, branch_column,
                    conflict_type="STRUCTURAL_CONFLICT", sheet_id=sheet_id,
                    column_id=column_id, property_name="column", conflicts=conflicts,
                    resolutions=resolutions,
                )
                if selected is not None:
                    merged_sheet["columns"].append(copy.deepcopy(selected))
                continue
            if main_column is None or branch_column is None:
                survivor = branch_column if main_column is None else main_column
                if survivor is None or not _object_changed(survivor, base_column, "column"):
                    continue
                selected = _choose(
                    base_column, main_column, branch_column,
                    conflict_type="COLUMN_DELETE_MODIFY_CONFLICT", sheet_id=sheet_id,
                    column_id=column_id, property_name="column", conflicts=conflicts,
                    resolutions=resolutions,
                )
                if selected is not None:
                    merged_sheet["columns"].append(selected)
                continue
            merged_sheet["columns"].append(
                {
                    "column_id": column_id,
                    "name": _choose(
                        base_column.get("name"), main_column.get("name"), branch_column.get("name"),
                        conflict_type="COLUMN_RENAME_CONFLICT", sheet_id=sheet_id,
                        column_id=column_id, property_name="name", conflicts=conflicts,
                        resolutions=resolutions,
                    ),
                    "position": _choose(
                        base_column.get("position"), main_column.get("position"), branch_column.get("position"),
                        conflict_type="COLUMN_MOVE_CONFLICT", sheet_id=sheet_id,
                        column_id=column_id, property_name="position", conflicts=conflicts,
                        resolutions=resolutions,
                    ),
                    "data_type": _choose(
                        base_column.get("data_type"), main_column.get("data_type"), branch_column.get("data_type"),
                        conflict_type="STRUCTURAL_CONFLICT", sheet_id=sheet_id,
                        column_id=column_id, property_name="data_type", conflicts=conflicts,
                        resolutions=resolutions,
                    ),
                }
            )

        base_rows = _by_id(base_sheet.get("rows", []), "row_id")
        main_rows = _by_id(main_sheet.get("rows", []), "row_id")
        branch_rows = _by_id(branch_sheet.get("rows", []), "row_id")
        for row_id in set(base_rows) | set(main_rows) | set(branch_rows):
            base_row = base_rows.get(row_id)
            main_row = main_rows.get(row_id)
            branch_row = branch_rows.get(row_id)
            if base_row is None:
                selected = main_row if branch_row is None else branch_row if main_row is None else _choose(
                    None, main_row, branch_row,
                    conflict_type="STRUCTURAL_CONFLICT", sheet_id=sheet_id, row_id=row_id,
                    property_name="row", conflicts=conflicts, resolutions=resolutions,
                )
                if selected is not None:
                    merged_sheet["rows"].append(copy.deepcopy(selected))
                continue
            if main_row is None or branch_row is None:
                survivor = branch_row if main_row is None else main_row
                if survivor is None or not _object_changed(survivor, base_row, "row"):
                    continue
                selected = _choose(
                    base_row, main_row, branch_row,
                    conflict_type="ROW_DELETE_MODIFY_CONFLICT", sheet_id=sheet_id,
                    row_id=row_id, property_name="row", conflicts=conflicts,
                    resolutions=resolutions,
                )
                if selected is not None:
                    merged_sheet["rows"].append(selected)
                continue
            merged_row = {
                "row_id": row_id,
                "position": _choose(
                    base_row.get("position"), main_row.get("position"), branch_row.get("position"),
                    conflict_type="ROW_MOVE_CONFLICT", sheet_id=sheet_id, row_id=row_id,
                    property_name="position", conflicts=conflicts, resolutions=resolutions,
                ),
                "physical_row_id": main_row.get("physical_row_id"),
            }
            merged_row.update(
                _merge_cell_maps(
                    base_row, main_row, branch_row, sheet_id, row_id,
                    conflicts, resolutions,
                )
            )
            merged_sheet["rows"].append(merged_row)
        merged_sheet["columns"].sort(key=lambda item: (item.get("position", 0), item["column_id"]))
        merged_sheet["rows"].sort(key=lambda item: (item.get("position", 0), item["row_id"]))
        for position, column in enumerate(merged_sheet["columns"]):
            column["position"] = position
        for position, row in enumerate(merged_sheet["rows"]):
            row["position"] = position
        merged["sheets"].append(merged_sheet)
    merged["sheets"].sort(key=lambda item: (item.get("position", 0), item["sheet_id"]))
    for position, sheet in enumerate(merged["sheets"]):
        sheet["position"] = position
    return {"merged": merged, "conflicts": conflicts}


def resolution_key(conflict: dict[str, Any]) -> tuple:
    base_state = conflict.get("base_state") or {}
    return _conflict_key(
        conflict["conflict_type"], conflict.get("sheet_id"), conflict.get("row_id"),
        conflict.get("column_id"), base_state.get("property", "state"),
    )
