"""Stable workbook identities independent of Excel addresses and SQLite tables."""

import uuid
from datetime import datetime, timezone
from typing import Any

from ..store.identifiers import SafeIdentifier


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16].upper()}"


def column_letter(position: int) -> str:
    value = position + 1
    output = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        output = chr(65 + remainder) + output
    return output


def primary_sheet(conn, repository_id: str):
    return conn.execute(
        """
        SELECT * FROM WORKBOOK_SHEETS
        WHERE REPOSITORY_ID=? AND STATUS='ACTIVE'
        ORDER BY SHEET_ORDER LIMIT 1
        """,
        (repository_id,),
    ).fetchone()


def sheet_table_id(conn, branch_id: str, sheet_id: str) -> str:
    row = conn.execute(
        "SELECT DATA_TABLE_ID FROM BRANCH_SHEET_TABLES WHERE BRANCH_ID=? AND SHEET_ID=?",
        (branch_id, sheet_id),
    ).fetchone()
    if not row:
        raise ValueError(f"Sheet {sheet_id} has no physical data mapping")
    # This value is interpolated directly into dynamic SQL (physical table
    # reads/writes) at every call site -- SafeIdentifier is the type
    # boundary that guarantees it's a validated identifier before any of
    # that f-string SQL is built, not just "internally generated so it's
    # probably fine."
    return SafeIdentifier(row[0])


