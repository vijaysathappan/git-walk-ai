"""Agentic, explainable risk review for a single commit.

This is Phase 1's original goal ("AI commit validation"), completed here
using everything Phase 2 proved out: the same deterministic risk-scoring
pattern (``merge_risk.evaluate_change_risk``), the same cleared-field
explainability investigation (``ai.change_inspection``), and the same
non-blocking, additive-only posture — a commit must always succeed on its
own; this review only ever runs strictly AFTER a commit already succeeded
(the frontend calls it once ``commit_id`` exists), never as a gate.

Mirrors ``ai/merge_agent.py``'s PLAN -> INVESTIGATE -> SCORE -> EXPLAIN
agentic loop and audit primitives (``AI_AGENT_RUNS``/``AI_AGENT_STEPS``),
but scoped to ``resource_type="COMMIT"`` and a single commit's own changes
rather than a merge request's branch-vs-main diff.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .. import database
from ..merge_risk import deterministic_risk_level, evaluate_change_risk
from ..observability import record_audit_event
from ..repositories.commit_store import get_commit, reconstruct_branch
from . import agent_runtime
from .change_inspection import column_names, detect_cleared_fields, investigate_cleared_field
from .classification_util import repository_classification
from .evidence import build_commit_evidence
from .gateway import ai_gateway
from .response_parsing import extract_recommendation_and_summary
from .service import ai_service

_ALLOWED_RECOMMENDATIONS = {"LOOKS_GOOD", "REVIEW_RECOMMENDED"}
_RISK_TO_BASELINE_RECOMMENDATION = {
    "LOW": "LOOKS_GOOD", "MEDIUM": "LOOKS_GOOD", "HIGH": "REVIEW_RECOMMENDED", "CRITICAL": "REVIEW_RECOMMENDED",
}
_RECOMMENDATION_ORDER = {"LOOKS_GOOD": 0, "REVIEW_RECOMMENDED": 1}


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def get_latest_review(commit_id: str) -> dict[str, Any] | None:
    conn = database._get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM COMMIT_AI_REVIEWS WHERE COMMIT_ID=? ORDER BY CREATED_AT DESC LIMIT 1", (commit_id,)
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


def _question_for_commit_review(
    commit: dict[str, Any], field_investigations: list[dict[str, Any]], risk_result, baseline_recommendation: str,
) -> str:
    field_lines = "\n".join(
        f"- {item['column_name']} (row {item['row_id']}): cleared from {item['previous_value']!r} to empty, "
        f"last touched by {item['changed_by'] or 'unknown'} "
        f"({item['prior_edit_count']} prior edit(s) recorded for this cell)."
        for item in field_investigations
    )
    field_block = (
        f"\nFields this commit cleared to empty (investigate and explain EACH one by name):\n{field_lines}\n"
        if field_investigations else "\nNo fields were cleared to empty in this commit.\n"
    )
    risk_lines = "\n".join(
        f"- {component['dimension']}: {component['score']}/100 ({component['classification']}, weight {component['weight']}) "
        f"— {component['explanation']} Evidence: {component['evidence']}"
        for component in risk_result.to_dict()["components"]
    )
    return (
        "A deterministic risk-scoring engine has already computed the authoritative risk assessment below "
        "for this single commit to a personal branch — it is NOT your job to invent a risk level. Baseline: "
        f"risk_level={deterministic_risk_level(risk_result.score)} (score {risk_result.score}/100), baseline "
        f"recommendation={baseline_recommendation}. Component breakdown:\n{risk_lines}\n"
        "\nYour job is to explain this baseline to the author in plain language, citing the specific changes "
        "that drive each component's score.\n"
        + field_block +
        "Return recommended_actions as a list: one entry per cleared field above with action_type "
        "'FIELD_EXPLANATION', title set to the exact column name, and rationale explaining specifically why "
        "that field is likely now empty based on the evidence — say 'cannot be determined from available "
        "history' if the evidence doesn't support a confident explanation, don't guess. Then add exactly one "
        "final entry with action_type set to LOOKS_GOOD or REVIEW_RECOMMENDED "
        f"(it must be at least as cautious as the baseline recommendation, {baseline_recommendation} — you "
        "may escalate to REVIEW_RECOMMENDED if you spot something the scoring engine missed, but never "
        "downgrade it), and rationale summarizing the commit's risk in plain language for its own author. "
        "This is a personal commit, not a merge — you are informing the author, not approving anything.\n"
        f"Commit message: {commit.get('message')!r}. Total changes: {len(commit.get('changes') or [])}."
    )


def _parse_commit_review(
    result: dict[str, Any], field_investigations: list[dict[str, Any]], risk_result, baseline_recommendation: str,
) -> dict[str, Any]:
    actions = result.get("recommended_actions") or []
    overall, summary = extract_recommendation_and_summary(result, _ALLOWED_RECOMMENDATIONS)
    field_explanations = {
        str(item.get("title", "")).strip(): item.get("rationale", "")
        for item in actions if str(item.get("action_type", "")).upper() == "FIELD_EXPLANATION"
    }
    ai_recommendation = str((overall or {}).get("action_type") or baseline_recommendation).upper()
    if ai_recommendation not in _ALLOWED_RECOMMENDATIONS:
        ai_recommendation = baseline_recommendation
    recommendation = max(
        ai_recommendation, baseline_recommendation, key=lambda item: _RECOMMENDATION_ORDER[item],
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
        "risk_level": deterministic_risk_level(risk_result.score),
        "risk_score": risk_result.score,
        "risk_breakdown": risk_result.to_dict()["components"],
        "warnings": result.get("warnings") or [],
        "ai_request_id": result.get("ai_request_id"),
        "field_investigations": explained_fields,
    }


def _begin_commit_review_run(organization_id: str, user_id: str, commit_id: str) -> str:
    conn = database._get_connection()
    now = database._utcnow()
    run_id = _id("AIAR")
    try:
        agent = conn.execute(
            "SELECT * FROM AI_AGENTS WHERE AGENT_KEY='COMMIT_REVIEW_AGENT' AND STATUS='ACTIVE'"
        ).fetchone()
        ai_service._ensure_settings(conn, organization_id, user_id)
        conn.execute(
            "INSERT INTO AI_AGENT_RUNS VALUES (?,?,?,?,NULL,?,?,?,?,0,?,NULL,?,NULL,NULL)",
            (
                run_id, organization_id, user_id, agent["AGENT_ID"] if agent else None,
                f"Review risk for commit {commit_id}", "COMMIT", commit_id, "RUNNING",
                agent["MAX_STEPS"] if agent else 6, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


async def review_commit(organization_id: str, user_id: str, commit_id: str) -> dict[str, Any]:
    """PLAN -> INVESTIGATE -> SCORE -> EXPLAIN. Runs strictly after the
    commit has already succeeded (see module docstring) and never mutates
    it — purely an informational review for the commit's own author.
    Fully synchronous -- see ``start_commit_review`` for the background-
    launching twin sharing this same logic."""
    commit = get_commit(commit_id)
    if not commit:
        raise KeyError("Commit does not exist")
    ai_service._require(user_id, "ai.agent.run", organization_id)
    run_id = _begin_commit_review_run(organization_id, user_id, commit_id)
    return await _run_commit_review(run_id, organization_id, user_id, commit)


def start_commit_review(organization_id: str, user_id: str, commit_id: str) -> dict[str, Any]:
    """Background-launching twin of ``review_commit``."""
    commit = get_commit(commit_id)
    if not commit:
        raise KeyError("Commit does not exist")
    ai_service._require(user_id, "ai.agent.run", organization_id)
    run_id = _begin_commit_review_run(organization_id, user_id, commit_id)

    async def _body() -> None:
        try:
            await _run_commit_review(run_id, organization_id, user_id, commit)
        except Exception as exc:
            agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))

    agent_runtime.launch(_body())
    return {"agent_run_id": run_id, "status": "RUNNING"}


async def _run_commit_review(run_id: str, organization_id: str, user_id: str, commit: dict[str, Any]) -> dict[str, Any]:
    commit_id = commit["commit_id"]
    changes = commit.get("changes") or []
    cleared_changes = detect_cleared_fields(changes)
    steps = [ai_service._record_agent_step(
        run_id, 1, "PLAN", "COMPLETED",
        f"Identified {len(cleared_changes)} field(s) cleared to empty out of {len(changes)} change(s)",
    )]

    branch_state = reconstruct_branch(commit["branch_id"], commit_id)
    names = column_names(branch_state)
    field_investigations = [investigate_cleared_field(commit["branch_id"], change, names) for change in cleared_changes]
    tool_call = ai_service._record_read_tool_call(
        organization_id, user_id, run_id, None, "get_commit_change_context",
        {"commit_id": commit_id, "cleared_field_count": len(field_investigations)},
        {"fields_investigated": [item["column_name"] for item in field_investigations]},
    ) if field_investigations else None
    steps.append(ai_service._record_agent_step(
        run_id, 2, "INVESTIGATE", "COMPLETED",
        f"Pulled edit history for {len(field_investigations)} cleared field(s)"
        if field_investigations else "No cleared fields required investigation",
        tool_call["tool_call_id"] if tool_call else None,
    ))

    risk_result = evaluate_change_risk(
        changes, conflicts=[], change_summary={"total": len(changes)},
        field_investigations=field_investigations, include_conflicts=False,
    )
    risk_level = deterministic_risk_level(risk_result.score)
    baseline = _RISK_TO_BASELINE_RECOMMENDATION[risk_level]
    steps.append(ai_service._record_agent_step(
        run_id, 3, "SCORE", "COMPLETED",
        f"Deterministic risk engine scored this commit {risk_result.score}/100 ({risk_level}); "
        f"baseline recommendation {baseline}",
    ))

    evidence = build_commit_evidence(
        changes, field_investigations=field_investigations, risk_breakdown=risk_result.to_dict()["components"],
    )

    result = await ai_gateway.generate(
        organization_id=organization_id, user_id=user_id, feature="commit_risk_review",
        question=_question_for_commit_review(commit, field_investigations, risk_result, baseline),
        evidence=evidence, context_hash=_hash({"commit_id": commit_id, "evidence": evidence}),
        classification=repository_classification(commit.get("repository_id")), agent_key="COMMIT_REVIEW_AGENT", agent_run_id=run_id,
    )
    parsed = _parse_commit_review(result, field_investigations, risk_result, baseline)
    steps.append(ai_service._record_agent_step(
        run_id, 4, "EXPLAIN", "COMPLETED",
        f"Recommended {parsed['recommendation']} ({parsed['risk_level']} risk, score {parsed['risk_score']}/100) "
        f"with {len(parsed['field_investigations'])} field explanation(s)",
    ))

    review_id = _id("CAR")
    conn = database._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO COMMIT_AI_REVIEWS
                (REVIEW_ID, COMMIT_ID, REPOSITORY_ID, BRANCH_ID, RISK_LEVEL, RISK_SCORE, RISK_BREAKDOWN_JSON,
                 FIELD_INVESTIGATIONS_JSON, RECOMMENDATION, SUMMARY, CONFIDENCE, WARNINGS_JSON,
                 AI_REQUEST_ID, CREATED_BY, CREATED_AT, INSUFFICIENT_EVIDENCE)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(COMMIT_ID) DO UPDATE SET
                RISK_LEVEL=excluded.RISK_LEVEL, RISK_SCORE=excluded.RISK_SCORE,
                RISK_BREAKDOWN_JSON=excluded.RISK_BREAKDOWN_JSON,
                FIELD_INVESTIGATIONS_JSON=excluded.FIELD_INVESTIGATIONS_JSON,
                RECOMMENDATION=excluded.RECOMMENDATION, SUMMARY=excluded.SUMMARY,
                CONFIDENCE=excluded.CONFIDENCE, WARNINGS_JSON=excluded.WARNINGS_JSON,
                AI_REQUEST_ID=excluded.AI_REQUEST_ID, CREATED_AT=excluded.CREATED_AT,
                INSUFFICIENT_EVIDENCE=excluded.INSUFFICIENT_EVIDENCE
            """,
            (
                review_id, commit_id, commit["repository_id"], commit["branch_id"],
                parsed["risk_level"], parsed["risk_score"], json.dumps(parsed["risk_breakdown"], default=str),
                json.dumps(parsed["field_investigations"], default=str), parsed["recommendation"],
                parsed["summary"], parsed["confidence"], json.dumps(parsed["warnings"]),
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
        "AI_COMMIT_REVIEW_COMPLETED", actor_user_id=user_id,
        repository_id=commit["repository_id"], branch_id=commit["branch_id"], commit_id=commit_id,
        payload={"review_id": review_id, "recommendation": parsed["recommendation"], "risk_level": parsed["risk_level"],
                 "agent_run_id": run_id, "fields_investigated": len(field_investigations)},
    )
    return {"review_id": review_id, "commit_id": commit_id, **parsed}
