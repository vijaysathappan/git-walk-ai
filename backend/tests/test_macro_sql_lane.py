"""SQL fast lane: eligibility narrows correctly, and evaluate() computes
VBA-accurate arithmetic -- including sequential same-iteration dependency
between two assignments, which is why this isn't literally "one SQL
UPDATE" (see sql_lane.py's module docstring)."""

import unittest

from app.macros import sql_lane
from app.macros.parser.statement_parser import parse_sub


class SqlLaneEligibilityTests(unittest.TestCase):
    def test_simple_loop_is_eligible(self):
        sub = parse_sub(
            "Public Sub T()\n"
            "    For i = 2 To 4\n"
            "        Cells(i, 5).Value = Cells(i, 3).Value * Cells(i, 4).Value\n"
            "    Next i\n"
            "End Sub\n"
        )
        eligible, reason = sql_lane.is_eligible(sub)
        self.assertTrue(eligible, reason)

    def test_no_loop_is_not_eligible(self):
        sub = parse_sub("Public Sub T()\n    Cells(1, 1).Value = 1\nEnd Sub\n")
        eligible, reason = sql_lane.is_eligible(sub)
        self.assertFalse(eligible)
        self.assertIn("single For loop", reason)

    def test_writing_a_fixed_row_is_not_eligible(self):
        sub = parse_sub("Public Sub T()\n    For i = 2 To 4\n        Cells(1, 1).Value = Cells(i, 1).Value\n    Next i\nEnd Sub\n")
        eligible, reason = sql_lane.is_eligible(sub)
        self.assertFalse(eligible)

    def test_negative_step_is_eligible(self):
        sub = parse_sub("Public Sub T()\n    For i = 4 To 2 Step -1\n        Cells(i, 2).Value = Cells(i, 1).Value\n    Next i\nEnd Sub\n")
        eligible, reason = sql_lane.is_eligible(sub)
        self.assertTrue(eligible, reason)


class SqlLaneEvaluateTests(unittest.TestCase):
    def test_computes_simple_arithmetic_per_row(self):
        sub = parse_sub(
            "Public Sub T()\n"
            "    For i = 2 To 4\n"
            "        Cells(i, 5).Value = Cells(i, 3).Value * Cells(i, 4).Value\n"
            "    Next i\n"
            "End Sub\n"
        )
        plan_ = sql_lane.plan(sub)
        row_values = {2: {3: 2, 4: 10}, 3: {3: 3, 4: 5}, 4: {3: 1, 4: 4}}
        results = sql_lane.evaluate(plan_, row_values)
        self.assertEqual({5: 20}, results[2])
        self.assertEqual({5: 15}, results[3])
        self.assertEqual({5: 4}, results[4])

    def test_second_assignment_sees_first_assignments_new_value(self):
        sub = parse_sub(
            "Public Sub T()\n"
            "    For i = 2 To 2\n"
            "        Cells(i, 5).Value = Cells(i, 3).Value * Cells(i, 4).Value\n"
            "        Cells(i, 6).Value = Cells(i, 5).Value + 1\n"
            "    Next i\n"
            "End Sub\n"
        )
        plan_ = sql_lane.plan(sub)
        row_values = {2: {3: 2, 4: 10}}
        results = sql_lane.evaluate(plan_, row_values)
        # col 5 = 2*10 = 20; col 6 must see the NEW col5 (20), not a stale/absent value
        self.assertEqual(20, results[2][5])
        self.assertEqual(21, results[2][6])

    def test_referenced_columns_includes_reads_and_writes(self):
        sub = parse_sub(
            "Public Sub T()\n"
            "    For i = 2 To 2\n"
            "        Cells(i, 5).Value = Cells(i, 3).Value * Cells(i, 4).Value\n"
            "    Next i\n"
            "End Sub\n"
        )
        plan_ = sql_lane.plan(sub)
        self.assertEqual({3, 4, 5}, sql_lane.referenced_columns(plan_))

    def test_excel_rows_handles_negative_step(self):
        sub = parse_sub("Public Sub T()\n    For i = 4 To 2 Step -1\n        Cells(i, 1).Value = 1\n    Next i\nEnd Sub\n")
        plan_ = sql_lane.plan(sub)
        self.assertEqual([4, 3, 2], sql_lane.excel_rows(plan_))


if __name__ == "__main__":
    unittest.main()
