"""Agentic "Who Should I Ask" Discovery.

Answers "who's the best person to ask about X on this repository right
now" by combining three signals this product has never cross-referenced
before: real per-sheet expertise (``expertise_service.repository_expertise``),
current open-review workload (the same ``open_workload_days`` metric Review
Queue Depth already surfaces), and LIVE presence
(``presence_store.repository_activity_overview``). The single most
knowledgeable person on paper is often the wrong answer if they're buried
in backlog or offline right now -- today a user has to manually
cross-reference three separate panels to work that out for themselves.

Which sheets are relevant to a topic is deterministic keyword overlap
against real sheet names, not a judgment call handed to the model --
picking the WRONG sheet would silently misdirect the whole ranking, so
that step stays pure Python. The deterministic composite ranking below is
authoritative; the AI's only job is to explain in plain language why the
top pick beats the runner-ups, citing the real numbers -- it can never
override the ranking (the same "explain, don't decide" guardrail every
other agent in this codebase already enforces).

PLAN -> RANK -> EXPLAIN loop, audited via the same
AI_AGENT_RUNS/AI_AGENT_STEPS primitives every other agent uses. Read-only:
this agent only recommends who to ask, it never assigns or notifies
anyone on the caller's behalf.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from .. import database
from ..observability import record_audit_event
from ..services.expertise_service import repository_expertise
from . import agent_runtime
from .gateway import ai_gateway
from .response_parsing import sanitize_free_text
from .retrieval import EvidenceItem
from .service import ai_service

_BACKLOG_WORKLOAD_DAYS = 10.0  # same threshold team_signals.py already uses to call a queue "backed up"
_MAX_CANDIDATES_RETURNED = 5


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(token) > 2}


def _presence_bonus(status: str | None, is_active_now: bool) -> float:
    if status == "NEED_HELP":
        return 0.8  # actively at their desk, just flagged for something else -- still reachable
    if is_active_now or status == "ONLINE":
        return 1.0
    return 0.3  # offline: still knowledgeable, just not immediately reachable


def _rank_candidates(topic: str, expertise: dict[str, Any], activity: list[dict[str, Any]]) -> list[dict[str, Any]]:
    topic_tokens = _tokenize(topic)
    activity_by_user = {item["user_id"]: item for item in activity}

    matched_sheets = [
        sheet for sheet in expertise["sheets"]
        if topic_tokens and (_tokenize(sheet["sheet_name"]) & topic_tokens)
    ]
    # No sheet name matched the topic (a generic question, or a topic that
    # doesn't map to a sheet name) -- fall back to org-wide expertise across
    # every sheet rather than returning nothing.
    relevant_sheets = matched_sheets or expertise["sheets"]

    relevant_score_by_user: dict[str, float] = {}
    relevant_touches_by_user: dict[str, int] = {}
    for sheet in relevant_sheets:
        for expert in sheet["experts"]:
            relevant_score_by_user[expert["user_id"]] = relevant_score_by_user.get(expert["user_id"], 0.0) + expert["score"]
            relevant_touches_by_user[expert["user_id"]] = relevant_touches_by_user.get(expert["user_id"], 0) + expert["touches"]

    top_score = max(relevant_score_by_user.values(), default=1.0) or 1.0
    candidates = []
    for expert in expertise["experts"]:
        user_id = expert["user_id"]
        score = relevant_score_by_user.get(user_id, 0.0)
        if score <= 0:
            continue
        presence = activity_by_user.get(user_id, {})
        workload_penalty = min(1.0, expert["open_workload_days"] / _BACKLOG_WORKLOAD_DAYS)
        presence_bonus = _presence_bonus(presence.get("status"), bool(presence.get("is_active_now")))
        availability_score = round((score / top_score) * 0.6 + presence_bonus * 0.25 - workload_penalty * 0.15, 4)
        candidates.append({
            "user_id": user_id, "email": expert["email"], "display_name": expert["display_name"],
            "relevant_score": round(score, 2), "relevant_touches": relevant_touches_by_user.get(user_id, 0),
            "open_workload_days": expert["open_workload_days"], "open_merge_requests": expert["open_merge_requests"],
            "status": presence.get("status", "OFFLINE"), "is_active_now": bool(presence.get("is_active_now")),
            "availability_score": availability_score,
            "matched_sheets": [sheet["sheet_name"] for sheet in relevant_sheets if any(e["user_id"] == user_id for e in sheet["experts"])][:3],
        })
    candidates.sort(key=lambda item: -item["availability_score"])
    return candidates[:_MAX_CANDIDATES_RETURNED], bool(matched_sheets)


def _question_for_discovery(topic: str, candidates: list[dict[str, Any]], topic_matched_a_sheet: bool) -> str:
    lines = "\n".join(
        f"{index + 1}. {item['display_name']} -- relevance score {item['relevant_score']} on "
        f"{', '.join(item['matched_sheets']) or 'the repository overall'}, currently "
        f"{'online' if item['is_active_now'] else 'offline'}"
        f"{' (flagged Need Help)' if item['status'] == 'NEED_HELP' else ''}, "
        f"{item['open_merge_requests']} open review(s) totalling {item['open_workload_days']} workload-day(s). "
        f"Composite availability score: {item['availability_score']}."
        for index, item in enumerate(candidates)
    )
    scope_note = (
        "These sheet(s) matched the topic by name." if topic_matched_a_sheet
        else "No sheet name matched this topic, so ranking falls back to overall repository expertise."
    )
    asked = f'who should I ask about {topic}?' if topic.strip() else "who's the best person to ask for help on this repository in general?"
    return (
        f'A teammate asked: "{asked}" A deterministic ranking (below, already computed '
        "from real expertise scores, live presence, and open review workload -- it is NOT your job to re-rank or "
        f"pick someone else) produced this order:\n{lines}\n{scope_note}\n"
        "Write one short recommendation (2-3 sentences) explaining IN PLAIN LANGUAGE why the #1 person is the "
        "best pick right now, referencing the specific numbers above (their expertise, current availability, and "
        "workload) -- and if the #1 pick is offline or has a heavy queue, briefly note the #2 pick as a backup. "
        "Never suggest anyone not in this list, and never contradict the given order."
    )


async def _run_discovery(run_id: str, organization_id: str, user_id: str, repository_id: str, topic: str) -> dict[str, Any]:
    from ..store.presence_store import repository_activity_overview

    expertise = repository_expertise(repository_id)
    activity = repository_activity_overview(repository_id)
    ai_service._record_agent_step(
        run_id, 1, "PLAN", "COMPLETED",
        f"Loaded expertise for {len(expertise['experts'])} member(s) and live presence for {len(activity)} member(s)",
    )

    candidates, topic_matched_a_sheet = _rank_candidates(topic, expertise, activity)
    tool_call = ai_service._record_read_tool_call(
        organization_id, user_id, run_id, None, "get_reviewer_availability_context",
        {"repository_id": repository_id, "topic": topic}, {"candidates_ranked": len(candidates)},
    )
    ai_service._record_agent_step(
        run_id, 2, "RANK", "COMPLETED",
        f"Ranked {len(candidates)} candidate(s) by expertise, presence, and workload"
        + ("" if topic_matched_a_sheet else " (no sheet name matched the topic -- used overall expertise)"),
        tool_call["tool_call_id"],
    )

    if not candidates:
        ai_service._record_agent_step(run_id, 3, "EXPLAIN", "COMPLETED", "No candidate has any recorded expertise on this repository yet")
        return {
            "topic": topic, "candidates": [], "recommended_user_id": None,
            "rationale": "Nobody on this repository has commit history yet, so there is no expertise to rank.",
            "confidence": 0.0, "warnings": [], "ai_request_id": None,
        }

    evidence = [
        EvidenceItem(
            type="reviewer_candidate", id=item["user_id"], title=item["display_name"],
            summary=f"Availability score {item['availability_score']}", data=item,
        ).serializable()
        for item in candidates
    ]
    result = await ai_gateway.generate(
        organization_id=organization_id, user_id=user_id, feature="reviewer_discovery",
        question=_question_for_discovery(topic, candidates, topic_matched_a_sheet), evidence=evidence,
        context_hash=_hash({"repository_id": repository_id, "topic": topic, "candidates": candidates}),
        classification="INTERNAL", agent_key="DISCOVERY_AGENT", agent_run_id=run_id,
    )
    ai_service._record_agent_step(run_id, 3, "EXPLAIN", "COMPLETED", f"Recommended {candidates[0]['display_name']}")

    # A free model occasionally echoes its entire structured response back
    # into `answer` instead of one prose recommendation -- sanitized here
    # so that never reaches a user; the deterministic ranking above already
    # stands on its own even if this narration comes back empty.
    rationale = sanitize_free_text(result.get("answer"))
    recommendation_id = _id("DISC")
    conn = database._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO DISCOVERY_RECOMMENDATIONS
                (RECOMMENDATION_ID, REPOSITORY_ID, ASKED_BY, TOPIC, RECOMMENDED_USER_ID, CANDIDATES_JSON,
                 RATIONALE, CONFIDENCE, WARNINGS_JSON, AI_REQUEST_ID, CREATED_AT)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                recommendation_id, repository_id, user_id, topic, candidates[0]["user_id"], json.dumps(candidates, default=str),
                rationale, float(result.get("confidence") or 0.0), json.dumps(result.get("warnings") or []),
                result.get("ai_request_id"), database._utcnow(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    record_audit_event(
        "AI_DISCOVERY_RECOMMENDATION_COMPLETED", actor_user_id=user_id, repository_id=repository_id,
        payload={"agent_run_id": run_id, "topic": topic, "recommended_user_id": candidates[0]["user_id"]},
    )
    return {
        "topic": topic, "candidates": candidates, "recommended_user_id": candidates[0]["user_id"],
        "rationale": rationale, "confidence": float(result.get("confidence") or 0.0),
        "warnings": result.get("warnings") or [], "ai_request_id": result.get("ai_request_id"),
    }


async def discover_who_to_ask(organization_id: str, user_id: str, repository_id: str, topic: str) -> dict[str, Any]:
    """Fully synchronous entrypoint used by tests and any server-to-server caller."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    run_id, _ = agent_runtime.create_run(
        organization_id, user_id, "DISCOVERY_AGENT", f"Find who to ask about: {topic}" if topic.strip() else "Find who to ask for general help on this repository", "REPOSITORY", repository_id,
    )
    try:
        result = await _run_discovery(run_id, organization_id, user_id, repository_id, topic)
        agent_runtime.finish_run(run_id, 3)
        return {"agent_run_id": run_id, **result}
    except Exception as exc:
        agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))
        raise


def start_discovery(organization_id: str, user_id: str, repository_id: str, topic: str) -> dict[str, Any]:
    """Background-launching entrypoint for the live-progress UI."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    run_id, _ = agent_runtime.create_run(
        organization_id, user_id, "DISCOVERY_AGENT", f"Find who to ask about: {topic}" if topic.strip() else "Find who to ask for general help on this repository", "REPOSITORY", repository_id,
    )

    async def _body() -> None:
        try:
            # Discovery's result is small (a ranked candidate list + one
            # rationale) and has no dedicated domain table of its own to
            # read back from, unlike the other three agents -- storing it
            # in the generic fallback slot is the right amount of plumbing
            # here rather than building a bespoke "get latest" endpoint.
            result = await _run_discovery(run_id, organization_id, user_id, repository_id, topic)
            agent_runtime.finish_run(run_id, 3, result)
        except Exception as exc:
            agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))

    agent_runtime.launch(_body())
    return {"agent_run_id": run_id, "status": "RUNNING"}
