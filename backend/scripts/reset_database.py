"""Reset the local Git Walk SQLite database to an empty product schema."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import database  # noqa: E402


def reset_database() -> tuple[int, int, int]:
    """Drop all persisted objects, recreate the schema, and return before/after counts."""
    connection = sqlite3.connect(str(database.DB_PATH), timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN EXCLUSIVE")
        objects = connection.execute(
            """
            SELECT type, name
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' AND type IN ('view', 'trigger', 'table')
            ORDER BY CASE type WHEN 'view' THEN 1 WHEN 'trigger' THEN 2 ELSE 3 END
            """
        ).fetchall()
        for object_type, name in objects:
            escaped_name = name.replace('"', '""')
            connection.execute(f'DROP {object_type.upper()} IF EXISTS "{escaped_name}"')
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    database.initialize_product_schema()

    verification = database._get_connection()
    try:
        tables = [
            row[0]
            for row in verification.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        populated = {
            table: verification.execute(
                f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
            ).fetchone()[0]
            for table in tables
        }
        populated = {table: count for table, count in populated.items() if count}
        expected_seeds = {"APP_USERS": 1, "CATEGORIES": 2}
        if populated != expected_seeds:
            raise RuntimeError(
                "Reset verification failed; only required schema seeds are allowed, "
                f"found: {populated}"
            )
        system_user = verification.execute(
            "SELECT USER_ID, EMAIL, ROLE FROM APP_USERS"
        ).fetchone()
        category_ids = {
            row[0] for row in verification.execute("SELECT CATEGORY_ID FROM CATEGORIES")
        }
        if tuple(system_user) != ("USR_SYSTEM", "system@local", "system") or category_ids != {
            "CAT_HOME", "CAT_UNSORTED"
        }:
            raise RuntimeError("Reset verification failed; unexpected seed data exists")
        return len(objects), len(tables), sum(expected_seeds.values())
    finally:
        verification.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm permanent deletion of all local application data.",
    )
    args = parser.parse_args()
    if not args.yes:
        parser.error("Pass --yes to confirm the destructive reset")

    removed, recreated, required_seeds = reset_database()
    print(f"Database: {database.DB_PATH}")
    print(f"Removed objects: {removed}")
    print(f"Recreated empty tables: {recreated}")
    print(f"Required schema seed rows: {required_seeds}")
    print("Verification: all user, repository, branch, commit, audit, and dataset data is empty")


if __name__ == "__main__":
    main()
