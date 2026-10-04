"""Tests for reconcile_orphaned_upload_tables() — the startup remediation
for the non-atomic upload-provisioning sequence (create_sqlite_table_from_df,
register_dataset, store_initial_formula_metadata each commit on their own
connection, so a crash between them can leave a SQLite table with no
DATASET_REGISTRY row)."""

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from app import database


class UploadReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "reconcile.db"
        database.initialize_product_schema()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_orphaned_table_from_a_crashed_upload_is_dropped(self):
        """Simulates the exact crash the audit flagged: the physical table
        gets created and committed, but the process dies before
        register_dataset() ever runs — leaving a table with no registry
        row."""
        database.create_sqlite_table_from_df(
            "QUEUE_BOARD_ORPHAN1", pd.DataFrame([{"A": 1}])
        )
        conn = database._get_connection()
        try:
            self.assertIsNotNone(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='QUEUE_BOARD_ORPHAN1'"
                ).fetchone()
            )
        finally:
            conn.close()

        dropped = database.reconcile_orphaned_upload_tables()

        self.assertIn("QUEUE_BOARD_ORPHAN1", dropped)
        conn = database._get_connection()
        try:
            self.assertIsNone(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='QUEUE_BOARD_ORPHAN1'"
                ).fetchone()
            )
        finally:
            conn.close()

    def test_a_fully_registered_table_is_left_alone(self):
        user = database.get_or_create_user("reconcile-owner@example.com")
        database.create_sqlite_table_from_df(
            "QUEUE_BOARD_REGISTERED1", pd.DataFrame([{"A": 1}])
        )
        database.register_dataset(
            "QUEUE_BOARD_REGISTERED1", user["user_id"], "sheet.xlsx", 1, 1
        )

        dropped = database.reconcile_orphaned_upload_tables()

        self.assertNotIn("QUEUE_BOARD_REGISTERED1", dropped)
        conn = database._get_connection()
        try:
            self.assertIsNotNone(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='QUEUE_BOARD_REGISTERED1'"
                ).fetchone()
            )
        finally:
            conn.close()

    def test_reconciliation_is_idempotent(self):
        database.create_sqlite_table_from_df(
            "QUEUE_BOARD_ORPHAN2", pd.DataFrame([{"A": 1}])
        )
        first = database.reconcile_orphaned_upload_tables()
        second = database.reconcile_orphaned_upload_tables()

        self.assertIn("QUEUE_BOARD_ORPHAN2", first)
        self.assertEqual([], second)


if __name__ == "__main__":
    unittest.main()
