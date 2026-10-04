"""Dataset/repository/branch CRUD: upload provisioning, working copies,
branch lifecycle, categories, and repository/branch access checks."""

import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import settings
from ..domain import BranchPolicy, RepositoryPolicy, RepositoryRole
from ..excel.identity import ensure_branch_identities, semantic_snapshot, stable_id
from .auth_store import bind_or_verify_device, consume_login_code, get_or_create_user, verify_user_credentials
from .identifiers import SafeIdentifier
from .presence_store import list_active_presence, _touch_daily_activity
from .schema import (
    _current_db_path,
    _ensure_stage2_commit_foundation,
    _get_connection,
    _map_dtype,
    _sanitize_column_name,
    _sqlite_scalar,
    _utcnow,
)
from .sync_store import _store_dataset_version


def create_sqlite_table_from_df(table_name: str, df: pd.DataFrame) -> dict[str, Any]:
    """
    Dynamically create a SQLite table from a pandas DataFrame and seed it
    with the DataFrame's rows.

    Parameters
    ----------
    table_name : str
        The target table name (already expected to be sanitized by the caller).
    df : pd.DataFrame
        The data parsed from the uploaded Excel file.

    Returns
    -------
    dict
        {"row_count": int, "column_count": int, "columns": list[str]}

    Raises
    ------
    RuntimeError
        On any SQLite error (with automatic rollback).
    """
    table_name = SafeIdentifier(table_name)
    # --- Sanitize column names ------------------------------------------------
    sanitized_cols: list[str] = []
    seen: set[str] = set()
    for raw_col in df.columns:
        col = _sanitize_column_name(str(raw_col))
        # deduplicate
        base, suffix = col, 1
        while col in seen:
            col = f"{base}_{suffix}"[:30]
            suffix += 1
        seen.add(col)
        sanitized_cols.append(col)

    # Map pandas dtypes to SQLite types
    col_defs = []
    for col, dtype in zip(sanitized_cols, df.dtypes):
        col_defs.append(f'"{col}" {_map_dtype(dtype)}')

    # Build CREATE TABLE DDL with ROW_ID auto-increment PK
    ddl_columns = ",\n    ".join(
        ["ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT"] + col_defs
    )
    create_sql = f'CREATE TABLE IF NOT EXISTS "{table_name}" (\n    {ddl_columns}\n);'

    # Build INSERT statement
    placeholders = ", ".join(["?"] * len(sanitized_cols))
    col_list = ", ".join(f'"{c}"' for c in sanitized_cols)
    insert_sql = f'INSERT INTO "{table_name}" ({col_list}) VALUES ({placeholders})'

    # Prepare row data — convert NaN/NaT to None for SQLite
    rows: list[tuple] = []
    for _, row in df.iterrows():
        rows.append(tuple(_sqlite_scalar(value) for value in row))

    # Execute within a transaction
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(create_sql)
        cursor.executemany(insert_sql, rows)
        conn.commit()
        return {
            "row_count": len(rows),
            "column_count": len(sanitized_cols),
            "columns": sanitized_cols,
        }
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(f"SQLite table creation failed: {exc}") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Validation helpers (used by the sync endpoint)
# ---------------------------------------------------------------------------
# Whitelist pattern for identifiers (table names, column names)
def get_repository_organization_id(repository_id: str) -> str | None:
    conn = _get_connection()
    try:
        row = conn.execute("SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (repository_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_branch_protection(repository_id: str, branch_id: str) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM BRANCH_PROTECTION_RULES WHERE REPOSITORY_ID=? AND BRANCH_ID=?", (repository_id, branch_id)
        ).fetchone()
        return {key.lower(): row[key] for key in row.keys()} if row else None
    finally:
        conn.close()


def update_branch_protection(
    repository_id: str, branch_id: str, organization_id: str,
    allow_direct_commits: bool, required_approvals: int, require_validation: bool, actor_id: str,
) -> dict[str, Any]:
    conn = _get_connection()
    try:
        now = _utcnow()
        rule_id = f"BPR_{uuid.uuid4().hex[:18].upper()}"
        conn.execute(
            """INSERT INTO BRANCH_PROTECTION_RULES
                   (RULE_ID,ORGANIZATION_ID,REPOSITORY_ID,BRANCH_ID,ALLOW_DIRECT_COMMITS,REQUIRED_APPROVALS,REQUIRE_VALIDATION,CREATED_BY,CREATED_AT,UPDATED_AT)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(REPOSITORY_ID,BRANCH_ID) DO UPDATE SET
                   ALLOW_DIRECT_COMMITS=excluded.ALLOW_DIRECT_COMMITS,
                   REQUIRED_APPROVALS=excluded.REQUIRED_APPROVALS,
                   REQUIRE_VALIDATION=excluded.REQUIRE_VALIDATION,
                   UPDATED_AT=excluded.UPDATED_AT""",
            (rule_id, organization_id, repository_id, branch_id, int(allow_direct_commits),
             max(0, required_approvals), int(require_validation), actor_id, now, now),
        )
        conn.commit()
        return get_branch_protection(repository_id, branch_id)
    finally:
        conn.close()


def _workspace_event(
    conn: sqlite3.Connection, table_id: str, event_type: str,
    actor_user_id: str, payload: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO WORKSPACE_EVENTS (TABLE_ID, EVENT_TYPE, ACTOR_USER_ID, PAYLOAD_JSON, CREATED_AT) VALUES (?, ?, ?, ?, ?)",
        (table_id, event_type, actor_user_id, json.dumps(payload), _utcnow()),
    )


def _accept_pending_invitations(
    conn: sqlite3.Connection, user_id: str, email: str, now: str
) -> None:
    invitations = conn.execute(
        "SELECT TABLE_ID, ROLE FROM DATASET_INVITATIONS WHERE EMAIL=? COLLATE NOCASE AND STATUS='pending'",
        (email,),
    ).fetchall()
    for invitation in invitations:
        conn.execute(
            "INSERT INTO DATASET_MEMBERS (TABLE_ID, USER_ID, ROLE, ADDED_AT) VALUES (?, ?, ?, ?) ON CONFLICT(TABLE_ID, USER_ID) DO UPDATE SET ROLE=excluded.ROLE",
            (invitation["TABLE_ID"], user_id, invitation["ROLE"], now),
        )
        repository = conn.execute(
            "SELECT REPOSITORY_ID, CREATED_BY FROM WORKBOOK_REPOSITORIES WHERE TABLE_ID=?",
            (invitation["TABLE_ID"],),
        ).fetchone()
        if repository:
            conn.execute(
                """
                INSERT INTO REPOSITORY_MEMBERS
                    (REPOSITORY_ID, USER_ID, ROLE, GRANTED_BY, CREATED_AT, UPDATED_AT)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(REPOSITORY_ID, USER_ID) DO UPDATE SET
                    ROLE=excluded.ROLE, UPDATED_AT=excluded.UPDATED_AT
                """,
                (
                    repository["REPOSITORY_ID"], user_id, invitation["ROLE"],
                    repository["CREATED_BY"], now, now,
                ),
            )
            from ..access_control.bootstrap import ensure_repository_security
            ensure_repository_security(conn, repository["REPOSITORY_ID"], now)
        conn.execute(
            "UPDATE DATASET_INVITATIONS SET STATUS='accepted', UPDATED_AT=? WHERE TABLE_ID=? AND EMAIL=? COLLATE NOCASE",
            (now, invitation["TABLE_ID"], email),
        )
        _workspace_event(conn, invitation["TABLE_ID"], "INVITATION_ACCEPTED", user_id, {"email": email, "role": invitation["ROLE"]})