def _ensure_sheet_mapping(
    conn, branch_id: str, repository_id: str, primary_table_id: str
) -> None:
    """Backfill mappings for legacy single-table repositories."""
    now = utcnow()
    sheets = conn.execute(
        """
        SELECT SHEET_ID, SHEET_ORDER FROM WORKBOOK_SHEETS
        WHERE REPOSITORY_ID=? AND STATUS='ACTIVE' ORDER BY SHEET_ORDER
        """,
        (repository_id,),
    ).fetchall()
    for index, sheet in enumerate(sheets):
        exists = conn.execute(
            "SELECT 1 FROM BRANCH_SHEET_TABLES WHERE BRANCH_ID=? AND SHEET_ID=?",
            (branch_id, sheet["SHEET_ID"]),
        ).fetchone()
        if exists:
            continue
        if index == 0:
            mapped_table = primary_table_id
        else:
            mapped_table = f"SHEET_DATA_{uuid.uuid4().hex[:12].upper()}"
            conn.execute(
                f'CREATE TABLE "{mapped_table}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT)'
            )
        conn.execute(
            """
            INSERT INTO BRANCH_SHEET_TABLES
                (BRANCH_ID, SHEET_ID, DATA_TABLE_ID, IS_PRIMARY, CREATED_AT, UPDATED_AT)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (branch_id, sheet["SHEET_ID"], mapped_table, int(index == 0), now, now),
        )


def ensure_branch_identities(
    conn,
    branch_id: str,
    repository_id: str,
    table_id: str,
    source_branch_id: str | None = None,
) -> None:
    """Initialize stable identities for every mapped worksheet on a branch."""
    primary = primary_sheet(conn, repository_id)
    if not primary:
        raise ValueError("Repository has no active workbook sheet")
    now = utcnow()
    branch_sheet_exists = conn.execute(
        "SELECT 1 FROM BRANCH_SHEETS WHERE BRANCH_ID=? LIMIT 1", (branch_id,)
    ).fetchone()
    if not branch_sheet_exists:
        if source_branch_id:
            conn.execute(
                """
                INSERT INTO BRANCH_SHEETS
                    (BRANCH_ID, SHEET_ID, SHEET_NAME, SHEET_POSITION,
                     STATUS, CREATED_AT, UPDATED_AT)
                SELECT ?, SHEET_ID, SHEET_NAME, SHEET_POSITION,
                       STATUS, ?, ? FROM BRANCH_SHEETS WHERE BRANCH_ID=?
                """,
                (branch_id, now, now, source_branch_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO BRANCH_SHEETS
                    (BRANCH_ID, SHEET_ID, SHEET_NAME, SHEET_POSITION,
                     STATUS, CREATED_AT, UPDATED_AT)
                SELECT ?, SHEET_ID, SHEET_NAME, SHEET_ORDER,
                       STATUS, ?, ? FROM WORKBOOK_SHEETS
                WHERE REPOSITORY_ID=?
                """,
                (branch_id, now, now, repository_id),
            )
    _ensure_sheet_mapping(conn, branch_id, repository_id, table_id)

    branch_sheets = conn.execute(
        """
        SELECT SHEET_ID FROM BRANCH_SHEETS
        WHERE BRANCH_ID=? AND STATUS='ACTIVE' ORDER BY SHEET_POSITION
        """,
        (branch_id,),
    ).fetchall()
    for branch_sheet in branch_sheets:
        sheet_id = branch_sheet["SHEET_ID"]
        if conn.execute(
            "SELECT 1 FROM SHEET_COLUMNS WHERE BRANCH_ID=? AND SHEET_ID=? LIMIT 1",
            (branch_id, sheet_id),
        ).fetchone():
            continue
        mapped_table = sheet_table_id(conn, branch_id, sheet_id)
        if source_branch_id:
            conn.execute(
                """
                INSERT INTO SHEET_COLUMNS
                    (BRANCH_ID, SHEET_ID, COLUMN_ID, COLUMN_NAME, COLUMN_POSITION,
                     DATA_TYPE, STATUS, CREATED_AT, UPDATED_AT)
                SELECT ?, SHEET_ID, COLUMN_ID, COLUMN_NAME, COLUMN_POSITION,
                       DATA_TYPE, STATUS, ?, ? FROM SHEET_COLUMNS
                WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE'
                """,
                (branch_id, now, now, source_branch_id, sheet_id),
            )
            conn.execute(
                """
                INSERT INTO SHEET_ROWS
                    (BRANCH_ID, SHEET_ID, ROW_ID, PHYSICAL_ROW_ID, ROW_POSITION,
                     STATUS, CREATED_AT, UPDATED_AT)
                SELECT ?, SHEET_ID, ROW_ID, PHYSICAL_ROW_ID, ROW_POSITION,
                       STATUS, ?, ? FROM SHEET_ROWS
                WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE'
                """,
                (branch_id, now, now, source_branch_id, sheet_id),
            )
            conn.execute(
                """
                INSERT INTO CELL_METADATA
                    (BRANCH_ID, SHEET_ID, ROW_ID, COLUMN_ID, FORMULA,
                     STYLE_HASH, COMMENT_TEXT, UPDATED_AT)
                SELECT ?, SHEET_ID, ROW_ID, COLUMN_ID, FORMULA,
                       STYLE_HASH, COMMENT_TEXT, ? FROM CELL_METADATA
                WHERE BRANCH_ID=? AND SHEET_ID=?
                """,
                (branch_id, now, source_branch_id, sheet_id),
            )
            continue

        columns = conn.execute(f'PRAGMA table_info("{mapped_table}")').fetchall()
        for position, column in enumerate(
            item for item in columns if item["name"].upper() != "ROW_ID"
        ):
            conn.execute(
                """
                INSERT INTO SHEET_COLUMNS
                    (BRANCH_ID, SHEET_ID, COLUMN_ID, COLUMN_NAME, COLUMN_POSITION,
                     DATA_TYPE, STATUS, CREATED_AT, UPDATED_AT)
                VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (
                    branch_id, sheet_id, stable_id("COL"), column["name"].upper(),
                    position, column["type"] or "TEXT", now, now,
                ),
            )
        physical_rows = conn.execute(
            f'SELECT ROW_ID FROM "{mapped_table}" ORDER BY ROW_ID'
        ).fetchall()
        for position, row in enumerate(physical_rows):
            conn.execute(
                """
                INSERT INTO SHEET_ROWS
                    (BRANCH_ID, SHEET_ID, ROW_ID, PHYSICAL_ROW_ID, ROW_POSITION,
                     STATUS, CREATED_AT, UPDATED_AT)
                VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (branch_id, sheet_id, stable_id("ROW"), int(row[0]), position, now, now),
            )


