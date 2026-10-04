"""Fast lane: recognizes the common "per-row calculation" macro shape --
a single ``For`` loop over a literal row range, whose body is one or more
``Cells(i, <const column>).Value = <expr>`` assignments, where every cell
the expression reads is on the SAME row ``i`` (no fixed/absolute-row
references, no cross-iteration state, no structural operations).

Eligibility is intentionally narrow and named as such (see plan()'s
docstring) -- anything outside it is not a bug to fix here, it is Phase
3's general interpreter's job. Evaluation reads every affected row via
ONE bulk SQL SELECT (bounded by 1 round trip, not row count) and computes
every assignment with plain Python arithmetic -- no per-row queries, no
loop/branch interpretation, hence "fast lane": no "logic lag" regardless
of how many rows are affected.

Nothing here writes to the database. It only computes the
{sheet_id, row_id, column_id, old_value, new_value} change list that
app.macros.run_service hands to the exact same commit_semantic_delta every
other Git Walk edit goes through -- so normalization, audit, and the
"skip if value is unchanged" rule are the product's existing ones, never
duplicated or reimplemented here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .parser import ast as A

_ARITHMETIC_OPS = {"+", "-", "*", "/", "\\", "mod", "^"}


class NotEligibleError(ValueError):
    """Raised with a precise, user-facing reason -- never silently caught
    and turned into a generic message."""


@dataclass(frozen=True)
class _PlannedAssignment:
    column_index: int  # 1-based Excel column, as written in Cells(row, N)
    expr: A.Expr
    line: int


@dataclass(frozen=True)
class _Plan:
    start_row: int  # 1-based Excel row number
    end_row: int
    step: int
    assignments: list[_PlannedAssignment]


def _match_cell_value(expr: A.Expr) -> tuple[A.Expr, A.Expr] | None:
    """Matches the general-AST shape of ``Cells(row, col).Value`` --
    ``MemberAccess(Call(Identifier("Cells"), [row, col]), "Value")`` --
    returning (row_expr, col_expr), or None if `expr` isn't that shape."""
    if not (isinstance(expr, A.MemberAccess) and expr.name.lower() == "value"):
        return None
    call = expr.base
    if not (isinstance(call, A.Call) and len(call.args) == 2):
        return None
    if not (isinstance(call.base, A.Identifier) and call.base.name.lower() == "cells"):
        return None
    return call.args[0], call.args[1]


def _fold_int(expr: A.Expr) -> int:
    """Evaluates an expression that must be a compile-time integer
    constant (loop bounds, column indices) -- literals and +/-/* combined
    with unary minus only. Anything else (a variable, a function call, a
    property chain like `.End(xlUp).Row`) is not something the fast lane
    can resolve without running code, so it is not eligible here."""
    if isinstance(expr, A.Literal) and isinstance(expr.value, (int, float)):
        return int(expr.value)
    if isinstance(expr, A.UnaryOp) and expr.op == "-":
        return -_fold_int(expr.operand)
    if isinstance(expr, A.BinOp) and expr.op in ("+", "-", "*"):
        left, right = _fold_int(expr.left), _fold_int(expr.right)
        return {"+": left + right, "-": left - right, "*": left * right}[expr.op]
    raise NotEligibleError("loop bounds and column indices must be constant numbers")


def _check_same_row_expr(expr: A.Expr, loop_var: str) -> None:
    """Walks an expression, requiring every Cells() reference to read the
    SAME row as the loop variable (no fixed-row lookups -- those need real
    execution, not a single SQL statement) and every bare variable
    reference to be the loop variable itself (no other free variables --
    that would be cross-iteration state, which is out of scope here)."""
    if isinstance(expr, A.Literal):
        return
    cell_ref = _match_cell_value(expr)
    if cell_ref is not None:
        row_expr, col_expr = cell_ref
        if not (isinstance(row_expr, A.Identifier) and row_expr.name.lower() == loop_var.lower()):
            raise NotEligibleError("reading a cell on a fixed row (not the loop counter) is not supported in the fast lane yet")
        _fold_int(col_expr)  # column must resolve to a constant
        return
    if isinstance(expr, A.Identifier):
        if expr.name.lower() != loop_var.lower():
            raise NotEligibleError(f"variable '{expr.name}' is not supported in the fast lane (only the loop counter and cell references are)")
        return
    if isinstance(expr, A.UnaryOp):
        _check_same_row_expr(expr.operand, loop_var)
        return
    if isinstance(expr, A.BinOp):
        if expr.op not in _ARITHMETIC_OPS:
            raise NotEligibleError(f"operator '{expr.op}' is not supported in the fast lane (arithmetic only)")
        _check_same_row_expr(expr.left, loop_var)
        _check_same_row_expr(expr.right, loop_var)
        return
    if isinstance(expr, (A.Call, A.MemberAccess)):
        raise NotEligibleError("only Cells(row, col).Value references are supported in the fast lane (no other property/method calls)")
    raise NotEligibleError("expression form is not supported in the fast lane yet")


