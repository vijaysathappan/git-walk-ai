"""Regression test for a real reported bug: a Virtual Run macro that
creates a brand-new worksheet (e.g. a Dictionary-aggregation summary sheet)
displayed correctly on the personal branch it was run against, but the new
sheet and its content vanished after merging that branch into main. Root
cause was in app.excel.diff_engine.semantic_diff (see test_stage2_version_
engine.py's unit-level regression test) -- this test proves the fix holds
through the REAL macro-run -> commit -> merge pipeline, not just the pure
diff function in isolation."""

import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from vba_fixture_builder import build_xlsm  # noqa: E402

from app import database  # noqa: E402
from app.macros import run_service  # noqa: E402
from app.services.merge_service import MergeActor, merge_service  # noqa: E402

NEW_SHEET_MACRO = (
    "Public Sub Summarize()\n"
    "    Dim totals As Object\n"
    "    Dim i As Long, category As String, amount As Double\n"
    "    Dim summary As Object\n"
    "    Set totals = CreateObject(\"Scripting.Dictionary\")\n"
    "    For i = 2 To 4\n"
    "        category = Cells(i, 1).Value\n"
    "        amount = Cells(i, 2).Value\n"
    "        If totals.Exists(category) Then\n"
    "            totals(category) = totals(category) + amount\n"
    "        Else\n"
    "            totals.Add category, amount\n"
    "        End If\n"
    "    Next i\n"
    "    Set summary = Worksheets.Add()\n"
    "    summary.Cells(1, 1).Value = \"Category Name\"\n"
    "    summary.Cells(1, 2).Value = \"Total Amount\"\n"
    "    Dim key As Variant\n"
    "    Dim r As Long\n"
    "    r = 2\n"
    "    For Each key In totals.Keys\n"
    "        summary.Cells(r, 1).Value = key\n"
    "        summary.Cells(r, 2).Value = totals(key)\n"
    "        r = r + 1\n"
    "    Next key\n"
    "End Sub\n"
)


class MacroNewSheetSurvivesMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "macro_new_sheet_merge.db"
        database.initialize_product_schema()

        self.owner = database.get_or_create_user(f"owner_{uuid.uuid4().hex[:8]}@example.com")
        self.table_id = "QUEUE_BOARD_NEWSHEETMERGE"
        conn = database._get_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{self.table_id}"')
            conn.execute(f'CREATE TABLE "{self.table_id}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT, CATEGORY TEXT, AMOUNT INTEGER)')
            conn.executemany(
                f'INSERT INTO "{self.table_id}" (CATEGORY, AMOUNT) VALUES (?, ?)',
                [("Food", 10), ("Travel", 20), ("Food", 5)],
            )
            conn.commit()
        finally:
            conn.close()
        registered = database.register_dataset(self.table_id, self.owner["user_id"], "newsheet.xlsx", 3, 2)
        self.repository_id = registered["repository_id"]
        self.main_branch_id = registered["main_branch_id"]

        xlsm_bytes = build_xlsm([["Category", "Amount"]], "Module1", NEW_SHEET_MACRO)
        run_service.register_source(self.table_id, "newsheet.xlsm", xlsm_bytes, self.owner["user_id"])
        extraction = run_service.extract(self.table_id, self.owner["user_id"])
        self.assertEqual("COMPLETED", extraction["status"])
        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        self.assertTrue(listing["macros"][0]["runnable"], listing["macros"][0]["block_reasons"])
        self.assertEqual("INTERPRETED", listing["macros"][0]["execution_lane"])
        self.macro_id = listing["macros"][0]["macro_id"]

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_new_sheet_created_by_a_macro_survives_merge_to_main(self):
        working_copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"], branch_mode="new")
        branch_id = working_copy["branch_id"]

        prepared = run_service.prepare_run(self.table_id, self.macro_id, branch_id, self.owner["user_id"])
        self.assertGreater(prepared["preview_change_count"], 0)
        confirmed = run_service.confirm_run(self.table_id, prepared["run_id"], self.owner["user_id"])
        self.assertEqual("EXECUTED", confirmed["status"])

        # Sanity check: the new sheet is genuinely on the personal branch
        # before we even attempt the merge (matches the user's report that
        # it displayed correctly there).
        branch_state = database.get_table_snapshot(working_copy["table_id"])
        branch_sheet_names = {sheet["name"] for sheet in branch_state["semantic"]["sheets"]}
        self.assertIn("Sheet2", branch_sheet_names)

        maker = MergeActor(self.owner["user_id"], self.owner["email"])
        request = merge_service.create_request(
            source_branch_id=branch_id, target_branch_id=self.main_branch_id,
            title="Bring in the summary sheet", description="test", actor=maker,
        )
        merge_service.review(merge_request_id=request["merge_request_id"], decision="APPROVED", comment="self-review", actor=maker)
        merge_service.merge(request["merge_request_id"], maker)

        main_state = database.get_table_snapshot(self.table_id)
        main_sheets = {sheet["name"]: sheet for sheet in main_state["semantic"]["sheets"]}
        self.assertIn("Sheet2", main_sheets, "the new sheet itself is missing from main after merge")
        summary_sheet = main_sheets["Sheet2"]
        self.assertEqual(2, len(summary_sheet["columns"]), "the new sheet's columns are missing from main after merge")
        self.assertEqual(2, len(summary_sheet["rows"]), "the new sheet's row data is missing from main after merge")

        category_column = next(c for c in summary_sheet["columns"] if c["name"] == "CATEGORY_NAME")
        total_column = next(c for c in summary_sheet["columns"] if c["name"] == "TOTAL_AMOUNT")
        by_category = {
            row["values"][category_column["column_id"]]: row["values"][total_column["column_id"]]
            for row in summary_sheet["rows"]
        }
        # New-sheet columns are always created as TEXT (see virtual_workbook.
        # _new_sheet_changes) -- values round-trip as strings, which is
        # unrelated to the merge bug this test targets.
        self.assertEqual({"Food": "15", "Travel": "20"}, by_category)


if __name__ == "__main__":
    unittest.main()
