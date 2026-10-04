"""Deterministic workbook validators used before review and merge."""

from collections import Counter
from typing import Any


def validate_workbook(
    candidate: dict[str, Any], base: dict[str, Any] | None = None
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []

    def add(
        rule: str, severity: str, message: str, *, sheet_id: str | None = None,
        row_id: str | None = None, column_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        results.append(
            {
                "rule_code": rule, "severity": severity,
                "status": "FAILED" if severity == "ERROR" else "WARNING",
                "message": message, "sheet_id": sheet_id, "row_id": row_id,
                "column_id": column_id, "details": details or {},
            }
        )

    sheets = candidate.get("sheets", [])
    if not sheets:
        add("WORKBOOK_PARSE_INTEGRITY", "ERROR", "Workbook contains no active sheets.")
    sheet_names = [str(sheet.get("name", "")).strip().casefold() for sheet in sheets]
    if len(sheet_names) != len(set(sheet_names)):
        add("DUPLICATE_SHEET_NAME", "ERROR", "Workbook contains duplicate sheet names.")

    base_sheets = {sheet["sheet_id"]: sheet for sheet in (base or {}).get("sheets", [])}
    for sheet in sheets:
        sheet_id = sheet["sheet_id"]
        columns = sheet.get("columns", [])
        rows = sheet.get("rows", [])
        names = [str(column.get("name", "")).strip().upper() for column in columns]
        if any(not name for name in names):
            add("REQUIRED_COLUMN_NAME", "ERROR", "A column has an empty name.", sheet_id=sheet_id)
        if len(names) != len(set(names)):
            add("DUPLICATE_COLUMN_NAME", "ERROR", "Sheet contains duplicate column names.", sheet_id=sheet_id)
        column_by_id = {column["column_id"]: column for column in columns}
        for row in rows:
            for column_id, value in row.get("values", {}).items():
                column = column_by_id.get(column_id)
                if not column or value is None:
                    continue
                data_type = str(column.get("data_type") or "TEXT").upper()
                if data_type == "INTEGER" and not isinstance(value, int):
                    add("DATA_TYPE", "ERROR", f"{column['name']} expects an integer.", sheet_id=sheet_id, row_id=row["row_id"], column_id=column_id)
                if data_type in {"REAL", "NUMERIC"} and not isinstance(value, (int, float)):
                    add("DATA_TYPE", "ERROR", f"{column['name']} expects a number.", sheet_id=sheet_id, row_id=row["row_id"], column_id=column_id)
            for column_id, formula in row.get("formulas", {}).items():
                if isinstance(formula, str) and "#REF!" in formula.upper():
                    add("BROKEN_FORMULA_REFERENCE", "ERROR", "Formula contains #REF!.", sheet_id=sheet_id, row_id=row["row_id"], column_id=column_id)

        key_columns = [
            column for column in columns
            if column.get("name", "").upper() == "ID"
            or column.get("name", "").upper().endswith("_ID")
        ]
        for column in key_columns:
            values = [row.get("values", {}).get(column["column_id"]) for row in rows]
            duplicates = [value for value, count in Counter(value for value in values if value not in (None, "")).items() if count > 1]
            if duplicates:
                add("DUPLICATE_BUSINESS_KEY", "WARNING", f"{column['name']} contains {len(duplicates)} duplicate key value(s).", sheet_id=sheet_id, column_id=column["column_id"], details={"sample": duplicates[:10]})

        base_sheet = base_sheets.get(sheet_id)
        if base_sheet:
            base_rows = {row["row_id"]: row for row in base_sheet.get("rows", [])}
            for row in rows:
                old_row = base_rows.get(row["row_id"])
                if not old_row:
                    continue
                for column_id, old_formula in old_row.get("formulas", {}).items():
                    if old_formula and not row.get("formulas", {}).get(column_id):
                        add("FORMULA_TO_STATIC", "ERROR", "A formula was replaced by a static value.", sheet_id=sheet_id, row_id=row["row_id"], column_id=column_id, details={"old_formula": old_formula, "new_value": row.get("values", {}).get(column_id)})
            base_count = len(base_sheet.get("rows", []))
            if base_count and abs(len(rows) - base_count) / base_count > 0.5:
                add("ROW_COUNT_ANOMALY", "WARNING", f"Row count changed from {base_count} to {len(rows)}.", sheet_id=sheet_id)

    errors = sum(item["severity"] == "ERROR" for item in results)
    warnings = sum(item["severity"] == "WARNING" for item in results)
    return {
        "status": "FAILED" if errors else "PASSED",
        "error_count": errors,
        "warning_count": warnings,
        "results": results,
    }
