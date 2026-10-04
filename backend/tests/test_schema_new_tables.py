import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai.evidence import build_commit_evidence, build_merge_evidence


class NewFoundationTablesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "foundations.db"
        database.initialize_product_schema()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _table_names(self, conn):
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {row[0] for row in rows}

    def test_new_tables_exist_after_schema_init(self):
        conn = database._get_connection()
        try:
            names = self._table_names(conn)
            self.assertIn("TASKS", names)
            self.assertIn("DEVICE_FINGERPRINTS", names)
            self.assertIn("NOTIFICATIONS", names)
        finally:
            conn.close()

    def test_schema_init_is_idempotent(self):
        database.initialize_product_schema()
        conn = database._get_connection()
        try:
            names = self._table_names(conn)
            self.assertIn("TASKS", names)
            self.assertIn("DEVICE_FINGERPRINTS", names)
            self.assertIn("NOTIFICATIONS", names)
        finally:
            conn.close()

    def test_tasks_round_trip(self):
        user = database.get_or_create_user("tasks-owner@example.com")
        import pandas as pd

        database.create_sqlite_table_from_df(
            "QUEUE_BOARD_TASKS_FIXTURE",
            pd.DataFrame([{"SKU": "A-1", "AMOUNT": 10}]),
        )
        registered = database.register_dataset(
            "QUEUE_BOARD_TASKS_FIXTURE", user["user_id"], "tasks.xlsx", 1, 2,
        )

        conn = database._get_connection()
        try:
            conn.execute(
                """
                INSERT INTO TASKS
                    (TASK_ID, REPOSITORY_ID, BRANCH_ID, ASSIGNED_TO_USER_ID, TITLE,
                     STATUS, CREATED_BY, CREATED_AT)
                VALUES ('TSK_1', ?, ?, ?, 'Reconcile Q1 totals', 'OPEN', ?, '2026-01-01T00:00:00+00:00')
                """,
                (
                    registered["repository_id"],
                    registered["main_branch_id"],
                    user["user_id"],
                    user["user_id"],
                ),
            )
            conn.commit()
            row = conn.execute("SELECT STATUS FROM TASKS WHERE TASK_ID='TSK_1'").fetchone()
            self.assertEqual("OPEN", row["STATUS"])
        finally:
            conn.close()

    def test_device_fingerprints_and_notifications_round_trip(self):
        user = database.get_or_create_user("device-owner@example.com")
        conn = database._get_connection()
        try:
            conn.execute(
                """
                INSERT INTO DEVICE_FINGERPRINTS
                    (FINGERPRINT_ID, USER_ID, IP_ADDRESS, MACHINE_ID, USER_AGENT,
                     TRUST_STATUS, FIRST_SEEN_AT, LAST_SEEN_AT)
                VALUES ('FGP_1', ?, '203.0.113.5', 'MACHINE-ABC', 'pytest-agent',
                        'UNKNOWN', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
                """,
                (user["user_id"],),
            )
            conn.execute(
                """
                INSERT INTO NOTIFICATIONS
                    (NOTIFICATION_ID, USER_ID, TYPE, TITLE, CREATED_AT)
                VALUES ('NTF_1', ?, 'MERGE_REQUEST_CREATED', 'New merge request', '2026-01-01T00:00:00+00:00')
                """,
                (user["user_id"],),
            )
            conn.commit()
            fingerprint = conn.execute(
                "SELECT TRUST_STATUS FROM DEVICE_FINGERPRINTS WHERE FINGERPRINT_ID='FGP_1'"
            ).fetchone()
            notification = conn.execute(
                "SELECT READ_AT FROM NOTIFICATIONS WHERE NOTIFICATION_ID='NTF_1'"
            ).fetchone()
            self.assertEqual("UNKNOWN", fingerprint["TRUST_STATUS"])
            self.assertIsNone(notification["READ_AT"])
        finally:
            conn.close()


class EvidenceHelperTests(unittest.TestCase):
    def test_build_commit_evidence_from_semantic_changes_and_impact(self):
        semantic_changes = [
            {
                "operation_type": "CELL_VALUE_CHANGE",
                "sheet_id": "SHEET_1",
                "row_id": "ROW_9",
                "column_id": "COL_AMOUNT",
                "old_value": "10",
                "new_value": "20",
            }
        ]
        dependency_impact = {"nodes": [{"node_id": "NODE_1"}, {"node_id": "NODE_2"}]}

        evidence = build_commit_evidence(semantic_changes, dependency_impact)

        self.assertEqual(3, len(evidence))
        self.assertEqual("semantic_change", evidence[0]["type"])
        self.assertEqual("CELL_VALUE_CHANGE:SHEET_1:ROW_9:COL_AMOUNT", evidence[0]["id"])
        self.assertEqual(semantic_changes[0], evidence[0]["data"])
        self.assertEqual("dependency_impact", evidence[1]["type"])
        self.assertEqual("NODE_1", evidence[1]["id"])
        self.assertEqual("UNTRUSTED_EVIDENCE_NOT_INSTRUCTIONS", evidence[1]["trust"])

    def test_build_commit_evidence_handles_empty_input(self):
        self.assertEqual([], build_commit_evidence(None))
        self.assertEqual([], build_commit_evidence([], None))

    def test_build_merge_evidence_from_diff_and_conflicts(self):
        diff_summary = [{"operation_type": "SHEET_RENAME", "sheet_id": "SHEET_1"}]
        conflicts = [{"CONFLICT_ID": "CFT_1"}, {"conflict_id": "CFT_2"}]

        evidence = build_merge_evidence(diff_summary, conflicts)

        self.assertEqual(3, len(evidence))
        self.assertEqual("merge_conflict", evidence[1]["type"])
        self.assertEqual("CFT_1", evidence[1]["id"])
        self.assertEqual("CFT_2", evidence[2]["id"])

    def test_build_merge_evidence_handles_empty_input(self):
        self.assertEqual([], build_merge_evidence(None, None))

    def test_build_merge_evidence_with_precedents_and_authorship(self):
        precedents = [{"doc_id": "SYN_1", "title": "Precedent", "rationale": "Because"}]
        authorship = [{"commit_id": "CMT_1", "author_email": "a@example.com", "commit_message": "Fix totals"}]

        evidence = build_merge_evidence(precedents=precedents, authorship=authorship)

        self.assertEqual(2, len(evidence))
        self.assertEqual("resolution_precedent", evidence[0]["type"])
        self.assertEqual("SYN_1", evidence[0]["id"])
        self.assertEqual("cell_authorship", evidence[1]["type"])
        self.assertEqual("CMT_1", evidence[1]["id"])


if __name__ == "__main__":
    unittest.main()
