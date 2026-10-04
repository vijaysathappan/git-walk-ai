"""Atomic semantic commit persistence and branch-state reconstruction."""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from ..database import DB_PATH, _get_connection, _risk_score, _values_match, validate_identifier
from ..excel.diff_engine import apply_deltas
from ..excel.values import normalize_excel_value, values_semantically_equal
from ..excel.identity import (
    column_letter,
    ensure_branch_identities,
    semantic_snapshot,
    sheet_table_id,
    stable_id,
)


SUPPORTED_OPERATIONS = {
    "CELL_VALUE_UPDATE", "CELL_FORMULA_UPDATE", "CELL_FORMAT_UPDATE",
    "CELL_COMMENT_UPDATE", "ROW_INSERT", "ROW_DELETE", "ROW_MOVE",
    "COLUMN_INSERT", "COLUMN_DELETE", "COLUMN_MOVE", "COLUMN_RENAME",
    "SHEET_CREATE", "SHEET_DELETE", "SHEET_RENAME", "SHEET_MOVE",
}


class BranchHeadChangedError(RuntimeError):
    def __init__(self, expected_head: str, current_head: str):
        self.expected_head = expected_head
        self.current_head = current_head
        super().__init__(f"Branch HEAD changed from {expected_head} to {current_head}")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str, separators=(",", ":"))


