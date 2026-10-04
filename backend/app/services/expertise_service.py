"""Expertise Radar + workload-aware reviewer suggestions.

Two capabilities, both deterministic-first and built entirely from data
already recorded elsewhere in the product — no new schema:

- ``repository_expertise()``: who has actually touched which sheet, and how
  much open work (their own unmerged requests) each person currently has,
  derived from ``COMMIT_CHANGES``' per-sheet attribution and ``COMMITS``'
  authorship — the same tables the Merge Conflict Agent and Risk Drift Radar
  already read for cell-level attribution.
- ``suggest_reviewers()``: for one merge request, ranks candidate reviewers
  by real expertise on the sheets that request actually touches (from the
  semantic diff ``merge_service.get_request()`` already computed), broken
  ties by whoever currently has the lightest open-work load, and always
  excludes the request's own author. An optional grounded AI call (through
  the same ``AIGateway`` every other agent in this product uses) adds a
  one-line plain-English rationale for the top pick — but the ranking
  itself never depends on that call succeeding, so a misconfigured or
  unavailable AI provider can never break the suggestion, only remove its
  narration.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import database
from ..ai.response_parsing import sanitize_free_text
from ..ai.retrieval import EvidenceItem


_DECAY_HALF_LIFE_DAYS = 21.0  # a touch from 21 days ago counts half as much as one from today


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _decay_weight(age_days: float) -> float:
    return 0.5 ** (max(0.0, age_days) / _DECAY_HALF_LIFE_DAYS)


def repository_expertise(repository_id: str, days: int = 90) -> dict[str, Any]:
    """Per-member sheet expertise and current open workload for one repository.

    Expertise is recency-weighted: a touch from today counts fully, one from
    three weeks ago counts half as much, six weeks ago a quarter, and so on
    (``_decay_weight``) — so "who knows this sheet" reflects who could
    actually explain it *right now*, not just whoever happened to do a bulk
    edit months ago and hasn't touched it since. Both the decayed score and
    the plain touch count are returned; the decayed score drives ranking.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    now = datetime.now(timezone.utc)
    conn = database._get_connection()
    try:
        sheet_names = {
            row["SHEET_ID"]: row["SHEET_NAME"]
            for row in conn.execute(
                "SELECT SHEET_ID, SHEET_NAME FROM WORKBOOK_SHEETS WHERE REPOSITORY_ID=?", (repository_id,)
            ).fetchall()
        }
        # Day-level granularity (not per-change) keeps this query cheap on a
        # busy repository while still giving the decay curve a real,
        # meaningful age to work from.
        touch_rows = conn.execute(
            """SELECT C.AUTHOR_USER_ID AS USER_ID, C.AUTHOR_EMAIL AS EMAIL, CH.SHEET_ID AS SHEET_ID,
                      DATE(C.CREATED_AT) AS DAY, COUNT(*) AS TOUCHES
               FROM COMMIT_CHANGES CH JOIN COMMITS C ON C.COMMIT_ID = CH.COMMIT_ID
               WHERE CH.REPOSITORY_ID=? AND C.CREATED_AT>=?
               GROUP BY C.AUTHOR_USER_ID, CH.SHEET_ID, DAY""",
            (repository_id, since),
        ).fetchall()
        commit_rows = conn.execute(
            """SELECT AUTHOR_USER_ID AS USER_ID, AUTHOR_EMAIL AS EMAIL, COUNT(*) AS COMMITS,
                      MAX(CREATED_AT) AS LAST_COMMIT_AT
               FROM COMMITS WHERE REPOSITORY_ID=? AND CREATED_AT>=? GROUP BY AUTHOR_USER_ID""",
            (repository_id, since),
        ).fetchall()
        open_mr_rows = conn.execute(
            """SELECT CREATED_BY, CREATED_AT FROM MERGE_REQUESTS
               WHERE REPOSITORY_ID=? AND STATUS NOT IN ('MERGED','CLOSED')""",
            (repository_id,),
        ).fetchall()
        display_name_rows = conn.execute("SELECT USER_ID, DISPLAY_NAME FROM APP_USERS").fetchall()
    finally:
        conn.close()

    display_names = {row["USER_ID"]: row["DISPLAY_NAME"] for row in display_name_rows if row["DISPLAY_NAME"]}

    # Workload = one unit per open request the moment it's opened, plus its
    # age in days on top — so someone with two open requests still reads as
    # busier than someone with one even when both are brand new, but a
    # request that's been sitting for two weeks weighs more than one opened
    # an hour ago.
    open_mr_count: dict[str, int] = {}
    open_workload_days: dict[str, float] = {}
    for row in open_mr_rows:
        user_id = row["CREATED_BY"]
        open_mr_count[user_id] = open_mr_count.get(user_id, 0) + 1
        created_dt = _parse_iso(row["CREATED_AT"])
        age_days = max(0.0, (now - created_dt).total_seconds() / 86400) if created_dt else 0.0
        open_workload_days[user_id] = open_workload_days.get(user_id, 0.0) + 1.0 + age_days

    sheets_by_user: dict[str, dict[str, dict[str, float]]] = {}
    for row in touch_rows:
        user_id, sheet_id = row["USER_ID"], row["SHEET_ID"]
        day_dt = _parse_iso(row["DAY"] + "T00:00:00")
        age_days = max(0.0, (now - day_dt).total_seconds() / 86400) if day_dt else 0.0
        weight = _decay_weight(age_days)
        bucket = sheets_by_user.setdefault(user_id, {}).setdefault(sheet_id, {"touches": 0, "score": 0.0})
        bucket["touches"] += row["TOUCHES"]
        bucket["score"] += row["TOUCHES"] * weight

    experts: list[dict[str, Any]] = []
    for row in commit_rows:
        user_id = row["USER_ID"]
        sheet_touches = sheets_by_user.get(user_id, {})
        top_sheets = sorted(sheet_touches.items(), key=lambda item: item[1]["score"], reverse=True)[:5]
        experts.append({
            "user_id": user_id,
            "email": row["EMAIL"],
            "display_name": display_names.get(user_id) or row["EMAIL"],
            "commits": row["COMMITS"],
            "last_commit_at": row["LAST_COMMIT_AT"],
            "open_merge_requests": open_mr_count.get(user_id, 0),
            "open_workload_days": round(open_workload_days.get(user_id, 0.0), 1),
            "top_sheets": [
                {"sheet_id": sheet_id, "sheet_name": sheet_names.get(sheet_id, sheet_id),
                 "touches": bucket["touches"], "score": round(bucket["score"], 2)}
                for sheet_id, bucket in top_sheets
            ],
        })
    experts.sort(key=lambda item: item["commits"], reverse=True)

    sheet_leaderboard: dict[str, list[dict[str, Any]]] = {}
    for user_id, sheet_touches in sheets_by_user.items():
        expert_name = display_names.get(user_id) or next(
            (e["email"] for e in experts if e["user_id"] == user_id), user_id
        )
        for sheet_id, bucket in sheet_touches.items():
            sheet_leaderboard.setdefault(sheet_id, []).append({
                "user_id": user_id, "display_name": expert_name,
                "touches": bucket["touches"], "score": round(bucket["score"], 2),
            })
    sheets = [
        {
            "sheet_id": sheet_id,
            "sheet_name": sheet_names.get(sheet_id, sheet_id),
            # Not capped to a small display count here: ai.discovery_agent's
            # reviewer-suggestion ranker reads this same list to score every
            # candidate's relevance -- a hard top-5 cut here silently zeroed
            # out real contributors who ranked 6th+ per sheet. Callers that
            # only want a short display list (e.g. the UI) truncate their own copy.
            "experts": sorted(ranking, key=lambda item: item["score"], reverse=True),
        }
        for sheet_id, ranking in sheet_leaderboard.items()
    ]
    sheets.sort(key=lambda item: sum(e["score"] for e in item["experts"]), reverse=True)

    return {"repository_id": repository_id, "since": since, "experts": experts, "sheets": sheets}


