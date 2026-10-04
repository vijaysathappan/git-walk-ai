"""AST node types for the supported VBA subset.

Expressions use a general postfix-chain model -- ``a.b(1).c`` parses as
``MemberAccess(Call(MemberAccess(Identifier("a"), "b"), [Literal(1)]), "c")``
-- so the same grammar covers ``Cells(i, 3).Value``, ``Worksheets("Summary")
.Delete``, ``totals.Exists(category)``, and ``src.Rows.Count`` uniformly.
Phase 2's dedicated ``CellExpr``/``VarRef`` nodes are gone; sql_lane.py
recognizes the ``Cells(row, col).Value`` shape as a pattern over this
general model instead of a special-cased node.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class Expr:
    pass


@dataclass(frozen=True)
class Literal(Expr):
    value: object  # int, float, str, or bool


@dataclass(frozen=True)
class Identifier(Expr):
    name: str


@dataclass(frozen=True)
class MemberAccess(Expr):
    """``base.name`` -- a property/field read, or the callee of a Call."""
    base: Expr
    name: str


@dataclass(frozen=True)
class Call(Expr):
    """``base(args...)`` -- a function call, method call, or indexing
    operation (``Cells(r, c)``, ``totals(key)``, ``Worksheets("Sheet1")``,
    ``totals.Add(k, v)`` where base is a MemberAccess)."""
    base: Expr
    args: list[Expr] = field(default_factory=list)


@dataclass(frozen=True)
class UnaryOp(Expr):
    op: str
    operand: Expr


@dataclass(frozen=True)
class BinOp(Expr):
    op: str
    left: Expr
    right: Expr


class Stmt:
    pass


@dataclass(frozen=True)
class DimStmt(Stmt):
    names: list[str]
    type_name: str | None


@dataclass(frozen=True)
class AssignStmt(Stmt):
    """Covers both ``target = expr`` and ``Set target = expr`` -- VBA's
    Set/non-Set distinction is about reference vs. value semantics, which
    the interpreter's own object model already handles uniformly."""
    target: Expr
    value: Expr
    line: int


@dataclass(frozen=True)
class CallStmt(Stmt):
    """A bare statement-level call with no assignment, e.g.
    ``totals.Add category, amount`` or ``Worksheets("Summary").Delete``."""
    call: Expr
    line: int


@dataclass(frozen=True)
class ForStmt(Stmt):
    var: str
    start: Expr
    end: Expr
    step: Expr | None
    body: list[Stmt]
    line: int


@dataclass(frozen=True)
class ForEachStmt(Stmt):
    var: str
    iterable: Expr
    body: list[Stmt]
    line: int


@dataclass(frozen=True)
class DoLoopStmt(Stmt):
    """Pre-test only: ``Do While <cond> ... Loop`` / ``Do Until <cond> ...
    Loop``. ``negate=True`` for Until (loop continues while NOT cond)."""
    condition: Expr
    negate: bool
    body: list[Stmt]
    line: int


@dataclass(frozen=True)
class IfBranch:
    condition: Expr | None  # None for the final Else
    body: list[Stmt]


@dataclass(frozen=True)
class IfStmt(Stmt):
    branches: list[IfBranch]  # If, ElseIf*, Else? in order
    line: int


@dataclass(frozen=True)
class ExitStmt(Stmt):
    kind: str  # "For" | "Do" | "Sub"
    line: int


@dataclass(frozen=True)
class OnErrorStmt(Stmt):
    mode: str  # "RESUME_NEXT" | "GOTO_0"
    line: int


@dataclass(frozen=True)
class SubDecl:
    name: str
    body: list[Stmt]