def _decode(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _normalized_identifier(value: str) -> str:
    identifier = str(value or "").strip().upper()
    if not validate_identifier(identifier) or identifier == "ROW_ID":
        raise ValueError(f"Invalid column identifier: {value}")
    return identifier


def _branch_context(conn, table_id: str, branch_id: str) -> dict[str, Any]:
    branch = conn.execute(
        """
        SELECT B.*, R.DEFAULT_BRANCH_ID
        FROM BRANCHES B JOIN WORKBOOK_REPOSITORIES R ON R.REPOSITORY_ID=B.REPOSITORY_ID
        WHERE B.BRANCH_ID=? AND B.DATA_TABLE_ID=? AND B.STATUS='ACTIVE'
        """,
        (branch_id, table_id),
    ).fetchone()
    if not branch:
        raise ValueError("Active branch does not match the workbook table")
    ensure_branch_identities(
        conn, branch_id, branch["REPOSITORY_ID"], table_id,
        source_branch_id=branch["DEFAULT_BRANCH_ID"] if branch["BRANCH_TYPE"] == "USER" else None,
    )
    sheet = conn.execute(
        """
        SELECT * FROM BRANCH_SHEETS WHERE BRANCH_ID=? AND STATUS='ACTIVE'
        ORDER BY SHEET_POSITION LIMIT 1
        """,
        (branch_id,),
    ).fetchone()
    if not sheet:
        raise ValueError("Branch has no active sheet")
    return {"branch": branch, "sheet_id": sheet["SHEET_ID"]}


def _row_identity(conn, branch_id: str, sheet_id: str, row_id: str):
    row = conn.execute(
        """
        SELECT * FROM SHEET_ROWS
        WHERE BRANCH_ID=? AND SHEET_ID=? AND ROW_ID=? AND STATUS='ACTIVE'
        """,
        (branch_id, sheet_id, row_id),
    ).fetchone()
    if not row:
        raise ValueError(f"Stable row {row_id} does not exist on this branch")
    return row


def _column_identity(conn, branch_id: str, sheet_id: str, column_id: str):
    column = conn.execute(
        """
        SELECT * FROM SHEET_COLUMNS
        WHERE BRANCH_ID=? AND SHEET_ID=? AND COLUMN_ID=? AND STATUS='ACTIVE'
        """,
        (branch_id, sheet_id, column_id),
    ).fetchone()
    if not column:
        raise ValueError(f"Stable column {column_id} does not exist on this branch")
    return column


def _insert_change(
    conn,
    commit_id: str,
    repository_id: str,
    branch_id: str,
    change: dict[str, Any],
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO COMMIT_CHANGES
            (CHANGE_ID, COMMIT_ID, REPOSITORY_ID, BRANCH_ID, SHEET_ID,
             OPERATION_TYPE, ROW_ID, COLUMN_ID, PREVIOUS_ROW_POSITION,
             NEW_ROW_POSITION, PREVIOUS_COLUMN_POSITION, NEW_COLUMN_POSITION,
             PREVIOUS_CELL_REFERENCE, NEW_CELL_REFERENCE, OLD_VALUE, NEW_VALUE,
             OLD_FORMULA, NEW_FORMULA, OLD_DATA_TYPE, NEW_DATA_TYPE,
             OLD_STYLE_HASH, NEW_STYLE_HASH, OLD_COMMENT, NEW_COMMENT,
             METADATA_JSON, CREATED_AT)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?)
        """,
        (
            stable_id("CH"), commit_id, repository_id, branch_id,
            change.get("sheet_id"), change["operation_type"], change.get("row_id"),
            change.get("column_id"), change.get("previous_row_position"),
            change.get("new_row_position"), change.get("previous_column_position"),
            change.get("new_column_position"), change.get("previous_cell_reference"),
            change.get("new_cell_reference"), _json(change.get("old_value")),
            _json(change.get("new_value")), change.get("old_formula"),
            change.get("new_formula"), change.get("old_data_type"),
            change.get("new_data_type"), change.get("old_style_hash"),
            change.get("new_style_hash"), change.get("old_comment"),
            change.get("new_comment"), _json(change.get("metadata")), now,
        ),
    )


def _audit(
    conn,
    batch_id: str,
    version: int,
    table_id: str,
    physical_row_id: int,
    column_name: str,
    old_value: Any,
    new_value: Any,
    user_id: str,
    user_email: str,
    message: str,
    risk: int,
    now: str,
    status: str,
) -> None:
    conn.execute(
        """
        INSERT INTO AUDIT_COMMITS
            (AUDIT_ID, BATCH_ID, VERSION, TABLE_ID, ROW_ID, COLUMN_NAME,
             OLD_VALUE, NEW_VALUE, USER_ID, USER_EMAIL, SOURCE, STATUS,
             COMMIT_MESSAGE, RISK_SCORE, CREATED_AT)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'semantic_commit', ?, ?, ?, ?)
        """,
        (
            stable_id("AUD"), batch_id, version, table_id, physical_row_id,
            column_name, _json(old_value), _json(new_value), user_id, user_email,
            status, message, risk, now,
        ),
    )


def commit_semantic_delta(
    *,
    table_id: str,
    repository_id: str,
    branch_id: str,
    expected_head_commit_id: str,
    base_version: int,
    changes: list[dict[str, Any]],
    user_id: str,
    user_email: str,
    message: str,
    additional_parent_commit_id: str | None = None,
    reverts_commit_id: str | None = None,
    commit_status: str = "COMMITTED",
) -> dict[str, Any]:
    if not validate_identifier(table_id):
        raise ValueError("Invalid table identifier")
    if not changes:
        raise ValueError("There are no semantic workbook changes to commit")
    if len(changes) > 10_000:
        raise ValueError("A semantic commit cannot exceed 10,000 operations")
    normalized = []
    for raw in changes:
        change = dict(raw)
        operation = str(change.get("operation_type", "")).upper()
        if operation not in SUPPORTED_OPERATIONS:
            raise ValueError(f"Unsupported semantic operation: {operation}")
        change["operation_type"] = operation
        normalized.append(change)

    conn = _get_connection()
    now = _utcnow()
    commit_id = stable_id("CMT")
    version = base_version + 1
    risk = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        context = _branch_context(conn, table_id, branch_id)
        branch = context["branch"]
        default_sheet_id = context["sheet_id"]
        if branch["REPOSITORY_ID"] != repository_id:
            raise ValueError("Repository does not match branch")
        current_head = branch["HEAD_COMMIT_ID"]
        if current_head != expected_head_commit_id:
            raise BranchHeadChangedError(expected_head_commit_id, current_head)
        registry = conn.execute(
            "SELECT CURRENT_VERSION FROM DATASET_REGISTRY WHERE TABLE_ID=?", (table_id,)
        ).fetchone()
        if not registry:
            raise ValueError("Branch dataset is not registered")
        if int(registry[0]) != base_version:
            raise BranchHeadChangedError(expected_head_commit_id, current_head)

        priority = {
            "SHEET_CREATE": 0, "SHEET_RENAME": 1, "SHEET_MOVE": 1,
            "COLUMN_INSERT": 2, "COLUMN_RENAME": 3, "COLUMN_MOVE": 3,
            "ROW_INSERT": 4, "ROW_MOVE": 5,
            "CELL_VALUE_UPDATE": 6, "CELL_FORMULA_UPDATE": 6,
            "CELL_FORMAT_UPDATE": 6, "CELL_COMMENT_UPDATE": 6,
            "ROW_DELETE": 7, "COLUMN_DELETE": 8, "SHEET_DELETE": 9,
        }
        normalized.sort(key=lambda item: priority[item["operation_type"]])
        stored_changes: list[dict[str, Any]] = []
        changed_cells = formula_changes = row_operations = column_operations = sheet_operations = 0

        for change in normalized:
            operation = change["operation_type"]
            sheet_id = change.get("sheet_id") or default_sheet_id
            change["sheet_id"] = sheet_id
            if operation == "SHEET_CREATE":
                sheet_id = change.get("sheet_id") or stable_id("SHEET")
                name = str(change.get("new_value") or "Sheet")[:255]
                if conn.execute(
                    "SELECT 1 FROM BRANCH_SHEETS WHERE BRANCH_ID=? AND STATUS='ACTIVE' AND SHEET_NAME=? COLLATE NOCASE",
                    (branch_id, name),
                ).fetchone():
                    raise ValueError(f"Worksheet {name} already exists")
                position = int(change.get("new_row_position") or 0)
                conn.execute(
                    "UPDATE BRANCH_SHEETS SET SHEET_POSITION=SHEET_POSITION+1, UPDATED_AT=? "
                    "WHERE BRANCH_ID=? AND STATUS='ACTIVE' AND SHEET_POSITION>=?",
                    (now, branch_id, position),
                )
                conn.execute(
                    "INSERT INTO BRANCH_SHEETS VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)",
                    (branch_id, sheet_id, name, position, now, now),
                )
                physical_table = f"BRANCH_SHEET_{uuid.uuid4().hex[:12].upper()}"
                conn.execute(
                    f'CREATE TABLE "{physical_table}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT)'
                )
                conn.execute(
                    """
                    INSERT INTO BRANCH_SHEET_TABLES
                        (BRANCH_ID, SHEET_ID, DATA_TABLE_ID, IS_PRIMARY, CREATED_AT, UPDATED_AT)
                    VALUES (?, ?, ?, 0, ?, ?)
                    """,
                    (branch_id, sheet_id, physical_table, now, now),
                )
                conn.execute(
                    "INSERT INTO DATASET_REGISTRY VALUES (?, ?, ?, 0, 0, ?, ?, ?)",
                    (physical_table, user_id, f"sheet:{name}", now, now, version),
                )
                change.update({"sheet_id": sheet_id, "new_value": name, "new_row_position": position})
                sheet_operations += 1
            elif operation in {"SHEET_RENAME", "SHEET_MOVE", "SHEET_DELETE"}:
                sheet = conn.execute(
                    "SELECT * FROM BRANCH_SHEETS WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE'",
                    (branch_id, sheet_id),
                ).fetchone()
                if not sheet: raise ValueError(f"Sheet {sheet_id} does not exist")
                if operation == "SHEET_RENAME":
                    name = str(change.get("new_value") or "").strip()[:255]
                    if not name: raise ValueError("Sheet name cannot be empty")
                    if conn.execute(
                        "SELECT 1 FROM BRANCH_SHEETS WHERE BRANCH_ID=? AND SHEET_ID!=? AND STATUS='ACTIVE' AND SHEET_NAME=? COLLATE NOCASE",
                        (branch_id, sheet_id, name),
                    ).fetchone():
                        raise ValueError(f"Worksheet {name} already exists")
                    change["old_value"] = sheet["SHEET_NAME"]
                    conn.execute("UPDATE BRANCH_SHEETS SET SHEET_NAME=?, UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=?", (name, now, branch_id, sheet_id))
                elif operation == "SHEET_MOVE":
                    old_position = int(sheet["SHEET_POSITION"])
                    new_position = int(change.get("new_row_position") or 0)
                    change["previous_row_position"] = old_position
                    if old_position < new_position:
                        conn.execute("UPDATE BRANCH_SHEETS SET SHEET_POSITION=SHEET_POSITION-1, UPDATED_AT=? WHERE BRANCH_ID=? AND STATUS='ACTIVE' AND SHEET_POSITION>? AND SHEET_POSITION<=?", (now, branch_id, old_position, new_position))
                    elif new_position < old_position:
                        conn.execute("UPDATE BRANCH_SHEETS SET SHEET_POSITION=SHEET_POSITION+1, UPDATED_AT=? WHERE BRANCH_ID=? AND STATUS='ACTIVE' AND SHEET_POSITION>=? AND SHEET_POSITION<?", (now, branch_id, new_position, old_position))
                    conn.execute("UPDATE BRANCH_SHEETS SET SHEET_POSITION=?, UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=?", (int(change.get("new_row_position") or 0), now, branch_id, sheet_id))
                else:
                    active_sheet_count = conn.execute(
                        "SELECT COUNT(*) FROM BRANCH_SHEETS WHERE BRANCH_ID=? AND STATUS='ACTIVE'",
                        (branch_id,),
                    ).fetchone()[0]
                    if active_sheet_count <= 1:
                        raise ValueError("A workbook repository must retain at least one worksheet")
                    change["old_value"] = sheet["SHEET_NAME"]
                    conn.execute("UPDATE BRANCH_SHEETS SET STATUS='DELETED', UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=?", (now, branch_id, sheet_id))
                    conn.execute("UPDATE BRANCH_SHEETS SET SHEET_POSITION=SHEET_POSITION-1, UPDATED_AT=? WHERE BRANCH_ID=? AND STATUS='ACTIVE' AND SHEET_POSITION>?", (now, branch_id, sheet["SHEET_POSITION"]))
                sheet_operations += 1
            elif operation == "COLUMN_INSERT":
                physical_table = sheet_table_id(conn, branch_id, sheet_id)
                column_id = change.get("column_id") or stable_id("COL")
                if not re.fullmatch(r"COL_[A-Z0-9]+", column_id): raise ValueError("Invalid stable column ID")
                name = _normalized_identifier(change.get("new_value"))
                if conn.execute(f'PRAGMA table_info("{physical_table}")').fetchall() and conn.execute(
                    "SELECT 1 FROM SHEET_COLUMNS WHERE BRANCH_ID=? AND SHEET_ID=? AND COLUMN_NAME=? AND STATUS='ACTIVE'",
                    (branch_id, sheet_id, name),
                ).fetchone(): raise ValueError(f"Column {name} already exists")
                data_type = str(change.get("new_data_type") or "TEXT").upper()
                if data_type not in {"TEXT", "INTEGER", "REAL", "NUMERIC", "BLOB"}: data_type = "TEXT"
                conn.execute(f'ALTER TABLE "{physical_table}" ADD COLUMN "{name}" {data_type}')
                position = int(change.get("new_column_position") or 0)
                conn.execute("UPDATE SHEET_COLUMNS SET COLUMN_POSITION=COLUMN_POSITION+1, UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE' AND COLUMN_POSITION>=?", (now, branch_id, sheet_id, position))
                conn.execute("INSERT INTO SHEET_COLUMNS VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)", (branch_id, sheet_id, column_id, name, position, data_type, now, now))
                change.update({"column_id": column_id, "new_value": name, "new_column_position": position, "new_data_type": data_type})
                column_operations += 1
            elif operation in {"COLUMN_RENAME", "COLUMN_MOVE", "COLUMN_DELETE"}:
                physical_table = sheet_table_id(conn, branch_id, sheet_id)
                column = _column_identity(conn, branch_id, sheet_id, change.get("column_id"))
                old_name = column["COLUMN_NAME"]
                if operation == "COLUMN_RENAME":
                    new_name = _normalized_identifier(change.get("new_value"))
                    if conn.execute(
                        "SELECT 1 FROM SHEET_COLUMNS WHERE BRANCH_ID=? AND SHEET_ID=? AND COLUMN_ID!=? AND COLUMN_NAME=? AND STATUS='ACTIVE'",
                        (branch_id, sheet_id, column["COLUMN_ID"], new_name),
                    ).fetchone():
                        raise ValueError(f"Column {new_name} already exists on this worksheet")
                    conn.execute(f'ALTER TABLE "{physical_table}" RENAME COLUMN "{old_name}" TO "{new_name}"')
                    conn.execute("UPDATE SHEET_COLUMNS SET COLUMN_NAME=?, UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=? AND COLUMN_ID=?", (new_name, now, branch_id, sheet_id, column["COLUMN_ID"]))
                    change.update({"old_value": old_name, "new_value": new_name})
                elif operation == "COLUMN_MOVE":
                    old_position = int(column["COLUMN_POSITION"])
                    new_position = int(change.get("new_column_position") or 0)
                    change["previous_column_position"] = old_position
                    if old_position < new_position:
                        conn.execute("UPDATE SHEET_COLUMNS SET COLUMN_POSITION=COLUMN_POSITION-1, UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE' AND COLUMN_POSITION>? AND COLUMN_POSITION<=?", (now, branch_id, sheet_id, old_position, new_position))
                    elif new_position < old_position:
                        conn.execute("UPDATE SHEET_COLUMNS SET COLUMN_POSITION=COLUMN_POSITION+1, UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE' AND COLUMN_POSITION>=? AND COLUMN_POSITION<?", (now, branch_id, sheet_id, new_position, old_position))
                    conn.execute("UPDATE SHEET_COLUMNS SET COLUMN_POSITION=?, UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=? AND COLUMN_ID=?", (new_position, now, branch_id, sheet_id, column["COLUMN_ID"]))
                else:
                    old_values = conn.execute(
                        f'''SELECT I.ROW_ID, D."{old_name}" AS VALUE
                            FROM SHEET_ROWS I JOIN "{physical_table}" D
                              ON D.ROW_ID=I.PHYSICAL_ROW_ID
                            WHERE I.BRANCH_ID=? AND I.SHEET_ID=? AND I.STATUS='ACTIVE' ''',
                        (branch_id, sheet_id),
                    ).fetchall()
                    change["old_value"] = {str(row[0]): row[1] for row in old_values}
                    change["metadata"] = {
                        **(change.get("metadata") or {}),
                        "column_name": old_name,
                        "data_type": column["DATA_TYPE"],
                    }
                    conn.execute(f'ALTER TABLE "{physical_table}" DROP COLUMN "{old_name}"')
                    conn.execute("UPDATE SHEET_COLUMNS SET STATUS='DELETED', UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=? AND COLUMN_ID=?", (now, branch_id, sheet_id, column["COLUMN_ID"]))
                    conn.execute("UPDATE SHEET_COLUMNS SET COLUMN_POSITION=COLUMN_POSITION-1, UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE' AND COLUMN_POSITION>?", (now, branch_id, sheet_id, column["COLUMN_POSITION"]))
                column_operations += 1
            elif operation == "ROW_INSERT":
                physical_table = sheet_table_id(conn, branch_id, sheet_id)
                row_id = change.get("row_id") or stable_id("ROW")
                if not re.fullmatch(r"ROW_[A-Z0-9]+", row_id): raise ValueError("Invalid stable row ID")
                raw_values = change.get("new_value") or {}
                values_by_name = {}
                values_by_id = {}
                for key, value in raw_values.items():
                    column = conn.execute(
                        "SELECT * FROM SHEET_COLUMNS WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE' AND (COLUMN_ID=? OR COLUMN_NAME=?)",
                        (branch_id, sheet_id, str(key), str(key).upper()),
                    ).fetchone()
                    if not column: raise ValueError(f"Inserted row references unknown column {key}")
                    values_by_name[column["COLUMN_NAME"]] = value
                    values_by_id[column["COLUMN_ID"]] = value
                if values_by_name:
                    names = list(values_by_name)
                    cursor = conn.execute(
                        f'INSERT INTO "{physical_table}" ({", ".join(f"\"{name}\"" for name in names)}) VALUES ({", ".join("?" for _ in names)})',
                        tuple(values_by_name[name] for name in names),
                    )
                else:
                    cursor = conn.execute(f'INSERT INTO "{physical_table}" DEFAULT VALUES')
                physical_id = int(cursor.lastrowid)
                position = int(change.get("new_row_position") if change.get("new_row_position") is not None else 1_000_000)
                conn.execute("UPDATE SHEET_ROWS SET ROW_POSITION=ROW_POSITION+1, UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE' AND ROW_POSITION>=?", (now, branch_id, sheet_id, position))
                conn.execute("INSERT INTO SHEET_ROWS VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?)", (branch_id, sheet_id, row_id, physical_id, position, now, now))
                change.update({"row_id": row_id, "new_value": values_by_id, "new_row_position": position, "metadata": {**(change.get("metadata") or {}), "physical_row_id": physical_id}})
                for column_name, value in values_by_name.items():
                    _audit(conn, commit_id, version, physical_table, physical_id, column_name, None, value, user_id, user_email, message, risk, now, "INSERTED")
                row_operations += 1
            elif operation in {"ROW_MOVE", "ROW_DELETE"}:
                physical_table = sheet_table_id(conn, branch_id, sheet_id)
                row = _row_identity(conn, branch_id, sheet_id, change.get("row_id"))
                if operation == "ROW_MOVE":
                    old_position = int(row["ROW_POSITION"])
                    new_position = int(change.get("new_row_position") or 0)
                    change["previous_row_position"] = old_position
                    if old_position < new_position:
                        conn.execute("UPDATE SHEET_ROWS SET ROW_POSITION=ROW_POSITION-1, UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE' AND ROW_POSITION>? AND ROW_POSITION<=?", (now, branch_id, sheet_id, old_position, new_position))
                    elif new_position < old_position:
                        conn.execute("UPDATE SHEET_ROWS SET ROW_POSITION=ROW_POSITION+1, UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE' AND ROW_POSITION>=? AND ROW_POSITION<?", (now, branch_id, sheet_id, new_position, old_position))
                    conn.execute("UPDATE SHEET_ROWS SET ROW_POSITION=?, UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=? AND ROW_ID=?", (new_position, now, branch_id, sheet_id, row["ROW_ID"]))
                else:
                    physical = conn.execute(f'SELECT * FROM "{physical_table}" WHERE ROW_ID=?', (row["PHYSICAL_ROW_ID"],)).fetchone()
                    columns = conn.execute("SELECT COLUMN_ID, COLUMN_NAME FROM SHEET_COLUMNS WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE'", (branch_id, sheet_id)).fetchall()
                    change["old_value"] = {column["COLUMN_ID"]: physical[column["COLUMN_NAME"]] for column in columns}
                    for column in columns:
                        _audit(conn, commit_id, version, physical_table, row["PHYSICAL_ROW_ID"], column["COLUMN_NAME"], physical[column["COLUMN_NAME"]], None, user_id, user_email, message, risk, now, "DELETED")
                    conn.execute(f'DELETE FROM "{physical_table}" WHERE ROW_ID=?', (row["PHYSICAL_ROW_ID"],))
                    conn.execute("UPDATE SHEET_ROWS SET STATUS='DELETED', UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=? AND ROW_ID=?", (now, branch_id, sheet_id, row["ROW_ID"]))
                    conn.execute("UPDATE SHEET_ROWS SET ROW_POSITION=ROW_POSITION-1, UPDATED_AT=? WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE' AND ROW_POSITION>?", (now, branch_id, sheet_id, row["ROW_POSITION"]))
                row_operations += 1
            else:
                physical_table = sheet_table_id(conn, branch_id, sheet_id)
                row = _row_identity(conn, branch_id, sheet_id, change.get("row_id"))
                column = _column_identity(conn, branch_id, sheet_id, change.get("column_id"))
                change["previous_cell_reference"] = f"{column_letter(column['COLUMN_POSITION'])}{row['ROW_POSITION'] + 2}"
                change["new_cell_reference"] = change["previous_cell_reference"]
                if operation == "CELL_VALUE_UPDATE":
                    current = conn.execute(f'SELECT "{column["COLUMN_NAME"]}" FROM "{physical_table}" WHERE ROW_ID=?', (row["PHYSICAL_ROW_ID"],)).fetchone()[0]
                    change["old_value"] = current
                    new_value = normalize_excel_value(
                        change.get("new_value"),
                        reference=current,
                        data_type=column["DATA_TYPE"],
                    )
                    change["new_value"] = new_value
                    if values_semantically_equal(current, new_value):
                        continue
                    conn.execute(f'UPDATE "{physical_table}" SET "{column["COLUMN_NAME"]}"=? WHERE ROW_ID=?', (new_value, row["PHYSICAL_ROW_ID"]))
                    _audit(conn, commit_id, version, physical_table, row["PHYSICAL_ROW_ID"], column["COLUMN_NAME"], current, new_value, user_id, user_email, message, risk, now, "UPDATED")
                    changed_cells += 1
                else:
                    metadata = conn.execute("SELECT * FROM CELL_METADATA WHERE BRANCH_ID=? AND SHEET_ID=? AND ROW_ID=? AND COLUMN_ID=?", (branch_id, sheet_id, row["ROW_ID"], column["COLUMN_ID"])).fetchone()
                    old_formula = metadata["FORMULA"] if metadata else None
                    old_style = metadata["STYLE_HASH"] if metadata else None
                    old_comment = metadata["COMMENT_TEXT"] if metadata else None
                    formula = change.get("new_formula") if operation == "CELL_FORMULA_UPDATE" else old_formula
                    style = change.get("new_style_hash") if operation == "CELL_FORMAT_UPDATE" else old_style
                    comment = change.get("new_comment") if operation == "CELL_COMMENT_UPDATE" else old_comment
                    conn.execute("INSERT INTO CELL_METADATA VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(BRANCH_ID,SHEET_ID,ROW_ID,COLUMN_ID) DO UPDATE SET FORMULA=excluded.FORMULA, STYLE_HASH=excluded.STYLE_HASH, COMMENT_TEXT=excluded.COMMENT_TEXT, UPDATED_AT=excluded.UPDATED_AT", (branch_id, sheet_id, row["ROW_ID"], column["COLUMN_ID"], formula, style, comment, now))
                    change.update({"old_formula": old_formula, "old_style_hash": old_style, "old_comment": old_comment})
                    if operation == "CELL_FORMULA_UPDATE": formula_changes += 1
                    else: changed_cells += 1
            stored_changes.append(change)

        if not stored_changes:
            raise ValueError("There are no effective workbook changes to commit")
        risk_inputs = [
            {"row_id": index + 1, "column_name": change.get("operation_type", "CHANGE")}
            for index, change in enumerate(stored_changes)
        ]
        risk = _risk_score(risk_inputs)
        conn.execute(
            "UPDATE AUDIT_COMMITS SET RISK_SCORE=? WHERE BATCH_ID=?",
            (risk, commit_id),
        )

        active_sheets = conn.execute(
            "SELECT SHEET_ID FROM BRANCH_SHEETS WHERE BRANCH_ID=? AND STATUS='ACTIVE'",
            (branch_id,),
        ).fetchall()
        for active_sheet in active_sheets:
            normalized_sheet_id = active_sheet["SHEET_ID"]
            active_rows = conn.execute("SELECT ROW_ID FROM SHEET_ROWS WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE' ORDER BY ROW_POSITION, ROW_ID", (branch_id, normalized_sheet_id)).fetchall()
            for position, row in enumerate(active_rows):
                conn.execute("UPDATE SHEET_ROWS SET ROW_POSITION=? WHERE BRANCH_ID=? AND SHEET_ID=? AND ROW_ID=?", (position, branch_id, normalized_sheet_id, row[0]))
            active_columns = conn.execute("SELECT COLUMN_ID FROM SHEET_COLUMNS WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE' ORDER BY COLUMN_POSITION, COLUMN_ID", (branch_id, normalized_sheet_id)).fetchall()
            for position, column in enumerate(active_columns):
                conn.execute("UPDATE SHEET_COLUMNS SET COLUMN_POSITION=? WHERE BRANCH_ID=? AND SHEET_ID=? AND COLUMN_ID=?", (position, branch_id, normalized_sheet_id, column[0]))

        canonical = json.dumps(stored_changes, sort_keys=True, default=str, separators=(",", ":"))
        commit_hash = hashlib.sha256(f"{current_head}:{user_id}:{message}:{canonical}".encode()).hexdigest()
        conn.execute("INSERT INTO COMMITS (COMMIT_ID,REPOSITORY_ID,BRANCH_ID,AUTHOR_USER_ID,AUTHOR_EMAIL,MESSAGE,CREATED_AT,CHANGE_COUNT,COMMIT_HASH,STATUS,REVERTS_COMMIT_ID,DATASET_VERSION) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (commit_id, repository_id, branch_id, user_id, user_email, message, now, len(stored_changes), commit_hash, commit_status, reverts_commit_id, version))
        if current_head:
            conn.execute("INSERT INTO COMMIT_PARENTS VALUES (?, ?, 0)", (commit_id, current_head))
        if additional_parent_commit_id and additional_parent_commit_id != current_head:
            conn.execute("INSERT INTO COMMIT_PARENTS VALUES (?, ?, 1)", (commit_id, additional_parent_commit_id))
        for change in stored_changes:
            _insert_change(conn, commit_id, repository_id, branch_id, change, now)
        row_count = conn.execute(f'SELECT COUNT(*) FROM "{table_id}"').fetchone()[0]
        column_count = len(conn.execute(f'PRAGMA table_info("{table_id}")').fetchall()) - 1
        conn.execute("UPDATE DATASET_REGISTRY SET CURRENT_VERSION=?,UPDATED_AT=?,ROW_COUNT=?,COLUMN_COUNT=? WHERE TABLE_ID=?", (version, now, row_count, column_count, table_id))
        mapped_tables = conn.execute(
            "SELECT DATA_TABLE_ID FROM BRANCH_SHEET_TABLES WHERE BRANCH_ID=?",
            (branch_id,),
        ).fetchall()
        for mapped in mapped_tables:
            mapped_table = mapped["DATA_TABLE_ID"]
            mapped_rows = conn.execute(
                f'SELECT COUNT(*) FROM "{mapped_table}"'
            ).fetchone()[0]
            mapped_columns = len(
                conn.execute(f'PRAGMA table_info("{mapped_table}")').fetchall()
            ) - 1
            conn.execute(
                """
                UPDATE DATASET_REGISTRY
                SET CURRENT_VERSION=?, UPDATED_AT=?, ROW_COUNT=?, COLUMN_COUNT=?
                WHERE TABLE_ID=?
                """,
                (version, now, mapped_rows, mapped_columns, mapped_table),
            )
        snapshot = semantic_snapshot(conn, table_id)
        snapshot["head_commit_id"] = commit_id
        from ..services.semantic_ledger_service import ledger_for_connection

        storage_result = ledger_for_connection(conn).persist_commit(
            conn, commit_id, repository_id, branch_id, snapshot
        )
        head_update = conn.execute(
            "UPDATE BRANCHES SET HEAD_COMMIT_ID=?,UPDATED_AT=? WHERE BRANCH_ID=? AND HEAD_COMMIT_ID=?",
            (commit_id, now, branch_id, current_head),
        )
        if head_update.rowcount != 1:
            latest = conn.execute(
                "SELECT HEAD_COMMIT_ID FROM BRANCHES WHERE BRANCH_ID=?", (branch_id,)
            ).fetchone()
            raise BranchHeadChangedError(expected_head_commit_id, latest[0] if latest else "DELETED")
        checkpoint_created = False
        conn.commit()
        return {
            "status": "SUCCESS", "commit_id": commit_id, "batch_id": commit_id,
            "parent_commit_id": current_head, "head_commit_id": commit_id,
            "version": version, "base_version": base_version,
            "change_count": len(stored_changes), "changed_cells": changed_cells,
            "formula_changes": formula_changes, "row_operations": row_operations,
            "column_operations": column_operations, "sheet_operations": sheet_operations,
            "risk_score": risk, "checkpoint_created": checkpoint_created,
            "commit_hash": commit_hash, "timestamp": now, **storage_result,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _change_rows(conn, commit_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM COMMIT_CHANGES WHERE COMMIT_ID=? ORDER BY CREATED_AT, CHANGE_ID",
        (commit_id,),
    ).fetchall()
    output = []
    for row in rows:
        item = {key.lower(): row[key] for key in row.keys()}
        item["old_value"] = _decode(item["old_value"])
        item["new_value"] = _decode(item["new_value"])
        item["metadata"] = _decode(item.pop("metadata_json")) or {}
        output.append(item)
    return output


def _is_effective_change(change: dict[str, Any]) -> bool:
    return not (
        change["operation_type"] == "CELL_VALUE_UPDATE"
        and values_semantically_equal(
            change.get("old_value"), change.get("new_value")
        )
    )


def reconstruct_branch(branch_id: str, commit_id: str | None = None) -> dict[str, Any]:
    conn = _get_connection()
    try:
        branch = conn.execute("SELECT * FROM BRANCHES WHERE BRANCH_ID=?", (branch_id,)).fetchone()
        if not branch: raise ValueError("Branch does not exist")
        target = commit_id or branch["HEAD_COMMIT_ID"]
        from ..services.semantic_ledger_service import ledger_for_connection

        manifest_state = ledger_for_connection(conn).reconstruct(conn, target)
        if manifest_state is not None:
            manifest_state["branch_id"] = branch_id
            return manifest_state
        ancestry = []
        cursor = target
        seen = set()
        while cursor and cursor not in seen:
            seen.add(cursor); ancestry.append(cursor)
            checkpoint = conn.execute("SELECT SNAPSHOT_JSON FROM BRANCH_CHECKPOINTS WHERE COMMIT_ID=?", (cursor,)).fetchone()
            if checkpoint:
                state = json.loads(checkpoint[0])
                for descendant in reversed(ancestry[:-1]):
                    state = apply_deltas(state, _change_rows(conn, descendant))
                state["head_commit_id"] = target
                return state
            parent = conn.execute("SELECT PARENT_COMMIT_ID FROM COMMIT_PARENTS WHERE COMMIT_ID=? AND PARENT_ORDER=0", (cursor,)).fetchone()
            cursor = parent[0] if parent else None
        raise ValueError("No checkpoint is available for this branch history")
    finally:
        conn.close()


def list_branch_commits(branch_id: str, limit: int = 100) -> list[dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT C.*, P.PARENT_COMMIT_ID
            FROM COMMITS C LEFT JOIN COMMIT_PARENTS P ON P.COMMIT_ID=C.COMMIT_ID AND P.PARENT_ORDER=0
            WHERE C.BRANCH_ID=? ORDER BY C.CREATED_AT DESC LIMIT ?
            """,
            (branch_id, max(1, min(limit, 500))),
        ).fetchall()
        output = []
        for row in rows:
            item = {key.lower(): row[key] for key in row.keys()}
            changes = [
                change for change in _change_rows(conn, item["commit_id"])
                if _is_effective_change(change)
            ]
            item.update({
                "change_count": len(changes),
                "cell_changes": sum(
                    change["operation_type"].startswith("CELL_") for change in changes
                ),
                "formula_changes": sum(
                    change["operation_type"] == "CELL_FORMULA_UPDATE" for change in changes
                ),
                "row_changes": sum(
                    change["operation_type"].startswith("ROW_") for change in changes
                ),
                "column_changes": sum(
                    change["operation_type"].startswith("COLUMN_") for change in changes
                ),
                "changed_sheets": len({
                    change.get("sheet_id") for change in changes if change.get("sheet_id")
                }),
            })
            output.append(item)
        return output
    finally:
        conn.close()


