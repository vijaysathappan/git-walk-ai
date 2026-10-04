"""Pure semantic workbook diff and delta replay engine."""

import copy
import json
from typing import Any

from .values import values_semantically_equal


def _json_value(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str, separators=(",", ":"))


def _sequence_moves(
    before_items: list[dict[str, Any]], after_items: list[dict[str, Any]], id_key: str
) -> list[tuple[str, int, int]]:
    """Return intentional reorders while ignoring shifts caused by insert/delete."""
    after_ids = {item[id_key] for item in after_items}
    before_by_id = {item[id_key]: item for item in before_items}
    working = [item[id_key] for item in before_items if item[id_key] in after_ids]
    target = [item[id_key] for item in after_items if item[id_key] in before_by_id]
    after_by_id = {item[id_key]: item for item in after_items}
    moves = []
    for target_index, identity in enumerate(target):
        current_index = working.index(identity)
        if current_index == target_index:
            continue
        working.insert(target_index, working.pop(current_index))
        moves.append(
            (
                identity,
                int(before_by_id[identity].get("position", current_index)),
                int(after_by_id[identity].get("position", target_index)),
            )
        )
    return moves


def semantic_diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    before_sheets = {sheet["sheet_id"]: sheet for sheet in before.get("sheets", [])}
    after_sheets = {sheet["sheet_id"]: sheet for sheet in after.get("sheets", [])}
    for sheet_id, sheet in before_sheets.items():
        if sheet_id not in after_sheets:
            changes.append({"operation_type": "SHEET_DELETE", "sheet_id": sheet_id})
    for sheet_id, sheet in after_sheets.items():
        previous = before_sheets.get(sheet_id)
        if not previous:
            # A brand-new sheet: emit SHEET_CREATE, then fall through into
            # the SAME column/row diff logic below against an empty
            # placeholder, instead of `continue`-ing past it -- otherwise
            # every column/row the new sheet actually contains is silently
            # dropped from the change list (this is exactly what broke a
            # Virtual Run macro's new sheet on merge: the personal branch
            # commit itself carries the full COLUMN_INSERT/ROW_INSERT list,
            # but merge() recomputes `changes` from scratch via this
            # function, which threw all of it away for any new sheet).
            changes.append({"operation_type": "SHEET_CREATE", "sheet_id": sheet_id, "new_value": sheet["name"], "new_row_position": sheet.get("position")})
            previous = {"name": sheet["name"], "position": sheet.get("position"), "columns": [], "rows": []}
        else:
            if previous["name"] != sheet["name"]:
                changes.append({"operation_type": "SHEET_RENAME", "sheet_id": sheet_id, "old_value": previous["name"], "new_value": sheet["name"]})
            if previous.get("position") != sheet.get("position"):
                changes.append({"operation_type": "SHEET_MOVE", "sheet_id": sheet_id, "previous_row_position": previous.get("position"), "new_row_position": sheet.get("position")})

        before_columns = {column["column_id"]: column for column in previous.get("columns", [])}
        after_columns = {column["column_id"]: column for column in sheet.get("columns", [])}
        for column_id, column in before_columns.items():
            if column_id not in after_columns:
                changes.append({"operation_type": "COLUMN_DELETE", "sheet_id": sheet_id, "column_id": column_id, "previous_column_position": column.get("position"), "old_value": column.get("name")})
        for column_id, column in after_columns.items():
            old_column = before_columns.get(column_id)
            if not old_column:
                changes.append({"operation_type": "COLUMN_INSERT", "sheet_id": sheet_id, "column_id": column_id, "new_column_position": column.get("position"), "new_value": column.get("name"), "new_data_type": column.get("data_type")})
                continue
            if old_column.get("name") != column.get("name"):
                changes.append({"operation_type": "COLUMN_RENAME", "sheet_id": sheet_id, "column_id": column_id, "old_value": old_column.get("name"), "new_value": column.get("name")})
        for column_id, old_position, new_position in _sequence_moves(
            previous.get("columns", []), sheet.get("columns", []), "column_id"
        ):
            changes.append({"operation_type": "COLUMN_MOVE", "sheet_id": sheet_id, "column_id": column_id, "previous_column_position": old_position, "new_column_position": new_position})

        before_rows = {row["row_id"]: row for row in previous.get("rows", [])}
        after_rows = {row["row_id"]: row for row in sheet.get("rows", [])}
        for row_id, row in before_rows.items():
            if row_id not in after_rows:
                changes.append({"operation_type": "ROW_DELETE", "sheet_id": sheet_id, "row_id": row_id, "previous_row_position": row.get("position"), "old_value": row.get("values")})
        for row_id, row in after_rows.items():
            old_row = before_rows.get(row_id)
            if not old_row:
                changes.append({"operation_type": "ROW_INSERT", "sheet_id": sheet_id, "row_id": row_id, "new_row_position": row.get("position"), "new_value": row.get("values"), "metadata": {"formulas": row.get("formulas", {})}})
                continue
            for column_id in set(old_row.get("values", {})) | set(row.get("values", {})):
                old_value = old_row.get("values", {}).get(column_id)
                new_value = row.get("values", {}).get(column_id)
                if not values_semantically_equal(old_value, new_value):
                    changes.append({"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet_id, "row_id": row_id, "column_id": column_id, "old_value": old_value, "new_value": new_value})
                old_formula = old_row.get("formulas", {}).get(column_id)
                new_formula = row.get("formulas", {}).get(column_id)
                if old_formula != new_formula:
                    changes.append({"operation_type": "CELL_FORMULA_UPDATE", "sheet_id": sheet_id, "row_id": row_id, "column_id": column_id, "old_formula": old_formula, "new_formula": new_formula})
                old_style = old_row.get("styles", {}).get(column_id)
                new_style = row.get("styles", {}).get(column_id)
                if old_style != new_style:
                    changes.append({"operation_type": "CELL_FORMAT_UPDATE", "sheet_id": sheet_id, "row_id": row_id, "column_id": column_id, "old_style_hash": old_style, "new_style_hash": new_style})
                old_comment = old_row.get("comments", {}).get(column_id)
                new_comment = row.get("comments", {}).get(column_id)
                if old_comment != new_comment:
                    changes.append({"operation_type": "CELL_COMMENT_UPDATE", "sheet_id": sheet_id, "row_id": row_id, "column_id": column_id, "old_comment": old_comment, "new_comment": new_comment})
        for row_id, old_position, new_position in _sequence_moves(
            previous.get("rows", []), sheet.get("rows", []), "row_id"
        ):
            changes.append({"operation_type": "ROW_MOVE", "sheet_id": sheet_id, "row_id": row_id, "previous_row_position": old_position, "new_row_position": new_position})
    return changes


def apply_deltas(snapshot: dict[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any]:
    """Replay normalized semantic changes onto a checkpoint snapshot."""
    state = copy.deepcopy(snapshot)
    sheets = {sheet["sheet_id"]: sheet for sheet in state.setdefault("sheets", [])}
    for change in changes:
        operation = change["operation_type"]
        sheet_id = change.get("sheet_id")
        sheet = sheets.get(sheet_id)
        if operation == "SHEET_CREATE":
            sheet = {"sheet_id": sheet_id, "name": change.get("new_value"), "position": change.get("new_row_position", len(sheets)), "columns": [], "rows": []}
            state["sheets"].append(sheet); sheets[sheet_id] = sheet
            continue
        if not sheet:
            continue
        if operation == "SHEET_DELETE":
            state["sheets"] = [item for item in state["sheets"] if item["sheet_id"] != sheet_id]; sheets.pop(sheet_id, None); continue
        if operation == "SHEET_RENAME": sheet["name"] = change.get("new_value"); continue
        if operation == "SHEET_MOVE": sheet["position"] = change.get("new_row_position"); continue
        columns = {column["column_id"]: column for column in sheet.setdefault("columns", [])}
        rows = {row["row_id"]: row for row in sheet.setdefault("rows", [])}
        column_id = change.get("column_id"); row_id = change.get("row_id")
        if operation == "COLUMN_INSERT":
            position = change.get("new_column_position", len(sheet["columns"]))
            for item in sheet["columns"]:
                if item.get("position", 0) >= position:
                    item["position"] = item.get("position", 0) + 1
            sheet["columns"].append({"column_id": column_id, "name": change.get("new_value"), "position": position, "data_type": change.get("new_data_type") or "TEXT"})
        elif operation == "COLUMN_DELETE":
            deleted_position = columns.get(column_id, {}).get("position")
            sheet["columns"] = [item for item in sheet["columns"] if item["column_id"] != column_id]
            for row in sheet["rows"]: row.get("values", {}).pop(column_id, None)
            if deleted_position is not None:
                for item in sheet["columns"]:
                    if item.get("position", 0) > deleted_position:
                        item["position"] = item.get("position", 0) - 1
        elif operation == "COLUMN_RENAME" and column_id in columns: columns[column_id]["name"] = change.get("new_value")
        elif operation == "COLUMN_MOVE" and column_id in columns:
            old_position = columns[column_id].get("position", 0)
            new_position = change.get("new_column_position", old_position)
            for item in sheet["columns"]:
                position = item.get("position", 0)
                if old_position < new_position and old_position < position <= new_position:
                    item["position"] = position - 1
                elif new_position < old_position and new_position <= position < old_position:
                    item["position"] = position + 1
            columns[column_id]["position"] = new_position
        elif operation == "ROW_INSERT":
            position = change.get("new_row_position", len(sheet["rows"]))
            for item in sheet["rows"]:
                if item.get("position", 0) >= position:
                    item["position"] = item.get("position", 0) + 1
            sheet["rows"].append({"row_id": row_id, "position": position, "values": change.get("new_value") or {}, "formulas": (change.get("metadata") or {}).get("formulas", {}), "styles": {}, "comments": {}})
        elif operation == "ROW_DELETE":
            deleted_position = rows.get(row_id, {}).get("position")
            sheet["rows"] = [item for item in sheet["rows"] if item["row_id"] != row_id]
            if deleted_position is not None:
                for item in sheet["rows"]:
                    if item.get("position", 0) > deleted_position:
                        item["position"] = item.get("position", 0) - 1
        elif operation == "ROW_MOVE" and row_id in rows:
            old_position = rows[row_id].get("position", 0)
            new_position = change.get("new_row_position", old_position)
            for item in sheet["rows"]:
                position = item.get("position", 0)
                if old_position < new_position and old_position < position <= new_position:
                    item["position"] = position - 1
                elif new_position < old_position and new_position <= position < old_position:
                    item["position"] = position + 1
            rows[row_id]["position"] = new_position
        elif row_id in rows and column_id:
            if operation == "CELL_VALUE_UPDATE": rows[row_id].setdefault("values", {})[column_id] = change.get("new_value")
            elif operation == "CELL_FORMULA_UPDATE": rows[row_id].setdefault("formulas", {})[column_id] = change.get("new_formula")
            elif operation == "CELL_FORMAT_UPDATE": rows[row_id].setdefault("styles", {})[column_id] = change.get("new_style_hash")
            elif operation == "CELL_COMMENT_UPDATE": rows[row_id].setdefault("comments", {})[column_id] = change.get("new_comment")
    for sheet in state.get("sheets", []):
        sheet["columns"].sort(key=lambda item: item.get("position", 0))
        sheet["rows"].sort(key=lambda item: item.get("position", 0))
        for position, column in enumerate(sheet["columns"]):
            column["position"] = position
        for position, row in enumerate(sheet["rows"]):
            row["position"] = position
    state["sheets"].sort(key=lambda item: item.get("position", 0))
    return state
