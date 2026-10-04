"""AI Macro Explainer.

Explains what a macro actually does, in plain language a non-technical
reviewer can follow before deciding whether to approve running it --
grounded in real, deterministic structure (the macro's own parsed AST:
loop bounds, columns/sheets referenced, whether it creates a new sheet or
aggregates via a Dictionary -- see ``_structural_facts``), its
already-computed static risk classification (``static_gate.classify``),
and its real run history (``MACRO_RUNS``, joined across every extraction
this proc has ever had -- never invented, never guessed).

Modeled directly on ``app.ai.formula_explainer`` -- same PLAN -> INVESTIGATE
-> EXPLAIN loop via ``app.ai.agent_runtime``, same durable cache-before-call
discipline, same evidence-grounding rule enforced by the gateway itself
(a citation to evidence that wasn't actually supplied is stripped, never
silently trusted). Purely read-only: this agent never writes to the
workbook or runs the macro, so it has no confirmation step at all.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .. import database
from ..ai import agent_runtime
from ..ai.gateway import ai_gateway
from ..ai.response_parsing import sanitize_free_text
from ..ai.retrieval import EvidenceItem
from ..ai.service import ai_service
from ..observability import record_audit_event
from .parser import ast as A
from .parser.statement_parser import ParseError, parse_sub
from .run_service import _repository_access

_MAX_RUN_HISTORY = 5


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Deterministic structural facts -- walked directly from the parsed AST,
# never inferred by the model. Self-contained (no private cross-module
# imports from sql_lane/interpreter) since this only needs to describe
# structure, not decide eligibility or execute anything.
# ---------------------------------------------------------------------------

def _literal_repr(expr: A.Expr) -> Any:
    if isinstance(expr, A.Literal):
        return expr.value
    if isinstance(expr, A.Identifier):
        return expr.name
    return "(expression)"


def _walk_expr(expr: A.Expr, facts: dict[str, Any]) -> None:
    if isinstance(expr, (A.Literal, A.Identifier)):
        return
    if isinstance(expr, A.MemberAccess):
        _walk_expr(expr.base, facts)
        return
    if isinstance(expr, A.Call):
        base = expr.base
        if isinstance(base, A.Identifier):
            name = base.name.lower()
            if name == "cells" and len(expr.args) == 2:
                col_expr = expr.args[1]
                if isinstance(col_expr, A.Literal) and isinstance(col_expr.value, (int, float)):
                    facts["columns_referenced"].add(int(col_expr.value))
            elif name in ("worksheets", "sheets") and expr.args:
                name_expr = expr.args[0]
                if isinstance(name_expr, A.Literal) and isinstance(name_expr.value, str):
                    facts["sheets_referenced"].add(name_expr.value)
            elif name == "createobject" and expr.args:
                target = expr.args[0]
                if isinstance(target, A.Literal) and str(target.value).strip().lower() == "scripting.dictionary":
                    facts["uses_dictionary"] = True
        if isinstance(expr.base, A.MemberAccess) and expr.base.name.lower() == "add":
            add_base = expr.base.base
            if isinstance(add_base, A.Identifier) and add_base.name.lower() in ("worksheets", "sheets"):
                facts["creates_new_sheet"] = True
        _walk_expr(expr.base, facts)
        for arg in expr.args:
            _walk_expr(arg, facts)
        return
    if isinstance(expr, A.UnaryOp):
        _walk_expr(expr.operand, facts)
        return
    if isinstance(expr, A.BinOp):
        _walk_expr(expr.left, facts)
        _walk_expr(expr.right, facts)
        return


def _walk_stmts(stmts: list[A.Stmt], facts: dict[str, Any]) -> None:
    for stmt in stmts:
        if isinstance(stmt, A.DimStmt):
            continue
        elif isinstance(stmt, A.AssignStmt):
            _walk_expr(stmt.target, facts)
            _walk_expr(stmt.value, facts)
        elif isinstance(stmt, A.CallStmt):
            _walk_expr(stmt.call, facts)
        elif isinstance(stmt, A.ForStmt):
            facts["loops"].append({"kind": "For", "var": stmt.var, "start": _literal_repr(stmt.start), "end": _literal_repr(stmt.end)})
            _walk_stmts(stmt.body, facts)
        elif isinstance(stmt, A.ForEachStmt):
            facts["loops"].append({"kind": "For Each", "var": stmt.var})
            _walk_stmts(stmt.body, facts)
        elif isinstance(stmt, A.DoLoopStmt):
            facts["loops"].append({"kind": "Do While/Until"})
            _walk_stmts(stmt.body, facts)
        elif isinstance(stmt, A.IfStmt):
            for branch in stmt.branches:
                _walk_stmts(branch.body, facts)
        # ExitStmt / OnErrorStmt: nothing structural to record


def structural_facts(sub_ast: A.SubDecl) -> dict[str, Any]:
    """Deterministic, never-guessed facts about what a macro's parsed
    structure actually touches."""
    facts: dict[str, Any] = {
        "loops": [], "sheets_referenced": set(), "columns_referenced": set(),
        "creates_new_sheet": False, "uses_dictionary": False,
    }
    _walk_stmts(sub_ast.body, facts)
    facts["sheets_referenced"] = sorted(facts["sheets_referenced"])
    facts["columns_referenced"] = sorted(facts["columns_referenced"])
    return facts


def _run_history(conn, repository_id: str, module_name: str, proc_name: str) -> list[dict[str, Any]]:
    """Every real run of this logical macro (by name, across every
    extraction it has ever had -- macro_id itself isn't stable across
    re-extractions), most recent first."""
    rows = conn.execute(
        """
        SELECT MR.STATUS, MR.PREVIEW_CHANGE_COUNT, MR.CREATED_AT
        FROM MACRO_RUNS MR JOIN MACRO_DEFINITIONS MD ON MD.MACRO_ID = MR.MACRO_ID
        WHERE MR.REPOSITORY_ID=? AND MD.MODULE_NAME=? AND MD.PROC_NAME=?
        ORDER BY MR.CREATED_AT DESC LIMIT ?
        """,
        (repository_id, module_name, proc_name, _MAX_RUN_HISTORY),
    ).fetchall()
    return [{"status": row["STATUS"], "change_count": row["PREVIEW_CHANGE_COUNT"], "created_at": row["CREATED_AT"]} for row in rows]


def _question_for_macro(
    macro_row, facts: dict[str, Any], run_history: list[dict[str, Any]],
) -> str:
    loop_descriptions = []
    for loop in facts["loops"]:
        if loop["kind"] == "For":
            loop_descriptions.append(f"a For loop on {loop['var']} from {loop['start']} to {loop['end']}")
        elif loop["kind"] == "For Each":
            loop_descriptions.append(f"a For Each loop on {loop['var']}")
        else:
            loop_descriptions.append("a Do While/Until loop")
    structure_line = (
        f"Loops: {'; '.join(loop_descriptions) or 'none'}. "
        f"Columns referenced (1-based): {facts['columns_referenced'] or 'none identified'}. "
        f"Sheets referenced by name: {facts['sheets_referenced'] or 'none (uses the active/default sheet only)'}. "
        + ("Creates a new worksheet. " if facts["creates_new_sheet"] else "")
        + ("Uses a Scripting.Dictionary to aggregate values across rows. " if facts["uses_dictionary"] else "")
    )
    history_lines = "\n".join(
        f"- {item['status']} on {item['created_at']}, {item['change_count']} change(s)" for item in run_history
    ) or "This macro has never been run yet."
    reason = f", currently blocked because: {macro_row['BLOCK_REASONS_JSON']}" if macro_row["STATIC_RISK"] != "RUNNABLE" else ""
    return (
        "Explain exactly what this VBA macro does, in plain language a non-technical reviewer can follow, "
        "before they decide whether to approve running it.\n"
        f"Procedure: {macro_row['PROC_NAME']} (module {macro_row['MODULE_NAME']}).\n"
        f"Static classification (parsed deterministically -- trust this over your own reading of the code): "
        f"{macro_row['STATIC_RISK']}"
        + (f", execution lane {macro_row['EXECUTION_LANE']}" if macro_row["EXECUTION_LANE"] else "")
        + reason + ".\n"
        f"Structure parsed directly from the code: {structure_line}\n"
        f"Real run history for this exact procedure:\n{history_lines}\n"
        "Return your explanation as a sequence of entries in recommended_actions, action_type set to exactly "
        "'STEP' for each, in the order a reader should follow them -- title is a short label, rationale is the "
        "actual explanation text. If something about this macro is worth flagging as a risk (a broad blast "
        "radius, an ambiguous or ungrounded column reference, a structural change like adding a sheet, or that "
        "it is already blocked), add exactly one final entry with action_type 'RISK_NOTE' -- omit it entirely "
        "if nothing is notable, never invent a concern that isn't there. Only describe behavior that follows "
        "from the structure and evidence above; never invent a column, sheet, or capability not present."
    )


def _parse_explanation(result: dict[str, Any]) -> dict[str, Any]:
    actions = result.get("recommended_actions") or []
    steps = [
        {"title": item.get("title") or f"Step {index + 1}", "explanation": sanitize_free_text(item.get("rationale"))}
        for index, item in enumerate(item for item in actions if str(item.get("action_type", "")).upper() == "STEP")
    ]
    steps = [step for step in steps if step["explanation"]]
    risk_note = sanitize_free_text(next(
        (item.get("rationale") for item in actions if str(item.get("action_type", "")).upper() == "RISK_NOTE"), "",
    )) or None
    return {
        "summary": sanitize_free_text(result.get("answer")),
        "steps": steps,
        "risk_note": risk_note,
        "confidence": float(result.get("confidence") or 0.0),
        "warnings": result.get("warnings") or [],
        "ai_request_id": result.get("ai_request_id"),
    }


def get_cached_explanation(repository_id: str, module_name: str, proc_name: str, source_hash: str) -> dict[str, Any] | None:
    conn = database._get_connection()
    try:
        row = conn.execute(
            """SELECT * FROM MACRO_EXPLANATIONS
               WHERE REPOSITORY_ID=? AND MODULE_NAME=? AND PROC_NAME=? AND SOURCE_OBJECT_HASH=?""",
            (repository_id, module_name, proc_name, source_hash),
        ).fetchone()
        if not row:
            return None
        item = {key.lower(): row[key] for key in row.keys()}
        item["steps"] = json.loads(item.pop("step_by_step_json") or "[]")
        item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
        return item
    finally:
        conn.close()


async def _run_macro_explanation(
    run_id: str, organization_id: str, user_id: str, table_id: str, macro_id: str,
) -> dict[str, Any]:
    # organization_id is resolved by the caller from the REPOSITORY's own
    # ORGANIZATION_ID (see explain_macro/start_macro_explanation) -- never
    # from the acting user's "primary" organization, which can silently
    # differ for a user who belongs to more than one org, causing a
    # perfectly valid org-scoped role grant to be checked against the
    # wrong org and denied.
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.view")
        macro_row = conn.execute(
            "SELECT * FROM MACRO_DEFINITIONS WHERE MACRO_ID=? AND REPOSITORY_ID=?",
            (macro_id, repository["REPOSITORY_ID"]),
        ).fetchone()
        if not macro_row:
            raise KeyError("Macro does not exist.")
        from ..services.semantic_ledger_service import ledger_for_connection
        source = ledger_for_connection(conn).objects.get_bytes(conn, macro_row["SOURCE_OBJECT_HASH"]).decode("utf-8")
        run_history = _run_history(conn, repository["REPOSITORY_ID"], macro_row["MODULE_NAME"], macro_row["PROC_NAME"])
    finally:
        conn.close()

    try:
        sub_ast = parse_sub(source)
        facts = structural_facts(sub_ast)
    except ParseError:
        facts = {"loops": [], "sheets_referenced": [], "columns_referenced": [], "creates_new_sheet": False, "uses_dictionary": False}
    ai_service._record_agent_step(
        run_id, 1, "PLAN", "COMPLETED",
        f"Parsed the macro's structure ({len(facts['loops'])} loop(s), {len(facts['columns_referenced'])} column(s) referenced)",
    )

    tool_call = ai_service._record_read_tool_call(
        organization_id, user_id, run_id, None, "get_macro_context",
        {"table_id": table_id, "macro_id": macro_id},
        {"run_history_count": len(run_history)},
    )
    ai_service._record_agent_step(
        run_id, 2, "INVESTIGATE", "COMPLETED",
        f"Read {len(run_history)} prior run(s) of this procedure" if run_history else "No prior runs found for this procedure",
        tool_call["tool_call_id"],
    )

    # Always grounded on the macro's own deterministic structure/classification
    # (never empty, even for a macro that's never been run) -- plus one item
    # per real prior run, when any exist.
    evidence = [
        EvidenceItem(
            type="macro_structure", id=macro_id,
            title=f"{macro_row['PROC_NAME']} ({macro_row['MODULE_NAME']})",
            summary=f"{macro_row['STATIC_RISK']}, {len(facts['loops'])} loop(s), columns {facts['columns_referenced']}",
            data=facts,
        ).serializable(),
    ] + [
        EvidenceItem(
            type="macro_run_history", id=f"{macro_id}:{index}",
            title=f"Run on {item['created_at']}", summary=f"{item['status']}, {item['change_count']} change(s)", data=item,
        ).serializable()
        for index, item in enumerate(run_history)
    ]

    result = await ai_gateway.generate(
        organization_id=organization_id, user_id=user_id, feature="MACRO_EXPLANATION",
        question=_question_for_macro(macro_row, facts, run_history),
        evidence=evidence, context_hash=_hash({"source": source, "evidence": evidence}),
        classification="INTERNAL", agent_key="MACRO_EXPLAINER_AGENT", agent_run_id=run_id,
    )
    parsed = _parse_explanation(result)
    ai_service._record_agent_step(
        run_id, 3, "EXPLAIN", "COMPLETED",
        f"Explained the macro in {len(parsed['steps'])} step(s)" + (" with a risk note" if parsed["risk_note"] else ""),
    )

    explanation_id = _id("MEX")
    conn = database._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO MACRO_EXPLANATIONS
                (EXPLANATION_ID, REPOSITORY_ID, MODULE_NAME, PROC_NAME, SOURCE_OBJECT_HASH,
                 SUMMARY, STEP_BY_STEP_JSON, RISK_NOTE, CONFIDENCE, WARNINGS_JSON,
                 AI_REQUEST_ID, CREATED_BY, CREATED_AT)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(REPOSITORY_ID, MODULE_NAME, PROC_NAME, SOURCE_OBJECT_HASH) DO UPDATE SET
                SUMMARY=excluded.SUMMARY, STEP_BY_STEP_JSON=excluded.STEP_BY_STEP_JSON,
                RISK_NOTE=excluded.RISK_NOTE, CONFIDENCE=excluded.CONFIDENCE,
                WARNINGS_JSON=excluded.WARNINGS_JSON, AI_REQUEST_ID=excluded.AI_REQUEST_ID, CREATED_AT=excluded.CREATED_AT
            """,
            (
                explanation_id, macro_row["REPOSITORY_ID"], macro_row["MODULE_NAME"], macro_row["PROC_NAME"],
                macro_row["SOURCE_OBJECT_HASH"], parsed["summary"], json.dumps(parsed["steps"]), parsed["risk_note"],
                parsed["confidence"], json.dumps(parsed["warnings"]), parsed["ai_request_id"], user_id, database._utcnow(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    record_audit_event(
        "AI_MACRO_EXPLAINED", actor_user_id=user_id, repository_id=macro_row["REPOSITORY_ID"],
        payload={"agent_run_id": run_id, "macro_id": macro_id, "proc_name": macro_row["PROC_NAME"]},
    )
    return {"explanation_id": explanation_id, "cache_hit": False, **parsed}


async def explain_macro(user_id: str, table_id: str, macro_id: str) -> dict[str, Any]:
    """Synchronous entrypoint: cache-or-run, returns the final result.
    organization_id is always the REPOSITORY's own org (resolved here via
    _repository_access), never the caller's "primary" organization -- a
    user who belongs to more than one org can otherwise have a perfectly
    valid, correctly-scoped role grant checked against the wrong org."""
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.view")
        macro_row = conn.execute("SELECT * FROM MACRO_DEFINITIONS WHERE MACRO_ID=? AND REPOSITORY_ID=?", (macro_id, repository["REPOSITORY_ID"])).fetchone()
        if not macro_row:
            raise KeyError("Macro does not exist.")
    finally:
        conn.close()
    organization_id = repository["ORGANIZATION_ID"]
    ai_service._require(user_id, "ai.agent.run", organization_id)
    cached = get_cached_explanation(repository["REPOSITORY_ID"], macro_row["MODULE_NAME"], macro_row["PROC_NAME"], macro_row["SOURCE_OBJECT_HASH"])
    if cached:
        return {"cache_hit": True, **cached}
    run_id, _ = agent_runtime.create_run(
        organization_id, user_id, "MACRO_EXPLAINER_AGENT",
        f"Explain macro {macro_row['PROC_NAME']}", "MACRO", macro_id,
    )
    try:
        result = await _run_macro_explanation(run_id, organization_id, user_id, table_id, macro_id)
        agent_runtime.finish_run(run_id, 3)
        return {"agent_run_id": run_id, **result}
    except Exception as exc:
        agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))
        raise


