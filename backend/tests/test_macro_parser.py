"""VBA statement parser: the supported subset parses into the expected
AST shape, and unsupported constructs fail closed with a line number --
never silently accepted or approximated."""

import unittest

from app.macros.parser import ast as A
from app.macros.parser.statement_parser import ParseError, parse_sub


class ParserTests(unittest.TestCase):
    def test_parses_simple_loop_with_arithmetic(self):
        sub = parse_sub(
            "Public Sub Recalc()\n"
            "    Dim i As Long\n"
            "    For i = 2 To 4\n"
            "        Cells(i, 5).Value = Cells(i, 3).Value * Cells(i, 4).Value\n"
            "    Next i\n"
            "End Sub\n"
        )
        self.assertEqual("Recalc", sub.name)
        stmts = [s for s in sub.body if not isinstance(s, A.DimStmt)]
        self.assertEqual(1, len(stmts))
        for_stmt = stmts[0]
        self.assertIsInstance(for_stmt, A.ForStmt)
        self.assertEqual("i", for_stmt.var)
        self.assertEqual(A.Literal(2), for_stmt.start)
        self.assertEqual(A.Literal(4), for_stmt.end)
        self.assertEqual(1, len(for_stmt.body))
        assign = for_stmt.body[0]
        self.assertIsInstance(assign, A.AssignStmt)

        def cell(row, col):
            return A.MemberAccess(A.Call(A.Identifier("Cells"), [A.Identifier(row) if isinstance(row, str) else A.Literal(row), A.Literal(col)]), "Value")

        self.assertEqual(A.BinOp("*", cell("i", 3), cell("i", 4)), assign.value)

    def test_parses_step_clause(self):
        sub = parse_sub("Public Sub T()\n    For i = 1 To 10 Step 2\n        Cells(i, 1).Value = 1\n    Next i\nEnd Sub\n")
        for_stmt = sub.body[0]
        self.assertEqual(A.Literal(2), for_stmt.step)

    def test_operator_precedence(self):
        sub = parse_sub("Public Sub T()\n    For i = 1 To 1\n        Cells(i, 1).Value = 2 + 3 * 4\n    Next i\nEnd Sub\n")
        assign = sub.body[0].body[0]
        # 2 + (3 * 4), not (2 + 3) * 4
        self.assertEqual(A.BinOp("+", A.Literal(2), A.BinOp("*", A.Literal(3), A.Literal(4))), assign.value)

    def test_parenthesized_expression(self):
        sub = parse_sub("Public Sub T()\n    For i = 1 To 1\n        Cells(i, 1).Value = (2 + 3) * 4\n    Next i\nEnd Sub\n")
        assign = sub.body[0].body[0]
        self.assertEqual(A.BinOp("*", A.BinOp("+", A.Literal(2), A.Literal(3)), A.Literal(4)), assign.value)

    def test_string_literal_with_escaped_quote(self):
        sub = parse_sub('Public Sub T()\n    For i = 1 To 1\n        Cells(i, 1).Value = "say ""hi"""\n    Next i\nEnd Sub\n')
        assign = sub.body[0].body[0]
        self.assertEqual(A.Literal('say "hi"'), assign.value)

    def test_bare_call_statement_parses_as_a_call_stmt(self):
        # Parsing is purely syntactic -- Shell is a perfectly parseable bare
        # statement call. Rejecting it is static_gate's job (the dangerous-
        # keyword scan), not the parser's; see test_macro_static_gate.py.
        sub = parse_sub('Public Sub T()\n    Shell "cmd.exe"\nEnd Sub\n')
        call_stmt = sub.body[0]
        self.assertIsInstance(call_stmt, A.CallStmt)
        self.assertEqual(A.Call(A.Identifier("Shell"), [A.Literal("cmd.exe")]), call_stmt.call)

    def test_parameterized_sub_is_a_parse_error(self):
        with self.assertRaises(ParseError):
            parse_sub("Public Sub T(x As Long)\n    Cells(1, 1).Value = x\nEnd Sub\n")

    def test_unclosed_sub_is_a_parse_error(self):
        with self.assertRaises(ParseError):
            parse_sub("Public Sub T()\n    Cells(1, 1).Value = 1\n")

    def test_line_continuation_is_collapsed_before_tokenizing(self):
        sub = parse_sub(
            "Public Sub T()\n"
            "    For i = 1 To 1\n"
            "        Cells(i, 1).Value = 1 + _\n"
            "            2\n"
            "    Next i\n"
            "End Sub\n"
        )
        assign = sub.body[0].body[0]
        self.assertEqual(A.BinOp("+", A.Literal(1), A.Literal(2)), assign.value)


