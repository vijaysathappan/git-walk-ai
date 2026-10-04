import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import load_workbook

from app.openxml_injector import OFFICE_ADDIN_ID, inject_taskpane_manifest


class OpenXmlInjectorTests(unittest.TestCase):
    def test_embeds_table_id_in_addin_settings(self):
        backend_dir = Path(__file__).resolve().parent.parent
        source = backend_dir / "test_upload.xlsx"

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "configured.xlsx"
            inject_taskpane_manifest(
                str(source),
                str(output),
                table_id="QUEUE_BOARD_TEST1234",
                metadata={
                    "repository_id": "REP_TEST",
                    "branch_id": "BR_TEST",
                    "branch_name": "users/test/revenue-model",
                    "working_copy_id": "WC_TEST",
                    "base_commit_id": "CMT_TEST",
                    "issued_at": "2026-08-10T00:00:00+00:00",
                    "signature": "signed-value",
                },
            )

            with zipfile.ZipFile(output) as workbook:
                settings_xml = workbook.read(
                    "xl/webextensions/webextension1.xml"
                ).decode("utf-8")
                workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")

            self.assertIn('name="tableId"', settings_xml)
            self.assertIn('value="QUEUE_BOARD_TEST1234"', settings_xml)
            self.assertIn(f'id="{OFFICE_ADDIN_ID}"', settings_xml)
            self.assertIn(
                f'<we:reference id="{OFFICE_ADDIN_ID}"',
                settings_xml,
            )
            self.assertIn('name="_EXCEL_SQLITE_SYNC_TABLE_ID"', workbook_xml)
            self.assertIn('name="_GITWALK_REPOSITORY_ID"', workbook_xml)
            self.assertIn('name="repository_id"', settings_xml)
            self.assertIn('value="REP_TEST"', settings_xml)

            configured_workbook = load_workbook(output, read_only=True)
            embedded_name = configured_workbook.defined_names[
                "_EXCEL_SQLITE_SYNC_TABLE_ID"
            ]
            self.assertEqual('"QUEUE_BOARD_TEST1234"', embedded_name.attr_text)
            self.assertEqual(
                '"BR_TEST"',
                configured_workbook.defined_names["_GITWALK_BRANCH_ID"].attr_text,
            )
            self.assertEqual(
                '"users/test/revenue-model"',
                configured_workbook.defined_names["_GITWALK_BRANCH_NAME"].attr_text,
            )
            configured_workbook.close()


if __name__ == "__main__":
    unittest.main()