async def suggest_reviewers(
    organization_id: str, actor_user_id: str, merge_request: dict[str, Any], *, use_ai: bool = True,
) -> dict[str, Any]:
    """``merge_request`` is the dict already returned by
    ``merge_service.get_request()`` — access to it has already been checked
    by that call, and its semantic-diff ``changes`` already carry the real
    ``sheet_id`` of every touched cell/row/column, so this never re-derives
    or re-authorizes the same read; it only ranks reviewers against it."""
    repository_id = merge_request["repository_id"]
    author_id = merge_request.get("created_by")
    touched_sheet_ids = {item.get("sheet_id") for item in merge_request.get("changes", []) if item.get("sheet_id")}

    expertise = repository_expertise(repository_id)
    candidates: list[dict[str, Any]] = []
    for expert in expertise["experts"]:
        if expert["user_id"] == author_id:
            continue  # never suggest reviewing your own work
        relevant_sheets = [sheet for sheet in expert["top_sheets"] if sheet["sheet_id"] in touched_sheet_ids]
        candidates.append({
            "user_id": expert["user_id"], "email": expert["email"], "display_name": expert["display_name"],
            "commits": expert["commits"], "last_commit_at": expert["last_commit_at"],
            "open_merge_requests": expert["open_merge_requests"],
            "open_workload_days": expert["open_workload_days"],
            "relevant_touches": sum(sheet["touches"] for sheet in relevant_sheets),
            "relevant_score": round(sum(sheet["score"] for sheet in relevant_sheets), 2),
        })

    conn = database._get_connection()
    try:
        member_rows = conn.execute(
            """SELECT M.USER_ID AS USER_ID, U.EMAIL AS EMAIL, U.DISPLAY_NAME AS DISPLAY_NAME
               FROM REPOSITORY_MEMBERS M JOIN APP_USERS U ON U.USER_ID = M.USER_ID
               WHERE M.REPOSITORY_ID=? AND M.USER_ID != ?""",
            (repository_id, author_id),
        ).fetchall()
    finally:
        conn.close()

    known_ids = {candidate["user_id"] for candidate in candidates}
    for row in member_rows:
        if row["USER_ID"] not in known_ids:
            candidates.append({
                "user_id": row["USER_ID"], "email": row["EMAIL"],
                "display_name": row["DISPLAY_NAME"] or row["EMAIL"],
                "commits": 0, "last_commit_at": None, "open_merge_requests": 0, "open_workload_days": 0.0,
                "relevant_touches": 0, "relevant_score": 0.0,
            })

    # Rank: most relevant, recency-weighted expertise on the touched sheets
    # first; ties broken by whoever currently has the lightest open-work
    # load, weighted by how long their open requests have actually been
    # sitting — not just how many of them there are.
    candidates.sort(key=lambda item: (-item["relevant_score"], item["open_workload_days"]))
    ranked = candidates[:5]

    result: dict[str, Any] = {
        "merge_request_id": merge_request.get("merge_request_id"),
        "touched_sheets": len(touched_sheet_ids),
        "candidates": ranked,
        "rationale": None,
    }

    if use_ai and ranked:
        try:
            result["rationale"] = await explain_suggestion(organization_id, actor_user_id, merge_request, ranked)
        except Exception:
            result["rationale"] = None  # advisory only — never break the deterministic ranking
    return result


