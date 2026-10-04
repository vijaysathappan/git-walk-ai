"""Real VBA extraction: builds genuine .xlsm fixtures (real MS-OVBA/CFB
bytes, no Excel/COM involved -- see tests/fixtures/vba_fixture_builder.py)
and verifies app.macros.extractor reads actual Sub source, not a guess."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from vba_fixture_builder import build_xlsm  # noqa: E402

from app.macros.extractor import extract_macros  # noqa: E402


SIMPLE_CALC = """Public Sub RecalculatePrices()
    Dim i As Long
    For i = 2 To 4
        Cells(i, 3).Value = Cells(i, 1).Value * Cells(i, 2).Value
    Next i
End Sub
"""

WITH_PARAM = """Public Sub GreetUser(name As String)
    MsgBox "Hello " & name
End Sub
"""

AUTO_EXEC = """Public Sub Workbook_Open()
    Cells(1, 1).Value = "opened"
End Sub
"""

DANGEROUS = """Public Sub ExportAndNotify()
    Dim result As Long
    result = Shell("cmd.exe /c echo hello", 1)
End Sub
"""


class MacroExtractionTests(unittest.TestCase):
    def test_extracts_real_sub_source_from_genuine_xlsm(self):
        data = build_xlsm([["Qty", "Price", "Total"], [2, 10, None]], "Module1", SIMPLE_CALC)
        outcome = extract_macros(data)
        self.assertIsNone(outcome.error_message)
        self.assertEqual(1, len(outcome.macros))
        macro = outcome.macros[0]
        self.assertEqual("Module1", macro.module_name)
        self.assertEqual("RecalculatePrices", macro.proc_name)
        self.assertIn("Cells(i, 3).Value = Cells(i, 1).Value * Cells(i, 2).Value", macro.source)
        self.assertFalse(macro.has_parameters)
        self.assertFalse(macro.is_auto_exec)

    def test_detects_parameters(self):
        data = build_xlsm([["A"]], "Module1", WITH_PARAM)
        outcome = extract_macros(data)
        self.assertEqual(1, len(outcome.macros))
        self.assertTrue(outcome.macros[0].has_parameters)

    def test_detects_auto_exec_names(self):
        data = build_xlsm([["A"]], "Module1", AUTO_EXEC)
        outcome = extract_macros(data)
        self.assertEqual(1, len(outcome.macros))
        self.assertTrue(outcome.macros[0].is_auto_exec)

    def test_multiple_subs_in_one_module_are_all_extracted(self):
        combined = SIMPLE_CALC + "\n" + WITH_PARAM
        data = build_xlsm([["A"]], "Module1", combined)
        outcome = extract_macros(data)
        names = sorted(macro.proc_name for macro in outcome.macros)
        self.assertEqual(["GreetUser", "RecalculatePrices"], names)

    def test_dangerous_macro_source_is_still_extracted_for_display(self):
        # Extraction never judges safety -- that's static_gate's job. It must
        # still surface dangerous macros' source so they can be shown as
        # blocked, not silently dropped.
        data = build_xlsm([["A"]], "Module1", DANGEROUS)
        outcome = extract_macros(data)
        self.assertEqual(1, len(outcome.macros))
        self.assertIn("Shell(", outcome.macros[0].source)

    def test_non_xlsm_bytes_fail_closed_never_raises(self):
        # oletools tolerates garbage input (no VBA project found, not an
        # error) rather than raising -- either way, no macro is ever
        # fabricated from unparseable bytes.
        outcome = extract_macros(b"not a real workbook")
        self.assertEqual([], outcome.macros)


if __name__ == "__main__":
    unittest.main()
