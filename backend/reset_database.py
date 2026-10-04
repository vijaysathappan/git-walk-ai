"""Reset Git Walk's configured SQLite database to an empty product schema."""

import argparse
import sqlite3
from pathlib import Path

from app import database


def reset_database(confirm: bool = False) -> Path:
    if not confirm:
        raise RuntimeError("Refusing to reset without --yes")
    target = database.DB_PATH.resolve()
    if target.name != "queue_board.db":
        raise RuntimeError(f"Refusing to reset non-canonical database: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        for (name,) in triggers:
            connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (name,) in tables:
            connection.execute(f'DROP TABLE IF EXISTS "{name}"')
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)
    database.initialize_product_schema()
    return target


def database_summary() -> dict[str, int]:
    connection = sqlite3.connect(database.DB_PATH)
    try:
        return {
            "users": connection.execute("SELECT COUNT(*) FROM APP_USERS").fetchone()[0],
            "repositories": connection.execute("SELECT COUNT(*) FROM WORKBOOK_REPOSITORIES").fetchone()[0],
            "branches": connection.execute("SELECT COUNT(*) FROM BRANCHES").fetchone()[0],
            "commits": connection.execute("SELECT COUNT(*) FROM COMMITS").fetchone()[0],
            "memberships": connection.execute("SELECT COUNT(*) FROM REPOSITORY_MEMBERS").fetchone()[0],
        }
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="confirm destructive reset")
    arguments = parser.parse_args()
    print(f"Reset complete: {reset_database(arguments.yes)}")
    print(f"Empty schema verified: {database_summary()}")