def plan(sub: A.SubDecl) -> _Plan:
    """Raises NotEligibleError with a precise reason if `sub` isn't in the
    fast-lane shape; otherwise returns the resolved plan."""
    stmts = [s for s in sub.body if not isinstance(s, A.DimStmt)]
    if len(stmts) != 1 or not isinstance(stmts[0], A.ForStmt):
        raise NotEligibleError("the fast lane only supports a Sub body that is a single For loop (optionally preceded by Dim statements)")
    for_stmt = stmts[0]
    if not for_stmt.body or not all(isinstance(s, A.AssignStmt) for s in for_stmt.body):
        raise NotEligibleError("the fast lane only supports assignment statements inside the loop")

    start_row = _fold_int(for_stmt.start)
    end_row = _fold_int(for_stmt.end)
    step = _fold_int(for_stmt.step) if for_stmt.step is not None else 1
    if step == 0:
        raise NotEligibleError("a loop step of 0 never terminates")
    if start_row < 2 or end_row < 2:
        raise NotEligibleError("row 1 is the header row and has no data to update")

    assignments: list[_PlannedAssignment] = []
    for assign in for_stmt.body:
        cell_ref = _match_cell_value(assign.target)
        if cell_ref is None:
            raise NotEligibleError("the fast lane only supports assigning to Cells(row, col).Value")
        row_expr, col_expr = cell_ref
        if not (isinstance(row_expr, A.Identifier) and row_expr.name.lower() == for_stmt.var.lower()):
            raise NotEligibleError("the fast lane only supports writing to the loop's own row (Cells(i, ...))")
        column_index = _fold_int(col_expr)
        _check_same_row_expr(assign.value, for_stmt.var)
        assignments.append(_PlannedAssignment(column_index=column_index, expr=assign.value, line=assign.line))
    return _Plan(start_row=start_row, end_row=end_row, step=step, assignments=assignments)


def is_eligible(sub: A.SubDecl) -> tuple[bool, str | None]:
    try:
        plan(sub)
        return True, None
    except NotEligibleError as exc:
        return False, str(exc)


def excel_rows(plan_: _Plan) -> list[int]:
    return list(range(plan_.start_row, plan_.end_row + (1 if plan_.step > 0 else -1), plan_.step))


def referenced_columns(plan_: _Plan) -> set[int]:
    """Every 1-based Excel column index the plan ever reads or writes --
    what run_service needs to fetch in its one bulk SELECT."""
    columns: set[int] = {assignment.column_index for assignment in plan_.assignments}

    def _walk(expr: A.Expr) -> None:
        cell_ref = _match_cell_value(expr)
        if cell_ref is not None:
            columns.add(_fold_int(cell_ref[1]))
        elif isinstance(expr, A.UnaryOp):
            _walk(expr.operand)
        elif isinstance(expr, A.BinOp):
            _walk(expr.left)
            _walk(expr.right)

    for assignment in plan_.assignments:
        _walk(assignment.expr)
    return columns


# ---------------------------------------------------------------------------
# Evaluation -- one bulk read (run_service fetches every referenced column
# for every affected row in a single SELECT), then this pure-Python pass
# computes each assignment's new values, in program order, across ALL rows
# at once. A later assignment in the same loop body sees an EARLIER one's
# just-computed value for the same row (via `results`), exactly like VBA
# executes statements in sequence -- this is why evaluation can't simply be
# "one SQL UPDATE": SQL's SET col_a=x, col_b=y evaluates both right-hand
# sides against the pre-update row, which would silently disagree with VBA
# whenever one assignment reads a column an earlier one in the same loop
# just wrote. Arithmetic itself is VBA's, not SQL's (integer division,
# Mod, and type coercion have different rules in each) -- this is what
# makes the fast lane numerically exact, not just fast.
# ---------------------------------------------------------------------------

def evaluate(plan_: _Plan, row_values: dict[int, dict[int, Any]]) -> dict[int, dict[int, Any]]:
    """row_values: {excel_row: {excel_col: current_value}}, covering every
    column in referenced_columns(plan_) for every row in excel_rows(plan_).
    Returns {excel_row: {column_index: new_value}}."""
    results: dict[int, dict[int, Any]] = {row: {} for row in excel_rows(plan_)}
    for assignment in plan_.assignments:
        for row, computed in results.items():
            context = {**row_values.get(row, {}), **computed}
            computed[assignment.column_index] = _eval_expr(assignment.expr, context)
    return results


def _eval_expr(expr: A.Expr, context: dict[int, Any]) -> Any:
    if isinstance(expr, A.Literal):
        return expr.value
    cell_ref = _match_cell_value(expr)
    if cell_ref is not None:
        column_index = _fold_int(cell_ref[1])
        if column_index not in context:
            raise ValueError(f"no value available for column {column_index}")
        return context[column_index]
    if isinstance(expr, A.UnaryOp):
        return -_as_number(_eval_expr(expr.operand, context))
    if isinstance(expr, A.BinOp):
        left = _as_number(_eval_expr(expr.left, context))
        right = _as_number(_eval_expr(expr.right, context))
        if expr.op == "+":
            return left + right
        if expr.op == "-":
            return left - right
        if expr.op == "*":
            return left * right
        if expr.op == "/":
            return left / right
        if expr.op == "\\":  # VBA integer division: truncate operands, then truncate the quotient toward zero
            return math.trunc(math.trunc(left) / math.trunc(right))
        if expr.op == "mod":  # VBA Mod: result takes the sign of the dividend (truncated division remainder)
            return left - right * math.trunc(left / right)
        if expr.op == "^":
            return left ** right
    raise ValueError(f"unsupported expression node {expr!r}")


def _as_number(value: Any) -> float:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return value
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cannot use {value!r} in arithmetic") from exc