def resolve_repository_owner(email: str, employee_id: str | None = None) -> dict[str, Any]:
    """Resolve or provision a delegated repository owner identity."""
    user = get_or_create_user(email)
    normalized_employee_id = (employee_id or "").strip()[:80] or None
    if normalized_employee_id:
        conn = _get_connection()
        try:
            conn.execute(
                "UPDATE APP_USERS SET EMPLOYEE_ID=? WHERE USER_ID=?",
                (normalized_employee_id, user["user_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        user["employee_id"] = normalized_employee_id
    return user


def register_dataset(
    table_id: str,
    owner_user_id: str,
    filename: str,
    row_count: int,
    column_count: int,
    sheet_names: list[str] | None = None,
    category_id: str = "CAT_UNSORTED",
    repository_name: str | None = None,
    description: str | None = None,
    visibility: str = "private",
    business_owner: str | None = None,
    data_classification: str = "internal",
    retention_policy: str | None = None,
    sheet_tables: list[dict[str, str]] | None = None,
) -> dict[str, str]:
    conn = _get_connection()
    try:
        now = _utcnow()
        conn.execute(
            """
            INSERT OR REPLACE INTO DATASET_REGISTRY
                (TABLE_ID, OWNER_USER_ID, ORIGINAL_FILENAME, ROW_COUNT,
                 COLUMN_COUNT, CREATED_AT, UPDATED_AT, CURRENT_VERSION)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (table_id, owner_user_id, filename, row_count, column_count, now, now),
        )
        _store_dataset_version(
            conn, table_id, 0, None, "Initial workbook import", owner_user_id, now
        )
        existing = conn.execute(
            "SELECT REPOSITORY_ID, DEFAULT_BRANCH_ID FROM WORKBOOK_REPOSITORIES WHERE TABLE_ID=?",
            (table_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE WORKBOOK_REPOSITORIES SET CREATED_BY=?, UPDATED_AT=? WHERE REPOSITORY_ID=?",
                (owner_user_id, now, existing["REPOSITORY_ID"]),
            )
            conn.execute(
                """
                INSERT INTO REPOSITORY_MEMBERS
                    (REPOSITORY_ID, USER_ID, ROLE, GRANTED_BY, CREATED_AT, UPDATED_AT)
                VALUES (?, ?, 'owner', ?, ?, ?)
                ON CONFLICT(REPOSITORY_ID, USER_ID) DO UPDATE SET ROLE='owner', UPDATED_AT=excluded.UPDATED_AT
                """,
                (existing["REPOSITORY_ID"], owner_user_id, owner_user_id, now, now),
            )
            conn.commit()
            return {
                "repository_id": existing["REPOSITORY_ID"],
                "main_branch_id": existing["DEFAULT_BRANCH_ID"],
            }

        repository_id = f"REP_{uuid.uuid4().hex[:12].upper()}"
        main_branch_id = f"BR_{uuid.uuid4().hex[:12].upper()}"
        root_commit = f"CMT_{uuid.uuid4().hex[:12].upper()}"
        category = conn.execute(
            "SELECT 1 FROM CATEGORIES WHERE CATEGORY_ID=? AND STATUS='ACTIVE'",
            (category_id,),
        ).fetchone()
        if not category:
            category_id = "CAT_UNSORTED"
        normalized_name = (repository_name or Path(filename).stem).strip()[:120]
        if not normalized_name:
            raise ValueError("Repository name is required")
        slug_base = re.sub(r"[^a-z0-9]+", "-", normalized_name.lower()).strip("-") or "repository"
        slug = slug_base
        suffix = 2
        while conn.execute(
            "SELECT 1 FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_SLUG=?", (slug,)
        ).fetchone():
            slug = f"{slug_base}-{suffix}"
            suffix += 1
        normalized_visibility = visibility if visibility in {"private", "internal"} else "private"
        normalized_classification = (
            data_classification if data_classification in {"public", "internal", "confidential", "restricted"}
            else "internal"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO WORKBOOK_REPOSITORIES
                (REPOSITORY_ID, TABLE_ID, CATEGORY_ID, REPOSITORY_NAME, DESCRIPTION,
                 DEFAULT_BRANCH_ID, CREATED_BY, CREATED_AT, UPDATED_AT, STATUS, MAIN_PROTECTED,
                 REPOSITORY_SLUG, VISIBILITY, BUSINESS_OWNER, DATA_CLASSIFICATION, RETENTION_POLICY)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 0, ?, ?, ?, ?, ?)
            """,
            (
                repository_id, table_id, category_id, normalized_name,
                description or f"Repository for {filename}", main_branch_id,
                owner_user_id, now, now, slug, normalized_visibility,
                business_owner, normalized_classification, retention_policy,
            ),
        )
        conn.execute(
            """
            INSERT INTO REPOSITORY_MEMBERS
                (REPOSITORY_ID, USER_ID, ROLE, GRANTED_BY, CREATED_AT, UPDATED_AT)
            VALUES (?, ?, 'owner', ?, ?, ?)
            """,
            (repository_id, owner_user_id, owner_user_id, now, now),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO BRANCHES
                (BRANCH_ID, REPOSITORY_ID, DATA_TABLE_ID, BRANCH_NAME, BRANCH_TYPE,
                 CREATED_BY, BASE_COMMIT_ID, HEAD_COMMIT_ID, STATUS, CREATED_AT, UPDATED_AT)
            VALUES (?, ?, ?, 'main', 'MAIN', ?, ?, ?, 'ACTIVE', ?, ?)
            """,
            (main_branch_id, repository_id, table_id, owner_user_id, root_commit, root_commit, now, now),
        )
        configured_sheets = sheet_tables or [
            {"name": name, "table_id": table_id if index == 0 else ""}
            for index, name in enumerate(sheet_names or ["Sheet1"])
        ]
        for sheet_order, sheet_config in enumerate(configured_sheets):
            sheet_name = str(sheet_config.get("name") or f"Sheet{sheet_order + 1}")[:255]
            physical_table = str(sheet_config.get("table_id") or "")
            if not physical_table:
                physical_table = f"SHEET_DATA_{uuid.uuid4().hex[:12].upper()}"
                conn.execute(
                    f'CREATE TABLE "{physical_table}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT)'
                )
            sheet_id = f"SHEET_{uuid.uuid4().hex[:12].upper()}"
            conn.execute(
                """
                INSERT INTO WORKBOOK_SHEETS
                    (SHEET_ID, REPOSITORY_ID, SHEET_NAME, SHEET_ORDER, STATUS, CREATED_AT)
                VALUES (?, ?, ?, ?, 'ACTIVE', ?)
                """,
                (
                    sheet_id, repository_id, sheet_name, sheet_order, now,
                ),
            )
            conn.execute(
                """
                INSERT INTO BRANCH_SHEET_TABLES
                    (BRANCH_ID, SHEET_ID, DATA_TABLE_ID, IS_PRIMARY, CREATED_AT, UPDATED_AT)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (main_branch_id, sheet_id, physical_table, int(sheet_order == 0), now, now),
            )
            if physical_table != table_id:
                physical_rows = conn.execute(
                    f'SELECT COUNT(*) FROM "{physical_table}"'
                ).fetchone()[0]
                physical_columns = len(
                    conn.execute(f'PRAGMA table_info("{physical_table}")').fetchall()
                ) - 1
                conn.execute(
                    """
                    INSERT OR REPLACE INTO DATASET_REGISTRY
                        (TABLE_ID, OWNER_USER_ID, ORIGINAL_FILENAME, ROW_COUNT,
                         COLUMN_COUNT, CREATED_AT, UPDATED_AT, CURRENT_VERSION)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        physical_table, owner_user_id, f"{filename}#{sheet_name}",
                        physical_rows, physical_columns, now, now,
                    ),
                )
        main_branch = conn.execute(
            "SELECT * FROM BRANCHES WHERE BRANCH_ID=?", (main_branch_id,)
        ).fetchone()
        ensure_branch_identities(
            conn, main_branch_id, repository_id, table_id
        )
        _ensure_stage2_commit_foundation(conn, main_branch, now)
        from ..access_control.bootstrap import ensure_repository_security
        ensure_repository_security(conn, repository_id, now)
        conn.commit()
        return {"repository_id": repository_id, "main_branch_id": main_branch_id}
    finally:
        conn.close()


def store_initial_formula_metadata(
    table_id: str, formulas_by_sheet: dict[str, list[dict[str, Any]]]
) -> None:
    """Attach uploaded formulas to stable cells after repository identities exist."""
    if not formulas_by_sheet:
        return
    conn = _get_connection()
    try:
        repository = conn.execute(
            "SELECT REPOSITORY_ID, DEFAULT_BRANCH_ID FROM WORKBOOK_REPOSITORIES WHERE TABLE_ID=?",
            (table_id,),
        ).fetchone()
        if not repository:
            raise ValueError("Repository does not exist")
        branch = conn.execute(
            "SELECT DATA_TABLE_ID FROM BRANCHES WHERE BRANCH_ID=?",
            (repository["DEFAULT_BRANCH_ID"],),
        ).fetchone()
        ensure_branch_identities(
            conn, repository["DEFAULT_BRANCH_ID"], repository["REPOSITORY_ID"], branch[0]
        )
        now = _utcnow()
        for sheet_name, formulas in formulas_by_sheet.items():
            sheet = conn.execute(
                """
                SELECT SHEET_ID FROM BRANCH_SHEETS
                WHERE BRANCH_ID=? AND SHEET_NAME=? COLLATE NOCASE AND STATUS='ACTIVE'
                """,
                (repository["DEFAULT_BRANCH_ID"], sheet_name),
            ).fetchone()
            if not sheet:
                continue
            for formula in formulas:
                row = conn.execute(
                    """
                    SELECT ROW_ID FROM SHEET_ROWS
                    WHERE BRANCH_ID=? AND SHEET_ID=? AND ROW_POSITION=? AND STATUS='ACTIVE'
                    """,
                    (repository["DEFAULT_BRANCH_ID"], sheet[0], formula["row_position"]),
                ).fetchone()
                column = conn.execute(
                    """
                    SELECT COLUMN_ID FROM SHEET_COLUMNS
                    WHERE BRANCH_ID=? AND SHEET_ID=? AND COLUMN_POSITION=? AND STATUS='ACTIVE'
                    """,
                    (repository["DEFAULT_BRANCH_ID"], sheet[0], formula["column_position"]),
                ).fetchone()
                if not row or not column:
                    continue
                conn.execute(
                    """
                    INSERT INTO CELL_METADATA
                        (BRANCH_ID, SHEET_ID, ROW_ID, COLUMN_ID, FORMULA,
                         STYLE_HASH, COMMENT_TEXT, UPDATED_AT)
                    VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
                    ON CONFLICT(BRANCH_ID,SHEET_ID,ROW_ID,COLUMN_ID) DO UPDATE SET
                        FORMULA=excluded.FORMULA, UPDATED_AT=excluded.UPDATED_AT
                    """,
                    (
                        repository["DEFAULT_BRANCH_ID"], sheet[0], row[0], column[0],
                        formula["formula"], now,
                    ),
                )
        head = conn.execute(
            "SELECT HEAD_COMMIT_ID FROM BRANCHES WHERE BRANCH_ID=?",
            (repository["DEFAULT_BRANCH_ID"],),
        ).fetchone()[0]
        snapshot = semantic_snapshot(conn, table_id)
        snapshot["head_commit_id"] = head
        conn.execute(
            """
            UPDATE BRANCH_CHECKPOINTS SET SNAPSHOT_JSON=?
            WHERE BRANCH_ID=? AND COMMIT_ID=?
            """,
            (json.dumps(snapshot, default=str, separators=(",", ":")), repository["DEFAULT_BRANCH_ID"], head),
        )
        from ..services.semantic_ledger_service import ledger_for

        ledger_for(_current_db_path()).persist_commit(
            conn, head, repository["REPOSITORY_ID"], repository["DEFAULT_BRANCH_ID"], snapshot
        )
        conn.commit()
    finally:
        conn.close()


def _clone_table(conn: sqlite3.Connection, source_table: str, destination_table: str) -> None:
    # Self-validating: this is an internal helper the audit specifically
    # flagged as building dynamic SQL without checking its own inputs.
    source_table = SafeIdentifier(source_table)
    destination_table = SafeIdentifier(destination_table)
    ddl_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (source_table,)
    ).fetchone()
    if not ddl_row or not ddl_row[0]:
        raise ValueError(f"Source sheet data table {source_table} does not exist")
    ddl = re.sub(
        r"^(CREATE\s+TABLE\s+)(?:IF\s+NOT\s+EXISTS\s+)?(?:\"[^\"]+\"|\[[^\]]+\]|`[^`]+`|\S+)",
        rf'\1"{destination_table}"', ddl_row[0], count=1, flags=re.IGNORECASE,
    )
    conn.execute(ddl)
    conn.execute(f'INSERT INTO "{destination_table}" SELECT * FROM "{source_table}"')


def _materialize_branch_projection(
    conn: sqlite3.Connection, branch: sqlite3.Row, user_id: str, now: str
) -> sqlite3.Row:
    """Hydrate an editable compatibility projection from an immutable branch root."""
    from ..services.semantic_ledger_service import ledger_for_connection

    state = ledger_for_connection(conn).reconstruct(conn, branch["HEAD_COMMIT_ID"])
    if state is None:
        raise ValueError("The branch commit has not been migrated to semantic storage")
    version_row = conn.execute(
        "SELECT DATASET_VERSION FROM COMMITS WHERE COMMIT_ID=?", (branch["HEAD_COMMIT_ID"],)
    ).fetchone()
    version = int(version_row[0] or 0) if version_row else 0
    primary_table = None
    for sheet_index, sheet in enumerate(sorted(state["sheets"], key=lambda item: item["position"])):
        destination = SafeIdentifier(
            f"BRANCH_DATA_{uuid.uuid4().hex[:12].upper()}"
            if sheet_index == 0 else f"BRANCH_SHEET_{uuid.uuid4().hex[:12].upper()}"
        )
        columns = sorted(sheet.get("columns", []), key=lambda item: item["position"])
        definitions = [
            f'"{column["name"]}" {column.get("data_type") or "TEXT"}' for column in columns
        ]
        conn.execute(
            f'CREATE TABLE "{destination}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT{", " if definitions else ""}{", ".join(definitions)})'
        )
        rows = sorted(sheet.get("rows", []), key=lambda item: item["position"])
        for row in rows:
            physical_id = int(row.get("physical_row_id") or row["position"] + 1)
            names = ["ROW_ID"] + [column["name"] for column in columns]
            values = [physical_id] + [row.get("values", {}).get(column["column_id"]) for column in columns]
            conn.execute(
                f'INSERT INTO "{destination}" ({", ".join(f"\"{name}\"" for name in names)}) VALUES ({", ".join("?" for _ in names)})',
                values,
            )
        conn.execute(
            "INSERT INTO DATASET_REGISTRY VALUES (?,?,?,?,?,?,?,?)",
            (destination, user_id, f"projection:{branch['BRANCH_NAME']}:{sheet['name']}",
             len(rows), len(columns), now, now, version),
        )
        conn.execute(
            "INSERT INTO BRANCH_SHEET_TABLES VALUES (?,?,?,?,?,?)",
            (branch["BRANCH_ID"], sheet["sheet_id"], destination, int(sheet_index == 0), now, now),
        )
        conn.execute(
            "INSERT INTO BRANCH_SHEETS VALUES (?,?,?,?, 'ACTIVE',?,?)",
            (branch["BRANCH_ID"], sheet["sheet_id"], sheet["name"], sheet["position"], now, now),
        )
        for column in columns:
            conn.execute(
                "INSERT INTO SHEET_COLUMNS VALUES (?,?,?,?,?,?, 'ACTIVE',?,?)",
                (branch["BRANCH_ID"], sheet["sheet_id"], column["column_id"], column["name"],
                 column["position"], column.get("data_type") or "TEXT", now, now),
            )
        for row in rows:
            physical_id = int(row.get("physical_row_id") or row["position"] + 1)
            conn.execute(
                "INSERT INTO SHEET_ROWS VALUES (?,?,?,?,?, 'ACTIVE',?,?)",
                (branch["BRANCH_ID"], sheet["sheet_id"], row["row_id"], physical_id,
                 row["position"], now, now),
            )
            for column_id in set(row.get("formulas", {})) | set(row.get("styles", {})) | set(row.get("comments", {})):
                conn.execute(
                    "INSERT INTO CELL_METADATA VALUES (?,?,?,?,?,?,?,?)",
                    (branch["BRANCH_ID"], sheet["sheet_id"], row["row_id"], column_id,
                     row.get("formulas", {}).get(column_id), row.get("styles", {}).get(column_id),
                     row.get("comments", {}).get(column_id), now),
                )
        _store_dataset_version(
            conn, destination, version, branch["HEAD_COMMIT_ID"],
            "Semantic branch projection", user_id, now,
        )
        if sheet_index == 0:
            primary_table = destination
    if not primary_table:
        raise ValueError("A workbook branch must contain at least one worksheet")
    conn.execute(
        "UPDATE BRANCHES SET DATA_TABLE_ID=?,UPDATED_AT=? WHERE BRANCH_ID=?",
        (primary_table, now, branch["BRANCH_ID"]),
    )
    return conn.execute(
        "SELECT * FROM BRANCHES WHERE BRANCH_ID=?", (branch["BRANCH_ID"],)
    ).fetchone()


def create_working_copy(
    table_id: str, user_id: str, user_email: str, branch_mode: str = "continue",
    branch_id: str | None = None,
) -> dict[str, Any]:
    """Create or reuse an isolated personal branch and issue signed workbook identity."""
    if branch_mode not in {"continue", "new"}:
        raise ValueError("Working copy mode must be continue or new")
    conn = _get_connection()
    try:
        repo = conn.execute(
            "SELECT * FROM WORKBOOK_REPOSITORIES WHERE TABLE_ID=? AND STATUS='ACTIVE'", (table_id,)
        ).fetchone()
        if not repo:
            raise ValueError("Workbook repository does not exist")
        slug = re.sub(r"[^a-z0-9]+", "-", user_email.split("@", 1)[0].lower()).strip("-") or "user"
        branch_name_base = f"users/{slug}/{repo['REPOSITORY_NAME'].lower().replace(' ', '-')}"
        branch_name = branch_name_base
        branch = None
        if branch_mode == "continue":
            if branch_id:
                branch = conn.execute(
                    """
                    SELECT * FROM BRANCHES WHERE BRANCH_ID=? AND REPOSITORY_ID=?
                      AND CREATED_BY=? AND BRANCH_TYPE='USER' AND STATUS='ACTIVE'
                    """,
                    (branch_id, repo["REPOSITORY_ID"], user_id),
                ).fetchone()
                if not branch:
                    raise ValueError("The selected personal branch is not active")
            else:
                branch = conn.execute(
                    "SELECT * FROM BRANCHES WHERE REPOSITORY_ID=? AND BRANCH_NAME=? AND STATUS='ACTIVE'",
                    (repo["REPOSITORY_ID"], branch_name),
                ).fetchone()
        now = _utcnow()
        if not branch:
            suffix = 2
            while conn.execute(
                "SELECT 1 FROM BRANCHES WHERE REPOSITORY_ID=? AND BRANCH_NAME=?",
                (repo["REPOSITORY_ID"], branch_name),
            ).fetchone():
                branch_name = f"{branch_name_base}-{suffix}"
                suffix += 1
            branch_id = f"BR_{uuid.uuid4().hex[:12].upper()}"
            main = conn.execute(
                "SELECT * FROM BRANCHES WHERE BRANCH_ID=?", (repo["DEFAULT_BRANCH_ID"],)
            ).fetchone()
            ensure_branch_identities(
                conn, main["BRANCH_ID"], repo["REPOSITORY_ID"], main["DATA_TABLE_ID"]
            )
            source_mappings = conn.execute(
                """
                SELECT T.SHEET_ID, T.DATA_TABLE_ID, T.IS_PRIMARY
                FROM BRANCH_SHEET_TABLES T JOIN BRANCH_SHEETS S
                  ON S.BRANCH_ID=T.BRANCH_ID AND S.SHEET_ID=T.SHEET_ID
                WHERE T.BRANCH_ID=? AND S.STATUS='ACTIVE'
                ORDER BY S.SHEET_POSITION
                """,
                (main["BRANCH_ID"],),
            ).fetchall()
            cloned_mappings = []
            for source in source_mappings:
                destination = (
                    f"BRANCH_DATA_{uuid.uuid4().hex[:12].upper()}"
                    if source["IS_PRIMARY"] else f"BRANCH_SHEET_{uuid.uuid4().hex[:12].upper()}"
                )
                _clone_table(conn, source["DATA_TABLE_ID"], destination)
                row_count = conn.execute(f'SELECT COUNT(*) FROM "{destination}"').fetchone()[0]
                column_count = len(conn.execute(f'PRAGMA table_info("{destination}")').fetchall()) - 1
                conn.execute(
                    "INSERT INTO DATASET_REGISTRY VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                    (destination, user_id, f"branch:{branch_name}", row_count, column_count, now, now),
                )
                _store_dataset_version(
                    conn, destination, 0, None, "Personal branch checkout", user_id, now
                )
                cloned_mappings.append((source["SHEET_ID"], destination, source["IS_PRIMARY"]))
            branch_table = next(
                mapping[1] for mapping in cloned_mappings if mapping[2]
            )
            conn.execute(
                """
                INSERT INTO BRANCHES (BRANCH_ID, REPOSITORY_ID, DATA_TABLE_ID, BRANCH_NAME,
                    BRANCH_TYPE, CREATED_BY, BASE_COMMIT_ID, HEAD_COMMIT_ID, STATUS, CREATED_AT, UPDATED_AT)
                VALUES (?, ?, ?, ?, 'USER', ?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (branch_id, repo["REPOSITORY_ID"], branch_table, branch_name, user_id, main["HEAD_COMMIT_ID"], main["HEAD_COMMIT_ID"], now, now),
            )
            for sheet_id, mapped_table, is_primary in cloned_mappings:
                conn.execute(
                    """
                    INSERT INTO BRANCH_SHEET_TABLES
                        (BRANCH_ID, SHEET_ID, DATA_TABLE_ID, IS_PRIMARY, CREATED_AT, UPDATED_AT)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (branch_id, sheet_id, mapped_table, is_primary, now, now),
                )
            conn.execute("UPDATE WORKBOOK_REPOSITORIES SET MAIN_PROTECTED=1, UPDATED_AT=? WHERE REPOSITORY_ID=?", (now, repo["REPOSITORY_ID"]))
            branch = conn.execute("SELECT * FROM BRANCHES WHERE BRANCH_ID=?", (branch_id,)).fetchone()
            ensure_branch_identities(
                conn,
                branch_id,
                repo["REPOSITORY_ID"],
                branch_table,
                source_branch_id=main["BRANCH_ID"],
            )
        else:
            if str(branch["DATA_TABLE_ID"]).startswith("POINTER_"):
                branch = _materialize_branch_projection(conn, branch, user_id, now)
            ensure_branch_identities(
                conn,
                branch["BRANCH_ID"],
                repo["REPOSITORY_ID"],
                branch["DATA_TABLE_ID"],
                source_branch_id=repo["DEFAULT_BRANCH_ID"],
            )
        working_copy_id = f"WC_{uuid.uuid4().hex[:12].upper()}"
        issued_at = now
        fingerprint = hashlib.sha256(f"{repo['REPOSITORY_ID']}:{branch['BRANCH_ID']}:{working_copy_id}".encode()).hexdigest()
        payload = ":".join([repo["REPOSITORY_ID"], branch["BRANCH_ID"], working_copy_id, branch["HEAD_COMMIT_ID"], issued_at])
        signature = hmac.new(settings.auth_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        conn.execute(
            """
            INSERT INTO WORKING_COPIES (WORKING_COPY_ID, REPOSITORY_ID, BRANCH_ID, USER_ID,
                BASE_COMMIT_ID, GENERATED_AT, LAST_SEEN_AT, STATUS, WORKBOOK_FINGERPRINT, ISSUED_AT, SIGNATURE)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
            """,
            (working_copy_id, repo["REPOSITORY_ID"], branch["BRANCH_ID"], user_id, branch["HEAD_COMMIT_ID"], now, now, fingerprint, issued_at, signature),
        )
        conn.commit()
        return {
            "repository_id": repo["REPOSITORY_ID"], "branch_id": branch["BRANCH_ID"],
            "branch_name": branch["BRANCH_NAME"], "table_id": branch["DATA_TABLE_ID"],
            "working_copy_id": working_copy_id, "base_commit_id": branch["HEAD_COMMIT_ID"],
            "issued_at": issued_at, "signature": signature,
        }
    finally:
        conn.close()


def working_copy_checkout_options(table_id: str, user_id: str) -> dict[str, Any]:
    """Describe reusable branches before issuing another local workbook."""
    conn = _get_connection()
    try:
        repository = conn.execute(
            """
            SELECT R.REPOSITORY_ID,R.REPOSITORY_NAME,M.ROLE
            FROM WORKBOOK_REPOSITORIES R
            LEFT JOIN REPOSITORY_MEMBERS M
              ON M.REPOSITORY_ID=R.REPOSITORY_ID AND M.USER_ID=?
            WHERE R.TABLE_ID=? AND R.STATUS='ACTIVE'
            """,
            (user_id, table_id),
        ).fetchone()
        if not repository:
            raise ValueError("Workbook repository does not exist")
        branches = conn.execute(
            """
            SELECT B.BRANCH_ID,B.BRANCH_NAME,B.HEAD_COMMIT_ID,B.UPDATED_AT,
                   COUNT(CASE WHEN W.STATUS='ACTIVE' THEN 1 END) AS ACTIVE_COPIES,
                   MAX(W.LAST_SEEN_AT) AS LAST_OPENED_AT
            FROM BRANCHES B
            LEFT JOIN WORKING_COPIES W ON W.BRANCH_ID=B.BRANCH_ID AND W.USER_ID=?
            WHERE B.REPOSITORY_ID=? AND B.CREATED_BY=?
              AND B.BRANCH_TYPE='USER' AND B.STATUS='ACTIVE'
            GROUP BY B.BRANCH_ID
            ORDER BY B.UPDATED_AT DESC
            """,
            (user_id, repository["REPOSITORY_ID"], user_id),
        ).fetchall()
        return {
            "repository_id": repository["REPOSITORY_ID"],
            "repository_name": repository["REPOSITORY_NAME"],
            "role": repository["ROLE"],
            "can_edit": repository["ROLE"] in {"owner", "editor"},
            "branches": [
                {key.lower(): row[key] for key in row.keys()} for row in branches
            ],
        }
    finally:
        conn.close()


def authenticate_working_copy_identity(
    table_id: str,
    repository_id: str,
    branch_id: str,
    working_copy_id: str,
    base_commit_id: str,
    issued_at: str,
    signature: str,
    machine_id: str | None = None,
) -> dict[str, Any]:
    """Authenticate the owner of an active, signed workbook checkout."""
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT W.USER_ID,U.EMAIL
            FROM WORKING_COPIES W JOIN APP_USERS U ON U.USER_ID=W.USER_ID
            WHERE W.WORKING_COPY_ID=?
            """,
            (working_copy_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise PermissionError("Working copy is not registered")
    validate_working_copy(
        table_id, row["USER_ID"], repository_id, branch_id, working_copy_id,
        base_commit_id, issued_at, signature, machine_id,
    )
    return {"user_id": row["USER_ID"], "email": row["EMAIL"]}


def validate_working_copy(
    table_id: str,
    user_id: str,
    repository_id: str | None,
    branch_id: str | None,
    working_copy_id: str | None,
    base_commit_id: str | None,
    issued_at: str | None,
    signature: str | None,
    machine_id: str | None = None,
) -> dict[str, Any]:
    """Validate signed workbook identity against authoritative server records."""
    values = (
        repository_id, branch_id, working_copy_id, base_commit_id, issued_at, signature,
    )
    if any(not value for value in values):
        raise PermissionError(
            "This workbook is missing Git Walk branch identity. Download a fresh working copy."
        )
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT W.*, B.DATA_TABLE_ID, B.STATUS AS BRANCH_STATUS, B.BRANCH_NAME,
                   R.STATUS AS REPOSITORY_STATUS
            FROM WORKING_COPIES W
            JOIN BRANCHES B ON B.BRANCH_ID=W.BRANCH_ID
            JOIN WORKBOOK_REPOSITORIES R ON R.REPOSITORY_ID=W.REPOSITORY_ID
            WHERE W.WORKING_COPY_ID=?
            """,
            (working_copy_id,),
        ).fetchone()
        if not row:
            raise PermissionError("Working copy is not registered")
        expected_values = {
            "REPOSITORY_ID": repository_id,
            "BRANCH_ID": branch_id,
            "USER_ID": user_id,
            "DATA_TABLE_ID": table_id,
            "BASE_COMMIT_ID": base_commit_id,
            "ISSUED_AT": issued_at,
        }
        if any(str(row[key]) != str(value) for key, value in expected_values.items()):
            raise PermissionError("Workbook identity does not match the signed working copy")
        if row["STATUS"] != "ACTIVE" or row["BRANCH_STATUS"] != "ACTIVE":
            raise PermissionError("Working copy or branch is no longer active")
        if row["REPOSITORY_STATUS"] != "ACTIVE":
            raise PermissionError("Workbook repository is no longer active")
        payload = ":".join(
            [repository_id, branch_id, working_copy_id, base_commit_id, issued_at]
        )
        expected_signature = hmac.new(
            settings.auth_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, row["SIGNATURE"]):
            raise PermissionError("Workbook signature is not valid")
        if not hmac.compare_digest(signature, expected_signature):
            raise PermissionError("Workbook signature could not be verified")
        binding = bind_or_verify_device(working_copy_id, machine_id)
        if binding["status"] == "MISMATCH":
            raise PermissionError(
                "DEVICE_MISMATCH: This workbook is locked to a different device than the one it was first "
                "opened on. Ask the repository owner to trust this device before trying again."
            )
        now = _utcnow()
        conn.execute(
            "UPDATE WORKING_COPIES SET LAST_SEEN_AT=? WHERE WORKING_COPY_ID=?",
            (now, working_copy_id),
        )
        _touch_daily_activity(conn, user_id, now)
        conn.commit()
        return {key.lower(): row[key] for key in row.keys()}
    finally:
        conn.close()


def advance_branch_head(table_id: str, commit_id: str) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE BRANCHES SET HEAD_COMMIT_ID=?, UPDATED_AT=? WHERE DATA_TABLE_ID=? AND BRANCH_TYPE='USER'",
            (commit_id, _utcnow(), table_id),
        )
        conn.commit()
    finally:
        conn.close()


def working_copy_required(table_id: str) -> bool:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT BRANCH_TYPE FROM BRANCHES WHERE DATA_TABLE_ID=? AND STATUS='ACTIVE'",
            (table_id,),
        ).fetchone()
        return bool(row and row[0] == "USER")
    finally:
        conn.close()


def user_can_work_on_repository(table_id: str, user_id: str) -> bool:
    """Check repository edit access without bypassing protected-main rules."""
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM WORKBOOK_REPOSITORIES R
            JOIN REPOSITORY_MEMBERS M ON M.REPOSITORY_ID=R.REPOSITORY_ID
            WHERE R.TABLE_ID=? AND R.STATUS='ACTIVE'
              AND (M.USER_ID=? AND M.ROLE IN ('owner','editor') OR ?='USR_SYSTEM')
            """,
            (table_id, user_id, user_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def repository_name_available(name: str) -> dict[str, Any]:
    normalized = name.strip()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    if len(normalized) < 2 or not slug:
        return {"available": False, "slug": slug, "reason": "Use at least two letters or numbers"}
    conn = _get_connection()
    try:
        exists = conn.execute(
            "SELECT 1 FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_SLUG=? AND STATUS='ACTIVE'",
            (slug,),
        ).fetchone()
        return {
            "available": exists is None,
            "slug": slug,
            "reason": None if exists is None else "That repository name is already in use",
        }
    finally:
        conn.close()


def delete_repository(table_id: str, user_id: str) -> dict[str, Any]:
    """Owner-initiated repository deletion.

    Runs the full permanent-deletion pipeline immediately (export every
    record to JSON, email it to the repository's owner(s), raise an
    in-app notification, then hard-delete) rather than the old
    soft-delete-and-leave-it-forever behavior — see
    services/repository_purge_service.py for the full pipeline and the
    reasoning behind it (legal-hold blocking, what's excluded from the
    cascade and why, garbage collection of now-orphaned storage objects).
    Local import: this is a store-layer function calling up into a
    service-layer module, which the codebase avoids at module load time
    (the same pattern already used for e.g. ledger_for() calls elsewhere
    in this package) — safe here since it's resolved lazily, at call time.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT R.REPOSITORY_ID, M.ROLE FROM WORKBOOK_REPOSITORIES R
            JOIN REPOSITORY_MEMBERS M ON M.REPOSITORY_ID=R.REPOSITORY_ID
            WHERE R.TABLE_ID=? AND R.STATUS='ACTIVE' AND M.USER_ID=?
            """,
            (table_id, user_id),
        ).fetchone()
        policy = RepositoryPolicy.from_value(row["ROLE"] if row else None)
        if not row or not policy or not policy.can_delete_repository:
            raise PermissionError("Only the repository owner can delete this repository")
        repository_id = row["REPOSITORY_ID"]
    finally:
        conn.close()

    from ..services.repository_purge_service import purge_repository
    result = purge_repository(repository_id, actor_user_id=user_id, reason="OWNER_REQUESTED")
    return {"repository_id": repository_id, "table_id": table_id, "status": "DELETED", **result}


def delete_branch(branch_id: str, user_id: str) -> dict[str, Any]:
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT B.*, M.ROLE FROM BRANCHES B
            JOIN REPOSITORY_MEMBERS M ON M.REPOSITORY_ID=B.REPOSITORY_ID
            WHERE B.BRANCH_ID=? AND B.STATUS IN ('ACTIVE', 'MERGED') AND M.USER_ID=?
            """,
            (branch_id, user_id),
        ).fetchone()
        if not row:
            raise PermissionError("Branch does not exist or is not accessible")
        policy = BranchPolicy(
            actor_user_id=user_id,
            created_by=row["CREATED_BY"],
            branch_type=row["BRANCH_TYPE"],
            repository_role=RepositoryRole(row["ROLE"]),
        )
        if not policy.can_delete:
            raise PermissionError("Only the branch author or repository owner can delete this branch")
        now = _utcnow()
        conn.execute(
            "UPDATE BRANCHES SET STATUS='DELETED', ARCHIVED_AT=?, UPDATED_AT=? WHERE BRANCH_ID=?",
            (now, now, branch_id),
        )
        conn.execute(
            "UPDATE WORKING_COPIES SET STATUS='REVOKED', LAST_SEEN_AT=? WHERE BRANCH_ID=? AND STATUS='ACTIVE'",
            (now, branch_id),
        )
        conn.commit()
        return {"branch_id": branch_id, "status": "DELETED", "working_copy_status": "REVOKED"}
    finally:
        conn.close()


def update_branch_local_path(branch_id: str, local_path: str) -> None:
    conn = _get_connection()
    now = _utcnow()
    try:
        conn.execute(
            "UPDATE BRANCHES SET LOCAL_DOWNLOAD_PATH=?, UPDATED_AT=? WHERE BRANCH_ID=?",
            (local_path, now, branch_id),
        )
        conn.execute(
            "UPDATE WORKING_COPIES SET LOCAL_FILE_PATH=?, LAST_SEEN_AT=? WHERE BRANCH_ID=? AND STATUS='ACTIVE'",
            (local_path, now, branch_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_working_copy_local_path(working_copy_id: str, local_file_path: str) -> None:
    conn = _get_connection()
    now = _utcnow()
    try:
        conn.execute(
            "UPDATE WORKING_COPIES SET LOCAL_FILE_PATH=?, LAST_SEEN_AT=? WHERE WORKING_COPY_ID=?",
            (local_file_path, now, working_copy_id),
        )
        conn.commit()
    finally:
        conn.close()


def _normalize_path(raw_path: str | None) -> str:
    if not raw_path:
        return ""
    import urllib.parse
    cleaned = urllib.parse.unquote(str(raw_path).strip())
    if cleaned.lower().startswith("file:///"):
        cleaned = cleaned[8:]
    elif cleaned.lower().startswith("file://"):
        cleaned = cleaned[7:]
    cleaned = cleaned.replace("/", "\\")
    while len(cleaned) >= 3 and cleaned[0] == "\\" and cleaned[2] == ":":
        cleaned = cleaned[1:]
    return cleaned.rstrip("\\").lower()


def verify_workbook_access(
    repository_id: str,
    branch_id: str,
    working_copy_id: str,
    email: str,
    code: str | None = None,
    password: str | None = None,
    current_file_path: str = "",
    machine_id: str | None = None,
) -> dict[str, Any]:
    """Verify file path, OTP verification code (or password), and repository role before allowing workbook data load."""
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT W.*, B.DATA_TABLE_ID, B.LOCAL_DOWNLOAD_PATH, B.STATUS AS BRANCH_STATUS,
                   R.STATUS AS REPOSITORY_STATUS
            FROM WORKING_COPIES W
            JOIN BRANCHES B ON B.BRANCH_ID=W.BRANCH_ID
            JOIN WORKBOOK_REPOSITORIES R ON R.REPOSITORY_ID=W.REPOSITORY_ID
            WHERE W.WORKING_COPY_ID=?
            """,
            (working_copy_id,),
        ).fetchone()
        if not row:
            raise PermissionError("Working copy is not registered.")
        if repository_id and row["REPOSITORY_ID"] != repository_id:
            raise PermissionError("Workbook repository mismatch.")
        if branch_id and row["BRANCH_ID"] != branch_id:
            raise PermissionError("Workbook branch mismatch.")
        if row["STATUS"] in ("REVOKED", "CLOSED") or row["BRANCH_STATUS"] in ("DELETED", "MERGED"):
            raise PermissionError("This working copy or branch was merged or closed. Create a new branch in Git Walk.")
        if row["STATUS"] != "ACTIVE" or row["BRANCH_STATUS"] != "ACTIVE":
            raise PermissionError("Working copy or branch is no longer active.")
        if row["REPOSITORY_STATUS"] != "ACTIVE":
            raise PermissionError("Workbook repository is no longer active.")

        # 1. Path verification
        recorded_path = row["LOCAL_FILE_PATH"] or row["LOCAL_DOWNLOAD_PATH"]
        if recorded_path:
            norm_recorded = _normalize_path(recorded_path)
            norm_current = _normalize_path(current_file_path)
            if not norm_current or norm_recorded != norm_current:
                raise PermissionError(
                    f"FILE_PATH_MISMATCH: Current workbook path '{current_file_path}' does not match "
                    f"authorized download path '{recorded_path}'. Data loading blocked."
                )

        # 2. Authentication: verify OTP code or password
        from ..security import hash_login_code, create_session_token
        normalized_email = email.strip().lower()
        if "@" not in normalized_email:
            user_row = conn.execute(
                "SELECT EMAIL FROM APP_USERS WHERE USER_ID=? OR LOWER(EMAIL)=LOWER(?)",
                (normalized_email.upper(), normalized_email),
            ).fetchone()
            if user_row:
                normalized_email = user_row["EMAIL"].lower()

        if code:
            code_hash = hash_login_code(normalized_email, code.strip())
            if not consume_login_code(normalized_email, code_hash):
                raise PermissionError("INVALID_CODE: Invalid or expired verification code.")
            user = get_or_create_user(normalized_email)
        elif password:
            user = verify_user_credentials(normalized_email, password)
            if not user:
                raise PermissionError("INVALID_CREDENTIALS: User ID or password is incorrect.")
        else:
            raise PermissionError("AUTHENTICATION_REQUIRED: Verification code is required.")

        token = create_session_token(user["user_id"], user["email"])

        # 2b. Assigned-user check — a downloaded workbook is bound to the
        # user it was issued to, so a coworker can't authenticate on it with
        # their own credentials even if they know a valid OTP for themselves.
        if row["USER_ID"] and user["user_id"] != row["USER_ID"]:
            raise PermissionError(
                "ASSIGNED_USER_MISMATCH: This workbook was issued to a different account."
            )

        # 3. Role check on repository
        member = conn.execute(
            """
            SELECT ROLE FROM REPOSITORY_MEMBERS
            WHERE REPOSITORY_ID=? AND USER_ID=?
            """,
            (row["REPOSITORY_ID"], user["user_id"]),
        ).fetchone()
        if not member:
            raise PermissionError("NO_REPOSITORY_ACCESS: You are not a member of this repository.")
        role = member["ROLE"]
        if role not in ("owner", "editor"):
            raise PermissionError(
                f"INSUFFICIENT_ROLE: Your role is '{role}'. You must be an editor or owner to work on this repository."
            )

        # 4. Device binding — the actual anti-leakage mechanism: a copied
        # .xlsx opened on a different machine fails here even with a
        # byte-identical path, valid signature, and valid OTP.
        binding = bind_or_verify_device(working_copy_id, machine_id)
        if binding["status"] == "MISMATCH":
            raise PermissionError(
                "DEVICE_MISMATCH: This workbook is locked to a different device than the one it was first "
                "opened on. Ask the repository owner to trust this device before trying again."
            )

        now = _utcnow()
        conn.execute("UPDATE WORKING_COPIES SET LAST_SEEN_AT=? WHERE WORKING_COPY_ID=?", (now, working_copy_id))
        conn.commit()

        return {
            "user": user,
            "token": token,
            "role": role,
            "table_id": row["DATA_TABLE_ID"],
            "repository_id": row["REPOSITORY_ID"],
            "branch_id": row["BRANCH_ID"],
            "working_copy_id": working_copy_id,
        }
    finally:
        conn.close()


def get_branch_sheet_page(
    table_id: str,
    branch_id: str,
    sheet_id: str,
    user_id: str,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    repository = get_repository(table_id, user_id)
    if not repository:
        raise PermissionError("Repository access is required")
    conn = _get_connection()
    try:
        mapping = conn.execute(
            """
            SELECT T.DATA_TABLE_ID, S.SHEET_NAME, S.SHEET_POSITION
            FROM BRANCH_SHEET_TABLES T JOIN BRANCH_SHEETS S
              ON S.BRANCH_ID=T.BRANCH_ID AND S.SHEET_ID=T.SHEET_ID
            WHERE T.BRANCH_ID=? AND T.SHEET_ID=? AND S.STATUS='ACTIVE'
              AND S.BRANCH_ID IN (SELECT BRANCH_ID FROM BRANCHES WHERE REPOSITORY_ID=? AND STATUS IN ('ACTIVE','MERGED'))
            """,
            (branch_id, sheet_id, repository["repository_id"]),
        ).fetchone()
        if not mapping:
            branch = conn.execute(
                "SELECT HEAD_COMMIT_ID FROM BRANCHES WHERE BRANCH_ID=? AND REPOSITORY_ID=? AND STATUS IN ('ACTIVE','MERGED')",
                (branch_id, repository["repository_id"]),
            ).fetchone()
            if not branch:
                raise ValueError("Worksheet does not exist on this branch")
            from ..repositories.commit_store import reconstruct_branch

            state = reconstruct_branch(branch_id)
            sheet = next(
                (item for item in state.get("sheets", []) if item["sheet_id"] == sheet_id), None
            )
            if not sheet:
                raise ValueError("Worksheet does not exist on this branch")
            columns = sorted(sheet.get("columns", []), key=lambda item: item["position"])
            rows = sorted(sheet.get("rows", []), key=lambda item: item["position"])
            page_rows = rows[offset:offset + max(1, min(limit, 500))]
            commit = conn.execute(
                "SELECT DATASET_VERSION FROM COMMITS WHERE COMMIT_ID=?", (branch[0],)
            ).fetchone()
            return {
                "table_id": f"POINTER_{branch_id}", "columns": ["ROW_ID"] + [item["name"] for item in columns],
                "rows": [[row.get("physical_row_id") or row["position"] + 1] +
                         [row.get("values", {}).get(column["column_id"]) for column in columns]
                         for row in page_rows],
                "total": len(rows), "limit": limit, "offset": offset,
                "version": int(commit[0] or 0) if commit else 0, "updated_at": None,
                "repository_table_id": table_id, "branch_id": branch_id,
                "sheet_id": sheet_id, "sheet_name": sheet["name"],
                "sheet_position": sheet["position"], "storage_engine": "semantic_object_v1",
            }
        page = get_table_page(mapping["DATA_TABLE_ID"], limit, offset)
        page.update({
            "repository_table_id": table_id,
            "branch_id": branch_id,
            "sheet_id": sheet_id,
            "sheet_name": mapping["SHEET_NAME"],
            "sheet_position": mapping["SHEET_POSITION"],
        })
        return page
    finally:
        conn.close()


def create_semantic_branch(
    table_id: str, name: str, from_commit_id: str | None, user_id: str
) -> dict[str, Any]:
    """Create a constant-time branch pointer without cloning workbook data."""
    repository = get_repository(table_id, user_id)
    if not repository or not repository.get("capabilities", {}).get("edit"):
        raise PermissionError("Editor access is required to create a branch")
    normalized = re.sub(r"\s+", "-", name.strip()).strip("/")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{3,255}", normalized):
        raise ValueError("Branch names may contain letters, numbers, dot, dash, underscore, and slash")
    conn = _get_connection()
    try:
        base_commit = from_commit_id or repository["main_head_commit_id"]
        commit = conn.execute(
            "SELECT 1 FROM COMMITS WHERE COMMIT_ID=? AND REPOSITORY_ID=?",
            (base_commit, repository["repository_id"]),
        ).fetchone()
        if not commit:
            raise ValueError("The selected base commit does not exist in this repository")
        if conn.execute(
            "SELECT 1 FROM BRANCHES WHERE REPOSITORY_ID=? AND BRANCH_NAME=?",
            (repository["repository_id"], normalized),
        ).fetchone():
            raise ValueError("That branch name already exists")
        now = _utcnow()
        branch_id = stable_id("BR")
        pointer_id = f"POINTER_{branch_id.removeprefix('BR_')}"
        started = datetime.now(timezone.utc)
        conn.execute(
            """
            INSERT INTO BRANCHES
                (BRANCH_ID,REPOSITORY_ID,DATA_TABLE_ID,BRANCH_NAME,BRANCH_TYPE,
                 CREATED_BY,BASE_COMMIT_ID,HEAD_COMMIT_ID,STATUS,CREATED_AT,UPDATED_AT)
            VALUES (?,?,?,?, 'USER', ?,?,?, 'ACTIVE',?,?)
            """,
            (branch_id, repository["repository_id"], pointer_id, normalized, user_id,
             base_commit, base_commit, now, now),
        )
        conn.execute(
            "INSERT INTO OUTBOX_EVENTS VALUES (?, 'BRANCH_CREATED', ?, ?, 'PENDING', ?, NULL)",
            (stable_id("EVT"), branch_id,
             json.dumps({"branch_id": branch_id, "repository_id": repository["repository_id"],
                         "base_commit_id": base_commit}, separators=(",", ":")), now),
        )
        conn.commit()
        elapsed = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        return {"branch_id": branch_id, "name": normalized, "base_commit_id": base_commit,
                "head_commit_id": base_commit, "storage_bytes_added": 0,
                "branch_create_ms": round(elapsed, 3), "storage_engine": "semantic_object_v1"}
    finally:
        conn.close()


def list_categories() -> list[dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT C.*,
                   (SELECT COUNT(*) FROM CATEGORIES X WHERE X.PARENT_CATEGORY_ID=C.CATEGORY_ID AND X.STATUS='ACTIVE') AS CHILD_COUNT,
                   (SELECT COUNT(*) FROM WORKBOOK_REPOSITORIES R WHERE R.CATEGORY_ID=C.CATEGORY_ID AND R.STATUS='ACTIVE') AS REPOSITORY_COUNT
            FROM CATEGORIES C WHERE C.STATUS='ACTIVE'
            ORDER BY C.DISPLAY_ORDER, C.NAME
            """
        ).fetchall()
        return [{key.lower(): row[key] for key in row.keys()} for row in rows]
    finally:
        conn.close()


def create_category(
    name: str, description: str | None, parent_category_id: str | None
) -> dict[str, Any]:
    conn = _get_connection()
    try:
        parent_id = parent_category_id or "CAT_HOME"
        if not conn.execute(
            "SELECT 1 FROM CATEGORIES WHERE CATEGORY_ID=? AND STATUS='ACTIVE'", (parent_id,)
        ).fetchone():
            raise ValueError("Parent category does not exist")
        now = _utcnow()
        category_id = f"CAT_{uuid.uuid4().hex[:12].upper()}"
        conn.execute(
            """
            INSERT INTO CATEGORIES
                (CATEGORY_ID, PARENT_CATEGORY_ID, NAME, DESCRIPTION, DISPLAY_ORDER, STATUS, CREATED_AT, UPDATED_AT)
            VALUES (?, ?, ?, ?, 0, 'ACTIVE', ?, ?)
            """,
            (category_id, parent_id, name.strip(), description, now, now),
        )
        conn.commit()
        return {
            "category_id": category_id, "parent_category_id": parent_id,
            "name": name.strip(), "description": description, "status": "ACTIVE",
        }
    finally:
        conn.close()


def move_repository(table_id: str, category_id: str, user_id: str) -> dict[str, str]:
    conn = _get_connection()
    try:
        owner = conn.execute(
            "SELECT OWNER_USER_ID FROM DATASET_REGISTRY WHERE TABLE_ID=?", (table_id,)
        ).fetchone()
        if not owner or owner[0] != user_id:
            raise PermissionError("Only the repository owner can change its business area")
        if not conn.execute(
            "SELECT 1 FROM CATEGORIES WHERE CATEGORY_ID=? AND STATUS='ACTIVE'", (category_id,)
        ).fetchone():
            raise ValueError("Category does not exist")
        repository = conn.execute(
            "SELECT REPOSITORY_ID FROM WORKBOOK_REPOSITORIES WHERE TABLE_ID=?", (table_id,)
        ).fetchone()
        if not repository:
            raise ValueError("Repository does not exist")
        conn.execute(
            "UPDATE WORKBOOK_REPOSITORIES SET CATEGORY_ID=?, UPDATED_AT=? WHERE TABLE_ID=?",
            (category_id, _utcnow(), table_id),
        )
        conn.commit()
        # repository_id is returned so the API layer can record an audit
        # event against it — a prior version of this function omitted it,
        # which caused a KeyError (surfaced to the client as a raw HTTP 500)
        # every time a repository's business area was changed.
        return {"table_id": table_id, "category_id": category_id, "repository_id": repository["REPOSITORY_ID"]}
    finally:
        conn.close()


def get_repository(table_id: str, user_id: str) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT R.*, D.ORIGINAL_FILENAME, D.ROW_COUNT, D.COLUMN_COUNT,
                   D.CURRENT_VERSION, D.OWNER_USER_ID, C.NAME AS CATEGORY_NAME,
                   B.HEAD_COMMIT_ID AS MAIN_HEAD_COMMIT_ID,
                   COALESCE(M.ROLE, CASE WHEN ?='USR_SYSTEM' THEN 'owner' END) AS REPOSITORY_ROLE
            FROM WORKBOOK_REPOSITORIES R
            JOIN DATASET_REGISTRY D ON D.TABLE_ID=R.TABLE_ID
            JOIN CATEGORIES C ON C.CATEGORY_ID=R.CATEGORY_ID
            JOIN BRANCHES B ON B.BRANCH_ID=R.DEFAULT_BRANCH_ID
            LEFT JOIN REPOSITORY_MEMBERS M ON M.REPOSITORY_ID=R.REPOSITORY_ID AND M.USER_ID=?
            WHERE R.TABLE_ID=? AND R.STATUS='ACTIVE' AND (M.USER_ID IS NOT NULL OR ?='USR_SYSTEM')
            """,
            (user_id, user_id, table_id, user_id),
        ).fetchone()
        if not row:
            return None
        result = {key.lower(): row[key] for key in row.keys()}
        policy = RepositoryPolicy.from_value(result.get("repository_role"))
        result["capabilities"] = policy.capabilities() if policy else {}
        sheets = conn.execute(
            """
            SELECT S.*, T.DATA_TABLE_ID
            FROM WORKBOOK_SHEETS S
            LEFT JOIN BRANCH_SHEET_TABLES T
              ON T.SHEET_ID=S.SHEET_ID AND T.BRANCH_ID=?
            WHERE S.REPOSITORY_ID=? AND S.STATUS='ACTIVE' ORDER BY S.SHEET_ORDER
            """,
            (row["DEFAULT_BRANCH_ID"], row["REPOSITORY_ID"]),
        ).fetchall()
        result["sheets"] = [
            {key.lower(): item[key] for key in item.keys()} for item in sheets
        ]
        return result
    finally:
        conn.close()


def list_repository_branches(table_id: str, user_id: str) -> list[dict[str, Any]]:
    repository = get_repository(table_id, user_id)
    if not repository:
        return []
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT B.*, U.EMAIL AS CREATED_BY_EMAIL,
                   (SELECT COUNT(*) FROM WORKING_COPIES W WHERE W.BRANCH_ID=B.BRANCH_ID AND W.STATUS='ACTIVE') AS ACTIVE_COPIES
            FROM BRANCHES B
            LEFT JOIN APP_USERS U ON U.USER_ID=B.CREATED_BY
            WHERE B.REPOSITORY_ID=? AND B.STATUS IN ('ACTIVE','MERGED')
              AND (
                B.BRANCH_TYPE='MAIN' OR B.CREATED_BY=?
                OR ?=(SELECT CREATED_BY FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=B.REPOSITORY_ID)
                OR EXISTS (
                    SELECT 1 FROM MERGE_REQUESTS MR
                    WHERE MR.SOURCE_BRANCH_ID=B.BRANCH_ID
                      AND MR.STATUS NOT IN ('CLOSED')
                )
              )
            ORDER BY CASE B.BRANCH_TYPE WHEN 'MAIN' THEN 0 ELSE 1 END, B.UPDATED_AT DESC
            """,
            (repository["repository_id"], user_id, user_id),
        ).fetchall()
        return [{key.lower(): row[key] for key in row.keys()} for row in rows]
    finally:
        conn.close()


def list_user_working_copies(user_id: str) -> list[dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT W.WORKING_COPY_ID, W.REPOSITORY_ID, W.BRANCH_ID, W.BASE_COMMIT_ID,
                   W.GENERATED_AT, W.LAST_SEEN_AT, W.STATUS, B.BRANCH_NAME,
                   B.DATA_TABLE_ID, B.HEAD_COMMIT_ID, R.TABLE_ID AS MAIN_TABLE_ID,
                   R.REPOSITORY_NAME, D.ORIGINAL_FILENAME
            FROM WORKING_COPIES W
            JOIN BRANCHES B ON B.BRANCH_ID=W.BRANCH_ID
            JOIN WORKBOOK_REPOSITORIES R ON R.REPOSITORY_ID=W.REPOSITORY_ID
            JOIN DATASET_REGISTRY D ON D.TABLE_ID=R.TABLE_ID
            WHERE W.USER_ID=? AND W.STATUS='ACTIVE' AND B.STATUS='ACTIVE'
            ORDER BY W.LAST_SEEN_AT DESC
            """,
            (user_id,),
        ).fetchall()
        return [{key.lower(): row[key] for key in row.keys()} for row in rows]
    finally:
        conn.close()


def _is_dataset_owner_or_member(conn: sqlite3.Connection, table_id: str, user_id: str, role: str | None = None) -> bool:
    """The access clause shared verbatim by user_can_access_table and
    user_can_edit_table: is `user_id` the dataset owner, or a
    DATASET_MEMBERS row for this table (optionally restricted to `role`)?
    Consolidated into one helper instead of two hand-written copies of the
    same WHERE clause — extracted, not rewritten, so the SQL text is
    unchanged from what each caller had inline before."""
    role_clause = " AND M.ROLE=?" if role else ""
    params: tuple = (table_id, user_id, user_id) + ((role,) if role else ())
    row = conn.execute(
        f"""
        SELECT 1 FROM DATASET_REGISTRY D
        WHERE D.TABLE_ID=? AND (
            D.OWNER_USER_ID IN (?, 'USR_SYSTEM') OR EXISTS (
                SELECT 1 FROM DATASET_MEMBERS M
                WHERE M.TABLE_ID=D.TABLE_ID AND M.USER_ID=?{role_clause}
            )
        )
        """,
        params,
    ).fetchone()
    return row is not None


def user_can_access_table(table_id: str, user_id: str) -> bool:
    conn = _get_connection()
    try:
        if _is_dataset_owner_or_member(conn, table_id, user_id):
            return True
        row = conn.execute(
            """
            SELECT 1 FROM DATASET_REGISTRY D
            WHERE D.TABLE_ID=? AND EXISTS (
                SELECT 1 FROM BRANCH_SHEET_TABLES T
                JOIN BRANCHES B ON B.BRANCH_ID=T.BRANCH_ID
                JOIN REPOSITORY_MEMBERS RM ON RM.REPOSITORY_ID=B.REPOSITORY_ID
                JOIN WORKBOOK_REPOSITORIES R ON R.REPOSITORY_ID=B.REPOSITORY_ID
                WHERE T.DATA_TABLE_ID=D.TABLE_ID AND RM.USER_ID=?
                  AND B.STATUS IN ('ACTIVE','MERGED') AND R.STATUS='ACTIVE'
            )
            """,
            (table_id, user_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def user_can_access_branch(branch_id: str, user_id: str) -> bool:
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM BRANCHES B
            JOIN WORKBOOK_REPOSITORIES R ON R.REPOSITORY_ID=B.REPOSITORY_ID
            JOIN DATASET_REGISTRY D ON D.TABLE_ID=R.TABLE_ID
            WHERE B.BRANCH_ID=? AND B.STATUS IN ('ACTIVE','MERGED')
              AND (
                D.OWNER_USER_ID IN (?, 'USR_SYSTEM') OR EXISTS (
                    SELECT 1 FROM DATASET_MEMBERS M
                    WHERE M.TABLE_ID=D.TABLE_ID AND M.USER_ID=?
                )
              )
              AND (
                B.BRANCH_TYPE='MAIN' OR B.CREATED_BY=? OR D.OWNER_USER_ID IN (?, 'USR_SYSTEM')
                OR EXISTS (
                    SELECT 1 FROM MERGE_REQUESTS MR
                    WHERE MR.SOURCE_BRANCH_ID=B.BRANCH_ID
                      AND MR.STATUS NOT IN ('CLOSED')
                )
              )
            """,
            (branch_id, user_id, user_id, user_id, user_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def user_can_edit_table(table_id: str, user_id: str) -> bool:
    conn = _get_connection()
    try:
        if not _is_dataset_owner_or_member(conn, table_id, user_id, role="editor"):
            return False
        row = conn.execute(
            """
            SELECT 1 FROM DATASET_REGISTRY D
            WHERE D.TABLE_ID=? AND NOT EXISTS (
                SELECT 1 FROM WORKBOOK_REPOSITORIES R
                WHERE R.TABLE_ID=D.TABLE_ID AND R.MAIN_PROTECTED=1
            )
            """,
            (table_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_dataset_members(table_id: str) -> list[dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT U.USER_ID, U.EMAIL, U.DISPLAY_NAME, 'owner' AS ROLE, D.CREATED_AT AS ADDED_AT
            FROM DATASET_REGISTRY D JOIN APP_USERS U ON U.USER_ID=D.OWNER_USER_ID
            WHERE D.TABLE_ID=?
            UNION ALL
            SELECT U.USER_ID, U.EMAIL, U.DISPLAY_NAME, M.ROLE, M.ADDED_AT
            FROM DATASET_MEMBERS M JOIN APP_USERS U ON U.USER_ID=M.USER_ID
            WHERE M.TABLE_ID=?
            ORDER BY ROLE, EMAIL
            """,
            (table_id, table_id),
        ).fetchall()
        return [{key.lower(): row[key] for key in row.keys()} for row in rows]
    finally:
        conn.close()


def add_dataset_member(
    table_id: str, owner_user_id: str, email: str, role: str
) -> dict[str, Any]:
    normalized_role = role.strip().lower()
    if normalized_role not in {"viewer", "editor"}:
        raise ValueError("Role must be viewer or editor")
    conn = _get_connection()
    try:
        owner = conn.execute(
            "SELECT OWNER_USER_ID FROM DATASET_REGISTRY WHERE TABLE_ID=?",
            (table_id,),
        ).fetchone()
        if not owner or owner[0] != owner_user_id:
            raise PermissionError("Only the dataset owner can manage access")
        user = conn.execute(
            "SELECT USER_ID, EMAIL, DISPLAY_NAME FROM APP_USERS WHERE EMAIL=? COLLATE NOCASE",
            (email.strip().lower(),),
        ).fetchone()
        normalized_email = email.strip().lower()
        if user and user[0] == owner_user_id:
            raise ValueError("The dataset owner already has full access")
        now = _utcnow()
        if not user:
            invitation_id = f"INV_{uuid.uuid4().hex[:12].upper()}"
            conn.execute(
                """
                INSERT INTO DATASET_INVITATIONS
                    (INVITATION_ID, TABLE_ID, EMAIL, ROLE, STATUS, INVITED_BY, CREATED_AT, UPDATED_AT)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                ON CONFLICT(TABLE_ID, EMAIL) DO UPDATE SET
                    ROLE=excluded.ROLE, STATUS='pending', INVITED_BY=excluded.INVITED_BY,
                    UPDATED_AT=excluded.UPDATED_AT
                """,
                (invitation_id, table_id, normalized_email, normalized_role, owner_user_id, now, now),
            )
            _workspace_event(conn, table_id, "INVITATION_CREATED", owner_user_id, {"email": normalized_email, "role": normalized_role})
            conn.commit()
            return {"email": normalized_email, "role": normalized_role, "status": "pending"}
        conn.execute(
            "INSERT INTO DATASET_MEMBERS (TABLE_ID, USER_ID, ROLE, ADDED_AT) VALUES (?, ?, ?, ?) ON CONFLICT(TABLE_ID, USER_ID) DO UPDATE SET ROLE=excluded.ROLE",
            (table_id, user[0], normalized_role, now),
        )
        repository = conn.execute(
            "SELECT REPOSITORY_ID FROM WORKBOOK_REPOSITORIES WHERE TABLE_ID=?",
            (table_id,),
        ).fetchone()
        if repository:
            conn.execute(
                """
                INSERT INTO REPOSITORY_MEMBERS
                    (REPOSITORY_ID, USER_ID, ROLE, GRANTED_BY, CREATED_AT, UPDATED_AT)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(REPOSITORY_ID, USER_ID) DO UPDATE SET
                    ROLE=excluded.ROLE, GRANTED_BY=excluded.GRANTED_BY,
                    UPDATED_AT=excluded.UPDATED_AT
                """,
                (repository[0], user[0], normalized_role, owner_user_id, now, now),
            )
            from ..access_control.bootstrap import ensure_repository_security
            ensure_repository_security(conn, repository[0], now)
        conn.execute(
            "UPDATE DATASET_INVITATIONS SET STATUS='accepted', UPDATED_AT=? WHERE TABLE_ID=? AND EMAIL=? COLLATE NOCASE",
            (now, table_id, normalized_email),
        )
        _workspace_event(conn, table_id, "MEMBER_UPDATED", owner_user_id, {"user_id": user[0], "email": normalized_email, "role": normalized_role})
        conn.commit()
        return {
            "user_id": user[0], "email": user[1], "display_name": user[2],
            "role": normalized_role, "added_at": now, "status": "active",
        }
    finally:
        conn.close()


def remove_dataset_member(
    table_id: str, owner_user_id: str, email: str
) -> dict[str, Any]:
    normalized_email = email.strip().lower()
    conn = _get_connection()
    try:
        owner = conn.execute(
            "SELECT OWNER_USER_ID FROM DATASET_REGISTRY WHERE TABLE_ID=?", (table_id,)
        ).fetchone()
        if not owner or owner[0] != owner_user_id:
            raise PermissionError("Only the dataset owner can manage access")
        user = conn.execute(
            "SELECT USER_ID FROM APP_USERS WHERE EMAIL=? COLLATE NOCASE", (normalized_email,)
        ).fetchone()
        if user and user[0] == owner_user_id:
            raise ValueError("The repository owner cannot be removed")
        if user:
            conn.execute(
                "DELETE FROM DATASET_MEMBERS WHERE TABLE_ID=? AND USER_ID=?",
                (table_id, user[0]),
            )
            repository = conn.execute(
                "SELECT REPOSITORY_ID FROM WORKBOOK_REPOSITORIES WHERE TABLE_ID=?",
                (table_id,),
            ).fetchone()
            if repository:
                conn.execute(
                    "DELETE FROM REPOSITORY_MEMBERS WHERE REPOSITORY_ID=? AND USER_ID=? AND ROLE!='owner'",
                    (repository[0], user[0]),
                )
                organization = conn.execute(
                    "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?",
                    (repository[0],),
                ).fetchone()
                if organization and organization[0]:
                    conn.execute(
                        "DELETE FROM SECURITY_USER_ROLE_ASSIGNMENTS WHERE ORGANIZATION_ID=? AND USER_ID=? AND SCOPE_TYPE='REPOSITORY' AND SCOPE_ID=?",
                        (organization[0], user[0], repository[0]),
                    )
                    from ..access_control.bootstrap import bump_authorization_revision
                    bump_authorization_revision(conn, _utcnow())
        conn.execute(
            "UPDATE DATASET_INVITATIONS SET STATUS='revoked', UPDATED_AT=? WHERE TABLE_ID=? AND EMAIL=? COLLATE NOCASE",
            (_utcnow(), table_id, normalized_email),
        )
        _workspace_event(conn, table_id, "ACCESS_REVOKED", owner_user_id, {"email": normalized_email})
        conn.commit()
        return {"email": normalized_email, "status": "revoked"}
    finally:
        conn.close()


def get_workspace_snapshot(table_id: str) -> dict[str, Any]:
    conn = _get_connection()
    try:
        invitations = conn.execute(
            """
            SELECT INVITATION_ID, EMAIL, ROLE, STATUS, CREATED_AT, UPDATED_AT
            FROM DATASET_INVITATIONS WHERE TABLE_ID=? AND STATUS='pending'
            ORDER BY CREATED_AT
            """,
            (table_id,),
        ).fetchall()
        revision = conn.execute(
            "SELECT COALESCE(MAX(REVISION), 0) FROM WORKSPACE_EVENTS WHERE TABLE_ID=?",
            (table_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "revision": revision,
        "members": get_dataset_members(table_id),
        "invitations": [{key.lower(): row[key] for key in row.keys()} for row in invitations],
        "active_users": list_active_presence(table_id),
    }


def list_datasets(user_id: str) -> list[dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT D.*, U.EMAIL AS OWNER_EMAIL, R.REPOSITORY_ID, R.REPOSITORY_NAME,
                   R.CATEGORY_ID, R.DEFAULT_BRANCH_ID, R.MAIN_PROTECTED,
                   R.REPOSITORY_SLUG, R.VISIBILITY,
                   COALESCE(M.ROLE, 'locked') AS REPOSITORY_ROLE,
                   CASE WHEN M.ROLE IS NULL THEN 'denied' ELSE M.ROLE END AS ACCESS_LEVEL,
                   CASE WHEN M.ROLE IS NULL THEN 0 ELSE 1 END AS CAN_VIEW,
                   CASE WHEN M.ROLE IN ('owner','editor') THEN 1 ELSE 0 END AS CAN_EDIT,
                   C.NAME AS CATEGORY_NAME,
                   (SELECT COUNT(DISTINCT BATCH_ID) FROM AUDIT_COMMITS A
                    WHERE A.TABLE_ID=D.TABLE_ID) AS COMMIT_COUNT,
                   (SELECT COUNT(*) FROM BRANCHES B
                    WHERE B.REPOSITORY_ID=R.REPOSITORY_ID AND B.STATUS='ACTIVE') AS BRANCH_COUNT,
                   (SELECT B2.BRANCH_NAME FROM BRANCHES B2
                    WHERE B2.REPOSITORY_ID=R.REPOSITORY_ID AND B2.CREATED_BY=?
                      AND B2.BRANCH_TYPE='USER' AND B2.STATUS='ACTIVE' LIMIT 1) AS MY_BRANCH
            FROM DATASET_REGISTRY D
            JOIN APP_USERS U ON U.USER_ID=D.OWNER_USER_ID
            JOIN WORKBOOK_REPOSITORIES R ON R.TABLE_ID=D.TABLE_ID AND R.STATUS='ACTIVE'
            LEFT JOIN REPOSITORY_MEMBERS M ON M.REPOSITORY_ID=R.REPOSITORY_ID AND M.USER_ID=?
            JOIN CATEGORIES C ON C.CATEGORY_ID=R.CATEGORY_ID
            ORDER BY D.UPDATED_AT DESC
            """,
            (user_id, user_id),
        ).fetchall()
        return [{key.lower(): row[key] for key in row.keys()} for row in rows]
    finally:
        conn.close()


def get_table_page(table_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    table_id = SafeIdentifier(table_id)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    conn = _get_connection()
    try:
        columns = [row["name"] for row in conn.execute(f'PRAGMA table_info("{table_id}")')]
        if not columns:
            raise ValueError(f"Table {table_id} does not exist")
        total = conn.execute(f'SELECT COUNT(*) FROM "{table_id}"').fetchone()[0]
        rows = conn.execute(
            f'SELECT * FROM "{table_id}" ORDER BY ROW_ID LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()
        version_row = conn.execute(
            "SELECT CURRENT_VERSION, UPDATED_AT FROM DATASET_REGISTRY WHERE TABLE_ID=?",
            (table_id,),
        ).fetchone()
        return {
            "table_id": table_id,
            "columns": columns,
            "rows": [[row[column] for column in columns] for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "version": version_row[0] if version_row else 0,
            "updated_at": version_row[1] if version_row else None,
        }
    finally:
        conn.close()


