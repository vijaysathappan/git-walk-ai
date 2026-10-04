"""Static safety classification: a macro that touches anything outside the
workbook must always be BLOCKED_EXTERNAL. A macro that's safe but doesn't
fit the SQL fast lane's narrow shape falls through to the general
interpreter's safety check; only a macro neither lane accepts fails closed
as BLOCKED_UNSUPPORTED. A macro that fits the fast lane -- a single For
loop over a literal row range, same-row Cells(...) assignments only -- is
RUNNABLE/SQL; anything else safe (fixed-row references, no loop at all,
Dictionary aggregation, ...) is RUNNABLE/INTERPRETED."""

import unittest

from app.macros import static_gate
from app.macros.models import ExtractedMacro


def _macro(source: str, is_auto_exec: bool = False, has_parameters: bool = False) -> ExtractedMacro:
    return ExtractedMacro(
        module_name="Module1", proc_name="Test", source=source,
        is_auto_exec=is_auto_exec, has_parameters=has_parameters,
    )


class StaticGateTests(unittest.TestCase):
    def test_shell_call_is_blocked_external(self):
        macro = _macro('Public Sub Test()\n    Shell "cmd.exe /c echo hi"\nEnd Sub\n')
        risk, reasons, lane = static_gate.classify(macro)
        self.assertEqual("BLOCKED_EXTERNAL", risk)
        self.assertIsNone(lane)
        self.assertTrue(any("shell" in reason.construct.lower() for reason in reasons))

    def test_filesystem_object_is_blocked_external(self):
        macro = _macro(
            'Public Sub Test()\n'
            '    Dim fso As Object\n'
            '    Set fso = CreateObject("Scripting.FileSystemObject")\n'
            '    fso.CreateTextFile "C:\\out.txt"\n'
            'End Sub\n'
        )
        risk, _, lane = static_gate.classify(macro)
        self.assertEqual("BLOCKED_EXTERNAL", risk)
        self.assertIsNone(lane)

    def test_bare_assignment_without_a_loop_is_runnable_via_the_interpreter_lane(self):
        # Safe and outside the fast lane's shape (no For loop) -- doesn't
        # fit the SQL lane, but the general interpreter handles it fine.
        macro = _macro('Public Sub Test()\n    Cells(1, 1).Value = 42\nEnd Sub\n')
        risk, reasons, lane = static_gate.classify(macro)
        self.assertEqual("RUNNABLE", risk)
        self.assertEqual([], reasons)
        self.assertEqual("INTERPRETED", lane)

    def test_per_row_calculation_loop_is_runnable_via_sql_lane(self):
        macro = _macro(
            'Public Sub Test()\n'
            '    Dim i As Long\n'
            '    For i = 2 To 4\n'
            '        Cells(i, 5).Value = Cells(i, 3).Value * Cells(i, 4).Value\n'
            '    Next i\n'
            'End Sub\n'
        )
        risk, reasons, lane = static_gate.classify(macro)
        self.assertEqual("RUNNABLE", risk)
        self.assertEqual([], reasons)
        self.assertEqual("SQL", lane)

    def test_fixed_row_reference_inside_loop_is_runnable_via_interpreter_lane(self):
        # The SQL lane rejects fixed-row references (only same-row Cells(i,
        # ...) reads are eligible there), but this is a perfectly safe
        # in-workbook reference for the general interpreter to execute.
        macro = _macro(
            'Public Sub Test()\n'
            '    Dim i As Long\n'
            '    For i = 2 To 4\n'
            '        Cells(i, 5).Value = Cells(i, 3).Value * Cells(2, 6).Value\n'
            '    Next i\n'
            'End Sub\n'
        )
        risk, reasons, lane = static_gate.classify(macro)
        self.assertEqual("RUNNABLE", risk)
        self.assertEqual([], reasons)
        self.assertEqual("INTERPRETED", lane)

    def test_dictionary_creation_is_not_blocked_external(self):
        # CreateObject("Scripting.Dictionary") is the exact pattern the
        # interpreter lane exists to run; the keyword scanner would
        # otherwise flag any bare "CreateObject" as Suspicious and block
        # this before the interpreter is ever reached, making the whole
        # Dictionary-aggregation capability unreachable in practice.
        macro = _macro(
            'Public Sub Test()\n'
            '    Dim totals As Object\n'
            '    Set totals = CreateObject("Scripting.Dictionary")\n'
            '    totals.Add "a", 1\n'
            '    Cells(1, 1).Value = totals("a")\n'
            'End Sub\n'
        )
        risk, reasons, lane = static_gate.classify(macro)
        self.assertEqual("RUNNABLE", risk)
        self.assertEqual([], reasons)
        self.assertEqual("INTERPRETED", lane)

    def test_create_object_with_a_non_dictionary_target_is_still_blocked_external(self):
        # The exemption above is narrow -- any other CreateObject target
        # must still be caught by the keyword scan, same as before.
        macro = _macro(
            'Public Sub Test()\n'
            '    Dim x As Object\n'
            '    Set x = CreateObject("Excel.Application")\n'
            'End Sub\n'
        )
        risk, reasons, lane = static_gate.classify(macro)
        self.assertEqual("BLOCKED_EXTERNAL", risk)
        self.assertIsNone(lane)

    def test_dynamic_loop_bound_is_runnable_via_interpreter_lane(self):
        # The SQL lane needs a literal loop bound; the interpreter can
        # resolve a Dim'd variable bound at execution time instead.
        macro = _macro(
            'Public Sub Test()\n'
            '    Dim i As Long\n'
            '    Dim lastRow As Long\n'
            '    lastRow = Rows.Count\n'
            '    For i = 2 To lastRow\n'
            '        Cells(i, 5).Value = Cells(i, 3).Value\n'
            '    Next i\n'
            'End Sub\n'
        )
        risk, reasons, lane = static_gate.classify(macro)
        self.assertEqual("RUNNABLE", risk)
        self.assertEqual([], reasons)
        self.assertEqual("INTERPRETED", lane)

    def test_unsupported_property_access_is_blocked_unsupported(self):
        # `.Select` has no effect inside a closed workbook model and isn't
        # implemented -- fails closed rather than being silently ignored.
        macro = _macro('Public Sub Test()\n    Cells(1, 1).Select\nEnd Sub\n')
        risk, reasons, lane = static_gate.classify(macro)
        self.assertEqual("BLOCKED_UNSUPPORTED", risk)
        self.assertIsNone(lane)
        self.assertIn("select", reasons[0].reason.lower())

    def test_undefined_variable_reference_is_blocked_unsupported(self):
        # `total` is never Dim'd, assigned, or a loop variable -- neither
        # lane can resolve it, so this fails closed, not silently as Empty.
        macro = _macro('Public Sub Test()\n    Cells(1, 1).Value = total\nEnd Sub\n')
        risk, reasons, lane = static_gate.classify(macro)
        self.assertEqual("BLOCKED_UNSUPPORTED", risk)
        self.assertIsNone(lane)
        self.assertIn("total", reasons[0].reason.lower())

    def test_auto_exec_macro_is_blocked_unsupported_with_specific_reason(self):
        macro = _macro('Public Sub Test()\n    Cells(1, 1).Value = 1\nEnd Sub\n', is_auto_exec=True)
        risk, reasons, lane = static_gate.classify(macro)
        self.assertEqual("BLOCKED_UNSUPPORTED", risk)
        self.assertIsNone(lane)
        self.assertIn("Event-triggered", reasons[0].reason)

    def test_parameterized_macro_is_blocked_unsupported_with_specific_reason(self):
        macro = _macro('Public Sub Test()\n    Cells(1, 1).Value = 1\nEnd Sub\n', has_parameters=True)
        risk, reasons, lane = static_gate.classify(macro)
        self.assertEqual("BLOCKED_UNSUPPORTED", risk)
        self.assertIsNone(lane)
        self.assertIn("Parameterized", reasons[0].reason)

    def test_external_classification_takes_priority_over_other_reasons(self):
        macro = _macro('Public Sub Test()\n    Shell "cmd.exe"\nEnd Sub\n', has_parameters=True)
        risk, _, lane = static_gate.classify(macro)
        self.assertEqual("BLOCKED_EXTERNAL", risk)
        self.assertIsNone(lane)


if __name__ == "__main__":
    unittest.main()