async def explain_suggestion(
    organization_id: str, actor_user_id: str, merge_request: dict[str, Any], ranked: list[dict[str, Any]],
) -> str | None:
    from ..ai.gateway import ai_gateway

    evidence = [
        EvidenceItem(
            type="REVIEWER_CANDIDATE", id=candidate["user_id"], title=candidate["display_name"],
            summary=(
                f"{candidate['relevant_touches']} relevant edit(s) on the sheets this change touches; "
                f"{candidate['open_merge_requests']} open merge request(s) of their own right now."
            ),
            data=candidate,
        ).serializable()
        for candidate in ranked
    ]
    question = (
        "These are ranked candidate reviewers for a merge request, ranked by real expertise on the exact sheets "
        "this change touches, then by whoever currently has the lightest open-review workload. Write one plain, "
        "specific sentence explaining why the top-ranked candidate is a good fit. Do not propose a different "
        "candidate than the one already ranked first — only explain the existing ranking."
    )
    result = await ai_gateway.generate(
        organization_id=organization_id, user_id=actor_user_id, feature="reviewer_suggestion",
        question=question, evidence=evidence,
        context_hash=_hash({"mr": merge_request.get("merge_request_id"), "ranked": [c["user_id"] for c in ranked]}),
        classification="INTERNAL",
    )
    # A free model occasionally echoes its entire structured response back
    # into `answer` instead of one prose sentence -- sanitized so that
    # never reaches a user; the ranking itself never depends on this call.
    return sanitize_free_text(result.get("answer")) or None
