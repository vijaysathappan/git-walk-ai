"""Agentic "Risk Drift Radar": explains a branch's EUC risk finding drift
against main, with per-finding commit attribution.

No competitor examined this session has EUC risk/dependency/complexity
scoring at all -- Phase 2/3's deterministic-authoritative/AI-explains
pattern already differentiates this product on that alone. This module goes
one step further: for every risk finding a branch newly introduces, it
answers "who introduced this, in which commit, and when" -- a "git blame
for spreadsheet risk" -- which is only possible because this product
uniquely has both formula-dependency-level static analysis
(``euc.intelligence``) and per-cell commit authorship
(``repositories.commit_store``) in the same system.

Mirrors ``ai/merge_agent.py``/``ai/commit_review.py``'s PLAN -> ... ->
EXPLAIN agentic loop and audit primitives (``AI_AGENT_RUNS``/
``AI_AGENT_STEPS``), scoped to ``resource_type="EUC_BRANCH_COMPARISON"``.
There is no deterministic-baseline-authority conflict to guard here -- the
introduced/resolved finding list and severities are already fully
deterministic (``euc.intelligence``'s scoring engines); the AI's only job is
narrative synthesis, not inventing a risk level.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .. import database
from ..euc.branch_comparison import (
    attribute_finding,
    diff_findings,
    resolve_main_branch,
    snapshot_branch_for_comparison,
)
from ..excel.identity import semantic_snapshot
from ..observability import record_audit_event
from ..repositories.merge_store import branch_context
from . import agent_runtime
from .classification_util import repository_classification
from .gateway import ai_gateway
from .response_parsing import sanitize_free_text
from .retrieval import EvidenceItem
from .service import ai_service

_MAX_ATTRIBUTIONS = 25


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def get_latest_comparison(branch_id: str) -> dict[str, Any] | None:
    conn = database._get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM EUC_BRANCH_COMPARISONS WHERE BRANCH_ID=? ORDER BY CREATED_AT DESC LIMIT 1", (branch_id,)
        ).fetchone()
        if not row:
            return None
        item = {key.lower(): row[key] for key in row.keys()}
        item["findings_introduced"] = json.loads(item["findings_introduced_json"]) if item.get("findings_introduced_json") else []
        item["findings_resolved"] = json.loads(item["findings_resolved_json"]) if item.get("findings_resolved_json") else []
        item["attribution"] = json.loads(item["attribution_json"]) if item.get("attribution_json") else []
        item["warnings"] = json.loads(item["warnings_json"]) if item.get("warnings_json") else []
        return item
    finally:
        conn.close()


def _question_for_drift(
    introduced: list[dict[str, Any]], resolved: list[dict[str, Any]], attribution_by_finding: dict[str, dict[str, Any]],
) -> str:
    introduced_lines = "\n".join(
        f"- [{item.get('severity')}] {item.get('title')} at {item.get('sheet_id')}!{item.get('cell_address') or 'N/A'} "
        + (
            f"(introduced by {attribution_by_finding[item['finding_id']]['author_email'] or 'unknown'} in commit "
            f"{attribution_by_finding[item['finding_id']]['commit_id']} on {attribution_by_finding[item['finding_id']]['occurred_at']})"
            if item.get("finding_id") in attribution_by_finding else "(cannot be attributed to a single commit)"
        )
        for item in introduced
    ) or "None."
    resolved_lines = "\n".join(
        f"- [{item.get('severity')}] {item.get('title')} at {item.get('sheet_id')}!{item.get('cell_address') or 'N/A'}"
        for item in resolved
    ) or "None."
    return (
        "A deterministic EUC risk-scoring engine has already computed the finding lists below by comparing this "
        "branch's current state against main -- these lists and their attribution are NOT your job to invent or "
        "second-guess.\n\nRisk findings this branch INTRODUCES relative to main:\n" + introduced_lines +
        "\n\nRisk findings this branch RESOLVES relative to main:\n" + resolved_lines +
        "\n\nYour job is to explain, in plain business language for a non-technical repository owner, what this "
        "risk drift means -- prioritize the introduced findings, cite who introduced each one and when (when "
        "known), and explain in one sentence why the resolved findings are good news. Return recommended_actions "
        "as an empty list; put your full narrative in 'answer'."
    )


def _begin_drift_comparison_run(organization_id: str, user_id: str, branch_id: str) -> str:
    conn = database._get_connection()
    now = database._utcnow()
    run_id = _id("AIAR")
    try:
        agent = conn.execute(
            "SELECT * FROM AI_AGENTS WHERE AGENT_KEY='EUC_RISK_RADAR_AGENT' AND STATUS='ACTIVE'"
        ).fetchone()
        ai_service._ensure_settings(conn, organization_id, user_id)
        conn.execute(
            "INSERT INTO AI_AGENT_RUNS VALUES (?,?,?,?,NULL,?,?,?,?,0,?,NULL,?,NULL,NULL)",
            (
                run_id, organization_id, user_id, agent["AGENT_ID"] if agent else None,
                f"Compare EUC risk drift for branch {branch_id} against main",
                "EUC_BRANCH_COMPARISON", branch_id, "RUNNING", agent["MAX_STEPS"] if agent else 6, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


async def compare_branch_inventory(
    organization_id: str, user_id: str, repository_id: str, branch_id: str,
) -> dict[str, Any]:
    """PLAN -> SNAPSHOT -> ATTRIBUTE -> EXPLAIN. Runs the full, unmodified
    EUC pipeline against both main's and this branch's current state, diffs
    the resulting findings, attributes every introduced finding to the
    commit/author that caused it, and has the AI narrate the result.
    Fully synchronous -- see ``start_branch_comparison`` for the
    background-launching twin sharing this same logic."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    branch = branch_context(branch_id)
    if not branch:
        raise KeyError("Branch does not exist")
    main = resolve_main_branch(repository_id)
    run_id = _begin_drift_comparison_run(organization_id, user_id, branch_id)
    return await _run_branch_comparison(run_id, organization_id, user_id, repository_id, branch_id, branch, main)