def list_repository_commits(repository_id: str, limit: int = 300) -> list[dict[str, Any]]:
    """Every commit across every branch of this repository, each with ALL
    of its parents (not just the first) — unlike list_branch_commits(),
    this is branch-agnostic and merge-aware, the dataset a real multi-lane
    git graph needs. change-count enrichment is intentionally omitted here
    (cheap per-commit — this is a topology view, not a diff view)."""
    conn = _get_connection()
    try:
        limit = max(1, min(limit, 1000))
        rows = conn.execute(
            """
            SELECT C.*, B.BRANCH_NAME, B.BRANCH_TYPE
            FROM COMMITS C JOIN BRANCHES B ON B.BRANCH_ID=C.BRANCH_ID
            WHERE C.REPOSITORY_ID=? ORDER BY C.CREATED_AT DESC LIMIT ?
            """,
            (repository_id, limit),
        ).fetchall()
        commit_ids = [row["COMMIT_ID"] for row in rows]
        parents_by_commit: dict[str, list[str]] = {commit_id: [] for commit_id in commit_ids}
        if commit_ids:
            placeholders = ",".join("?" * len(commit_ids))
            for parent_row in conn.execute(
                f"SELECT COMMIT_ID, PARENT_COMMIT_ID FROM COMMIT_PARENTS WHERE COMMIT_ID IN ({placeholders}) ORDER BY PARENT_ORDER",
                commit_ids,
            ):
                parents_by_commit.setdefault(parent_row["COMMIT_ID"], []).append(parent_row["PARENT_COMMIT_ID"])
        output = []
        for row in rows:
            item = {key.lower(): row[key] for key in row.keys()}
            item["parent_commit_ids"] = parents_by_commit.get(item["commit_id"], [])
            output.append(item)
        return output
    finally:
        conn.close()


