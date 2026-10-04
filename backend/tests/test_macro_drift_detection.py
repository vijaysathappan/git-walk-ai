"""Phase 5 USP2: Macro Drift Detection -- re-registering a modified macro
source must flag exactly what changed (and whether the risk classification
itself changed), while a comment/whitespace-only change or byte-identical
re-extraction must never be reported as drift."""

import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from vba_fixture_builder import build_xlsm  # noqa: E402

from app import database  # noqa: E402
from app.macros import run_service  # noqa: E402

ORIGINAL_MACRO = (
    "Public Sub Recalc()\n"
    "    Dim i As Long\n"
    "    For i = 2 To 4\n"
    "        Cells(i, 3).Value = Cells(i, 1).Value * Cells(i, 2).Value\n"
    "    Next i\n"
    "End Sub\n"
)
COMMENT_ONLY_CHANGE = (
    "Public Sub Recalc()\n"
    "    ' Recomputes the total column.\n"
    "    Dim i As Long\n"
    "    For i = 2 To 4\n"
    "        Cells(i, 3).Value = Cells(i, 1).Value * Cells(i, 2).Value\n"
    "    Next i\n"
    "End Sub\n"
)
LOOP_BOUND_CHANGED = (
    "Public Sub Recalc()\n"
    "    Dim i As Long\n"
    "    For i = 2 To 10\n"
    "        Cells(i, 3).Value = Cells(i, 1).Value * Cells(i, 2).Value\n"
    "    Next i\n"
    "End Sub\n"
)
NOW_DANGEROUS = (
    'Public Sub Recalc()\n'
    '    Dim fso As Object\n'
    '    Set fso = CreateObject("Scripting.FileSystemObject")\n'
    'End Sub\n'
)


class MacroDriftDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "macro_drift.db"
        database.initialize_product_schema()

        self.owner = database.get_or_create_user(f"owner_{uuid.uuid4().hex[:8]}@example.com")
        self.table_id = "QUEUE_BOARD_MACRODRIFT"
        conn = database._get_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{self.table_id}"')
            conn.execute(f'CREATE TABLE "{self.table_id}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT, QTY INTEGER, PRICE INTEGER, TOTAL INTEGER)')
            conn.commit()
        finally:
            conn.close()
        database.register_dataset(self.table_id, self.owner["user_id"], "drift.xlsx", 0, 3)

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _register_and_extract(self, macro_source: str, filename: str):
        xlsm_bytes = build_xlsm([["Qty", "Price", "Total"]], "Module1", macro_source)
        run_service.register_source(self.table_id, filename, xlsm_bytes, self.owner["user_id"])
        return run_service.extract(self.table_id, self.owner["user_id"])

    def test_first_extraction_has_no_drift(self):
        self._register_and_extract(ORIGINAL_MACRO, "v1.xlsm")
        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        self.assertIsNone(listing["macros"][0]["drift"])

    def test_comment_only_change_is_not_reported_as_drift(self):
        self._register_and_extract(ORIGINAL_MACRO, "v1.xlsm")
        self._register_and_extract(COMMENT_ONLY_CHANGE, "v2.xlsm")
        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        self.assertIsNone(listing["macros"][0]["drift"])

    def test_re_extracting_identical_bytes_is_deduplicated_not_drifted(self):
        self._register_and_extract(ORIGINAL_MACRO, "v1.xlsm")
        result = self._register_and_extract(ORIGINAL_MACRO, "v1.xlsm")
        self.assertTrue(result.get("deduplicated"))
        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        self.assertIsNone(listing["macros"][0]["drift"])

    def test_genuine_logic_change_is_flagged_with_a_diff(self):
        self._register_and_extract(ORIGINAL_MACRO, "v1.xlsm")
        self._register_and_extract(LOOP_BOUND_CHANGED, "v2.xlsm")
        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        drift = listing["macros"][0]["drift"]
        self.assertIsNotNone(drift)
        self.assertFalse(drift["risk_changed"])
        diff_text = "\n".join(drift["diff"])
        self.assertIn("For i = 2 To 4", diff_text)
        self.assertIn("For i = 2 To 10", diff_text)

    def test_risk_classification_flip_is_flagged_prominently(self):
        self._register_and_extract(ORIGINAL_MACRO, "v1.xlsm")
        self._register_and_extract(NOW_DANGEROUS, "v2.xlsm")
        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        macro = next(m for m in listing["macros"] if m["proc_name"] == "Recalc")
        self.assertEqual("BLOCKED_EXTERNAL", macro["static_risk"])
        drift = macro["drift"]
        self.assertIsNotNone(drift)
        self.assertTrue(drift["risk_changed"])
        self.assertEqual("RUNNABLE", drift["previous_static_risk"])


if __name__ == "__main__":
    unittest.main()
