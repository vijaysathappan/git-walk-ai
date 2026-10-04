"""General lane: runs a macro's AST against a fresh VirtualWorkbook loaded
from the branch's live data, via app.macros.interpreter, and returns the
resulting pending SemanticChange list in the exact (changes, summary) shape
app.macros.execution.build_change_list (the SQL lane's equivalent) returns
-- so run_service can dispatch on MACRO_DEFINITIONS.EXECUTION_LANE without
either caller needing to know which lane actually ran.

Read-only against the database: VirtualWorkbook's one bulk load per sheet is
the only query here. Nothing is written until run_service hands the
returned changes to the same commit_semantic_delta every other Git Walk
edit goes through.
"""

from __future__ import annotations

from typing import Any

from . import interpreter
from .parser.ast import SubDecl
from .virtual_workbook import VirtualWorkbook


def build_change_list(
    conn, *, branch_id: str, repository_id: str, data_table_id: str, sub_ast: SubDecl,
    limits: interpreter.ExecutionLimits | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    workbook = VirtualWorkbook(conn, branch_id=branch_id, repository_id=repository_id, data_table_id=data_table_id)
    statements_executed = interpreter.run(sub_ast, workbook, limits or interpreter.ExecutionLimits())
    changes = workbook.pending_changes()
    summary = {"change_count": len(changes), "statements_executed": statements_executed}
    return changes, summary
