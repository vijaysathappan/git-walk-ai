"""Agentic merge-conflict resolver.

Investigates every OPEN conflict on a merge request (cell authorship via
``commit_store``, RAG precedents via ``merge_knowledge``) and proposes a
resolution per conflict using the existing ``AIGateway``/``GroundedAnswer``
contract — no gateway or provider changes required, per the confirmed
zero-config feature-routing fallback.

The generic ``run_agent()`` in ``ai.service`` is built for a single
read-only tool call per agent and doesn't fit this multi-conflict,
multi-step loop, so this module drives its own PLAN -> RETRIEVE -> PROPOSE
steps directly. It reuses the same audit primitives
(``AI_AGENT_RUNS``/``AI_AGENT_STEPS`` via ``AIService._record_agent_step``
and ``_record_read_tool_call``) so runs still show up in the same
admin/audit surfaces as every other agent.

The apply/write side is intentionally NOT reinvented here: applying a
suggestion goes through the existing ``_prepare_action``/``confirm_action``
governed-action mechanism in ``ai.service``, which in turn calls
``MergeService.resolve_conflict`` — the exact function a human clicking
"Keep main" / "Accept branch" / "Custom value" already uses.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from .. import database
from ..merge_risk import (
    baseline_recommendation,
    deterministic_risk_level,
    evaluate_merge_risk,
)
from . import agent_runtime
from ..observability import record_audit_event
from ..repositories.commit_store import get_cell_history, reconstruct_branch
from ..services.merge_service import MergeActor, merge_service
from .classification_util import repository_classification
from .change_inspection import (
    column_names as _column_names,
    detect_cleared_fields as _detect_cleared_fields,
    investigate_cleared_field as _investigate_cleared_field,
)
from .evidence import build_merge_evidence
from .gateway import ai_gateway
from .merge_knowledge import retrieve_precedents
from .response_parsing import extract_recommendation_and_summary, sanitize_free_text
from .retrieval import EvidenceItem
from .service import ai_service

_RECOMMENDATION_ORDER = {"APPROVE": 0, "HOLD_FOR_REVIEW": 1, "REJECT": 2}

_ALLOWED_RESOLUTIONS = {"KEEP_MAIN", "ACCEPT_BRANCH", "CUSTOM", "MANUAL_REVIEW"}
_CUSTOM_VALUE_PATTERN = re.compile(r"CUSTOM_VALUE:\s*(.+)", re.IGNORECASE)
_ALLOWED_RECOMMENDATIONS = {"APPROVE", "HOLD_FOR_REVIEW", "REJECT"}


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _question_for_conflict(conflict: dict[str, Any]) -> str:
    return (
        "You are helping resolve one Excel merge conflict. Using only the evidence provided "
        "(the conflict's base/main/branch state, prior resolution precedents, and cell authorship "
        "history), recommend exactly one resolution.\n"
        "Return your recommendation as the single entry in recommended_actions, with action_type set "
        "to exactly one of KEEP_MAIN, ACCEPT_BRANCH, CUSTOM, or MANUAL_REVIEW. If action_type is CUSTOM, "
        "the rationale must include a line formatted exactly as 'CUSTOM_VALUE: <the replacement value>'. "
        "Set risk_level to reflect how risky it would be to apply this resolution automatically. "
        f"Conflict type: {conflict.get('conflict_type')}."
    )


def _question_for_assessment(
    request: dict[str, Any], field_investigations: list[dict[str, Any]], risk_result, baseline: dict[str, Any],
) -> str:
    open_conflicts = sum(1 for item in request["conflicts"] if item["status"] == "OPEN")
    field_lines = "\n".join(
        f"- {item['column_name']} (row {item['row_id']}): cleared from {item['previous_value']!r} to empty, "
        f"last touched by {item['changed_by'] or 'unknown'} "
        f"({item['prior_edit_count']} prior edit(s) recorded for this cell)."
        for item in field_investigations
    )
    field_block = (
        f"\nFields cleared to empty in this change set (investigate and explain EACH one by name, don't "
        f"lump them together):\n{field_lines}\n"
        if field_investigations else "\nNo fields were cleared to empty in this change set.\n"
    )
    risk_lines = "\n".join(
        f"- {component['dimension']}: {component['score']}/100 ({component['classification']}, weight {component['weight']}) "
        f"— {component['explanation']} Evidence: {component['evidence']}"
        for component in risk_result.to_dict()["components"]
    )
    return (
        "A deterministic risk-scoring engine has already computed the authoritative risk assessment below "
        "from hard rules over this merge request's actual changes — it is NOT your job to invent a risk "
        f"level. Baseline: risk_level={baseline['risk_level']} (score {risk_result.score}/100), baseline "
        f"recommendation={baseline['recommendation']}. Component breakdown:\n{risk_lines}\n"
        "\nYour job is to EXPLAIN this baseline in plain language a non-technical reviewer can follow, "
        "citing the specific changes (evidence) that drive each component's score, and to recommend an "
        "action.\n"
        + field_block +
        "Return recommended_actions as a list: one entry per cleared field above with action_type "
        "'FIELD_EXPLANATION', title set to the exact column name, and rationale explaining specifically "
        "why that field is likely now empty (data correction, accidental deletion, replaced by another "
        "field, etc.) based on the evidence — say 'cannot be determined from available history' if the "
        "evidence doesn't support a confident explanation, don't guess. Then add exactly one final entry "
        "with action_type set to APPROVE, HOLD_FOR_REVIEW, or REJECT for your overall recommendation "
        f"(it must be at least as cautious as the baseline recommendation, {baseline['recommendation']} — "
        "you may escalate to a more cautious action if you spot something the scoring engine missed, but "
        "never downgrade it), and rationale explaining the risk breakdown above in plain language.\n"
        f"Open conflicts: {open_conflicts}. Total changes: {request.get('change_summary', {}).get('total', 0)}."
    )


def _parse_assessment(
    result: dict[str, Any], field_investigations: list[dict[str, Any]], risk_result, baseline: dict[str, Any],
) -> dict[str, Any]:
    actions = result.get("recommended_actions") or []
    overall, summary = extract_recommendation_and_summary(result, _ALLOWED_RECOMMENDATIONS)
    field_explanations = {
        str(item.get("title", "")).strip(): item.get("rationale", "")
        for item in actions if str(item.get("action_type", "")).upper() == "FIELD_EXPLANATION"
    }
    ai_recommendation = str((overall or {}).get("action_type") or baseline["recommendation"]).upper()
    if ai_recommendation not in _ALLOWED_RECOMMENDATIONS:
        ai_recommendation = baseline["recommendation"]
    # Guardrail: the model may only be as-or-more cautious than the
    # deterministic baseline, never less — it can escalate APPROVE to
    # HOLD_FOR_REVIEW, but can't talk a HIGH-risk baseline down to APPROVE.
    recommendation = max(
        ai_recommendation, baseline["recommendation"], key=lambda item: _RECOMMENDATION_ORDER[item],
    )
    explained_fields = [
        {**item, "explanation": field_explanations.get(item["column_name"], "No explanation was returned for this field.")}
        for item in field_investigations
    ]
    return {
        "recommendation": recommendation,
        "ai_recommendation": ai_recommendation,
        "summary": summary,
        "confidence": float(result.get("confidence") or 0.0),
        "risk_level": baseline["risk_level"],
        "risk_score": risk_result.score,
        "risk_breakdown": risk_result.to_dict()["components"],
        "warnings": result.get("warnings") or [],
        "ai_request_id": result.get("ai_request_id"),
        "field_investigations": explained_fields,
    }


def get_latest_assessment(merge_request_id: str) -> dict[str, Any] | None:
    conn = database._get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM MERGE_REQUEST_AI_ASSESSMENTS WHERE MERGE_REQUEST_ID=? ORDER BY CREATED_AT DESC LIMIT 1",
            (merge_request_id,),
        ).fetchone()
        if not row:
            return None
        item = {key.lower(): row[key] for key in row.keys()}
        item["warnings"] = json.loads(item["warnings_json"]) if item.get("warnings_json") else []
        item["field_investigations"] = json.loads(item["field_investigations_json"]) if item.get("field_investigations_json") else []
        item["risk_breakdown"] = json.loads(item["risk_breakdown_json"]) if item.get("risk_breakdown_json") else []
        return item
    finally:
        conn.close()


def _begin_assessment_run(organization_id: str, user_id: str, merge_request_id: str) -> str:
    conn = database._get_connection()
    now = database._utcnow()
    run_id = _id("AIAR")
    try:
        agent = conn.execute(
            "SELECT * FROM AI_AGENTS WHERE AGENT_KEY='MERGE_CONFLICT_AGENT' AND STATUS='ACTIVE'"
        ).fetchone()
        ai_service._ensure_settings(conn, organization_id, user_id)
        conn.execute(
            "INSERT INTO AI_AGENT_RUNS VALUES (?,?,?,?,NULL,?,?,?,?,0,?,NULL,?,NULL,NULL)",
            (
                run_id, organization_id, user_id, agent["AGENT_ID"] if agent else None,
                f"Assess merge risk for merge request {merge_request_id}",
                "MERGE_REQUEST", merge_request_id, "RUNNING", agent["MAX_STEPS"] if agent else 6, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


async def assess_merge_request(
    organization_id: str, user_id: str, merge_request_id: str, actor: MergeActor,
) -> dict[str, Any]:
    """Agentic, explainable risk assessment for the repository owner's
    approve/reject decision — runs whether or not the request has open
    conflicts, since a clean merge can still be risky. Drives its own
    PLAN -> INVESTIGATE -> EXPLAIN loop (audited via the same
    AI_AGENT_RUNS/AI_AGENT_STEPS primitives the conflict agent uses):
    PLAN detects fields cleared to empty, INVESTIGATE pulls each cleared
    cell's real edit history so the model explains from evidence rather
    than guessing, EXPLAIN asks the model to address each flagged field by
    name plus give an overall recommendation. Never writes to the merge
    request itself; the owner still clicks Approve/Reject/Merge as today.
    Fully synchronous -- used by tests and any server-to-server caller
    that wants the result directly. See ``start_merge_assessment`` for the
    background-launching twin this shares its real logic with."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    request = merge_service.get_request(merge_request_id, actor)
    run_id = _begin_assessment_run(organization_id, user_id, merge_request_id)
    try:
        return await _run_merge_assessment(run_id, organization_id, user_id, merge_request_id, request)
    except Exception as exc:
        agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))
        raise