def start_macro_explanation(user_id: str, table_id: str, macro_id: str) -> dict[str, Any]:
    """Background-launching entrypoint: returns immediately, pollable via
    the existing generic GET /ai-platform/agent-runs/{id}. If already
    cached, returns the cached result directly with no run at all.
    organization_id is always the REPOSITORY's own org -- see explain_macro."""
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.view")
        macro_row = conn.execute("SELECT * FROM MACRO_DEFINITIONS WHERE MACRO_ID=? AND REPOSITORY_ID=?", (macro_id, repository["REPOSITORY_ID"])).fetchone()
        if not macro_row:
            raise KeyError("Macro does not exist.")
    finally:
        conn.close()
    organization_id = repository["ORGANIZATION_ID"]
    ai_service._require(user_id, "ai.agent.run", organization_id)
    cached = get_cached_explanation(repository["REPOSITORY_ID"], macro_row["MODULE_NAME"], macro_row["PROC_NAME"], macro_row["SOURCE_OBJECT_HASH"])
    if cached:
        return {"status": "COMPLETED", "cache_hit": True, **cached}
    run_id, _ = agent_runtime.create_run(
        organization_id, user_id, "MACRO_EXPLAINER_AGENT",
        f"Explain macro {macro_row['PROC_NAME']}", "MACRO", macro_id,
    )

    async def _body() -> None:
        try:
            await _run_macro_explanation(run_id, organization_id, user_id, table_id, macro_id)
            agent_runtime.finish_run(run_id, 3)
        except Exception as exc:
            agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))

    agent_runtime.launch(_body())
    return {"agent_run_id": run_id, "status": "RUNNING"}
