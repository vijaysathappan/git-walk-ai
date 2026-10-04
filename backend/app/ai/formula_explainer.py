"""Agentic Formula Explainer.

Explains what a single cell's formula actually computes, in plain
language, grounded in real structure rather than free-associated prose:
the functions and cell references parsed directly out of the formula text
(deterministic, never guessed -- see ``ai.formula_knowledge.extract_*``),
each referenced cell's real edit history (``commit_store.get_cell_history``),
and -- when this repository already has a completed EUC dependency
analysis -- the cell's real downstream fan-out and technical criticality
from ``euc.dependency``. Falls back gracefully to the reference+history
path alone when no EUC run exists, the same "optional, gracefully absent"
convention ``CommitReviewPanel``'s dependency-impact strip already uses;
a formula explanation should work for every repository, not only ones
that opted into EUC governance.

Grounded via ``ai.formula_knowledge``'s FTS5 RAG corpus of function-pattern
precedents, and durably cached in ``FORMULA_EXPLANATIONS`` keyed by
(branch, cell, formula hash): a formula that hasn't changed since it was
last explained is served instantly with zero AI call, on top of the
generic 6-hour ``AI_RESPONSE_CACHE`` every other feature already gets.

PLAN -> INVESTIGATE -> EXPLAIN loop, audited via the same
AI_AGENT_RUNS/AI_AGENT_STEPS primitives every other agent in this codebase
uses. Purely read-only: this agent never writes to the workbook, so it has
no governed-action/confirmation step at all.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .. import database
from ..euc.branch_comparison import resolve_cell_identity
from ..euc.dependency.services.dependency_service import DependencyService
from ..observability import record_audit_event
from ..repositories.commit_store import get_cell_history, reconstruct_branch
from ..repositories.merge_store import branch_context
from . import agent_runtime
from .classification_util import repository_classification
from .formula_knowledge import (
    extract_cell_references,
    extract_functions,
    record_formula_precedent,
    retrieve_formula_patterns,
)
from .gateway import ai_gateway
from .response_parsing import sanitize_free_text
from .retrieval import EvidenceItem
from .service import ai_service

_dependency_service = DependencyService()
_MAX_REFERENCES_INVESTIGATED = 6


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _latest_euc_id(repository_id: str) -> str | None:
    conn = database._get_connection()
    try:
        row = conn.execute(
            "SELECT EUC_ID FROM EUC_ASSETS WHERE REPOSITORY_ID=? ORDER BY CREATED_AT DESC LIMIT 1", (repository_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _resolve_reference(branch_state: dict[str, Any], current_sheet_id: str, token: str) -> tuple[str, str, str] | None:
    """Best-effort resolve one raw reference token (e.g. ``B7`` or
    ``'Other Sheet'!C4:C9``) back to a stable ``(sheet_id, row_id,
    column_id)`` on the current branch. Only the first cell of a range is
    resolved -- a range's aggregate behavior is already explained by the
    function wrapping it (e.g. SUM), not by walking every member cell.
    Returns ``None`` rather than guessing when the sheet name can't be
    matched or the address falls outside the sheet's data region."""
    cell_part = token.split(":")[0]
    sheet_id = current_sheet_id
    if "!" in cell_part:
        sheet_part, cell_part = cell_part.rsplit("!", 1)
        sheet_name = sheet_part.strip("'")
        match = next(
            (sheet["sheet_id"] for sheet in branch_state.get("sheets", []) if sheet.get("name") == sheet_name), None,
        )
        if not match:
            return None
        sheet_id = match
    identity = resolve_cell_identity(branch_state, sheet_id, cell_part.replace("$", ""))
    if not identity:
        return None
    return identity["sheet_id"], identity["row_id"], identity["column_id"]