def start_merge_assessment(
    organization_id: str, user_id: str, merge_request_id: str, actor: MergeActor,
) -> dict[str, Any]:
    """Background-launching twin of ``assess_merge_request``: creates the
    same run row synchronously (so a bad merge_request_id or a disabled
    agent 404s immediately) then runs the identical logic in the
    background, returning ``{agent_run_id, status}`` right away for the
    live-progress UI to poll via the generic
    ``GET /ai-platform/agent-runs/{id}`` endpoint."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    request = merge_service.get_request(merge_request_id, actor)
    run_id = _begin_assessment_run(organization_id, user_id, merge_request_id)

    async def _body() -> None:
        try:
            await _run_merge_assessment(run_id, organization_id, user_id, merge_request_id, request)
        except Exception as exc:
            agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))

    agent_runtime.launch(_body())
    return {"agent_run_id": run_id, "status": "RUNNING"}


async def _run_merge_assessment(
    run_id: str, organization_id: str, user_id: str, merge_request_id: str, request: dict[str, Any],
) -> dict[str, Any]:
    cleared_changes = _detect_cleared_fields(request.get("changes") or [])
    steps = [ai_service._record_agent_step(
        run_id, 1, "PLAN", "COMPLETED",
        f"Identified {len(cleared_changes)} field(s) cleared to empty out of {len(request.get('changes') or [])} change(s)",
    )]

    branch_state = reconstruct_branch(request["source_branch_id"], request["source_head_commit_id"])
    column_names = _column_names(branch_state)
    field_investigations = [
        _investigate_cleared_field(request["source_branch_id"], change, column_names) for change in cleared_changes
    ]
    tool_call = ai_service._record_read_tool_call(
        organization_id, user_id, run_id, None, "get_merge_conflict_context",
        {"merge_request_id": merge_request_id, "cleared_field_count": len(field_investigations)},
        {"fields_investigated": [item["column_name"] for item in field_investigations]},
    ) if field_investigations else None
    steps.append(ai_service._record_agent_step(
        run_id, 2, "INVESTIGATE", "COMPLETED",
        f"Pulled edit history for {len(field_investigations)} cleared field(s)"
        if field_investigations else "No cleared fields required investigation",
        tool_call["tool_call_id"] if tool_call else None,
    ))

    open_conflicts = [item for item in request["conflicts"] if item["status"] == "OPEN"]

    # SCORE step: the deterministic engine is authoritative for risk_level —
    # computed BEFORE the AI call so the model explains this baseline
    # instead of inventing its own risk assessment from scratch.
    risk_result = evaluate_merge_risk(request, field_investigations)
    risk_level = deterministic_risk_level(risk_result.score)
    baseline = {
        "risk_level": risk_level,
        "recommendation": baseline_recommendation(risk_level, len(open_conflicts)),
    }
    steps.append(ai_service._record_agent_step(
        run_id, 3, "SCORE", "COMPLETED",
        f"Deterministic risk engine scored this merge {risk_result.score}/100 ({risk_level}); "
        f"baseline recommendation {baseline['recommendation']}",
    ))

    precedents = retrieve_precedents(open_conflicts[0], limit=3, organization_id=organization_id) if open_conflicts else []
    field_evidence = [
        EvidenceItem(
            type="cleared_field_investigation", id=f"{item['sheet_id']}:{item['row_id']}:{item['column_id']}",
            title=f"{item['column_name']} cleared", summary=f"Previously {item['previous_value']!r}, now empty.",
            data=item,
        ).serializable()
        for item in field_investigations
    ]
    risk_evidence = [
        EvidenceItem(
            type="risk_component", id=component["dimension"], title=component["dimension"].replace("_", " ").title(),
            summary=component["explanation"], data=component,
        ).serializable()
        for component in risk_result.to_dict()["components"]
    ]
    evidence = build_merge_evidence(
        diff_summary=request.get("changes") or [], conflicts=open_conflicts, precedents=precedents,
        authorship=(request.get("timeline") or [])[:50],
    ) + field_evidence + risk_evidence

    result = await ai_gateway.generate(
        organization_id=organization_id, user_id=user_id, feature="merge_request_risk_assessment",
        question=_question_for_assessment(request, field_investigations, risk_result, baseline), evidence=evidence,
        context_hash=_hash({"merge_request_id": merge_request_id, "evidence": evidence}),
        classification=repository_classification(request.get("repository_id")),
        agent_key="MERGE_CONFLICT_AGENT", agent_run_id=run_id,
    )
    parsed = _parse_assessment(result, field_investigations, risk_result, baseline)
    steps.append(ai_service._record_agent_step(
        run_id, 4, "EXPLAIN", "COMPLETED",
        f"Recommended {parsed['recommendation']} ({parsed['risk_level']} risk, score {parsed['risk_score']}/100) "
        f"with {len(parsed['field_investigations'])} field explanation(s)",
    ))

    assessment_id = _id("MRA")
    conn = database._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO MERGE_REQUEST_AI_ASSESSMENTS
                (ASSESSMENT_ID, MERGE_REQUEST_ID, RISK_LEVEL, RECOMMENDATION, SUMMARY,
                 CONFIDENCE, WARNINGS_JSON, FIELD_INVESTIGATIONS_JSON, RISK_SCORE, RISK_BREAKDOWN_JSON,
                 AI_REQUEST_ID, CREATED_BY, CREATED_AT, INSUFFICIENT_EVIDENCE)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                assessment_id, merge_request_id, parsed["risk_level"], parsed["recommendation"],
                parsed["summary"], parsed["confidence"], json.dumps(parsed["warnings"]),
                json.dumps(parsed["field_investigations"], default=str),
                parsed["risk_score"], json.dumps(parsed["risk_breakdown"], default=str),
                parsed["ai_request_id"], user_id, database._utcnow(),
                int(bool(result.get("insufficient_evidence"))),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    conn = database._get_connection()
    try:
        conn.execute(
            "UPDATE AI_AGENT_RUNS SET STATUS='COMPLETED',CURRENT_STEP=?,COMPLETED_AT=? WHERE AGENT_RUN_ID=?",
            (len(steps), database._utcnow(), run_id),
        )
        conn.commit()
    finally:
        conn.close()
    record_audit_event(
        "AI_MERGE_REQUEST_ASSESSMENT_COMPLETED", actor_user_id=user_id,
        repository_id=request["repository_id"], merge_request_id=merge_request_id,
        payload={"assessment_id": assessment_id, "recommendation": parsed["recommendation"], "risk_level": parsed["risk_level"],
                 "agent_run_id": run_id, "fields_investigated": len(field_investigations)},
    )
    if parsed["risk_level"] in {"HIGH", "CRITICAL"}:
        try:
            conn = database._get_connection()
            try:
                owner_row = conn.execute(
                    "SELECT CREATED_BY FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (request["repository_id"],)
                ).fetchone()
            finally:
                conn.close()
            if owner_row and owner_row[0] != user_id:
                database.create_notification(
                    owner_row[0], "MERGE_RISK_FLAGGED", f"{parsed['risk_level']} risk merge request needs review",
                    f"AI risk assessment flagged '{request.get('title', merge_request_id)}' as {parsed['risk_level']} risk: {parsed['summary'][:200]}",
                    resource_type="MERGE_REQUEST", resource_id=merge_request_id,
                )
        except Exception:
            pass
    return {"assessment_id": assessment_id, **parsed}


def prepare_apply_action(
    organization_id: str, user_id: str, merge_request_id: str, conflict_id: str, actor: MergeActor,
) -> dict[str, Any]:
    """Prepare (but not execute) applying the most recent PROPOSED suggestion
    for one conflict. Returns a PENDING_CONFIRMATION action the caller must
    separately confirm via the existing ``POST /ai-platform/actions/{id}/confirm``
    endpoint — mirrors every other high-risk AI action in this codebase."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    # Access-checked read: raises if the actor cannot see this merge request.
    merge_service.get_request(merge_request_id, actor)
    suggestion = next(
        (item for item in list_suggestions(merge_request_id)
         if item["conflict_id"] == conflict_id and item["status"] == "PROPOSED"),
        None,
    )
    if not suggestion:
        raise KeyError("No proposed AI suggestion exists for this conflict")
    return ai_service._prepare_action(organization_id, user_id, None, {
        "action_type": "APPLY_MERGE_RESOLUTION",
        "merge_request_id": merge_request_id,
        "conflict_id": conflict_id,
        "resolution_type": suggestion["resolution_type"],
        "custom_value": suggestion["custom_value"],
        "suggestion_id": suggestion["suggestion_id"],
    })


