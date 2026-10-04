"""Phase 4: the macro_governance block in repository_insights() -- macro
population health (from the current extraction) plus run activity (across
all extractions ever), and the MACRO_RUN_BLOCKED audit event for a
non-runnable macro attempted directly against the API."""

import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from vba_fixture_builder import build_xlsm  # noqa: E402

from app import database  # noqa: E402
from app.macros import run_service  # noqa: E402
from app.repositories.governance_store import list_audit_events, repository_insights  # noqa: E402

RUNNABLE_MACRO = (
    "Public Sub Recalc()\n"
    "    Dim i As Long\n"
    "    For i = 2 To 4\n"
    "        Cells(i, 3).Value = Cells(i, 1).Value * Cells(i, 2).Value\n"
    "    Next i\n"
    "End Sub\n"
)
BLOCKED_MACRO = (
    'Public Sub Leak()\n'
    '    Dim fso As Object\n'
    '    Set fso = CreateObject("Scripting.FileSystemObject")\n'
    'End Sub\n'
)


class MacroGovernanceInsightsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "macro_governance.db"
        database.initialize_product_schema()

        self.owner = database.get_or_create_user(f"owner_{uuid.uuid4().hex[:8]}@example.com")
        self.table_id = "QUEUE_BOARD_MACROGOV"
        conn = database._get_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{self.table_id}"')
            conn.execute(f'''
                CREATE TABLE "{self.table_id}" (
                    ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    QTY INTEGER, PRICE INTEGER, TOTAL INTEGER
                )
            ''')
            conn.executemany(
                f'INSERT INTO "{self.table_id}" (QTY, PRICE, TOTAL) VALUES (?, ?, ?)',
                [(2, 10, None), (3, 5, None), (1, 4, None)],
            )
            conn.commit()
        finally:
            conn.close()
        database.register_dataset(self.table_id, self.owner["user_id"], "gov.xlsx", 3, 3)
        repository = database.get_repository(self.table_id, self.owner["user_id"])
        self.repository_id = repository["repository_id"]

        working_copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"], branch_mode="new")
        self.branch_id = working_copy["branch_id"]

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_governance_block_reflects_extraction_and_run_activity(self):
        xlsm_bytes = build_xlsm([["Qty", "Price", "Total"], [2, 10, None]], "Module1", RUNNABLE_MACRO + "\n" + BLOCKED_MACRO)
        run_service.register_source(self.table_id, "gov.xlsm", xlsm_bytes, self.owner["user_id"])
        run_service.extract(self.table_id, self.owner["user_id"])

        insights = repository_insights(self.repository_id)
        governance = insights["macro_governance"]
        self.assertTrue(governance["has_macros"])
        self.assertEqual(2, governance["total_macros"])
        self.assertEqual(1, governance["runnable"])
        self.assertEqual(1, governance["blocked_external"])
        self.assertEqual(0, governance["blocked_unsupported"])
        self.assertEqual(1, governance["sql_lane"])
        self.assertEqual(0, governance["runs_last_30d"])
        self.assertIsNone(governance["most_run_macro"])
        self.assertIsNone(governance["last_run_at"])

        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        recalc = next(m for m in listing["macros"] if m["proc_name"] == "Recalc")
        prepared = run_service.prepare_run(self.table_id, recalc["macro_id"], self.branch_id, self.owner["user_id"])
        result = run_service.confirm_run(self.table_id, prepared["run_id"], self.owner["user_id"])
        self.assertEqual("EXECUTED", result["status"])

        governance = repository_insights(self.repository_id)["macro_governance"]
        self.assertEqual(1, governance["runs_last_30d"])
        self.assertEqual("Recalc", governance["most_run_macro"]["proc_name"])
        self.assertEqual(1, governance["most_run_macro"]["run_count"])
        self.assertIsNotNone(governance["last_run_at"])

    def test_attempting_to_run_a_blocked_macro_directly_records_an_audit_event(self):
        xlsm_bytes = build_xlsm([["Qty", "Price", "Total"], [2, 10, None]], "Module1", BLOCKED_MACRO)
        run_service.register_source(self.table_id, "gov.xlsm", xlsm_bytes, self.owner["user_id"])
        run_service.extract(self.table_id, self.owner["user_id"])
        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        blocked = listing["macros"][0]
        self.assertFalse(blocked["runnable"])

        with self.assertRaises(PermissionError):
            run_service.prepare_run(self.table_id, blocked["macro_id"], self.branch_id, self.owner["user_id"])

        events = list_audit_events(repository_id=self.repository_id, event_type="MACRO_RUN_BLOCKED")
        self.assertEqual(1, len(events))
        self.assertEqual("FAILED", events[0]["status"])


if __name__ == "__main__":
    unittest.main()