def start_branch_comparison(organization_id: str, user_id: str, repository_id: str, branch_id: str) -> dict[str, Any]:
    """Background-launching twin of ``compare_branch_inventory``."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    branch = branch_context(branch_id)
    if not branch:
        raise KeyError("Branch does not exist")
    main = resolve_main_branch(repository_id)
    run_id = _begin_drift_comparison_run(organization_id, user_id, branch_id)

    async def _body() -> None:
        try:
            await _run_branch_comparison(run_id, organization_id, user_id, repository_id, branch_id, branch, main)
        except Exception as exc:
            agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))

    agent_runtime.launch(_body())
    return {"agent_run_id": run_id, "status": "RUNNING"}


async def _run_branch_comparison(
    run_id: str, organization_id: str, user_id: str, repository_id: str, branch_id: str,
    branch: dict[str, Any], main: dict[str, Any],
) -> dict[str, Any]:
    steps = [ai_service._record_agent_step(
        run_id, 1, "PLAN", "COMPLETED",
        f"Comparing branch {branch['branch_name']} against main branch {main['branch_name']}",
    )]

    main_snapshot = snapshot_branch_for_comparison(
        repository_id, main["branch_id"], main["branch_name"], main["data_table_id"], user_id,
    )
    branch_snapshot = snapshot_branch_for_comparison(
        repository_id, branch_id, branch["branch_name"], branch["data_table_id"], user_id,
    )
    steps.append(ai_service._record_agent_step(
        run_id, 2, "SNAPSHOT", "COMPLETED",
        f"Analyzed main ({len(main_snapshot['findings'])} finding(s)) and "
        f"{branch['branch_name']} ({len(branch_snapshot['findings'])} finding(s))",
    ))

    diff = diff_findings(main_snapshot["findings"], branch_snapshot["findings"])
    conn = database._get_connection()
    try:
        branch_state = semantic_snapshot(conn, branch["data_table_id"])
    finally:
        conn.close()
    attributions = []
    for finding in diff["introduced"][:_MAX_ATTRIBUTIONS]:
        attribution = attribute_finding(branch_id, branch_state, finding)
        if attribution:
            attributions.append(attribution)
    attribution_by_finding = {item["finding_id"]: item for item in attributions}

    tool_call = ai_service._record_read_tool_call(
        organization_id, user_id, run_id, None, "get_euc_risk_drift_context",
        {"branch_id": branch_id, "introduced_count": len(diff["introduced"]), "resolved_count": len(diff["resolved"])},
        {"attributed_count": len(attributions)},
    )
    steps.append(ai_service._record_agent_step(
        run_id, 3, "ATTRIBUTE", "COMPLETED",
        f"Diffed findings ({len(diff['introduced'])} introduced, {len(diff['resolved'])} resolved) and "
        f"attributed {len(attributions)} to a specific commit/author",
        tool_call["tool_call_id"] if tool_call else None,
    ))

    evidence = [
        EvidenceItem(
            type="euc_finding_introduced", id=str(item.get("finding_id")),
            title=str(item.get("title") or "Introduced finding"),
            summary=f"{item.get('severity')} severity, category {item.get('category')}", data=item,
        ).serializable()
        for item in diff["introduced"]
    ] + [
        EvidenceItem(
            type="euc_finding_resolved", id=str(item.get("finding_id")),
            title=str(item.get("title") or "Resolved finding"),
            summary=f"{item.get('severity')} severity, category {item.get('category')}", data=item,
        ).serializable()
        for item in diff["resolved"]
    ] + [
        EvidenceItem(
            type="finding_attribution", id=str(item.get("finding_id")),
            title=f"Introduced by {item.get('author_email') or 'unknown author'}",
            summary=f"Commit {item.get('commit_id')} on {item.get('occurred_at')}", data=item,
        ).serializable()
        for item in attributions
    ]

    risk_score_delta = round(
        branch_snapshot["scores"]["inherent_risk"] - main_snapshot["scores"]["inherent_risk"], 2,
    )
    result = await ai_gateway.generate(
        organization_id=organization_id, user_id=user_id, feature="euc_branch_narrative",
        question=_question_for_drift(diff["introduced"], diff["resolved"], attribution_by_finding),
        evidence=evidence, context_hash=_hash({"branch_id": branch_id, "evidence": evidence}),
        classification=repository_classification(repository_id), agent_key="EUC_RISK_RADAR_AGENT", agent_run_id=run_id,
    )
    steps.append(ai_service._record_agent_step(
        run_id, 4, "EXPLAIN", "COMPLETED",
        f"Narrated risk drift (score delta {risk_score_delta:+.2f})",
    ))

    # A free model occasionally echoes its entire structured response back
    # into `answer` instead of one prose narrative -- sanitized so that
    # never reaches a user; the deterministic finding diff/attribution
    # above always stands on its own even if this narration comes back
    # empty.
    narrative = sanitize_free_text(result.get("answer"))
    comparison_id = _id("EBC")
    conn = database._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO EUC_BRANCH_COMPARISONS
                (COMPARISON_ID, REPOSITORY_ID, BRANCH_ID, MAIN_EUC_ID, BRANCH_EUC_ID, RISK_SCORE_DELTA,
                 FINDINGS_INTRODUCED_JSON, FINDINGS_RESOLVED_JSON, ATTRIBUTION_JSON, SUMMARY, CONFIDENCE,
                 WARNINGS_JSON, AI_REQUEST_ID, CREATED_BY, CREATED_AT)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                comparison_id, repository_id, branch_id, main_snapshot["euc_id"], branch_snapshot["euc_id"],
                risk_score_delta, json.dumps(diff["introduced"], default=str), json.dumps(diff["resolved"], default=str),
                json.dumps(attributions, default=str), narrative, float(result.get("confidence") or 0.0),
                json.dumps(result.get("warnings") or []), result.get("ai_request_id"), user_id, database._utcnow(),
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
        "AI_EUC_BRANCH_COMPARISON_COMPLETED", actor_user_id=user_id, repository_id=repository_id, branch_id=branch_id,
        payload={"comparison_id": comparison_id, "introduced": len(diff["introduced"]), "resolved": len(diff["resolved"]),
                 "attributed": len(attributions), "agent_run_id": run_id},
    )
    severe_introduced = [item for item in diff["introduced"] if item.get("severity") in {"HIGH", "CRITICAL"}]
    if severe_introduced:
        try:
            owner_id = main.get("repository_owner_id")
            if owner_id and owner_id != user_id:
                database.create_notification(
                    owner_id, "EUC_RISK_DRIFT", f"Branch {branch['branch_name']} introduces {len(severe_introduced)} high-risk finding(s)",
                    f"Risk Drift Radar found {len(severe_introduced)} new HIGH/CRITICAL finding(s) on {branch['branch_name']} vs main.",
                    resource_type="EUC_BRANCH_COMPARISON", resource_id=comparison_id,
                )
        except Exception:
            pass
    return {
        "comparison_id": comparison_id, "risk_score_delta": risk_score_delta,
        "findings_introduced": diff["introduced"], "findings_resolved": diff["resolved"],
        "attribution": attributions, "summary": narrative,
        "confidence": float(result.get("confidence") or 0.0), "warnings": result.get("warnings") or [],
    }