def list_suggestions(merge_request_id: str) -> list[dict[str, Any]]:
    conn = database._get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM MERGE_CONFLICT_AI_SUGGESTIONS WHERE MERGE_REQUEST_ID=? ORDER BY CREATED_AT DESC",
            (merge_request_id,),
        ).fetchall()
        output = []
        for row in rows:
            item = {key.lower(): row[key] for key in row.keys()}
            item["custom_value"] = json.loads(item["custom_value_json"]) if item.get("custom_value_json") else None
            item["precedents"] = json.loads(item["precedents_json"]) if item.get("precedents_json") else []
            output.append(item)
        return output
    finally:
        conn.close()


def _parse_recommendation(result: dict[str, Any]) -> dict[str, Any]:
    actions = result.get("recommended_actions") or []
    primary = actions[0] if actions else {}
    resolution_type = str(primary.get("action_type") or "MANUAL_REVIEW").upper()
    rationale = sanitize_free_text(primary.get("rationale")) or sanitize_free_text(result.get("answer"))
    custom_value: Any = None
    if resolution_type == "CUSTOM":
        match = _CUSTOM_VALUE_PATTERN.search(rationale)
        if match:
            custom_value = match.group(1).strip()
        else:
            resolution_type = "MANUAL_REVIEW"
    if resolution_type not in _ALLOWED_RESOLUTIONS:
        resolution_type = "MANUAL_REVIEW"
    return {
        "resolution_type": resolution_type,
        "custom_value": custom_value,
        "rationale": rationale,
        "confidence": float(result.get("confidence") or 0.0),
        "risk_level": str(primary.get("risk_level") or "MEDIUM").upper(),
        "ai_request_id": result.get("ai_request_id"),
    }


