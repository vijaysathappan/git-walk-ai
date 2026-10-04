"""The general-lane interpreter: statement/expression execution against the
virtual workbook, and the static check_safety() gate that must reject
anything outside the closed-world object model before execution ever
starts. Golden-fixture coverage climbs from a single assignment up to the
"COMPLEX_SUMMARY_MACRO" shape discussed in the Virtual Run design --
multi-sheet lookup, Dictionary-based aggregation, and new-sheet creation --
always with fixed/Dim'd loop bounds (never `.End(xlUp).Row`, which is
permanently out of scope)."""

import tempfile
import unittest
import uuid
from pathlib import Path

from app import database
from app.macros import interpreter
from app.macros.parser.statement_parser import parse_sub
from app.macros.virtual_workbook import InterpreterError, VirtualWorkbook


class CheckSafetyTests(unittest.TestCase):
    def test_simple_safe_macro_passes(self):
        sub = parse_sub('Public Sub T()\n    Cells(1, 1).Value = 42\nEnd Sub\n')
        safe, reasons = interpreter.check_safety(sub)
        self.assertTrue(safe, reasons)
        self.assertEqual([], reasons)

    def test_dictionary_aggregation_pattern_passes(self):
        sub = parse_sub(
            "Public Sub T()\n"
            "    Dim totals As Object\n"
            "    Dim i As Long, category As String, amount As Double\n"
            "    Set totals = CreateObject(\"Scripting.Dictionary\")\n"
            "    For i = 2 To 4\n"
            "        category = Cells(i, 1).Value\n"
            "        amount = Cells(i, 2).Value\n"
            "        If totals.Exists(category) Then\n"
            "            totals(category) = totals(category) + amount\n"
            "        Else\n"
            "            totals.Add category, amount\n"
            "        End If\n"
            "    Next i\n"
            "End Sub\n"
        )
        safe, reasons = interpreter.check_safety(sub)
        self.assertTrue(safe, reasons)

    def test_shell_call_is_syntactically_safe_but_would_be_caught_upstream(self):
        # check_safety only name-checks against the object model; the
        # dangerous-keyword scan in static_gate runs BEFORE check_safety is
        # ever reached (see static_gate.classify's module docstring) and
        # would reject this first. In isolation, "Shell" is simply an
        # unresolved identifier here, which check_safety also rejects --
        # belt and suspenders, not a single point of failure.
        sub = parse_sub('Public Sub T()\n    Shell "cmd.exe"\nEnd Sub\n')
        safe, reasons = interpreter.check_safety(sub)
        self.assertFalse(safe)
        self.assertIn("shell", reasons[0].lower())

    def test_createobject_with_a_different_target_is_rejected(self):
        sub = parse_sub('Public Sub T()\n    Dim x As Object\n    Set x = CreateObject("Excel.Application")\nEnd Sub\n')
        safe, reasons = interpreter.check_safety(sub)
        self.assertFalse(safe)
        self.assertTrue(any("CreateObject" in r for r in reasons))

    def test_unsupported_member_access_is_rejected(self):
        sub = parse_sub('Public Sub T()\n    Cells(1, 1).Select\nEnd Sub\n')
        safe, reasons = interpreter.check_safety(sub)
        self.assertFalse(safe)
        self.assertIn("select", reasons[0].lower())

    def test_worksheets_and_rows_and_columns_are_recognized_globals(self):
        sub = parse_sub(
            "Public Sub T()\n"
            "    Dim n As Long\n"
            "    n = Worksheets(\"Data\").Rows.Count\n"
            "    Cells(1, 1).Value = n\n"
            "End Sub\n"
        )
        safe, reasons = interpreter.check_safety(sub)
        self.assertTrue(safe, reasons)

    def test_for_each_loop_variable_is_a_recognized_local(self):
        sub = parse_sub(
            "Public Sub T()\n"
            "    Dim totals As Object, key As Variant\n"
            "    Set totals = CreateObject(\"Scripting.Dictionary\")\n"
            "    For Each key In totals.Keys\n"
            "        Cells(1, 1).Value = key\n"
            "    Next key\n"
            "End Sub\n"
        )
        safe, reasons = interpreter.check_safety(sub)
        self.assertTrue(safe, reasons)


