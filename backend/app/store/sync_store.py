"""Cell/table sync operations: single-cell reads/updates, bulk range edits,
whole-workbook commit application, snapshot retrieval, and rollback."""

import json
import re
import sqlite3
import uuid
from typing import Any

from ..excel.identity import semantic_snapshot
from .identifiers import SafeIdentifier, validate_identifier
from .schema import VersionConflictError, _get_connection, _utcnow


def read_cell(table_name: str, column_name: str, row_id: int) -> Any:
    """Read one cell from the canonical backend database."""
    table_name = SafeIdentifier(table_name)
    column_name = SafeIdentifier(column_name)

    sql = f'SELECT "{column_name}" FROM "{table_name}" WHERE ROW_ID = ?;'
    conn = _get_connection()
    try:
        row = conn.execute(sql, (row_id,)).fetchone()
        if row is None:
            raise ValueError(f"No row with ROW_ID={row_id} in table {table_name}")
        return row[0]
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Cell read failed: {exc}") from exc
    finally:
        conn.close()


def _values_match(requested: Any, persisted: Any) -> bool:
    """Compare values after SQLite column affinity has been applied."""
    if requested is None or persisted is None:
        return requested is None and persisted is None
    return str(requested) == str(persisted)


def update_cell(
    table_name: str, column_name: str, row_id: int, new_value: Any
) -> dict[str, Any]:
    """
    Execute a parameterized UPDATE on a single cell.

    Uses quoted identifiers for table/column but parameterized binding for
    values — safe against SQL injection.
    """
    table_name = SafeIdentifier(table_name)
    column_name = SafeIdentifier(column_name)

    select_sql = f'SELECT "{column_name}" FROM "{table_name}" WHERE ROW_ID = ?;'
    update_sql = f'UPDATE "{table_name}" SET "{column_name}" = ? WHERE ROW_ID = ?;'

    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        previous_row = conn.execute(select_sql, (row_id,)).fetchone()
        if previous_row is None:
            raise ValueError(
                f"No row with ROW_ID={row_id} in table {table_name}"
            )

        previous_value = previous_row[0]
        cursor = conn.execute(update_sql, (new_value, row_id))
        if cursor.rowcount == 0:
            raise ValueError(
                f"No row with ROW_ID={row_id} in table {table_name}"
            )
        conn.commit()

        persisted_value = conn.execute(select_sql, (row_id,)).fetchone()[0]
        if not _values_match(new_value, persisted_value):
            raise RuntimeError(
                "SQLite readback did not match the requested value: "
                f"requested={new_value!r}, persisted={persisted_value!r}"
            )

        # Make committed WAL pages promptly visible to external SQLite tools.
        conn.execute("PRAGMA wal_checkpoint(PASSIVE);").fetchone()
        return {
            "previous_value": previous_value,
            "persisted_value": persisted_value,
            "changed": not _values_match(previous_value, persisted_value),
        }
    except ValueError:
        conn.rollback()
        raise
    except RuntimeError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(f"Cell update failed: {exc}") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Authentication, audit, and product-facing data access
# ---------------------------------------------------------------------------

def _snapshot_from_connection(
    conn: sqlite3.Connection, table_id: SafeIdentifier, max_rows: int = 100_000
) -> dict[str, Any]:
    # Self-validating: does not rely on the caller having already checked
    # table_id, since this is called from several places (schema
    # migration, dataset_store, and get_table_snapshot below).
    table_id = SafeIdentifier(table_id)
    column_rows = conn.execute(f'PRAGMA table_info("{table_id}")').fetchall()
    columns = [row["name"] for row in column_rows]
    if not columns:
        raise ValueError(f"Table {table_id} does not exist")
    total = conn.execute(f'SELECT COUNT(*) FROM "{table_id}"').fetchone()[0]
    if total > max_rows:
        raise ValueError(
            f"Dataset has {total} rows; Excel pull is limited to {max_rows} rows"
        )
    rows = conn.execute(f'SELECT * FROM "{table_id}" ORDER BY ROW_ID').fetchall()
    return {
        "table_id": table_id,
        "columns": columns,
        "column_types": [row["type"] or "TEXT" for row in column_rows],
        "rows": [[row[column] for column in columns] for row in rows],
        "total": total,
    }