def _begin_conflict_analysis_run(organization_id: str, user_id: str, merge_request_id: str) -> str:
    conn = database._get_connection()
    now = database._utcnow()
    run_id = _id("AIAR")
    try:
        agent = conn.execute(
            "SELECT * FROM AI_AGENTS WHERE AGENT_KEY='MERGE_CONFLICT_AGENT' AND STATUS='ACTIVE'"
        ).fetchone()
        if not agent:
            raise KeyError("The merge conflict agent is not registered")
        ai_service._ensure_settings(conn, organization_id, user_id)
        settings_row = conn.execute(
            "SELECT AGENT_ACTIONS_ENABLED FROM AI_ORGANIZATION_SETTINGS WHERE ORGANIZATION_ID=?",
            (organization_id,),
        ).fetchone()
        if not settings_row[0]:
            raise PermissionError("AI agent actions are disabled for this organization")
        conn.execute(
            "INSERT INTO AI_AGENT_RUNS VALUES (?,?,?,?,NULL,?,?,?,?,0,?,NULL,?,NULL,NULL)",
            (
                run_id, organization_id, user_id, agent["AGENT_ID"],
                f"Resolve conflicts for merge request {merge_request_id}",
                "MERGE_REQUEST", merge_request_id, "RUNNING", agent["MAX_STEPS"], now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


async def analyze_merge_request(
    organization_id: str, user_id: str, merge_request_id: str, actor: MergeActor,
) -> dict[str, Any]:
    """PLAN -> RETRIEVE -> PROPOSE loop. Returns the agent run and every
    proposed suggestion for the merge request's currently OPEN conflicts.
    Fully synchronous -- see ``start_conflict_analysis`` for the
    background-launching twin sharing this same logic."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    run_id = _begin_conflict_analysis_run(organization_id, user_id, merge_request_id)
    return await _run_conflict_analysis(run_id, organization_id, user_id, merge_request_id, actor)


def start_conflict_analysis(
    organization_id: str, user_id: str, merge_request_id: str, actor: MergeActor,
) -> dict[str, Any]:
    """Background-launching twin of ``analyze_merge_request``."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    run_id = _begin_conflict_analysis_run(organization_id, user_id, merge_request_id)
    agent_runtime.launch(_run_conflict_analysis(run_id, organization_id, user_id, merge_request_id, actor))
    return {"agent_run_id": run_id, "status": "RUNNING"}


async def _run_conflict_analysis(
    run_id: str, organization_id: str, user_id: str, merge_request_id: str, actor: MergeActor,
) -> dict[str, Any]:
    steps = [ai_service._record_agent_step(
        run_id, 1, "PLAN", "COMPLETED",
        f"Selected the merge conflict agent to investigate merge request {merge_request_id}",
    )]

    try:
        request = merge_service.get_request(merge_request_id, actor)
        open_conflicts = [item for item in request["conflicts"] if item["status"] == "OPEN"]
        already_proposed = {
            item["conflict_id"] for item in list_suggestions(merge_request_id)
            if item["status"] != "DISMISSED"
        }
        pending_conflicts = [item for item in open_conflicts if item["conflict_id"] not in already_proposed]

        conflict_contexts: dict[str, dict[str, Any]] = {}
        for conflict in pending_conflicts:
            cell_history = (
                get_cell_history(
                    request["source_branch_id"], conflict.get("sheet_id"),
                    conflict.get("row_id"), conflict.get("column_id"),
                )
                if conflict.get("sheet_id") and conflict.get("column_id")
                else []
            )
            precedents = retrieve_precedents(conflict, limit=5, organization_id=organization_id)
            conflict_contexts[conflict["conflict_id"]] = {"cell_history": cell_history, "precedents": precedents}

        tool_call = ai_service._record_read_tool_call(
            organization_id, user_id, run_id, None, "get_merge_conflict_context",
            {"merge_request_id": merge_request_id, "conflict_ids": list(conflict_contexts)},
            {"conflicts_investigated": len(conflict_contexts)},
        ) if conflict_contexts else None
        steps.append(ai_service._record_agent_step(
            run_id, 2, "RETRIEVE", "COMPLETED",
            f"Gathered authorship history and resolution precedents for {len(conflict_contexts)} conflict(s)",
            tool_call["tool_call_id"] if tool_call else None,
        ))

        suggestions: list[dict[str, Any]] = []
        for conflict in pending_conflicts:
            context = conflict_contexts[conflict["conflict_id"]]
            evidence = build_merge_evidence(
                conflicts=[conflict], precedents=context["precedents"], authorship=context["cell_history"],
            )
            result = await ai_gateway.generate(
                organization_id=organization_id, user_id=user_id, feature="merge_conflict_resolution",
                question=_question_for_conflict(conflict), evidence=evidence,
                context_hash=_hash({"conflict_id": conflict["conflict_id"], "evidence": evidence}),
                classification=repository_classification(request.get("repository_id")),
                agent_key="MERGE_CONFLICT_AGENT", agent_run_id=run_id,
            )
            parsed = _parse_recommendation(result)
            suggestion_id = _id("MCS")
            conn = database._get_connection()
            try:
                conn.execute(
                    """
                    INSERT INTO MERGE_CONFLICT_AI_SUGGESTIONS
                        (SUGGESTION_ID, MERGE_REQUEST_ID, CONFLICT_ID, RESOLUTION_TYPE, CUSTOM_VALUE_JSON,
                         RATIONALE, CONFIDENCE, RISK_LEVEL, PRECEDENTS_JSON, AI_REQUEST_ID, STATUS, CREATED_AT)
                    VALUES (?,?,?,?,?,?,?,?,?,?,'PROPOSED',?)
                    """,
                    (
                        suggestion_id, merge_request_id, conflict["conflict_id"], parsed["resolution_type"],
                        json.dumps(parsed["custom_value"]) if parsed["custom_value"] is not None else None,
                        parsed["rationale"], parsed["confidence"], parsed["risk_level"],
                        json.dumps([item.get("doc_id") for item in context["precedents"]]),
                        parsed["ai_request_id"], database._utcnow(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            suggestions.append({"suggestion_id": suggestion_id, "conflict_id": conflict["conflict_id"], **parsed})

        steps.append(ai_service._record_agent_step(
            run_id, 3, "PROPOSE", "COMPLETED",
            f"Proposed {len(suggestions)} confirmation-gated resolution(s)",
        ))

        conn = database._get_connection()
        try:
            conn.execute(
                "UPDATE AI_AGENT_RUNS SET STATUS='COMPLETED',CURRENT_STEP=?,COMPLETED_AT=? WHERE AGENT_RUN_ID=?",
                (len(steps), database._utcnow(), run_id),
            )
            conn.commit()
        finally:
            conn.close()
        record_audit_event(
            "AI_MERGE_CONFLICT_ANALYSIS_COMPLETED", actor_user_id=user_id,
            repository_id=request["repository_id"], merge_request_id=merge_request_id,
            payload={"agent_run_id": run_id, "suggestions": len(suggestions), "skipped_existing": len(already_proposed)},
        )
        return {
            "agent_run_id": run_id, "status": "COMPLETED", "steps": steps,
            "suggestions": suggestions, "existing_suggestions": list_suggestions(merge_request_id),
        }
    except Exception as exc:
        conn = database._get_connection()
        try:
            conn.execute(
                "UPDATE AI_AGENT_RUNS SET STATUS='FAILED',ERROR_CODE=?,COMPLETED_AT=? WHERE AGENT_RUN_ID=?",
                (getattr(exc, "code", "AI_AGENT_FAILED"), database._utcnow(), run_id),
            )
            conn.commit()
        finally:
            conn.close()
        raise
