"""Phase 5 USP3: Saved Macro Recipes -- pure metadata over the unmodified
prepare_run()/confirm_run() pipeline. A recipe survives re-extraction (it's
keyed by proc name, not macro_id) and fails closed if its proc later
becomes unrunnable, rather than silently skipping or substituting."""

import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from vba_fixture_builder import build_xlsm  # noqa: E402

from app import database  # noqa: E402
from app.macros import run_service  # noqa: E402

RUNNABLE_MACRO = (
    "Public Sub Recalc()\n"
    "    Dim i As Long\n"
    "    For i = 2 To 4\n"
    "        Cells(i, 3).Value = Cells(i, 1).Value * Cells(i, 2).Value\n"
    "    Next i\n"
    "End Sub\n"
)
NOW_BLOCKED_MACRO = (
    'Public Sub Recalc()\n'
    '    Dim fso As Object\n'
    '    Set fso = CreateObject("Scripting.FileSystemObject")\n'
    'End Sub\n'
)


class MacroRecipeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "macro_recipes.db"
        database.initialize_product_schema()

        self.owner = database.get_or_create_user(f"owner_{uuid.uuid4().hex[:8]}@example.com")
        self.viewer = database.get_or_create_user(f"viewer_{uuid.uuid4().hex[:8]}@example.com")
        self.table_id = "QUEUE_BOARD_MACRORECIPE"
        conn = database._get_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{self.table_id}"')
            conn.execute(f'CREATE TABLE "{self.table_id}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT, QTY INTEGER, PRICE INTEGER, TOTAL INTEGER)')
            conn.executemany(
                f'INSERT INTO "{self.table_id}" (QTY, PRICE, TOTAL) VALUES (?, ?, ?)',
                [(2, 10, None), (3, 5, None), (1, 4, None)],
            )
            conn.commit()
        finally:
            conn.close()
        database.register_dataset(self.table_id, self.owner["user_id"], "recipe.xlsx", 3, 3)
        database.add_dataset_member(self.table_id, self.owner["user_id"], self.viewer["email"], "viewer")

        xlsm_bytes = build_xlsm([["Qty", "Price", "Total"]], "Module1", RUNNABLE_MACRO)
        run_service.register_source(self.table_id, "recipe.xlsm", xlsm_bytes, self.owner["user_id"])
        run_service.extract(self.table_id, self.owner["user_id"])
        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        self.macro_id = listing["macros"][0]["macro_id"]

        working_copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"], branch_mode="new")
        self.branch_id = working_copy["branch_id"]

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_save_and_list_recipe(self):
        saved = run_service.save_recipe(self.table_id, self.macro_id, self.owner["user_id"], "Monthly recalc", "Runs at month end")
        self.assertEqual("Monthly recalc", saved["name"])

        listing = run_service.list_recipes(self.table_id, self.owner["user_id"])
        self.assertEqual(1, len(listing["recipes"]))
        recipe = listing["recipes"][0]
        self.assertEqual("Monthly recalc", recipe["name"])
        self.assertTrue(recipe["currently_runnable"])
        self.assertEqual(0, recipe["run_count"])
        self.assertIsNone(recipe["last_run_at"])

    def test_cannot_save_a_blocked_macro_as_a_recipe(self):
        xlsm_bytes = build_xlsm([["Qty", "Price", "Total"]], "Module1", NOW_BLOCKED_MACRO)
        run_service.register_source(self.table_id, "recipe2.xlsm", xlsm_bytes, self.owner["user_id"])
        run_service.extract(self.table_id, self.owner["user_id"])
        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        blocked_macro_id = listing["macros"][0]["macro_id"]
        with self.assertRaises(PermissionError):
            run_service.save_recipe(self.table_id, blocked_macro_id, self.owner["user_id"], "Should fail")

    def test_running_a_recipe_goes_through_the_normal_prepare_confirm_pipeline(self):
        saved = run_service.save_recipe(self.table_id, self.macro_id, self.owner["user_id"], "Monthly recalc")
        prepared = run_service.prepare_run_from_recipe(self.table_id, saved["recipe_id"], self.branch_id, self.owner["user_id"])
        self.assertEqual("PENDING_CONFIRMATION", prepared["status"])
        self.assertEqual(3, prepared["preview_change_count"])

        confirmed = run_service.confirm_run(self.table_id, prepared["run_id"], self.owner["user_id"])
        self.assertEqual("EXECUTED", confirmed["status"])

        listing = run_service.list_recipes(self.table_id, self.owner["user_id"])
        recipe = listing["recipes"][0]
        self.assertEqual(1, recipe["run_count"])
        self.assertIsNotNone(recipe["last_run_at"])

    def test_recipe_survives_re_extraction_because_it_is_keyed_by_name_not_macro_id(self):
        saved = run_service.save_recipe(self.table_id, self.macro_id, self.owner["user_id"], "Monthly recalc")
        # Re-register the SAME logic under a new extraction run -- this
        # mints a brand-new macro_id for "Recalc", per how extract() works.
        xlsm_bytes = build_xlsm([["Qty", "Price", "Total"]], "Module1", RUNNABLE_MACRO.replace("2 To 4", "2 To 4  "))
        run_service.register_source(self.table_id, "recipe3.xlsm", xlsm_bytes, self.owner["user_id"])
        run_service.extract(self.table_id, self.owner["user_id"])
        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        new_macro_id = listing["macros"][0]["macro_id"]
        self.assertNotEqual(self.macro_id, new_macro_id)

        prepared = run_service.prepare_run_from_recipe(self.table_id, saved["recipe_id"], self.branch_id, self.owner["user_id"])
        self.assertEqual("PENDING_CONFIRMATION", prepared["status"])

    def test_recipe_fails_closed_when_the_proc_is_no_longer_runnable(self):
        saved = run_service.save_recipe(self.table_id, self.macro_id, self.owner["user_id"], "Monthly recalc")
        xlsm_bytes = build_xlsm([["Qty", "Price", "Total"]], "Module1", NOW_BLOCKED_MACRO)
        run_service.register_source(self.table_id, "recipe4.xlsm", xlsm_bytes, self.owner["user_id"])
        run_service.extract(self.table_id, self.owner["user_id"])

        with self.assertRaises(PermissionError):
            run_service.prepare_run_from_recipe(self.table_id, saved["recipe_id"], self.branch_id, self.owner["user_id"])

    def test_viewer_cannot_save_or_run_a_recipe_but_can_list(self):
        with self.assertRaises(PermissionError):
            run_service.save_recipe(self.table_id, self.macro_id, self.viewer["user_id"], "Nope")
        saved = run_service.save_recipe(self.table_id, self.macro_id, self.owner["user_id"], "Monthly recalc")
        with self.assertRaises(PermissionError):
            run_service.prepare_run_from_recipe(self.table_id, saved["recipe_id"], self.branch_id, self.viewer["user_id"])
        listing = run_service.list_recipes(self.table_id, self.viewer["user_id"])
        self.assertEqual(1, len(listing["recipes"]))

    def test_delete_recipe(self):
        saved = run_service.save_recipe(self.table_id, self.macro_id, self.owner["user_id"], "Monthly recalc")
        run_service.delete_recipe(self.table_id, saved["recipe_id"], self.owner["user_id"])
        listing = run_service.list_recipes(self.table_id, self.owner["user_id"])
        self.assertEqual(0, len(listing["recipes"]))


if __name__ == "__main__":
    unittest.main()
