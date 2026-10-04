"""Regression test for the silent-strip risk identified while planning
Phase 6: local_workbook_sanitizer.sanitize_local_workbook_file re-runs
inject_taskpane_manifest on every closed-workbook cycle, which would
otherwise drop the clipboard-DLP macro the first time Excel closes a
Git Walk-issued file. Confirms the macro survives sanitization.
"""

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook
from oletools.olevba import VBA_Parser

from app.excel.clipboard_dlp_macro import MODULE_NAME as DLP_MODULE_NAME, MODULE_SOURCE as DLP_MODULE_SOURCE
from app.excel.vba_writer import embed_vba_project
from app.services.local_workbook_sanitizer import sanitize_local_workbook_file


class SanitizerPreservesDlpMacroTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _build_macro_carrying_workbook(self) -> Path:
        plain_path = Path(self.temp_dir.name) / "plain.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Sheet1"
        sheet.append(["Category", "Amount"])
        sheet.append(["Food", 10])
        sheet.append(["Travel", 20])
        workbook.save(plain_path)

        macro_path = Path(self.temp_dir.name) / "with_macro.xlsm"
        embed_vba_project(str(plain_path), str(macro_path), DLP_MODULE_NAME, DLP_MODULE_SOURCE)
        return macro_path

    def test_macro_survives_a_sanitize_cycle(self):
        target = self._build_macro_carrying_workbook()

        result = sanitize_local_workbook_file(target, table_id="QUEUE_BOARD_TEST", force=True)

        self.assertTrue(result["success"], result)

        parser = VBA_Parser(str(target))
        try:
            modules = list(parser.extract_macros())
        finally:
            parser.close()
        matching = [m for m in modules if m[2].rsplit(".", 1)[0] == DLP_MODULE_NAME]
        self.assertEqual(1, len(matching), "the DLP macro must survive re-sanitization")
        self.assertIn("Auto_Open", matching[0][3])


if __name__ == "__main__":
    unittest.main()