def _question_for_formula(
    formula: str, functions: list[str], reference_histories: list[dict[str, Any]],
    dependency_context: dict[str, Any] | None,
) -> str:
    history_lines = "\n".join(
        f"- {item['cell_address']}: current value {item['current_value']!r}, last changed by "
        f"{item['changed_by'] or 'unknown'} ({item['prior_edit_count']} prior edit(s) on record)."
        for item in reference_histories
    ) or "No referenced cells had prior edit history on record."
    dependency_block = ""
    if dependency_context:
        dependency_block = (
            f"\nDependency graph context: this cell has {dependency_context.get('direct_dependents', 0)} direct "
            f"and {dependency_context.get('total_downstream', 0)} total downstream dependent cell(s) across "
            f"{dependency_context.get('affected_sheet_count', 0)} sheet(s); technical criticality "
            f"{dependency_context.get('technical_criticality', 'unknown')}.\n"
        )
    return (
        "Explain exactly what this Excel formula computes, in plain language a non-technical reviewer can follow.\n"
        f"Formula: {formula}\n"
        f"Functions used (parsed deterministically -- trust this list over your own reading of the formula "
        f"text): {', '.join(functions) or 'none (a plain reference or literal)'}.\n"
        f"Referenced cells and their real edit history:\n{history_lines}\n"
        + dependency_block +
        "Return your explanation as a sequence of entries in recommended_actions, action_type set to exactly "
        "'STEP' for each, in the order a reader should follow them (e.g. first what each referenced input "
        "represents, then how the functions combine them, then what the result means) -- title is a short label "
        "for that step, rationale is the actual explanation text. If something about this formula is worth "
        "flagging as a risk or gotcha (masks errors, depends on a cell with high downstream fan-out, uses a "
        "volatile or untraceable function, relies on an unsorted table for an approximate lookup, etc.), add "
        "exactly one final entry with action_type 'RISK_NOTE' explaining it -- omit it entirely if there is "
        "nothing notable, never invent a concern that isn't there. Only describe behavior that follows from the "
        "formula, functions, and evidence above; never invent a cell reference, function, or value not present "
        "in the evidence."
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
        # A free model occasionally echoes its ENTIRE structured response
        # back into `answer` instead of a short prose summary -- sanitized
        # here so that never reaches a user. Falls back to empty rather
        # than the model's stray text; the per-step explanations above are
        # the real content and are grounded independently.
        "summary": sanitize_free_text(result.get("answer")),
        "steps": steps,
        "risk_note": risk_note,
        "confidence": float(result.get("confidence") or 0.0),
        "warnings": result.get("warnings") or [],
        "ai_request_id": result.get("ai_request_id"),
    }


def get_cached_explanation(branch_id: str, sheet_id: str, row_id: str, column_id: str, formula_hash: str) -> dict[str, Any] | None:
    conn = database._get_connection()
    try:
        row = conn.execute(
            """SELECT * FROM FORMULA_EXPLANATIONS
               WHERE BRANCH_ID=? AND SHEET_ID=? AND ROW_ID=? AND COLUMN_ID=? AND FORMULA_HASH=?""",
            (branch_id, sheet_id, row_id, column_id, formula_hash),
        ).fetchone()
        if not row:
            return None
        item = {key.lower(): row[key] for key in row.keys()}
        item["steps"] = json.loads(item.pop("step_by_step_json") or "[]")
        item["referenced_cells"] = json.loads(item.pop("referenced_cells_json") or "[]")
        item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
        return item
    finally:
        conn.close()


