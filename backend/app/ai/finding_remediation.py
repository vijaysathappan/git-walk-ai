"""AI-Assisted Finding Remediation.

Investigates one EUC finding's full evidence (the observed evidence blob,
dependency blast radius, and any prior lifecycle actions already recorded
against it) and drafts a recommended lifecycle status + a written reason,
using the same ``AIGateway``/``GroundedAnswer`` contract as every other
agent in this codebase.

Deliberately does NOT have the AI auto-rewrite the workbook's formulas —
inventing a "corrected" formula for an arbitrary broken reference is a
real-data-integrity risk the AI cannot reliably ground, and would break
the "never invent a baseline you can't verify" principle already
established by the commit-review and merge-conflict agents. Instead this
turns the 30-45 minutes an analyst spends reading a finding, deciding
what it means, and writing a lifecycle reason into a drafted
recommendation the analyst reviews and confirms in a couple of minutes —
the actual write path is the SAME ``IntelligenceService.update_finding``
a human clicking through the findings register already uses, gated
through the existing ``_prepare_action``/``confirm_action`` governed-
action mechanism, not a new write path.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .. import database
from ..euc.intelligence import IntelligenceService
from . import agent_runtime
from .classification_util import euc_classification
from .evidence import build_finding_evidence
from .gateway import ai_gateway
from .response_parsing import sanitize_free_text
from .service import ai_service

_intelligence_service = IntelligenceService()

# RESOLVED and SUPPRESSED require a human's direct certainty that the
# underlying issue is actually fixed or genuinely irrelevant — the agent
# may only recommend statuses that keep a human review step meaningful.
_ALLOWED_RECOMMENDATIONS = {"ACKNOWLEDGED", "IN_REVIEW", "REMEDIATION_PLANNED", "ACCEPTED_RISK", "FALSE_POSITIVE"}
_DEFAULT_RECOMMENDATION = "ACKNOWLEDGED"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _question_for_finding(finding: dict[str, Any]) -> str:
    return (
        "You are triaging one EUC (end-user-computing spreadsheet) governance finding. "
        "Using only the evidence provided (the finding's observed evidence, its downstream dependency "
        "impact, and any prior lifecycle actions already recorded against it), recommend what should "
        "happen to it next. Express your recommendation as one recommended_actions entry whose "
        f"action_type is exactly one of: {', '.join(sorted(_ALLOWED_RECOMMENDATIONS))}. "
        "Write the rationale as the exact reason text a reviewer would record when applying that "
        "status change — specific to this finding's actual evidence, not a generic template. "
        "Do not propose a specific corrected formula; you may describe what kind of fix is needed "
        "in the rationale, but the actual formula edit is made by a human, not by you."
    )


def _parse_recommendation(result: dict[str, Any]) -> dict[str, Any]:
    actions = result.get("recommended_actions") or []
    primary = actions[0] if actions else {}
    status = str(primary.get("action_type") or _DEFAULT_RECOMMENDATION).upper()
    if status not in _ALLOWED_RECOMMENDATIONS:
        status = _DEFAULT_RECOMMENDATION
    reason = sanitize_free_text(primary.get("rationale")) or sanitize_free_text(result.get("answer"))
    if not reason:
        reason = "AI-drafted recommendation had no rationale; defaulting to acknowledge for manual review."
    return {
        "recommended_status": status, "reason": reason,
        "confidence": float(result.get("confidence") or 0.0),
        "risk_level": str(primary.get("risk_level") or "MEDIUM").upper(),
        "ai_request_id": result.get("ai_request_id"),
    }


def _begin_remediation_run(organization_id: str, user_id: str, finding_id: str) -> str:
    conn = database._get_connection()
    now = database._utcnow()
    run_id = _id("AIAR")
    try:
        agent = conn.execute(
            "SELECT * FROM AI_AGENTS WHERE AGENT_KEY='EUC_REMEDIATION_AGENT' AND STATUS='ACTIVE'"
        ).fetchone()
        if not agent:
            raise KeyError("The EUC finding remediation agent is not registered")
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
                f"Recommend remediation for finding {finding_id}",
                "EUC_FINDING", finding_id, "RUNNING", agent["MAX_STEPS"], now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


async def propose_remediation(
    organization_id: str, user_id: str, euc_id: str, finding_id: str,
) -> dict[str, Any]:
    """PLAN -> INVESTIGATE -> PROPOSE loop for one finding. Raises the same
    access/not-found errors ``IntelligenceService.finding_detail`` already
    raises, so the API layer's existing error mapping covers this too.
    Fully synchronous -- see ``start_remediation_proposal`` for the
    background-launching twin sharing this same logic."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    run_id = _begin_remediation_run(organization_id, user_id, finding_id)
    return await _run_remediation_proposal(run_id, organization_id, user_id, euc_id, finding_id)


