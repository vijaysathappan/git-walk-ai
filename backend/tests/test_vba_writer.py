"""Unit tests for app.excel.vba_writer.embed_vba_project -- the production
MS-OVBA/MS-CFB splice used to embed the local clipboard-DLP macro into every
workbook Git Walk issues. Mirrors the verification already done against
vba_fixture_builder.py's test-only copy of this same binary-format logic.
"""

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook
from oletools.olevba import VBA_Parser

from app.excel.vba_writer import embed_vba_project

MODULE_NAME = "mod_VirtualClipboard"
MODULE_SOURCE = (
    "Option Explicit\n\n"
    "Public Sub Auto_Open()\n"
    "    MsgBox \"opened\"\n"
    "End Sub\n\n"
    "Public Sub Auto_Close()\n"
    "    MsgBox \"closed\"\n"
    "End Sub\n"
)


class EmbedVbaProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _build_plain_xlsx(self) -> Path:
        path = Path(self.temp_dir.name) / "plain.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Sheet1"
        sheet.append(["Category", "Amount"])
        sheet.append(["Food", 10])
        workbook.save(path)
        return path

    def test_embedded_module_source_is_extracted_byte_identical(self):
        input_path = self._build_plain_xlsx()
        output_path = Path(self.temp_dir.name) / "with_macro.xlsm"

        embed_vba_project(str(input_path), str(output_path), MODULE_NAME, MODULE_SOURCE)

        parser = VBA_Parser(str(output_path))
        try:
            modules = list(parser.extract_macros())
        finally:
            parser.close()

        matching = [m for m in modules if m[2].rsplit(".", 1)[0] == MODULE_NAME]
        self.assertEqual(1, len(matching), "expected exactly one extracted module with the DLP module name")
        extracted_source = matching[0][3]
        self.assertIn("Auto_Open", extracted_source)
        self.assertIn("Auto_Close", extracted_source)
        self.assertIn('MsgBox "opened"', extracted_source)

    def test_original_sheet_data_survives_the_splice(self):
        input_path = self._build_plain_xlsx()
        output_path = Path(self.temp_dir.name) / "with_macro.xlsm"

        embed_vba_project(str(input_path), str(output_path), MODULE_NAME, MODULE_SOURCE)

        workbook = load_workbook(str(output_path))
        sheet = workbook["Sheet1"]
        self.assertEqual("Category", sheet.cell(row=1, column=1).value)
        self.assertEqual("Food", sheet.cell(row=2, column=1).value)
        self.assertEqual(10, sheet.cell(row=2, column=2).value)

    def test_re_embedding_over_an_already_macro_carrying_file_stays_valid(self):
        input_path = self._build_plain_xlsx()
        once = Path(self.temp_dir.name) / "once.xlsm"
        twice = Path(self.temp_dir.name) / "twice.xlsm"

        embed_vba_project(str(input_path), str(once), MODULE_NAME, MODULE_SOURCE)
        embed_vba_project(str(once), str(twice), MODULE_NAME, MODULE_SOURCE)

        parser = VBA_Parser(str(twice))
        try:
            modules = list(parser.extract_macros())
        finally:
            parser.close()
        matching = [m for m in modules if m[2].rsplit(".", 1)[0] == MODULE_NAME]
        self.assertEqual(1, len(matching))

        workbook = load_workbook(str(twice))
        self.assertEqual("Category", workbook["Sheet1"].cell(row=1, column=1).value)


if __name__ == "__main__":
    unittest.main()
