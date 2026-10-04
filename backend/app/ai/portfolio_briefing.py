"""Agentic Portfolio Briefing.

Turns the Portfolio Risk Command Center's already-deterministic, already-
correct org-wide roll-up (``euc.portfolio.portfolio_risk_overview`` /
``portfolio_open_findings``) into a manager-readable narrative -- the same
deterministic-authoritative-numbers-plus-AI-narration posture as the Team
Health Digest, applied at organization scale instead of one repository.
No RAG here: unlike a merge conflict or an anomalous access pattern, a
risk rollup has no useful "precedent" to retrieve against -- the numbers
either are what they are or they aren't, so grounding is the aggregate
summary and the specific findings behind it, nothing else.

PLAN -> GATHER -> EXPLAIN loop, audited via the same
AI_AGENT_RUNS/AI_AGENT_STEPS primitives every other agent in this codebase
uses. Purely read-only. Every briefing is persisted to
``PORTFOLIO_BRIEFINGS`` so a risk owner can compare this run's numbers
against a prior one, not just see the latest snapshot.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .. import database
from ..euc.portfolio import portfolio_open_findings, portfolio_risk_overview
from ..observability import record_audit_event
from . import agent_runtime
from .gateway import ai_gateway
from .response_parsing import sanitize_free_text
from .retrieval import EvidenceItem
from .service import ai_service

_TOP_REPOSITORY_COUNT = 5


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _build_observations(summary: dict[str, Any], top_assets: list[dict[str, Any]]) -> list[str]:
    observations = [
        f"{summary['scored_assets']} of {summary['total_assets']} EUC asset(s) have a current risk score; "
        f"{summary['not_scored_assets']} are awaiting analysis.",
    ]
    if summary["high_risk_count"]:
        observations.append(f"{summary['high_risk_count']} asset(s) are rated HIGH or VERY_HIGH residual risk.")
    if summary["critical_finding_total"]:
        observations.append(f"{summary['critical_finding_total']} CRITICAL-severity finding(s) remain open across the portfolio.")
    if summary["stale_count"]:
        observations.append(f"{summary['stale_count']} scored asset(s) are stale -- their last analysis predates the repository's current commit.")
    if top_assets:
        leader = top_assets[0]
        observations.append(
            f"The highest single residual-risk asset is '{leader['filename']}' in {leader['repository_name']} "
            f"(residual risk {leader['residual_risk']}, {leader['critical_finding_count']} critical finding(s))."
        )
    if len(observations) == 1:
        observations.append("No elevated risk, critical findings, or stale scores detected this period.")
    return observations


def _question_for_briefing(observations: list[str]) -> str:
    return (
        "These are real, already-computed observations about this organization's EUC risk portfolio -- every "
        "number and name comes straight from the deterministic scoring engine and the findings register, none of "
        "it is estimated. Write a short, plain-language briefing (3-5 sentences) for a risk owner, leading with "
        "the single biggest concern, exactly as if handing them a one-paragraph executive summary. Do not invent "
        "any number, asset name, or repository name not already present in the observations."
    )


def get_latest_briefing(organization_id: str) -> dict[str, Any] | None:
    conn = database._get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM PORTFOLIO_BRIEFINGS WHERE ORGANIZATION_ID=? ORDER BY CREATED_AT DESC LIMIT 1",
            (organization_id,),
        ).fetchone()
        if not row:
            return None
        item = {key.lower(): row[key] for key in row.keys()}
        item["top_risk_repositories"] = json.loads(item.pop("top_risk_repositories_json") or "[]")
        item["observations"] = json.loads(item.pop("observations_json") or "[]")
        item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
        return item
    finally:
        conn.close()


async def _run_portfolio_briefing(run_id: str, organization_id: str, user_id: str) -> dict[str, Any]:
    overview = portfolio_risk_overview(user_id, organization_id=organization_id)
    summary, assets = overview["summary"], overview["assets"]
    scored = [item for item in assets if item["status"] == "SCORED"]
    top_assets = sorted(scored, key=lambda item: -(item["residual_risk"] or 0))[:_TOP_REPOSITORY_COUNT]
    ai_service._record_agent_step(
        run_id, 1, "PLAN", "COMPLETED",
        f"Loaded the portfolio roll-up: {summary['total_assets']} asset(s), {summary['scored_assets']} scored",
    )

    findings = portfolio_open_findings(user_id, organization_id=organization_id, limit=15)
    tool_call = ai_service._record_read_tool_call(
        organization_id, user_id, run_id, None, "get_portfolio_context",
        {"organization_id": organization_id}, {"top_assets": len(top_assets), "open_findings": len(findings)},
    )
    ai_service._record_agent_step(
        run_id, 2, "GATHER", "COMPLETED",
        f"Pulled the {len(top_assets)} highest-risk asset(s) and {len(findings)} open critical/high finding(s)",
        tool_call["tool_call_id"],
    )

    observations = _build_observations(summary, top_assets)
    evidence = [
        EvidenceItem(
            type="portfolio_summary", id=organization_id, title="Portfolio risk summary",
            summary="Aggregate residual risk, finding counts, and staleness across every governed EUC asset", data=summary,
        ).serializable(),
    ] + [
        EvidenceItem(
            type="portfolio_asset", id=asset["euc_id"], title=f"{asset['repository_name']}: {asset['filename']}",
            summary=f"Residual risk {asset['residual_risk']}, {asset['critical_finding_count']} critical finding(s)", data=asset,
        ).serializable()
        for asset in top_assets
    ] + [
        EvidenceItem(
            type="euc_finding", id=finding["finding_id"], title=f"{finding['repository_name']}: {finding['title']}",
            summary=finding["description"] or "Open deterministic EUC finding", data=finding,
        ).serializable()
        for finding in findings
    ]

    result = await ai_gateway.generate(
        organization_id=organization_id, user_id=user_id, feature="portfolio_briefing",
        question=_question_for_briefing(observations), evidence=evidence,
        context_hash=_hash({"organization_id": organization_id, "observations": observations}),
        classification="INTERNAL", agent_key="PORTFOLIO_BRIEFING_AGENT", agent_run_id=run_id,
    )
    # A free model occasionally echoes its entire structured response back
    # into `answer` instead of one prose briefing -- sanitized so that
    # never reaches a user; the deterministic observations above always
    # stand on their own even if this narration comes back empty.
    narrative = sanitize_free_text(result.get("answer")) or None
    ai_service._record_agent_step(run_id, 3, "EXPLAIN", "COMPLETED", "Composed the plain-language portfolio briefing")

    briefing_id = _id("PBR")
    conn = database._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO PORTFOLIO_BRIEFINGS
                (BRIEFING_ID, ORGANIZATION_ID, TOTAL_ASSETS, HIGH_RISK_COUNT, CRITICAL_FINDING_TOTAL,
                 AVERAGE_RESIDUAL_RISK, STALE_COUNT, TOP_RISK_REPOSITORIES_JSON, OBSERVATIONS_JSON, NARRATIVE,
                 CONFIDENCE, WARNINGS_JSON, AI_REQUEST_ID, CREATED_BY, CREATED_AT)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                briefing_id, organization_id, summary["total_assets"], summary["high_risk_count"],
                summary["critical_finding_total"], summary["average_residual_risk"], summary["stale_count"],
                json.dumps([{"repository_name": a["repository_name"], "filename": a["filename"], "residual_risk": a["residual_risk"]} for a in top_assets]),
                json.dumps(observations), narrative, float(result.get("confidence") or 0.0),
                json.dumps(result.get("warnings") or []), result.get("ai_request_id"), user_id, database._utcnow(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    record_audit_event(
        "AI_PORTFOLIO_BRIEFING_COMPLETED", actor_user_id=user_id,
        payload={"organization_id": organization_id, "agent_run_id": run_id, "briefing_id": briefing_id},
    )
    return {
        "briefing_id": briefing_id, "summary": summary, "top_risk_repositories": top_assets,
        "observations": observations, "narrative": narrative, "confidence": float(result.get("confidence") or 0.0),
        "warnings": result.get("warnings") or [],
    }


async def generate_portfolio_briefing(organization_id: str, user_id: str) -> dict[str, Any]:
    """Fully synchronous entrypoint used by tests and any server-to-server
    caller that wants the briefing directly."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    run_id, _ = agent_runtime.create_run(
        organization_id, user_id, "PORTFOLIO_BRIEFING_AGENT",
        "Brief the EUC risk portfolio", "PORTFOLIO", organization_id,
    )
    try:
        result = await _run_portfolio_briefing(run_id, organization_id, user_id)
        agent_runtime.finish_run(run_id, 3)
        return {"agent_run_id": run_id, **result}
    except Exception as exc:
        agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))
        raise


def start_portfolio_briefing(organization_id: str, user_id: str) -> dict[str, Any]:
    """Background-launching entrypoint for the live-progress UI."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    run_id, _ = agent_runtime.create_run(
        organization_id, user_id, "PORTFOLIO_BRIEFING_AGENT",
        "Brief the EUC risk portfolio", "PORTFOLIO", organization_id,
    )

    async def _body() -> None:
        try:
            await _run_portfolio_briefing(run_id, organization_id, user_id)
            agent_runtime.finish_run(run_id, 3)
        except Exception as exc:
            agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))

    agent_runtime.launch(_body())
    return {"agent_run_id": run_id, "status": "RUNNING"}