def get_commit(commit_id: str) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        row = conn.execute("SELECT * FROM COMMITS WHERE COMMIT_ID=?", (commit_id,)).fetchone()
        if not row: return None
        result = {key.lower(): row[key] for key in row.keys()}
        result["changes"] = [
            change for change in _change_rows(conn, commit_id)
            if _is_effective_change(change)
        ]
        result["change_count"] = len(result["changes"])
        result["parents"] = [item[0] for item in conn.execute("SELECT PARENT_COMMIT_ID FROM COMMIT_PARENTS WHERE COMMIT_ID=? ORDER BY PARENT_ORDER", (commit_id,)).fetchall()]
        return result
    finally:
        conn.close()


def branch_change_timeline(
    branch_id: str, head_commit_id: str, base_commit_id: str
) -> list[dict[str, Any]]:
    """Return source-branch operations from the merge base in commit order."""
    conn = _get_connection()
    try:
        commit_ids = []
        cursor = head_commit_id
        seen = set()
        while cursor and cursor != base_commit_id and cursor not in seen:
            seen.add(cursor)
            commit = conn.execute(
                "SELECT BRANCH_ID FROM COMMITS WHERE COMMIT_ID=?", (cursor,)
            ).fetchone()
            if not commit or commit["BRANCH_ID"] != branch_id:
                break
            commit_ids.append(cursor)
            parent = conn.execute(
                """
                SELECT PARENT_COMMIT_ID FROM COMMIT_PARENTS
                WHERE COMMIT_ID=? AND PARENT_ORDER=0
                """,
                (cursor,),
            ).fetchone()
            cursor = parent[0] if parent else None
        timeline = []
        for commit_id in reversed(commit_ids):
            commit = conn.execute(
                "SELECT * FROM COMMITS WHERE COMMIT_ID=?", (commit_id,)
            ).fetchone()
            for sequence, change in enumerate(_change_rows(conn, commit_id), start=1):
                if not _is_effective_change(change):
                    continue
                timeline.append({
                    **change,
                    "sequence": len(timeline) + 1,
                    "commit_sequence": sequence,
                    "commit_id": commit_id,
                    "commit_message": commit["MESSAGE"],
                    "author_user_id": commit["AUTHOR_USER_ID"],
                    "author_email": commit["AUTHOR_EMAIL"],
                    "occurred_at": commit["CREATED_AT"],
                })
        return timeline
    finally:
        conn.close()