def _store_dataset_version(
    conn: sqlite3.Connection,
    table_id: str,
    version: int,
    batch_id: str | None,
    commit_message: str,
    user_id: str,
    created_at: str,
) -> None:
    exists = conn.execute(
        "SELECT 1 FROM DATASET_VERSIONS WHERE TABLE_ID=? AND VERSION=?",
        (table_id, version),
    ).fetchone()
    if exists:
        return
    snapshot = _snapshot_from_connection(conn, table_id)
    conn.execute(
        """
        INSERT OR IGNORE INTO DATASET_VERSIONS
            (TABLE_ID, VERSION, BATCH_ID, SNAPSHOT_JSON, COMMIT_MESSAGE,
             USER_ID, CREATED_AT)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            table_id, version, batch_id,
            json.dumps(snapshot, default=str, separators=(",", ":")),
            commit_message, user_id, created_at,
        ),
    )


def get_table_snapshot(
    table_id: str, version: int | None = None, max_rows: int = 100_000
) -> dict[str, Any]:
    """Return a complete ordered dataset snapshot for Excel clone/pull operations."""
    table_id = SafeIdentifier(table_id)
    conn = _get_connection()
    try:
        registry = conn.execute(
            "SELECT CURRENT_VERSION, UPDATED_AT FROM DATASET_REGISTRY WHERE TABLE_ID=?",
            (table_id,),
        ).fetchone()
        if not registry:
            raise ValueError(f"Dataset {table_id} is not registered")
        current_version = int(registry[0])
        requested_version = current_version if version is None else version
        if requested_version == current_version:
            snapshot = _snapshot_from_connection(conn, table_id, max_rows)
        else:
            stored = conn.execute(
                "SELECT SNAPSHOT_JSON FROM DATASET_VERSIONS WHERE TABLE_ID=? AND VERSION=?",
                (table_id, requested_version),
            ).fetchone()
            if not stored:
                raise ValueError(f"Version {requested_version} is not available")
            snapshot = json.loads(stored[0])
            if snapshot["total"] > max_rows:
                raise ValueError(
                    f"Dataset has {snapshot['total']} rows; Excel pull is limited to {max_rows} rows"
                )
        snapshot["version"] = requested_version
        snapshot["current_version"] = current_version
        snapshot["updated_at"] = registry[1]
        if requested_version == current_version:
            try:
                semantic = semantic_snapshot(conn, table_id)
                snapshot["semantic"] = semantic
                snapshot["head_commit_id"] = semantic["head_commit_id"]
                if semantic.get("sheets"):
                    primary = semantic["sheets"][0]
                    snapshot["sheet_id"] = primary["sheet_id"]
                    snapshot["semantic_columns"] = primary["columns"]
                    snapshot["row_identities"] = [
                        {
                            "row_id": row["row_id"],
                            "physical_row_id": row.get("physical_row_id"),
                            "position": row["position"],
                        }
                        for row in primary["rows"]
                    ]
            except ValueError:
                pass
        return snapshot
    finally:
        conn.close()


def _risk_score(changes: list[dict[str, Any]]) -> int:
    sensitive_markers = {"SALARY", "AMOUNT", "PRICE", "STATUS", "EMAIL", "PHONE", "ID"}
    rows = {int(change["row_id"]) for change in changes}
    sensitive = sum(
        1
        for change in changes
        if any(marker in change["column_name"].upper() for marker in sensitive_markers)
    )
    return min(100, len(changes) * 2 + len(rows) * 2 + sensitive * 5)


def apply_bulk_updates(
    table_id: str,
    changes: list[dict[str, Any]],
    user_id: str,
    user_email: str,
    source: str = "excel",
    commit_message: str | None = None,
) -> dict[str, Any]:
    """Atomically apply a range edit and record an immutable change set."""
    table_id = SafeIdentifier(table_id)
    if not changes:
        raise ValueError("At least one change is required")
    if len(changes) > 5000:
        raise ValueError("A single sync batch cannot exceed 5000 cells")

    normalized = []
    for change in changes:
        column = str(change["column_name"]).strip().upper()
        row_id = int(change["row_id"])
        if not validate_identifier(column) or row_id < 1:
            raise ValueError("Invalid row or column in bulk change")
        normalized.append({"row_id": row_id, "column_name": column, "new_value": change.get("new_value")})

    conn = _get_connection()
    batch_id = f"CHG_{uuid.uuid4().hex[:12].upper()}"
    now = _utcnow()
    risk = _risk_score(normalized)
    try:
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"].upper() for row in conn.execute(f'PRAGMA table_info("{table_id}")')}
        missing = {change["column_name"] for change in normalized} - columns
        if missing:
            raise ValueError(f"Columns not found: {', '.join(sorted(missing))}")

        registry = conn.execute(
            "SELECT CURRENT_VERSION FROM DATASET_REGISTRY WHERE TABLE_ID=?",
            (table_id,),
        ).fetchone()
        if not registry:
            raise ValueError(f"Dataset {table_id} is not registered")
        version = int(registry[0]) + 1
        changed_count = 0
        results = []

        for change in normalized:
            column = change["column_name"]
            row_id = change["row_id"]
            select_sql = f'SELECT "{column}" FROM "{table_id}" WHERE ROW_ID=?'
            row = conn.execute(select_sql, (row_id,)).fetchone()
            if row is None:
                raise ValueError(f"No row with ROW_ID={row_id} in {table_id}")
            old_value = row[0]
            new_value = change["new_value"]
            changed = not _values_match(old_value, new_value)
            if changed:
                conn.execute(
                    f'UPDATE "{table_id}" SET "{column}"=? WHERE ROW_ID=?',
                    (new_value, row_id),
                )
                changed_count += 1

            persisted = conn.execute(select_sql, (row_id,)).fetchone()[0]
            if not _values_match(new_value, persisted):
                raise RuntimeError(f"Readback mismatch at ROW_ID={row_id}, {column}")
            conn.execute(
                """
                INSERT INTO AUDIT_COMMITS
                    (AUDIT_ID, BATCH_ID, VERSION, TABLE_ID, ROW_ID, COLUMN_NAME,
                     OLD_VALUE, NEW_VALUE, USER_ID, USER_EMAIL, SOURCE, STATUS,
                     COMMIT_MESSAGE, RISK_SCORE, CREATED_AT)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"AUD_{uuid.uuid4().hex[:16].upper()}", batch_id, version,
                    table_id, row_id, column, json.dumps(old_value), json.dumps(persisted),
                    user_id, user_email, source, "UPDATED" if changed else "NO_CHANGE",
                    commit_message or f"Updated {len(normalized)} cell(s) from {source}",
                    risk, now,
                ),
            )
            results.append({
                "row_id": row_id,
                "column_name": column,
                "persisted_value": persisted,
                "changed": changed,
            })

        conn.execute(
            """
            UPDATE DATASET_REGISTRY
            SET CURRENT_VERSION=?, UPDATED_AT=?
            WHERE TABLE_ID=?
            """,
            (version, now, table_id),
        )
        _store_dataset_version(
            conn,
            table_id,
            version,
            batch_id,
            commit_message or f"Updated {len(normalized)} cell(s) from {source}",
            user_id,
            now,
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        return {
            "status": "SUCCESS",
            "batch_id": batch_id,
            "version": version,
            "requested_count": len(normalized),
            "changed_count": changed_count,
            "risk_score": risk,
            "timestamp": now,
            "results": results,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_workbook_commit(
    table_id: str,
    base_version: int,
    updates: list[dict[str, Any]],
    insert_rows: list[dict[str, Any]],
    delete_row_ids: list[int],
    new_columns: list[str],
    delete_columns: list[str],
    user_id: str,
    user_email: str,
    source: str,
    commit_message: str,
) -> dict[str, Any]:
    """Apply a Git-style workbook diff as one version-checked transaction."""
    table_id = SafeIdentifier(table_id)

    normalized_updates: dict[tuple[int, str], dict[str, Any]] = {}
    for change in updates:
        row_id = int(change["row_id"])
        column = str(change["column_name"]).strip().upper()
        if row_id < 1 or not validate_identifier(column) or column == "ROW_ID":
            raise ValueError("Invalid row or column in workbook update")
        normalized_updates[(row_id, column)] = {
            "row_id": row_id,
            "column_name": column,
            "new_value": change.get("new_value"),
        }
    updates = list(normalized_updates.values())

    def normalize_columns(values: list[str]) -> list[str]:
        result = []
        for raw in values:
            column = str(raw).strip().upper()
            if not validate_identifier(column) or column == "ROW_ID":
                raise ValueError(f"Invalid workbook column: {raw}")
            if column not in result:
                result.append(column)
        return result

    new_columns = normalize_columns(new_columns)
    delete_columns = normalize_columns(delete_columns)
    if set(new_columns) & set(delete_columns):
        raise ValueError("A column cannot be added and deleted in the same commit")

    normalized_inserts = []
    for raw_row in insert_rows:
        row = {}
        for raw_column, value in raw_row.items():
            column = str(raw_column).strip().upper()
            if not validate_identifier(column) or column == "ROW_ID":
                raise ValueError(f"Invalid inserted-row column: {raw_column}")
            row[column] = value
        normalized_inserts.append(row)

    delete_row_ids = list(dict.fromkeys(int(row_id) for row_id in delete_row_ids))
    if any(row_id < 1 for row_id in delete_row_ids):
        raise ValueError("Invalid ROW_ID in deleted rows")

    operation_size = (
        len(updates) + sum(len(row) for row in normalized_inserts)
        + len(delete_row_ids) + len(new_columns) + len(delete_columns)
    )
    if operation_size == 0:
        raise ValueError("There are no workbook changes to commit")
    if operation_size > 10_000:
        raise ValueError("A workbook commit cannot exceed 10,000 operations")

    conn = _get_connection()
    batch_id = f"CHG_{uuid.uuid4().hex[:12].upper()}"
    now = _utcnow()
    version = base_version + 1
    risk_inputs = updates + [
        {"row_id": 0, "column_name": column}
        for column in new_columns + delete_columns
    ] + [
        {"row_id": 0, "column_name": column}
        for row in normalized_inserts for column in row
    ] + [
        {"row_id": row_id, "column_name": "ROW_ID"}
        for row_id in delete_row_ids
    ]
    risk = _risk_score(risk_inputs)

    def audit(
        row_id: int, column: str, old_value: Any, new_value: Any, status: str
    ) -> None:
        conn.execute(
            """
            INSERT INTO AUDIT_COMMITS
                (AUDIT_ID, BATCH_ID, VERSION, TABLE_ID, ROW_ID, COLUMN_NAME,
                 OLD_VALUE, NEW_VALUE, USER_ID, USER_EMAIL, SOURCE, STATUS,
                 COMMIT_MESSAGE, RISK_SCORE, CREATED_AT)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"AUD_{uuid.uuid4().hex[:16].upper()}", batch_id, version,
                table_id, row_id, column,
                json.dumps(old_value, default=str), json.dumps(new_value, default=str),
                user_id, user_email, source, status, commit_message, risk, now,
            ),
        )

    try:
        conn.execute("BEGIN IMMEDIATE")
        registry = conn.execute(
            "SELECT CURRENT_VERSION FROM DATASET_REGISTRY WHERE TABLE_ID=?",
            (table_id,),
        ).fetchone()
        if not registry:
            raise ValueError(f"Dataset {table_id} is not registered")
        current_version = int(registry[0])
        if current_version != base_version:
            raise VersionConflictError(current_version)

        existing = {
            row["name"].upper(): row
            for row in conn.execute(f'PRAGMA table_info("{table_id}")')
        }
        already_present = set(new_columns) & set(existing)
        missing_deleted = set(delete_columns) - set(existing)
        if already_present:
            raise ValueError(f"Columns already exist: {', '.join(sorted(already_present))}")
        if missing_deleted:
            raise ValueError(f"Columns do not exist: {', '.join(sorted(missing_deleted))}")

        future_columns = (set(existing) | set(new_columns)) - set(delete_columns)
        referenced = {change["column_name"] for change in updates}
        referenced.update(column for row in normalized_inserts for column in row)
        missing_references = referenced - future_columns
        if missing_references:
            raise ValueError(
                f"Commit references unavailable columns: {', '.join(sorted(missing_references))}"
            )

        for column in new_columns:
            conn.execute(f'ALTER TABLE "{table_id}" ADD COLUMN "{column}" TEXT')
            audit(0, column, None, "TEXT", "COLUMN_ADDED")

        changed_updates = 0
        for change in updates:
            column = change["column_name"]
            row_id = change["row_id"]
            row = conn.execute(
                f'SELECT "{column}" FROM "{table_id}" WHERE ROW_ID=?',
                (row_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"No row with ROW_ID={row_id} in {table_id}")
            old_value = row[0]
            new_value = change.get("new_value")
            changed = not _values_match(old_value, new_value)
            if changed:
                conn.execute(
                    f'UPDATE "{table_id}" SET "{column}"=? WHERE ROW_ID=?',
                    (new_value, row_id),
                )
                changed_updates += 1
            persisted = conn.execute(
                f'SELECT "{column}" FROM "{table_id}" WHERE ROW_ID=?',
                (row_id,),
            ).fetchone()[0]
            if not _values_match(new_value, persisted):
                raise RuntimeError(f"Readback mismatch at ROW_ID={row_id}, {column}")
            audit(row_id, column, old_value, persisted, "UPDATED" if changed else "NO_CHANGE")

        inserted_ids = []
        for row in normalized_inserts:
            if row:
                columns = list(row)
                sql = (
                    f'INSERT INTO "{table_id}" ('
                    + ", ".join(f'"{column}"' for column in columns)
                    + ") VALUES ("
                    + ", ".join("?" for _ in columns)
                    + ")"
                )
                cursor = conn.execute(sql, tuple(row[column] for column in columns))
            else:
                cursor = conn.execute(f'INSERT INTO "{table_id}" DEFAULT VALUES')
                columns = []
            row_id = int(cursor.lastrowid)
            inserted_ids.append(row_id)
            if columns:
                persisted = conn.execute(
                    f'SELECT * FROM "{table_id}" WHERE ROW_ID=?', (row_id,)
                ).fetchone()
                for column in columns:
                    audit(row_id, column, None, persisted[column], "INSERTED")
            else:
                audit(row_id, "__ROW__", None, row_id, "INSERTED")

        deleted_count = 0
        for row_id in delete_row_ids:
            row = conn.execute(
                f'SELECT * FROM "{table_id}" WHERE ROW_ID=?', (row_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"No row with ROW_ID={row_id} in {table_id}")
            for column in row.keys():
                if column.upper() != "ROW_ID" and column.upper() not in delete_columns:
                    audit(row_id, column.upper(), row[column], None, "DELETED")
            conn.execute(f'DELETE FROM "{table_id}" WHERE ROW_ID=?', (row_id,))
            deleted_count += 1

        for column in delete_columns:
            values = conn.execute(
                f'SELECT ROW_ID, "{column}" FROM "{table_id}" ORDER BY ROW_ID'
            ).fetchall()
            old_values = {str(row[0]): row[1] for row in values}
            audit(0, column, old_values, None, "COLUMN_DELETED")
            conn.execute(f'ALTER TABLE "{table_id}" DROP COLUMN "{column}"')

        row_count = conn.execute(f'SELECT COUNT(*) FROM "{table_id}"').fetchone()[0]
        column_count = len(conn.execute(f'PRAGMA table_info("{table_id}")').fetchall()) - 1
        conn.execute(
            """
            UPDATE DATASET_REGISTRY
            SET CURRENT_VERSION=?, UPDATED_AT=?, ROW_COUNT=?, COLUMN_COUNT=?
            WHERE TABLE_ID=?
            """,
            (version, now, row_count, column_count, table_id),
        )
        _store_dataset_version(
            conn, table_id, version, batch_id, commit_message, user_id, now
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        return {
            "status": "SUCCESS",
            "batch_id": batch_id,
            "version": version,
            "base_version": base_version,
            "changed_updates": changed_updates,
            "inserted_count": len(inserted_ids),
            "inserted_row_ids": inserted_ids,
            "deleted_count": deleted_count,
            "columns_added": new_columns,
            "columns_deleted": delete_columns,
            "risk_score": risk,
            "timestamp": now,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_audit_history(table_id: str, limit: int = 100) -> list[dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM AUDIT_COMMITS
            WHERE TABLE_ID=? ORDER BY VERSION DESC, CREATED_AT DESC LIMIT ?
            """,
            (table_id, max(1, min(limit, 1000))),
        ).fetchall()
        history = []
        for row in rows:
            item = {key.lower(): row[key] for key in row.keys()}
            item["old_value"] = json.loads(item["old_value"]) if item["old_value"] is not None else None
            item["new_value"] = json.loads(item["new_value"]) if item["new_value"] is not None else None
            history.append(item)
        return history
    finally:
        conn.close()


def rollback_batch(
    table_id: str, batch_id: str, user_id: str, user_email: str
) -> dict[str, Any]:
    table_id = SafeIdentifier(table_id)
    conn = _get_connection()
    try:
        batch = conn.execute(
            "SELECT MIN(VERSION) AS VERSION FROM AUDIT_COMMITS WHERE TABLE_ID=? AND BATCH_ID=?",
            (table_id, batch_id),
        ).fetchone()
        if not batch or batch["VERSION"] is None:
            raise ValueError("Change set not found")
        target_version = max(0, int(batch["VERSION"]) - 1)
        stored = conn.execute(
            "SELECT SNAPSHOT_JSON FROM DATASET_VERSIONS WHERE TABLE_ID=? AND VERSION=?",
            (table_id, target_version),
        ).fetchone()
        if not stored:
            rows = conn.execute(
                """
                SELECT ROW_ID, COLUMN_NAME, OLD_VALUE FROM AUDIT_COMMITS
                WHERE TABLE_ID=? AND BATCH_ID=? ORDER BY CREATED_AT DESC
                """,
                (table_id, batch_id),
            ).fetchall()
            changes = [
                {
                    "row_id": row["ROW_ID"],
                    "column_name": row["COLUMN_NAME"],
                    "new_value": json.loads(row["OLD_VALUE"]) if row["OLD_VALUE"] is not None else None,
                }
                for row in rows
                if row["ROW_ID"] > 0 and validate_identifier(row["COLUMN_NAME"])
            ]
            if not changes:
                raise ValueError("This legacy change set cannot be restored automatically")
            conn.close()
            conn = None
            return apply_bulk_updates(
                table_id, changes, user_id, user_email, source="rollback",
                commit_message=f"Reverted change set {batch_id}",
            )

        snapshot = json.loads(stored[0])
        registry = conn.execute(
            "SELECT CURRENT_VERSION FROM DATASET_REGISTRY WHERE TABLE_ID=?",
            (table_id,),
        ).fetchone()
        new_version = int(registry[0]) + 1
        new_batch = f"CHG_{uuid.uuid4().hex[:12].upper()}"
        now = _utcnow()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f'DROP TABLE "{table_id}"')
        definitions = []
        column_types = snapshot.get("column_types") or ["TEXT"] * len(snapshot["columns"])
        for index, column in enumerate(snapshot["columns"]):
            if column.upper() == "ROW_ID":
                definitions.append('"ROW_ID" INTEGER PRIMARY KEY AUTOINCREMENT')
            else:
                data_type = re.sub(r"[^A-Z0-9_ ()]", "", str(column_types[index]).upper()) or "TEXT"
                definitions.append(f'"{column}" {data_type}')
        conn.execute(f'CREATE TABLE "{table_id}" ({", ".join(definitions)})')
        if snapshot["rows"]:
            columns_sql = ", ".join(f'"{column}"' for column in snapshot["columns"])
            placeholders = ", ".join("?" for _ in snapshot["columns"])
            conn.executemany(
                f'INSERT INTO "{table_id}" ({columns_sql}) VALUES ({placeholders})',
                snapshot["rows"],
            )
        conn.execute(
            """
            INSERT INTO AUDIT_COMMITS
                (AUDIT_ID, BATCH_ID, VERSION, TABLE_ID, ROW_ID, COLUMN_NAME,
                 OLD_VALUE, NEW_VALUE, USER_ID, USER_EMAIL, SOURCE, STATUS,
                 COMMIT_MESSAGE, RISK_SCORE, CREATED_AT)
            VALUES (?, ?, ?, ?, 0, '__DATASET__', ?, ?, ?, ?, 'rollback',
                    'SNAPSHOT_RESTORED', ?, 100, ?)
            """,
            (
                f"AUD_{uuid.uuid4().hex[:16].upper()}", new_batch, new_version,
                table_id, json.dumps({"version": int(registry[0])}),
                json.dumps({"version": target_version}), user_id, user_email,
                f"Reverted change set {batch_id} to version {target_version}", now,
            ),
        )
        conn.execute(
            """
            UPDATE DATASET_REGISTRY SET CURRENT_VERSION=?, UPDATED_AT=?,
                ROW_COUNT=?, COLUMN_COUNT=? WHERE TABLE_ID=?
            """,
            (new_version, now, snapshot["total"], len(snapshot["columns"]) - 1, table_id),
        )
        _store_dataset_version(
            conn, table_id, new_version, new_batch,
            f"Reverted change set {batch_id}", user_id, now,
        )
        conn.commit()
        return {
            "status": "SUCCESS", "batch_id": new_batch, "version": new_version,
            "restored_version": target_version, "timestamp": now,
        }
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


