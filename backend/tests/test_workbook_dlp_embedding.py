"""Integration test: every workbook issued via workbook_service.issue_branch_workbook
(the function backing /upload-and-provision and /work-on-workbook) must carry
the clipboard-DLP macro -- this is the regression test for the "silently
missing macro" risk identified while planning Phase 6.
"""

import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from oletools.olevba import VBA_Parser
from openpyxl import load_workbook

from app import database
from app.excel.clipboard_dlp_macro import MODULE_NAME as DLP_MODULE_NAME
from app.services.workbook_service import issue_branch_workbook


class IssueBranchWorkbookCarriesDlpMacroTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "workbook_dlp.db"
        database.initialize_product_schema()

        self.owner = database.get_or_create_user(f"owner_{uuid.uuid4().hex[:8]}@example.com")
        self.table_id = "QUEUE_BOARD_DLPTEST"
        conn = database._get_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{self.table_id}"')
            conn.execute(f'CREATE TABLE "{self.table_id}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT, CATEGORY TEXT, AMOUNT INTEGER)')
            conn.executemany(
                f'INSERT INTO "{self.table_id}" (CATEGORY, AMOUNT) VALUES (?, ?)',
                [("Food", 10), ("Travel", 20)],
            )
            conn.commit()
        finally:
            conn.close()
        database.register_dataset(self.table_id, self.owner["user_id"], "dlp.xlsx", 2, 2)

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_issued_workbook_is_a_valid_xlsm_carrying_the_dlp_module(self):
        result = issue_branch_workbook(self.table_id, self.owner["user_id"], self.owner["email"])

        path = Path(result["path"])
        self.assertEqual(".xlsm", path.suffix)
        self.assertTrue(zipfile.is_zipfile(path))

        parser = VBA_Parser(str(path))
        try:
            modules = list(parser.extract_macros())
        finally:
            parser.close()
        matching = [m for m in modules if m[2].rsplit(".", 1)[0] == DLP_MODULE_NAME]
        self.assertEqual(1, len(matching))
        self.assertIn("Auto_Open", matching[0][3])
        self.assertIn("Auto_Close", matching[0][3])

        # The taskpane manifest injection this file also depends on must
        # still have happened -- the DLP embedding step must not clobber it.
        workbook = load_workbook(str(path))
        defined_names = {name.lower() for name in workbook.defined_names.keys()}
        self.assertIn("_gitwalk_repository_id", defined_names, defined_names)


if __name__ == "__main__":
    unittest.main()