def get_cell_history(branch_id: str, sheet_id: str, row_id: str, column_id: str, limit: int = 100) -> list[dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT X.*, C.MESSAGE, C.AUTHOR_USER_ID, C.AUTHOR_EMAIL, C.COMMIT_HASH
            FROM COMMIT_CHANGES X JOIN COMMITS C ON C.COMMIT_ID=X.COMMIT_ID
            WHERE X.BRANCH_ID=? AND X.SHEET_ID=? AND X.ROW_ID=? AND X.COLUMN_ID=?
              AND X.OPERATION_TYPE IN ('CELL_VALUE_UPDATE','CELL_FORMULA_UPDATE','CELL_FORMAT_UPDATE','CELL_COMMENT_UPDATE')
            ORDER BY C.CREATED_AT DESC LIMIT ?
            """,
            (branch_id, sheet_id, row_id, column_id, max(1, min(limit, 500))),
        ).fetchall()
        output = []
        for row in rows:
            item = {key.lower(): row[key] for key in row.keys()}
            item["old_value"] = _decode(item["old_value"])
            item["new_value"] = _decode(item["new_value"])
            if _is_effective_change(item):
                output.append(item)
        return output
    finally:
        conn.close()


def branch_metrics(branch_id: str) -> dict[str, Any]:
    conn = _get_connection()
    try:
        commits = conn.execute(
            """
            SELECT C.COMMIT_ID FROM COMMITS C
            WHERE C.BRANCH_ID=? AND C.STATUS!='CHECKPOINT'
            """,
            (branch_id,),
        ).fetchall()
        changes = []
        for commit in commits:
            changes.extend(
                change for change in _change_rows(conn, commit["COMMIT_ID"])
                if _is_effective_change(change)
            )
        return {
            "commits": len(commits),
            "changes": len(changes),
            "changed_sheets": len({
                change.get("sheet_id") for change in changes if change.get("sheet_id")
            }),
            "changed_rows": len({
                (change.get("sheet_id"), change.get("row_id"))
                for change in changes if change.get("row_id")
            }),
            "changed_cells": sum(
                change["operation_type"].startswith("CELL_") for change in changes
            ),
            "formula_changes": sum(
                change["operation_type"] == "CELL_FORMULA_UPDATE" for change in changes
            ),
        }
    finally:
        conn.close()
