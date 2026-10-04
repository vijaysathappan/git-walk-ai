"""The general-lane interpreter: walks a macro's AST statement by
statement, evaluating expressions via plain Python attribute/call
protocols (``getattr``/``setattr``/``__call__``) against the closed-world
objects in virtual_workbook.py. This is what unlocks "mid/high complexity
but in-workbook" macros -- multi-sheet lookups, Dictionary-based
aggregation, nested loops, sheet management -- that the SQL fast lane
deliberately doesn't attempt (see sql_lane.py's module docstring).

Two independent safety mechanisms, not one:
1. ``check_safety()`` -- a STATIC, pre-execution walk (used by
   static_gate.py) that name-checks every identifier/property/method the
   AST references against the exact object-model surface this module
   implements. A macro referencing anything else is rejected before it
   ever runs -- fail closed, never approximated.
2. The object model itself (virtual_workbook.py) has no members for
   anything outside the workbook. Even if #1 had a bug, there is no
   ``Shell``/``FileSystemObject``/network call anywhere to reach.

Bounded, deterministic execution: a statement-count budget and a wall-
clock deadline (MACRO_MAX_STATEMENTS / MACRO_EXECUTION_TIMEOUT_SECONDS)
guarantee a macro can't hang the server, regardless of how it loops.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import virtual_workbook as VW
from .parser import ast as A

_COMPARE_OPS = {"=", "<>", "<", ">", "<=", ">="}


class InterpreterError(VW.InterpreterError):
    pass


class ExecutionLimitError(InterpreterError):
    pass


class _ExitFor(Exception):
    pass


class _ExitDo(Exception):
    pass


class _ExitSub(Exception):
    pass


@dataclass(frozen=True)
class ExecutionLimits:
    max_statements: int = 2_000_000
    timeout_seconds: float = 30.0


# ---------------------------------------------------------------------------
# Built-in functions -- pure string/math/date/type operations only, no I/O.
# ---------------------------------------------------------------------------

def _b_left(s: Any, n: Any) -> str:
    return str(s)[: int(n)]


def _b_right(s: Any, n: Any) -> str:
    n = int(n)
    return str(s)[-n:] if n > 0 else ""


def _b_mid(s: Any, start: Any, length: Any = None) -> str:
    text, start = str(s), int(start)
    return text[start - 1:] if length is None else text[start - 1:start - 1 + int(length)]


def _b_instr(*args: Any) -> int:
    if len(args) == 2:
        start, hay, needle = 1, args[0], args[1]
    else:
        start, hay, needle = args
    return str(hay).find(str(needle), int(start) - 1) + 1


def _b_is_numeric(v: Any) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


_BUILTINS: dict[str, Any] = {
    "left": _b_left, "right": _b_right, "mid": _b_mid,
    "trim": lambda s: str(s).strip(), "ltrim": lambda s: str(s).lstrip(), "rtrim": lambda s: str(s).rstrip(),
    "ucase": lambda s: str(s).upper(), "lcase": lambda s: str(s).lower(),
    "len": lambda s: len(str(s)),
    "instr": _b_instr,
    "replace": lambda s, find, repl: str(s).replace(str(find), str(repl)),
    "split": lambda s, delim=" ": str(s).split(str(delim)),
    "join": lambda items, delim=" ": str(delim).join(str(x) for x in items),
    "cstr": lambda v: str(v), "cint": lambda v: int(round(float(v))), "cdbl": lambda v: float(v),
    "clng": lambda v: int(round(float(v))), "cbool": lambda v: bool(v),
    "isempty": lambda v: v is None, "isnumeric": _b_is_numeric,
    "now": lambda: datetime.now(), "date": lambda: datetime.now().date(),
}

#: Property/method names the interpreter's object model implements.
#: Anything not in this set fails closed at static-gate time, before
#: execution -- see check_safety() below. Notably absent: cell formatting
#: (.Interior/.Color/.NumberFormat) -- the product's own commit model only
#: carries a STYLE_HASH fingerprint for diff *detection* (see
#: app.excel.formulas.style_hash), with no path to store or replay actual
#: style content server-side, so a macro format write has nothing correct
#: to commit yet. Rejecting it here is the fail-closed choice over
#: silently losing the write, which pending_changes() would otherwise do.
_SAFE_MEMBERS = {
    "value", "name", "cells",
    "rows", "columns", "count", "delete",
    "add", "exists", "item", "keys",
}

_SAFE_GLOBALS = {"cells", "worksheets", "sheets", "rows", "columns", "createobject", *_BUILTINS}


# ---------------------------------------------------------------------------
# Static safety check -- see module docstring, mechanism #1.
# ---------------------------------------------------------------------------

def _collect_locals(stmts: list[A.Stmt], locals_: set[str]) -> None:
    for stmt in stmts:
        if isinstance(stmt, A.DimStmt):
            locals_.update(name.lower() for name in stmt.names)
        elif isinstance(stmt, A.ForStmt):
            locals_.add(stmt.var.lower())
            _collect_locals(stmt.body, locals_)
        elif isinstance(stmt, A.ForEachStmt):
            locals_.add(stmt.var.lower())
            _collect_locals(stmt.body, locals_)
        elif isinstance(stmt, A.DoLoopStmt):
            _collect_locals(stmt.body, locals_)
        elif isinstance(stmt, A.IfStmt):
            for branch in stmt.branches:
                _collect_locals(branch.body, locals_)
        elif isinstance(stmt, A.AssignStmt):
            base: A.Expr = stmt.target
            while isinstance(base, (A.MemberAccess, A.Call)):
                base = base.base
            if isinstance(base, A.Identifier):
                locals_.add(base.name.lower())


def check_safety(sub: A.SubDecl) -> tuple[bool, list[str]]:
    """Returns (safe, reasons). Never raises -- a bad reference is a
    reason string, not an exception, so callers can report every problem
    found rather than just the first one."""
    locals_: set[str] = set()
    _collect_locals(sub.body, locals_)
    reasons: list[str] = []

    def check_expr(expr: A.Expr) -> None:
        if isinstance(expr, A.Literal):
            return
        if isinstance(expr, A.Identifier):
            name = expr.name.lower()
            if name not in locals_ and name not in _SAFE_GLOBALS:
                reasons.append(f"'{expr.name}' is not a supported name")
            return
        if isinstance(expr, A.MemberAccess):
            check_expr(expr.base)
            if expr.name.lower() not in _SAFE_MEMBERS:
                reasons.append(f"'.{expr.name}' is not a supported property or method")
            return
        if isinstance(expr, A.Call):
            if isinstance(expr.base, A.Identifier) and expr.base.name.lower() == "createobject":
                arg_ok = (
                    len(expr.args) == 1 and isinstance(expr.args[0], A.Literal)
                    and str(expr.args[0].value).strip().lower() == "scripting.dictionary"
                )
                if not arg_ok:
                    reasons.append('CreateObject is only supported for "Scripting.Dictionary"')
                return
            check_expr(expr.base)
            for arg in expr.args:
                check_expr(arg)
            return
        if isinstance(expr, A.UnaryOp):
            check_expr(expr.operand)
            return
        if isinstance(expr, A.BinOp):
            check_expr(expr.left)
            check_expr(expr.right)
            return
        reasons.append(f"unsupported expression form {type(expr).__name__}")

    def check_stmts(stmts: list[A.Stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, A.DimStmt):
                continue
            elif isinstance(stmt, A.AssignStmt):
                check_expr(stmt.target)
                check_expr(stmt.value)
            elif isinstance(stmt, A.CallStmt):
                check_expr(stmt.call)
            elif isinstance(stmt, A.ForStmt):
                check_expr(stmt.start)
                check_expr(stmt.end)
                if stmt.step is not None:
                    check_expr(stmt.step)
                check_stmts(stmt.body)
            elif isinstance(stmt, A.ForEachStmt):
                check_expr(stmt.iterable)
                check_stmts(stmt.body)
            elif isinstance(stmt, A.DoLoopStmt):
                check_expr(stmt.condition)
                check_stmts(stmt.body)
            elif isinstance(stmt, A.IfStmt):
                for branch in stmt.branches:
                    if branch.condition is not None:
                        check_expr(branch.condition)
                    check_stmts(branch.body)
            elif isinstance(stmt, (A.ExitStmt, A.OnErrorStmt)):
                continue
            else:
                reasons.append(f"unsupported statement form {type(stmt).__name__}")

    check_stmts(sub.body)
    return (len(reasons) == 0, reasons)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class _Interpreter:
    def __init__(self, workbook: VW.VirtualWorkbook, limits: ExecutionLimits):
        self.workbook = workbook
        self.limits = limits
        self.scope: dict[str, Any] = {
            "cells": workbook.cells,
            "worksheets": VW.VirtualWorksheets(workbook),
            "sheets": VW.VirtualWorksheets(workbook),
            "createobject": self._create_object,
            **_BUILTINS,
        }
        self.on_error_resume = False
        self.statement_count = 0
        self.deadline = time.monotonic() + limits.timeout_seconds

    @staticmethod
    def _create_object(target: str) -> VW.VBADictionary:
        if str(target).strip().lower() != "scripting.dictionary":
            raise InterpreterError(f'CreateObject("{target}") is not supported -- only "Scripting.Dictionary" is')
        return VW.VBADictionary()

    def _tick(self) -> None:
        self.statement_count += 1
        if self.statement_count > self.limits.max_statements:
            raise ExecutionLimitError(f"This macro exceeded the {self.limits.max_statements}-statement execution limit.")
        if self.statement_count % 200 == 0 and time.monotonic() > self.deadline:
            raise ExecutionLimitError(f"This macro exceeded the {self.limits.timeout_seconds}s execution time limit.")

    # ---- expressions ----------------------------------------------------

    def eval_expr(self, expr: A.Expr) -> Any:
        if isinstance(expr, A.Literal):
            return expr.value
        if isinstance(expr, A.Identifier):
            name = expr.name.lower()
            if name in self.scope:
                return self.scope[name]
            raise InterpreterError(f"'{expr.name}' is not defined (use Dim to declare it first)")
        if isinstance(expr, A.MemberAccess):
            base = self.eval_expr(expr.base)
            try:
                return getattr(base, expr.name.lower())
            except AttributeError as exc:
                raise InterpreterError(f"'.{expr.name}' is not supported on this object") from exc
        if isinstance(expr, A.Call):
            base = self.eval_expr(expr.base)
            args = [self.eval_expr(a) for a in expr.args]
            if not callable(base):
                raise InterpreterError("this expression is not callable")
            try:
                return base(*args)
            except TypeError as exc:
                raise InterpreterError(f"wrong number/type of arguments: {exc}") from exc
        if isinstance(expr, A.UnaryOp):
            value = self.eval_expr(expr.operand)
            if expr.op == "-":
                return -_as_number(value)
            if expr.op == "Not":
                return not _truthy(value)
        if isinstance(expr, A.BinOp):
            return self._eval_binop(expr)
        raise InterpreterError(f"unsupported expression node {type(expr).__name__}")

    def _eval_binop(self, expr: A.BinOp) -> Any:
        if expr.op == "And":
            return _truthy(self.eval_expr(expr.left)) and _truthy(self.eval_expr(expr.right))
        if expr.op == "Or":
            return _truthy(self.eval_expr(expr.left)) or _truthy(self.eval_expr(expr.right))
        left, right = self.eval_expr(expr.left), self.eval_expr(expr.right)
        if expr.op == "&":
            return _vba_str(left) + _vba_str(right)
        if expr.op in _COMPARE_OPS:
            return _compare(expr.op, left, right)
        left_n, right_n = _as_number(left), _as_number(right)
        if expr.op == "+":
            return left_n + right_n
        if expr.op == "-":
            return left_n - right_n
        if expr.op == "*":
            return left_n * right_n
        if expr.op == "/":
            return left_n / right_n
        if expr.op == "\\":
            return math.trunc(math.trunc(left_n) / math.trunc(right_n))
        if expr.op == "mod":
            return left_n - right_n * math.trunc(left_n / right_n)
        if expr.op == "^":
            return left_n ** right_n
        raise InterpreterError(f"unsupported operator {expr.op!r}")

    def _resolve_setter(self, target: A.Expr):
        if isinstance(target, A.Identifier):
            name = target.name.lower()
            return lambda value: self.scope.__setitem__(name, value)
        if isinstance(target, A.MemberAccess):
            base = self.eval_expr(target.base)
            attr = target.name.lower()

            def setter(value: Any) -> None:
                try:
                    setattr(base, attr, value)
                except AttributeError as exc:
                    raise InterpreterError(f"'.{target.name}' cannot be assigned on this object") from exc
            return setter
        if isinstance(target, A.Call):
            base = self.eval_expr(target.base)
            args = [self.eval_expr(a) for a in target.args]

            def setter(value: Any) -> None:
                if not hasattr(base, "set_index"):
                    raise InterpreterError(f"cannot assign to an indexed expression on {type(base).__name__}")
                base.set_index(*args, value)
            return setter
        raise InterpreterError("unsupported assignment target")

    # ---- statements -------------------------------------------------------

    def exec_sub(self, sub: A.SubDecl) -> None:
        try:
            self.exec_stmts(sub.body)
        except _ExitSub:
            pass

    def exec_stmts(self, stmts: list[A.Stmt]) -> None:
        for stmt in stmts:
            self._tick()
            if self.on_error_resume:
                try:
                    self.exec_stmt(stmt)
                except (_ExitFor, _ExitDo, _ExitSub, ExecutionLimitError):
                    raise
                except VW.InterpreterError:
                    # Catches the base class, not just interpreter.py's own
                    # subclass -- virtual_workbook.py's methods (worksheet
                    # lookups, renames, deleted-sheet guards, ...) raise the
                    # base VW.InterpreterError directly, which is NOT an
                    # instance of the InterpreterError subclass defined
                    # above, so this must not narrow to that subclass.
                    continue
            else:
                self.exec_stmt(stmt)

    def exec_stmt(self, stmt: A.Stmt) -> None:
        if isinstance(stmt, A.DimStmt):
            for name in stmt.names:
                self.scope.setdefault(name.lower(), None)
        elif isinstance(stmt, A.AssignStmt):
            self._resolve_setter(stmt.target)(self.eval_expr(stmt.value))
        elif isinstance(stmt, A.CallStmt):
            self.eval_expr(stmt.call)
        elif isinstance(stmt, A.ForStmt):
            self._exec_for(stmt)
        elif isinstance(stmt, A.ForEachStmt):
            self._exec_for_each(stmt)
        elif isinstance(stmt, A.DoLoopStmt):
            self._exec_do_loop(stmt)
        elif isinstance(stmt, A.IfStmt):
            self._exec_if(stmt)
        elif isinstance(stmt, A.ExitStmt):
            if stmt.kind == "For":
                raise _ExitFor()
            if stmt.kind == "Do":
                raise _ExitDo()
            raise _ExitSub()
        elif isinstance(stmt, A.OnErrorStmt):
            self.on_error_resume = stmt.mode == "RESUME_NEXT"
        else:
            raise InterpreterError(f"unsupported statement {type(stmt).__name__}")

    def _exec_for(self, stmt: A.ForStmt) -> None:
        start = int(_as_number(self.eval_expr(stmt.start)))
        end = int(_as_number(self.eval_expr(stmt.end)))
        step = int(_as_number(self.eval_expr(stmt.step))) if stmt.step is not None else 1
        if step == 0:
            raise InterpreterError("a loop step of 0 never terminates")
        name = stmt.var.lower()
        try:
            i = start
            while (step > 0 and i <= end) or (step < 0 and i >= end):
                self.scope[name] = i
                self.exec_stmts(stmt.body)
                i += step
        except _ExitFor:
            pass

    def _exec_for_each(self, stmt: A.ForEachStmt) -> None:
        iterable = self.eval_expr(stmt.iterable)
        name = stmt.var.lower()
        try:
            for item in iterable:
                self.scope[name] = item
                self.exec_stmts(stmt.body)
        except TypeError as exc:
            raise InterpreterError("this expression is not something For Each can iterate") from exc
        except _ExitFor:
            pass

    def _exec_do_loop(self, stmt: A.DoLoopStmt) -> None:
        try:
            while True:
                condition = _truthy(self.eval_expr(stmt.condition))
                if stmt.negate:
                    condition = not condition
                if not condition:
                    break
                self.exec_stmts(stmt.body)
        except _ExitDo:
            pass

    def _exec_if(self, stmt: A.IfStmt) -> None:
        for branch in stmt.branches:
            if branch.condition is None or _truthy(self.eval_expr(branch.condition)):
                self.exec_stmts(branch.body)
                return


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


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
        raise InterpreterError(f"cannot use {value!r} in arithmetic") from exc


def _vba_str(value: Any) -> str:
    return "" if value is None else str(value)


def _compare(op: str, left: Any, right: Any) -> bool:
    if isinstance(left, str) or isinstance(right, str):
        left, right = _vba_str(left), _vba_str(right)
    else:
        left, right = _as_number(left), _as_number(right)
    if op == "=":
        return left == right
    if op == "<>":
        return left != right
    if op == "<":
        return left < right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    return left >= right


def run(sub: A.SubDecl, workbook: VW.VirtualWorkbook, limits: ExecutionLimits | None = None) -> int:
    """Executes `sub` against `workbook` in place and returns the number of
    statements executed (for observability). Raises InterpreterError/
    ExecutionLimitError on failure; callers read workbook.pending_changes()
    for the resulting changes."""
    interp = _Interpreter(workbook, limits or ExecutionLimits())
    interp.exec_sub(sub)
    return interp.statement_count