class Phase3GrammarTests(unittest.TestCase):
    def test_if_elseif_else(self):
        sub = parse_sub(
            "Public Sub T()\n"
            "    If x > 10 Then\n"
            "        Cells(1, 1).Value = 1\n"
            "    ElseIf x > 5 Then\n"
            "        Cells(1, 1).Value = 2\n"
            "    Else\n"
            "        Cells(1, 1).Value = 3\n"
            "    End If\n"
            "End Sub\n"
        )
        if_stmt = sub.body[0]
        self.assertIsInstance(if_stmt, A.IfStmt)
        self.assertEqual(3, len(if_stmt.branches))
        self.assertIsNotNone(if_stmt.branches[0].condition)
        self.assertIsNotNone(if_stmt.branches[1].condition)
        self.assertIsNone(if_stmt.branches[2].condition)  # Else

    def test_do_while_and_do_until(self):
        sub = parse_sub("Public Sub T()\n    Do While x < 10\n        x = x + 1\n    Loop\nEnd Sub\n")
        do_stmt = sub.body[0]
        self.assertIsInstance(do_stmt, A.DoLoopStmt)
        self.assertFalse(do_stmt.negate)

        sub2 = parse_sub("Public Sub T()\n    Do Until x >= 10\n        x = x + 1\n    Loop\nEnd Sub\n")
        self.assertTrue(sub2.body[0].negate)

    def test_for_each(self):
        sub = parse_sub("Public Sub T()\n    For Each key In totals.Keys\n        Cells(1, 1).Value = key\n    Next key\nEnd Sub\n")
        for_each = sub.body[0]
        self.assertIsInstance(for_each, A.ForEachStmt)
        self.assertEqual("key", for_each.var)
        self.assertEqual(A.MemberAccess(A.Identifier("totals"), "Keys"), for_each.iterable)

    def test_set_statement_parses_like_a_plain_assignment(self):
        sub = parse_sub('Public Sub T()\n    Set totals = CreateObject("Scripting.Dictionary")\nEnd Sub\n')
        assign = sub.body[0]
        self.assertIsInstance(assign, A.AssignStmt)
        self.assertEqual(A.Identifier("totals"), assign.target)

    def test_bare_args_call_statement(self):
        sub = parse_sub("Public Sub T()\n    totals.Add category, amount\nEnd Sub\n")
        call_stmt = sub.body[0]
        self.assertIsInstance(call_stmt, A.CallStmt)
        expected = A.Call(A.MemberAccess(A.Identifier("totals"), "Add"), [A.Identifier("category"), A.Identifier("amount")])
        self.assertEqual(expected, call_stmt.call)

    def test_zero_arg_method_call_statement_no_parens(self):
        sub = parse_sub('Public Sub T()\n    Worksheets("Summary").Delete\nEnd Sub\n')
        call_stmt = sub.body[0]
        self.assertIsInstance(call_stmt, A.CallStmt)
        expected = A.MemberAccess(A.Call(A.Identifier("Worksheets"), [A.Literal("Summary")]), "Delete")
        self.assertEqual(expected, call_stmt.call)

    def test_exit_for_exit_do_exit_sub(self):
        sub = parse_sub("Public Sub T()\n    For i = 1 To 10\n        Exit For\n    Next i\n    Exit Sub\nEnd Sub\n")
        for_stmt = sub.body[0]
        self.assertIsInstance(for_stmt.body[0], A.ExitStmt)
        self.assertEqual("For", for_stmt.body[0].kind)
        self.assertEqual("Sub", sub.body[1].kind)

    def test_on_error_resume_next_and_goto_0(self):
        sub = parse_sub(
            "Public Sub T()\n"
            "    On Error Resume Next\n"
            "    Cells(1, 1).Value = 1\n"
            "    On Error GoTo 0\n"
            "End Sub\n"
        )
        self.assertIsInstance(sub.body[0], A.OnErrorStmt)
        self.assertEqual("RESUME_NEXT", sub.body[0].mode)
        self.assertIsInstance(sub.body[2], A.OnErrorStmt)
        self.assertEqual("GOTO_0", sub.body[2].mode)

    def test_colon_separated_statements_on_one_line(self):
        sub = parse_sub('Public Sub T()\n    On Error Resume Next: Worksheets("S").Delete: On Error GoTo 0\nEnd Sub\n')
        self.assertEqual(3, len(sub.body))
        self.assertIsInstance(sub.body[0], A.OnErrorStmt)
        self.assertIsInstance(sub.body[1], A.CallStmt)
        self.assertIsInstance(sub.body[2], A.OnErrorStmt)

    def test_colon_inside_string_literal_is_not_a_statement_separator(self):
        sub = parse_sub('Public Sub T()\n    Cells(1, 1).Value = "12:30"\nEnd Sub\n')
        self.assertEqual(1, len(sub.body))
        self.assertEqual(A.Literal("12:30"), sub.body[0].value)

    def test_multi_variable_dim(self):
        sub = parse_sub("Public Sub T()\n    Dim i As Long, total As Double\n    Cells(1, 1).Value = 1\nEnd Sub\n")
        dim_stmt = sub.body[0]
        self.assertIsInstance(dim_stmt, A.DimStmt)
        self.assertEqual(["i", "total"], dim_stmt.names)

    def test_nested_member_and_index_chain(self):
        sub = parse_sub("Public Sub T()\n    Cells(1, 1).Value = src.Rows.Count\nEnd Sub\n")
        assign = sub.body[0]
        expected = A.MemberAccess(A.MemberAccess(A.Identifier("src"), "Rows"), "Count")
        self.assertEqual(expected, assign.value)

    def test_do_without_while_or_until_is_a_parse_error(self):
        with self.assertRaises(ParseError):
            parse_sub("Public Sub T()\n    Do\n        x = 1\n    Loop\nEnd Sub\n")

    def test_on_error_goto_line_label_is_a_parse_error(self):
        with self.assertRaises(ParseError):
            parse_sub("Public Sub T()\n    On Error GoTo MyLabel\nEnd Sub\n")


if __name__ == "__main__":
    unittest.main()
