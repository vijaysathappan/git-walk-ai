import unittest

from app.excel.diff_engine import semantic_diff
from app.excel.values import normalize_excel_value, values_semantically_equal


class ExcelValueTests(unittest.TestCase):
    def test_excel_date_serial_matches_iso_timestamp(self):
        self.assertTrue(
            values_semantically_equal("2026-01-16 00:00:00", 46038)
        )
        self.assertEqual(
            "2026-01-16 00:00:00",
            normalize_excel_value(46038, reference="2026-01-16 00:00:00"),
        )

    def test_semantic_diff_ignores_date_representation_drift(self):
        base = {
            "sheets": [{
                "sheet_id": "SHEET_A",
                "name": "Data",
                "position": 0,
                "columns": [{
                    "column_id": "COL_DATE",
                    "name": "ORDER_DATE",
                    "position": 0,
                    "data_type": "TEXT",
                }],
                "rows": [{
                    "row_id": "ROW_A",
                    "position": 0,
                    "values": {"COL_DATE": "2026-01-16 00:00:00"},
                    "formulas": {},
                    "styles": {},
                    "comments": {},
                }],
            }]
        }
        branch = {
            "sheets": [{
                **base["sheets"][0],
                "rows": [{
                    **base["sheets"][0]["rows"][0],
                    "values": {"COL_DATE": 46038},
                }],
            }]
        }

        self.assertEqual([], semantic_diff(base, branch))


if __name__ == "__main__":
    unittest.main()
