"""End-to-end SQL fast lane: prepare -> preview -> confirm -> commit,
through the exact same commit_semantic_delta every other Git Walk edit
uses, plus the safety rails around it (RBAC, branch-HEAD staleness,
expiry, main-branch rejection)."""

import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from vba_fixture_builder import build_xlsm  # noqa: E402

from app import database  # noqa: E402
from app.macros import run_service  # noqa: E402
from app.repositories.commit_store import BranchHeadChangedError, commit_semantic_delta  # noqa: E402

CALC_MACRO = (
    "Public Sub Recalc()\n"
    "    Dim i As Long\n"
    "    For i = 2 To 4\n"
    "        Cells(i, 3).Value = Cells(i, 1).Value * Cells(i, 2).Value\n"
    "    Next i\n"
    "End Sub\n"
)


class MacroRunServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "macro_run.db"
        database.initialize_product_schema()

        self.owner = database.get_or_create_user(f"owner_{uuid.uuid4().hex[:8]}@example.com")
        self.editor = database.get_or_create_user(f"editor_{uuid.uuid4().hex[:8]}@example.com")
        self.viewer = database.get_or_create_user(f"viewer_{uuid.uuid4().hex[:8]}@example.com")

        self.table_id = "QUEUE_BOARD_MACRORUN"
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
        database.register_dataset(self.table_id, self.owner["user_id"], "macro_run.xlsx", 3, 3)
        database.add_dataset_member(self.table_id, self.owner["user_id"], self.editor["email"], "editor")
        database.add_dataset_member(self.table_id, self.owner["user_id"], self.viewer["email"], "viewer")
        repository = database.get_repository(self.table_id, self.owner["user_id"])
        self.repository_id = repository["repository_id"]
        self.main_branch_id = repository["default_branch_id"]

        working_copy = database.create_working_copy(self.table_id, self.editor["user_id"], self.editor["email"], branch_mode="new")
        self.branch_id = working_copy["branch_id"]
        self.branch_table_id = working_copy["table_id"]

        xlsm_bytes = build_xlsm([["Qty", "Price", "Total"], [2, 10, None]], "Module1", CALC_MACRO)
        run_service.register_source(self.table_id, "calc.xlsm", xlsm_bytes, self.editor["user_id"])
        extraction = run_service.extract(self.table_id, self.editor["user_id"])
        self.assertEqual("COMPLETED", extraction["status"])
        listing = run_service.list_macros(self.table_id, self.editor["user_id"])
        self.assertEqual(1, listing["macros"][0] and len(listing["macros"]))
        self.macro = listing["macros"][0]
        self.assertTrue(self.macro["runnable"], self.macro["block_reasons"])
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
        self.assertEqual([4, 15, 20], new_values)  # 1*4, 3*5, 2*10
        # Nothing written yet.
        self.assertEqual([None, None, None], self._physical_totals())

    def test_confirm_writes_through_the_real_commit_pipeline(self):
        prepared = run_service.prepare_run(self.table_id, self.macro_id, self.branch_id, self.editor["user_id"])
        result = run_service.confirm_run(self.table_id, prepared["run_id"], self.editor["user_id"])
        self.assertEqual("EXECUTED", result["status"])
        self.assertIsNotNone(result["commit_id"])
        self.assertEqual([20, 15, 4], self._physical_totals())

        conn = database._get_connection()
        try:
            commit = conn.execute("SELECT * FROM COMMITS WHERE COMMIT_ID=?", (result["commit_id"],)).fetchone()
            self.assertIsNotNone(commit)
            self.assertEqual(self.editor["user_id"], commit["AUTHOR_USER_ID"])
            self.assertIn("Recalc", commit["MESSAGE"])
            change_count = conn.execute("SELECT COUNT(*) FROM COMMIT_CHANGES WHERE COMMIT_ID=?", (result["commit_id"],)).fetchone()[0]
            self.assertEqual(3, change_count)
        finally:
            conn.close()

    def test_stale_branch_head_is_rejected_not_silently_overwritten(self):
        prepared = run_service.prepare_run(self.table_id, self.macro_id, self.branch_id, self.editor["user_id"])
        # Someone else commits to the same branch in the meantime.
        conn = database._get_connection()
        try:
            branch = conn.execute("SELECT * FROM BRANCHES WHERE BRANCH_ID=?", (self.branch_id,)).fetchone()
            registry = conn.execute("SELECT CURRENT_VERSION FROM DATASET_REGISTRY WHERE TABLE_ID=?", (self.branch_table_id,)).fetchone()
        finally:
            conn.close()
        commit_semantic_delta(
            table_id=self.branch_table_id, repository_id=self.repository_id, branch_id=self.branch_id,
            expected_head_commit_id=branch["HEAD_COMMIT_ID"], base_version=int(registry["CURRENT_VERSION"]),
            changes=self._unrelated_change(),
            user_id=self.owner["user_id"], user_email=self.owner["email"], message="unrelated edit",
        )
        with self.assertRaises(BranchHeadChangedError):
            run_service.confirm_run(self.table_id, prepared["run_id"], self.editor["user_id"])

    def _unrelated_change(self):
        conn = database._get_connection()
        try:
            sheet = conn.execute("SELECT SHEET_ID FROM BRANCH_SHEETS WHERE BRANCH_ID=? ORDER BY SHEET_POSITION LIMIT 1", (self.branch_id,)).fetchone()
            row = conn.execute("SELECT ROW_ID FROM SHEET_ROWS WHERE BRANCH_ID=? AND SHEET_ID=? ORDER BY ROW_POSITION LIMIT 1", (self.branch_id, sheet["SHEET_ID"])).fetchone()
            column = conn.execute("SELECT COLUMN_ID FROM SHEET_COLUMNS WHERE BRANCH_ID=? AND SHEET_ID=? AND COLUMN_NAME='QTY'", (self.branch_id, sheet["SHEET_ID"])).fetchone()
            return [{"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet["SHEET_ID"], "row_id": row["ROW_ID"], "column_id": column["COLUMN_ID"], "new_value": 42}]
        finally:
            conn.close()

    def test_expired_run_is_rejected(self):
        prepared = run_service.prepare_run(self.table_id, self.macro_id, self.branch_id, self.editor["user_id"])
        conn = database._get_connection()
        try:
            conn.execute("UPDATE MACRO_RUNS SET EXPIRES_AT='2000-01-01T00:00:00+00:00' WHERE RUN_ID=?", (prepared["run_id"],))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(ValueError):
            run_service.confirm_run(self.table_id, prepared["run_id"], self.editor["user_id"])

    def test_viewer_cannot_prepare_a_run(self):
        with self.assertRaises(PermissionError):
            run_service.prepare_run(self.table_id, self.macro_id, self.branch_id, self.viewer["user_id"])

    def test_main_branch_is_rejected(self):
        with self.assertRaises(PermissionError) as ctx:
            run_service.prepare_run(self.table_id, self.macro_id, self.main_branch_id, self.editor["user_id"])
        self.assertIn("main", str(ctx.exception).lower())

    def test_confirming_twice_fails_the_second_time(self):
        prepared = run_service.prepare_run(self.table_id, self.macro_id, self.branch_id, self.editor["user_id"])
        run_service.confirm_run(self.table_id, prepared["run_id"], self.editor["user_id"])
        with self.assertRaises(ValueError):
            run_service.confirm_run(self.table_id, prepared["run_id"], self.editor["user_id"])


if __name__ == "__main__":
    unittest.main()