async def _run_formula_explanation(
    run_id: str, organization_id: str, user_id: str, repository_id: str, branch_id: str,
    sheet_id: str, row_id: str, column_id: str, formula: str, cell_address: str | None,
) -> dict[str, Any]:
    functions = extract_functions(formula)
    references = extract_cell_references(formula)
    ai_service._record_agent_step(
        run_id, 1, "PLAN", "COMPLETED",
        f"Parsed {len(functions)} function(s) and {len(references)} cell reference(s) from the formula",
    )

    branch = branch_context(branch_id)
    if not branch or branch["repository_id"] != repository_id:
        raise PermissionError("Branch does not belong to this repository")
    branch_state = reconstruct_branch(branch_id)
    reference_histories: list[dict[str, Any]] = []
    for token in references[:_MAX_REFERENCES_INVESTIGATED]:
        resolved = _resolve_reference(branch_state, sheet_id, token)
        if not resolved:
            continue
        ref_sheet_id, ref_row_id, ref_column_id = resolved
        history = get_cell_history(branch_id, ref_sheet_id, ref_row_id, ref_column_id, limit=3)
        snapshot_sheet = next((s for s in branch_state.get("sheets", []) if s["sheet_id"] == ref_sheet_id), None)
        current_value = None
        if snapshot_sheet:
            row_match = next((r for r in snapshot_sheet.get("rows", []) if r["row_id"] == ref_row_id), None)
            if row_match:
                current_value = (row_match.get("values") or {}).get(ref_column_id)
        reference_histories.append({
            "cell_address": token, "sheet_id": ref_sheet_id, "row_id": ref_row_id, "column_id": ref_column_id,
            "current_value": current_value,
            "changed_by": history[0].get("author_email") if history else None,
            "prior_edit_count": len(history),
        })

    dependency_context = None
    euc_id = _latest_euc_id(repository_id)
    if euc_id and cell_address:
        try:
            node = _dependency_service.resolve_cell_node(euc_id, user_id, sheet_id, cell_address)
            dependency_context = _dependency_service.impact(euc_id, user_id, node["node_id"], max_depth=3)
        except Exception:
            dependency_context = None  # no completed EUC run, or the cell isn't in the graph -- fine, gracefully absent

    tool_call = ai_service._record_read_tool_call(
        organization_id, user_id, run_id, None, "get_formula_context",
        {"branch_id": branch_id, "sheet_id": sheet_id, "reference_count": len(references)},
        {"references_resolved": len(reference_histories), "dependency_context_available": bool(dependency_context)},
    )
    ai_service._record_agent_step(
        run_id, 2, "INVESTIGATE", "COMPLETED",
        f"Traced edit history for {len(reference_histories)} referenced cell(s)"
        + (" and pulled live dependency lineage" if dependency_context else ""),
        tool_call["tool_call_id"],
    )

    precedents = retrieve_formula_patterns(functions, limit=4, organization_id=organization_id)
    evidence = [
        EvidenceItem(
            type="formula_reference", id=f"{item['sheet_id']}:{item['row_id']}:{item['column_id']}",
            title=item["cell_address"], summary=f"Current value {item['current_value']!r}", data=item,
        ).serializable()
        for item in reference_histories
    ] + [
        EvidenceItem(
            type="formula_pattern_precedent", id=doc["doc_id"], title=doc["title"], summary=doc["explanation"], data=doc,
        ).serializable()
        for doc in precedents
    ]
    if dependency_context:
        evidence.append(EvidenceItem(
            type="dependency_impact", id=f"{sheet_id}:{cell_address}",
            title="Downstream dependency impact", summary="Real dependency graph fan-out for this cell",
            data=dependency_context,
        ).serializable())

    result = await ai_gateway.generate(
        organization_id=organization_id, user_id=user_id, feature="formula_explanation",
        question=_question_for_formula(formula, functions, reference_histories, dependency_context),
        evidence=evidence, context_hash=_hash({"formula": formula, "evidence": evidence}),
        classification=repository_classification(repository_id), agent_key="FORMULA_EXPLAINER_AGENT", agent_run_id=run_id,
    )
    parsed = _parse_explanation(result)
    ai_service._record_agent_step(
        run_id, 3, "EXPLAIN", "COMPLETED",
        f"Explained the formula in {len(parsed['steps'])} step(s)" + (" with a risk note" if parsed["risk_note"] else ""),
    )

    try:
        record_formula_precedent(functions, parsed["summary"], organization_id=organization_id)
    except Exception:
        pass  # self-improving corpus growth must never block the explanation itself

    explanation_id = _id("FEX")
    formula_hash = _hash(formula)
    conn = database._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO FORMULA_EXPLANATIONS
                (EXPLANATION_ID, REPOSITORY_ID, BRANCH_ID, SHEET_ID, ROW_ID, COLUMN_ID, FORMULA_HASH,
                 FORMULA_TEXT, CELL_ADDRESS, SUMMARY, STEP_BY_STEP_JSON, REFERENCED_CELLS_JSON, RISK_NOTE,
                 CONFIDENCE, WARNINGS_JSON, AI_REQUEST_ID, CREATED_BY, CREATED_AT, INSUFFICIENT_EVIDENCE)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(BRANCH_ID, SHEET_ID, ROW_ID, COLUMN_ID, FORMULA_HASH) DO UPDATE SET
                SUMMARY=excluded.SUMMARY, STEP_BY_STEP_JSON=excluded.STEP_BY_STEP_JSON,
                REFERENCED_CELLS_JSON=excluded.REFERENCED_CELLS_JSON, RISK_NOTE=excluded.RISK_NOTE,
                CONFIDENCE=excluded.CONFIDENCE, WARNINGS_JSON=excluded.WARNINGS_JSON,
                AI_REQUEST_ID=excluded.AI_REQUEST_ID, CREATED_AT=excluded.CREATED_AT,
                INSUFFICIENT_EVIDENCE=excluded.INSUFFICIENT_EVIDENCE
            """,
            (
                explanation_id, repository_id, branch_id, sheet_id, row_id, column_id, formula_hash,
                formula, cell_address, parsed["summary"], json.dumps(parsed["steps"]),
                json.dumps(reference_histories, default=str), parsed["risk_note"], parsed["confidence"],
                json.dumps(parsed["warnings"]), parsed["ai_request_id"], user_id, database._utcnow(),
                int(bool(result.get("insufficient_evidence"))),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    record_audit_event(
        "AI_FORMULA_EXPLAINED", actor_user_id=user_id, repository_id=repository_id,
        payload={"agent_run_id": run_id, "branch_id": branch_id, "sheet_id": sheet_id, "functions": functions},
    )
    return {"explanation_id": explanation_id, "formula": formula, "functions": functions, "cache_hit": False, **parsed}


async def explain_formula(
    organization_id: str, user_id: str, repository_id: str, branch_id: str,
    sheet_id: str, row_id: str, column_id: str, formula: str, cell_address: str | None = None,
) -> dict[str, Any]:
    """Fully synchronous entrypoint: checks the durable cache first, else
    runs the complete PLAN -> INVESTIGATE -> EXPLAIN loop and returns its
    final result. Used by tests and any server-to-server caller that wants
    the answer directly rather than watching it happen."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    formula_hash = _hash(formula)
    cached = get_cached_explanation(branch_id, sheet_id, row_id, column_id, formula_hash)
    if cached:
        return {"cache_hit": True, **cached}
    run_id, _ = agent_runtime.create_run(
        organization_id, user_id, "FORMULA_EXPLAINER_AGENT",
        f"Explain the formula at {sheet_id}:{row_id}:{column_id}",
        "FORMULA_CELL", f"{branch_id}:{sheet_id}:{row_id}:{column_id}",
    )
    try:
        result = await _run_formula_explanation(
            run_id, organization_id, user_id, repository_id, branch_id, sheet_id, row_id, column_id, formula, cell_address,
        )
        agent_runtime.finish_run(run_id, 3)
        return {"agent_run_id": run_id, **result}
    except Exception as exc:
        agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))
        raise


