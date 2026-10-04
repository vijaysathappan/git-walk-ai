"""End-to-end general (interpreted) lane: prepare -> preview -> confirm ->
commit, through the exact same run_service/commit_semantic_delta path
test_macro_run_service.py exercises for the SQL lane -- but for a macro
that the SQL lane explicitly rejects (a fixed-row Cells() reference inside
the loop), so this proves the dispatch-by-EXECUTION_LANE wiring in
run_service actually reaches interpreted_execution, not just that the
interpreter works in isolation (already covered by
test_macro_interpreter.py)."""

import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from vba_fixture_builder import build_xlsm  # noqa: E402

from app import database  # noqa: E402
from app.macros import run_service  # noqa: E402

# Cells(2, 2) is a FIXED row reference -- the SQL fast lane only accepts
# same-row Cells(i, ...) reads, so this macro is classified INTERPRETED.
FIXED_ROW_MACRO = (
    "Public Sub Recalc()\n"
    "    Dim i As Long\n"
    "    For i = 2 To 4\n"
    "        Cells(i, 3).Value = Cells(i, 1).Value + Cells(2, 2).Value\n"
    "    Next i\n"
    "End Sub\n"
)


class MacroRunServiceInterpretedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "macro_run_interpreted.db"
        database.initialize_product_schema()

        self.owner = database.get_or_create_user(f"owner_{uuid.uuid4().hex[:8]}@example.com")
        self.editor = database.get_or_create_user(f"editor_{uuid.uuid4().hex[:8]}@example.com")
        self.viewer = database.get_or_create_user(f"viewer_{uuid.uuid4().hex[:8]}@example.com")

        self.table_id = "QUEUE_BOARD_MACRORUN_INTERP"
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
        database.register_dataset(self.table_id, self.owner["user_id"], "macro_run_interp.xlsx", 3, 3)
        database.add_dataset_member(self.table_id, self.owner["user_id"], self.editor["email"], "editor")
        database.add_dataset_member(self.table_id, self.owner["user_id"], self.viewer["email"], "viewer")
        repository = database.get_repository(self.table_id, self.owner["user_id"])
        self.repository_id = repository["repository_id"]

        working_copy = database.create_working_copy(self.table_id, self.editor["user_id"], self.editor["email"], branch_mode="new")
        self.branch_id = working_copy["branch_id"]
        self.branch_table_id = working_copy["table_id"]

        xlsm_bytes = build_xlsm([["Qty", "Price", "Total"], [2, 10, None]], "Module1", FIXED_ROW_MACRO)
        run_service.register_source(self.table_id, "calc.xlsm", xlsm_bytes, self.editor["user_id"])
        extraction = run_service.extract(self.table_id, self.editor["user_id"])
        self.assertEqual("COMPLETED", extraction["status"])
        listing = run_service.list_macros(self.table_id, self.editor["user_id"])
        self.macro = listing["macros"][0]
        self.assertTrue(self.macro["runnable"], self.macro["block_reasons"])
        self.assertEqual("INTERPRETED", self.macro["execution_lane"])
        self.macro_id = self.macro["macro_id"]

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _physical_totals(self):
        conn = database._get_connection()
        try:
            return [row["TOTAL"] for row in conn.execute(f'SELECT TOTAL FROM "{self.branch_table_id}" ORDER BY ROW_ID')]
        finally:
            conn.close()

    def test_prepare_computes_correct_preview_without_writing(self):
        result = run_service.prepare_run(self.table_id, self.macro_id, self.branch_id, self.editor["user_id"])
        self.assertEqual("PENDING_CONFIRMATION", result["status"])
        self.assertEqual(3, result["preview_change_count"])
        new_values = sorted(change["new_value"] for change in result["preview"])
        self.assertEqual([11, 12, 13], new_values)  # 1+10, 2+10, 3+10
        self.assertEqual([None, None, None], self._physical_totals())

    def test_confirm_writes_through_the_real_commit_pipeline(self):
        prepared = run_service.prepare_run(self.table_id, self.macro_id, self.branch_id, self.editor["user_id"])
        result = run_service.confirm_run(self.table_id, prepared["run_id"], self.editor["user_id"])
        self.assertEqual("EXECUTED", result["status"])
        self.assertIsNotNone(result["commit_id"])
        self.assertEqual([12, 13, 11], self._physical_totals())

        conn = database._get_connection()
        try:
            commit = conn.execute("SELECT * FROM COMMITS WHERE COMMIT_ID=?", (result["commit_id"],)).fetchone()
            self.assertEqual(self.editor["user_id"], commit["AUTHOR_USER_ID"])
            change_count = conn.execute("SELECT COUNT(*) FROM COMMIT_CHANGES WHERE COMMIT_ID=?", (result["commit_id"],)).fetchone()[0]
            self.assertEqual(3, change_count)
        finally:
            conn.close()

    def test_viewer_cannot_prepare_a_run(self):
        with self.assertRaises(PermissionError):
            run_service.prepare_run(self.table_id, self.macro_id, self.branch_id, self.viewer["user_id"])


if __name__ == "__main__":
    unittest.main()