class _InterpreterTestCase(unittest.TestCase):
    """Base fixture: a two-sheet branch (Data, Rates) with real row/column
    identity so translated SemanticChanges can be inspected precisely."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "interp.db"
        database.initialize_product_schema()

        self.owner = database.get_or_create_user(f"owner_{uuid.uuid4().hex[:8]}@example.com")
        self.table_id = "QUEUE_BOARD_INTERP"
        conn = database._get_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{self.table_id}"')
            conn.execute(f'''
                CREATE TABLE "{self.table_id}" (
                    ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    CATEGORY TEXT, AMOUNT INTEGER, DOUBLED INTEGER
                )
            ''')
            conn.executemany(
                f'INSERT INTO "{self.table_id}" (CATEGORY, AMOUNT, DOUBLED) VALUES (?, ?, ?)',
                [("Food", 10, None), ("Travel", 20, None), ("Food", 5, None)],
            )
            conn.commit()
        finally:
            conn.close()
        database.register_dataset(self.table_id, self.owner["user_id"], "interp.xlsx", 3, 3)
        repository = database.get_repository(self.table_id, self.owner["user_id"])
        self.repository_id = repository["repository_id"]

        working_copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"], branch_mode="new")
        self.branch_id = working_copy["branch_id"]
        self.branch_table_id = working_copy["table_id"]

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _workbook(self) -> VirtualWorkbook:
        conn = database._get_connection()
        try:
            return VirtualWorkbook(conn, branch_id=self.branch_id, repository_id=self.repository_id, data_table_id=self.branch_table_id)
        finally:
            conn.close()

    def _run(self, source: str, limits: interpreter.ExecutionLimits | None = None) -> VirtualWorkbook:
        wb = self._workbook()
        sub = parse_sub(source)
        interpreter.run(sub, wb, limits)
        return wb


class SimpleExecutionTests(_InterpreterTestCase):
    def test_single_assignment(self):
        wb = self._run('Public Sub T()\n    Cells(2, 3).Value = 999\nEnd Sub\n')
        changes = wb.pending_changes()
        self.assertEqual(1, len(changes))
        self.assertEqual(999, changes[0]["new_value"])

    def test_for_loop_with_fixed_row_reference(self):
        # Exactly the shape the SQL fast lane rejects (a fixed-row lookup
        # inside the loop) but the interpreter runs correctly.
        source = (
            "Public Sub T()\n"
            "    Dim i As Long\n"
            "    For i = 2 To 4\n"
            "        Cells(i, 3).Value = Cells(i, 2).Value + Cells(2, 2).Value\n"
            "    Next i\n"
            "End Sub\n"
        )
        wb = self._run(source)
        self.assertEqual(20, wb.cells(2, 3).value)  # 10 + 10
        self.assertEqual(30, wb.cells(3, 3).value)  # 20 + 10
        self.assertEqual(15, wb.cells(4, 3).value)  # 5 + 10

    def test_if_elseif_else_branches(self):
        source = (
            "Public Sub T()\n"
            "    Dim i As Long\n"
            "    For i = 2 To 4\n"
            "        If Cells(i, 2).Value > 15 Then\n"
            "            Cells(i, 3).Value = \"high\"\n"
            "        ElseIf Cells(i, 2).Value > 8 Then\n"
            "            Cells(i, 3).Value = \"medium\"\n"
            "        Else\n"
            "            Cells(i, 3).Value = \"low\"\n"
            "        End If\n"
            "    Next i\n"
            "End Sub\n"
        )
        wb = self._run(source)
        self.assertEqual("medium", wb.cells(2, 3).value)  # 10
        self.assertEqual("high", wb.cells(3, 3).value)    # 20
        self.assertEqual("low", wb.cells(4, 3).value)      # 5

    def test_do_while_loop(self):
        source = (
            "Public Sub T()\n"
            "    Dim i As Long\n"
            "    i = 2\n"
            "    Do While i <= 4\n"
            "        Cells(i, 3).Value = Cells(i, 2).Value * 2\n"
            "        i = i + 1\n"
            "    Loop\n"
            "End Sub\n"
        )
        wb = self._run(source)
        self.assertEqual(20, wb.cells(2, 3).value)
        self.assertEqual(40, wb.cells(3, 3).value)
        self.assertEqual(10, wb.cells(4, 3).value)

    def test_do_until_loop(self):
        source = (
            "Public Sub T()\n"
            "    Dim i As Long\n"
            "    i = 2\n"
            "    Do Until i > 4\n"
            "        Cells(i, 3).Value = 1\n"
            "        i = i + 1\n"
            "    Loop\n"
            "End Sub\n"
        )
        wb = self._run(source)
        self.assertEqual(1, wb.cells(2, 3).value)
        self.assertEqual(1, wb.cells(4, 3).value)

    def test_exit_for_stops_the_loop_early(self):
        source = (
            "Public Sub T()\n"
            "    Dim i As Long\n"
            "    For i = 2 To 4\n"
            "        If Cells(i, 2).Value = 20 Then\n"
            "            Exit For\n"
            "        End If\n"
            "        Cells(i, 3).Value = 1\n"
            "    Next i\n"
            "End Sub\n"
        )
        wb = self._run(source)
        self.assertEqual(1, wb.cells(2, 3).value)
        self.assertIsNone(wb.cells(3, 3).value)  # loop exited before writing row 3
        self.assertIsNone(wb.cells(4, 3).value)

    def test_string_builtins(self):
        source = (
            "Public Sub T()\n"
            "    Cells(2, 3).Value = UCase(Left(Cells(2, 1).Value, 2))\n"
            "End Sub\n"
        )
        wb = self._run(source)
        self.assertEqual("FO", wb.cells(2, 3).value)

    def test_vba_integer_division_and_mod(self):
        source = (
            "Public Sub T()\n"
            "    Cells(1, 1).Value = 7 \\ 2\n"
            "    Cells(1, 2).Value = -7 Mod 2\n"
            "End Sub\n"
        )
        wb = self._run(source)
        self.assertEqual(3, wb.cells(1, 1).value)
        self.assertEqual(-1, wb.cells(1, 2).value)  # VBA Mod takes the dividend's sign

    def test_on_error_resume_next_suppresses_a_runtime_error_and_continues(self):
        source = (
            "Public Sub T()\n"
            "    On Error Resume Next\n"
            "    Worksheets(\"DoesNotExist\").Delete\n"
            "    Cells(1, 1).Value = \"reached\"\n"
            "End Sub\n"
        )
        wb = self._run(source)
        self.assertEqual("reached", wb.cells(1, 1).value)

    def test_without_on_error_resume_next_a_runtime_error_propagates(self):
        wb = self._workbook()
        sub = parse_sub('Public Sub T()\n    Worksheets("DoesNotExist").Delete\nEnd Sub\n')
        with self.assertRaises(InterpreterError):
            interpreter.run(sub, wb)

    def test_statement_limit_is_enforced(self):
        source = (
            "Public Sub T()\n"
            "    Dim i As Long\n"
            "    For i = 1 To 1000\n"
            "        Cells(1, 1).Value = i\n"
            "    Next i\n"
            "End Sub\n"
        )
        wb = self._workbook()
        sub = parse_sub(source)
        with self.assertRaises(interpreter.ExecutionLimitError):
            interpreter.run(sub, wb, interpreter.ExecutionLimits(max_statements=10))


class ComplexSummaryMacroTests(_InterpreterTestCase):
    """Mirrors the "COMPLEX_SUMMARY_MACRO" scenario from the design
    conversation: aggregate a data sheet by category into a brand-new
    summary sheet via a Scripting.Dictionary. The loop bound is a fixed
    Dim'd constant (3 data rows), never `.End(xlUp).Row`, which is
    permanently out of scope regardless of complexity."""

    MACRO = (
        "Public Sub Summarize()\n"
        "    Dim totals As Object\n"
        "    Dim i As Long, lastRow As Long\n"
        "    Dim category As String, amount As Double\n"
        "    Dim summary As Worksheet\n"
        "    Set totals = CreateObject(\"Scripting.Dictionary\")\n"
        "    lastRow = 4\n"
        "    For i = 2 To lastRow\n"
        "        category = Cells(i, 1).Value\n"
        "        amount = Cells(i, 2).Value\n"
        "        If totals.Exists(category) Then\n"
        "            totals(category) = totals(category) + amount\n"
        "        Else\n"
        "            totals.Add category, amount\n"
        "        End If\n"
        "    Next i\n"
        "    Set summary = Worksheets.Add()\n"
        "    summary.Cells(1, 1).Value = \"Category\"\n"
        "    summary.Cells(1, 2).Value = \"Total\"\n"
        "    Dim key As Variant\n"
        "    Dim r As Long\n"
        "    r = 2\n"
        "    For Each key In totals.Keys\n"
        "        summary.Cells(r, 1).Value = key\n"
        "        summary.Cells(r, 2).Value = totals(key)\n"
        "        r = r + 1\n"
        "    Next key\n"
        "End Sub\n"
    )

    def test_check_safety_passes(self):
        sub = parse_sub(self.MACRO)
        safe, reasons = interpreter.check_safety(sub)
        self.assertTrue(safe, reasons)

    def test_aggregates_correctly_into_a_new_sheet(self):
        wb = self._run(self.MACRO)
        changes = wb.pending_changes()
        creates = [c for c in changes if c["operation_type"] == "SHEET_CREATE"]
        columns = {c["new_value"]: c for c in changes if c["operation_type"] == "COLUMN_INSERT"}
        rows = [c for c in changes if c["operation_type"] == "ROW_INSERT"]
        self.assertEqual(1, len(creates))
        self.assertEqual({"CATEGORY", "TOTAL"}, set(columns))
        self.assertEqual(2, len(rows))

        by_category = {}
        for row_change in rows:
            values = row_change["new_value"]
            by_category[values[columns["CATEGORY"]["column_id"]]] = values[columns["TOTAL"]["column_id"]]
        self.assertEqual({"Food": 15.0, "Travel": 20.0}, by_category)

        # The source sheet itself is untouched -- this macro only reads it.
        source_changes = [c for c in changes if c["sheet_id"] == wb.worksheet(1).sheet_id]
        self.assertEqual([], source_changes)


if __name__ == "__main__":
    unittest.main()