def start_formula_explanation(
    organization_id: str, user_id: str, repository_id: str, branch_id: str,
    sheet_id: str, row_id: str, column_id: str, formula: str, cell_address: str | None = None,
) -> dict[str, Any]:
    """Background-launching entrypoint for the live-progress UI: returns
    immediately with ``{agent_run_id, status}``; the frontend polls
    ``GET /ai-platform/agent-runs/{id}`` for live steps, then fetches
    ``GET .../formula-explanation`` once status flips to COMPLETED. If this
    exact formula is already cached, returns the cached result directly
    with ``status: COMPLETED`` and no run at all -- there is nothing to
    watch happen."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    formula_hash = _hash(formula)
    cached = get_cached_explanation(branch_id, sheet_id, row_id, column_id, formula_hash)
    if cached:
        return {"status": "COMPLETED", "cache_hit": True, **cached}
    run_id, _ = agent_runtime.create_run(
        organization_id, user_id, "FORMULA_EXPLAINER_AGENT",
        f"Explain the formula at {sheet_id}:{row_id}:{column_id}",
        "FORMULA_CELL", f"{branch_id}:{sheet_id}:{row_id}:{column_id}",
    )

    async def _body() -> None:
        try:
            await _run_formula_explanation(
                run_id, organization_id, user_id, repository_id, branch_id, sheet_id, row_id, column_id, formula, cell_address,
            )
            agent_runtime.finish_run(run_id, 3)
        except Exception as exc:
            agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))

    agent_runtime.launch(_body())
    return {"agent_run_id": run_id, "status": "RUNNING"}
