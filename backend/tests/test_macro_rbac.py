"""Virtual Run RBAC: owner/editor can register a macro source and run
macros (macro.run); a viewer can only look (macro.view); someone with no
repository access at all gets nothing."""

import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from vba_fixture_builder import build_xlsm  # noqa: E402

from app import database  # noqa: E402
from app.macros import run_service  # noqa: E402

SAFE_MACRO = "Public Sub Recalc()\n    Cells(1, 1).Value = 1\nEnd Sub\n"


class MacroRBACTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "macro_rbac.db"
        database.initialize_product_schema()

        self.owner = database.get_or_create_user(f"owner_{uuid.uuid4().hex[:8]}@example.com")
        self.editor = database.get_or_create_user(f"editor_{uuid.uuid4().hex[:8]}@example.com")
        self.viewer = database.get_or_create_user(f"viewer_{uuid.uuid4().hex[:8]}@example.com")
        self.outsider = database.get_or_create_user(f"outsider_{uuid.uuid4().hex[:8]}@example.com")

        self.table_id = "QUEUE_BOARD_MACRORBAC"
        conn = database._get_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{self.table_id}"')
            conn.execute(f'CREATE TABLE "{self.table_id}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT, NAME TEXT)')
            conn.commit()
        finally:
            conn.close()
        database.register_dataset(self.table_id, self.owner["user_id"], "macro_rbac.xlsx", 0, 1)
        database.add_dataset_member(self.table_id, self.owner["user_id"], self.editor["email"], "editor")
        database.add_dataset_member(self.table_id, self.owner["user_id"], self.viewer["email"], "viewer")
        repository = database.get_repository(self.table_id, self.owner["user_id"])
        self.repository_id = repository["repository_id"]
        self.xlsm_bytes = build_xlsm([["A"]], "Module1", SAFE_MACRO)

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_editor_can_register_source_and_run_extraction(self):
        run_service.register_source(self.table_id, "book.xlsm", self.xlsm_bytes, self.editor["user_id"])
        result = run_service.extract(self.table_id, self.editor["user_id"])
        self.assertEqual("COMPLETED", result["status"])
        self.assertEqual(1, result["macro_count"])

    def test_owner_can_register_source(self):
        result = run_service.register_source(self.table_id, "book.xlsm", self.xlsm_bytes, self.owner["user_id"])
        self.assertEqual(self.repository_id, result["repository_id"])

    def test_viewer_cannot_register_source(self):
        with self.assertRaises(PermissionError):
            run_service.register_source(self.table_id, "book.xlsm", self.xlsm_bytes, self.viewer["user_id"])

    def test_viewer_can_list_macros_after_editor_registers_and_extracts(self):
        run_service.register_source(self.table_id, "book.xlsm", self.xlsm_bytes, self.editor["user_id"])
        run_service.extract(self.table_id, self.editor["user_id"])
        result = run_service.list_macros(self.table_id, self.viewer["user_id"])
        self.assertTrue(result["has_source"])
        self.assertEqual(1, len(result["macros"]))
        self.assertEqual("Recalc", result["macros"][0]["proc_name"])

    def test_outsider_has_no_access_at_all(self):
        with self.assertRaises(PermissionError):
            run_service.list_macros(self.table_id, self.outsider["user_id"])
        with self.assertRaises(PermissionError):
            run_service.register_source(self.table_id, "book.xlsm", self.xlsm_bytes, self.outsider["user_id"])

    def test_no_registered_source_yet_is_reported_not_an_error(self):
        result = run_service.list_macros(self.table_id, self.viewer["user_id"])
        self.assertFalse(result["has_source"])
        self.assertEqual([], result["macros"])

    def test_dangerous_macro_is_listed_but_never_runnable(self):
        dangerous = build_xlsm([["A"]], "Module1", 'Public Sub Bad()\n    Shell "cmd.exe"\nEnd Sub\n')
        run_service.register_source(self.table_id, "bad.xlsm", dangerous, self.editor["user_id"])
        run_service.extract(self.table_id, self.editor["user_id"])
        result = run_service.list_macros(self.table_id, self.viewer["user_id"])
        self.assertEqual("BLOCKED_EXTERNAL", result["macros"][0]["static_risk"])
        self.assertFalse(result["macros"][0]["runnable"])


if __name__ == "__main__":
    unittest.main()