def branch_identity_context(conn, table_id: str) -> dict[str, Any]:
    branch = conn.execute(
        """
        SELECT B.*, R.DEFAULT_BRANCH_ID
        FROM BRANCHES B JOIN WORKBOOK_REPOSITORIES R ON R.REPOSITORY_ID=B.REPOSITORY_ID
        LEFT JOIN BRANCH_SHEET_TABLES T ON T.BRANCH_ID=B.BRANCH_ID
        WHERE (B.DATA_TABLE_ID=? OR T.DATA_TABLE_ID=?) AND B.STATUS='ACTIVE'
        ORDER BY COALESCE(T.IS_PRIMARY, 1) DESC LIMIT 1
        """,
        (table_id, table_id),
    ).fetchone()
    if not branch:
        raise ValueError("Dataset is not attached to an active branch")
    ensure_branch_identities(
        conn, branch["BRANCH_ID"], branch["REPOSITORY_ID"], branch["DATA_TABLE_ID"]
    )
    primary = primary_sheet(conn, branch["REPOSITORY_ID"])
    return {
        "branch": branch,
        "sheet": primary,
        "branch_id": branch["BRANCH_ID"],
        "repository_id": branch["REPOSITORY_ID"],
        "sheet_id": primary["SHEET_ID"],
    }


def semantic_snapshot(conn, table_id: str) -> dict[str, Any]:
    context = branch_identity_context(conn, table_id)
    branch_id = context["branch_id"]
    branch_sheets = conn.execute(
        """
        SELECT S.SHEET_ID, S.SHEET_NAME, S.SHEET_POSITION, T.DATA_TABLE_ID
        FROM BRANCH_SHEETS S JOIN BRANCH_SHEET_TABLES T
          ON T.BRANCH_ID=S.BRANCH_ID AND T.SHEET_ID=S.SHEET_ID
        WHERE S.BRANCH_ID=? AND S.STATUS='ACTIVE'
        ORDER BY S.SHEET_POSITION
        """,
        (branch_id,),
    ).fetchall()
    sheets = []
    for branch_sheet in branch_sheets:
        sheet_id = branch_sheet["SHEET_ID"]
        mapped_table = branch_sheet["DATA_TABLE_ID"]
        columns = conn.execute(
            """
            SELECT COLUMN_ID, COLUMN_NAME, COLUMN_POSITION, DATA_TYPE
            FROM SHEET_COLUMNS WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE'
            ORDER BY COLUMN_POSITION
            """,
            (branch_id, sheet_id),
        ).fetchall()
        rows = conn.execute(
            """
            SELECT ROW_ID, PHYSICAL_ROW_ID, ROW_POSITION FROM SHEET_ROWS
            WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE' ORDER BY ROW_POSITION
            """,
            (branch_id, sheet_id),
        ).fetchall()
        metadata = {
            (row["ROW_ID"], row["COLUMN_ID"]): row
            for row in conn.execute(
                """
                SELECT ROW_ID, COLUMN_ID, FORMULA, STYLE_HASH, COMMENT_TEXT
                FROM CELL_METADATA WHERE BRANCH_ID=? AND SHEET_ID=?
                """,
                (branch_id, sheet_id),
            ).fetchall()
        }
        output_rows = []
        for identity in rows:
            physical = conn.execute(
                f'SELECT * FROM "{mapped_table}" WHERE ROW_ID=?',
                (identity["PHYSICAL_ROW_ID"],),
            ).fetchone()
            if physical is None:
                continue
            values, formulas, styles, comments = {}, {}, {}, {}
            for column in columns:
                column_id = column["COLUMN_ID"]
                values[column_id] = physical[column["COLUMN_NAME"]]
                cell = metadata.get((identity["ROW_ID"], column_id))
                if cell and cell["FORMULA"]:
                    formulas[column_id] = cell["FORMULA"]
                if cell and cell["STYLE_HASH"]:
                    styles[column_id] = cell["STYLE_HASH"]
                if cell and cell["COMMENT_TEXT"]:
                    comments[column_id] = cell["COMMENT_TEXT"]
            output_rows.append({
                "row_id": identity["ROW_ID"],
                "physical_row_id": identity["PHYSICAL_ROW_ID"],
                "position": identity["ROW_POSITION"],
                "values": values,
                "formulas": formulas,
                "styles": styles,
                "comments": comments,
            })
        sheets.append({
            "sheet_id": sheet_id,
            "name": branch_sheet["SHEET_NAME"],
            "position": branch_sheet["SHEET_POSITION"],
            "columns": [
                {
                    "column_id": column["COLUMN_ID"], "name": column["COLUMN_NAME"],
                    "position": column["COLUMN_POSITION"], "data_type": column["DATA_TYPE"],
                }
                for column in columns
            ],
            "rows": output_rows,
        })
    return {
        "repository_id": context["repository_id"],
        "branch_id": branch_id,
        "head_commit_id": context["branch"]["HEAD_COMMIT_ID"],
        "sheets": sheets,
    }