def start_remediation_proposal(organization_id: str, user_id: str, euc_id: str, finding_id: str) -> dict[str, Any]:
    """Background-launching twin of ``propose_remediation``."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    run_id = _begin_remediation_run(organization_id, user_id, finding_id)

    async def _body() -> None:
        try:
            await _run_remediation_proposal(run_id, organization_id, user_id, euc_id, finding_id)
        except Exception as exc:
            agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))

    agent_runtime.launch(_body())
    return {"agent_run_id": run_id, "status": "RUNNING"}


async def _run_remediation_proposal(run_id: str, organization_id: str, user_id: str, euc_id: str, finding_id: str) -> dict[str, Any]:
    steps = [ai_service._record_agent_step(
        run_id, 1, "PLAN", "COMPLETED",
        f"Selected the EUC finding remediation agent to investigate finding {finding_id}",
    )]

    finding = _intelligence_service.finding_detail(euc_id, user_id, finding_id)

    tool_call = ai_service._record_read_tool_call(
        organization_id, user_id, run_id, None, "get_finding_context",
        {"euc_id": euc_id, "finding_id": finding_id},
        {"rule_id": finding.get("rule_id"), "severity": finding.get("severity"),
         "prior_actions": len(finding.get("actions") or [])},
    )
    steps.append(ai_service._record_agent_step(
        run_id, 2, "INVESTIGATE", "COMPLETED",
        f"Gathered evidence, dependency impact, and {len(finding.get('actions') or [])} prior "
        f"lifecycle action(s) for finding {finding_id}",
        tool_call["tool_call_id"],
    ))

    evidence = build_finding_evidence(finding)
    result = await ai_gateway.generate(
        organization_id=organization_id, user_id=user_id, feature="euc_finding_remediation",
        question=_question_for_finding(finding), evidence=evidence,
        context_hash=_hash({"finding_id": finding_id, "evidence": evidence}),
        classification=euc_classification(euc_id), agent_key="EUC_REMEDIATION_AGENT", agent_run_id=run_id,
    )
    parsed = _parse_recommendation(result)
    steps.append(ai_service._record_agent_step(
        run_id, 3, "PROPOSE", "COMPLETED",
        f"Recommended {parsed['recommended_status']} ({parsed['risk_level']} risk, "
        f"{round(parsed['confidence'] * 100)}% confidence)",
    ))

    remediation_id = _id("EFR")
    conn = database._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO EUC_FINDING_REMEDIATIONS
                (REMEDIATION_ID, FINDING_ID, EUC_ID, RECOMMENDED_STATUS, REASON, CONFIDENCE,
                 RISK_LEVEL, AI_REQUEST_ID, STATUS, CREATED_BY, CREATED_AT)
            VALUES (?,?,?,?,?,?,?,?,'PROPOSED',?,?)
            """,
            (
                remediation_id, finding_id, euc_id, parsed["recommended_status"], parsed["reason"],
                parsed["confidence"], parsed["risk_level"], parsed["ai_request_id"], user_id, database._utcnow(),
            ),
        )
        conn.execute(
            "UPDATE AI_AGENT_RUNS SET STATUS='COMPLETED',CURRENT_STEP=?,COMPLETED_AT=? WHERE AGENT_RUN_ID=?",
            (len(steps), database._utcnow(), run_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "remediation_id": remediation_id, "agent_run_id": run_id, "finding_id": finding_id,
        "euc_id": euc_id, "status": "PROPOSED", **parsed,
    }


def list_remediations(euc_id: str, finding_id: str | None = None) -> list[dict[str, Any]]:
    conn = database._get_connection()
    try:
        if finding_id:
            rows = conn.execute(
                "SELECT * FROM EUC_FINDING_REMEDIATIONS WHERE EUC_ID=? AND FINDING_ID=? ORDER BY CREATED_AT DESC",
                (euc_id, finding_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM EUC_FINDING_REMEDIATIONS WHERE EUC_ID=? ORDER BY CREATED_AT DESC",
                (euc_id,),
            ).fetchall()
        return [{key.lower(): row[key] for key in row.keys()} for row in rows]
    finally:
        conn.close()


def prepare_apply_action(
    organization_id: str, user_id: str, euc_id: str, finding_id: str,
) -> dict[str, Any]:
    """Prepare (but not execute) applying the most recent PROPOSED
    remediation for one finding. Returns a PENDING_CONFIRMATION action the
    caller confirms via the existing POST /ai-platform/actions/{id}/confirm
    endpoint, mirroring every other governed AI action in this codebase."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    remediation = next(
        (item for item in list_remediations(euc_id, finding_id) if item["status"] == "PROPOSED"), None,
    )
    if not remediation:
        raise KeyError("No proposed AI remediation exists for this finding")
    return ai_service._prepare_action(organization_id, user_id, None, {
        "action_type": "APPLY_FINDING_REMEDIATION",
        "euc_id": euc_id, "finding_id": finding_id,
        "remediation_id": remediation["remediation_id"],
        "recommended_status": remediation["recommended_status"],
        "reason": remediation["reason"],
    })
