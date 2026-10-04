"""Onboarding ramp, behavioral-anomaly detection, and the Team Health
Digest — plus the reactive notification wiring that surfaces real problems
from all of this (and from ``expertise_service``) as actual notifications,
instead of leaving them to be found only by someone opening Team Activity.

Every detector here is deterministic and conservative by design: anomaly
detection compares a person only against their OWN trailing baseline (never
against teammates, so a consistently high-volume contributor is never
flagged just for being active), with thresholds picked specifically to
avoid crying wolf over ordinary day-to-day variance. The Team Health
Digest's AI narrative (like every other AI feature in this product) is
strictly optional narration over facts that are already fully computed
deterministically — if the AI call fails, every number and observation is
still returned, just without the plain-language wrapper.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import database
from ..ai.response_parsing import sanitize_free_text
from ..ai.retrieval import EvidenceItem
from ..observability import structured_log
from .expertise_service import _parse_iso, repository_expertise

_STALLED_AFTER_DAYS = 7
_ANOMALY_MIN_ABSOLUTE_CHANGES = 20
_ANOMALY_RATIO_THRESHOLD = 3.0
_NOTIFICATION_COOLDOWN_HOURS = 24


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


# ------------------------------------------------------- onboarding ramp --
def onboarding_ramp(repository_id: str, stalled_after_days: int = _STALLED_AFTER_DAYS) -> dict[str, Any]:
    """For every member: how long it took them to make their first commit
    after being onboarded (``ramp_days``), or, if they haven't committed
    yet, how long they've been waiting and whether that now counts as
    ``stalled`` — a real, measurable onboarding-friction number this
    product category has never surfaced before."""
    conn = database._get_connection()
    try:
        member_rows = conn.execute(
            """SELECT M.USER_ID AS USER_ID, U.EMAIL AS EMAIL, U.DISPLAY_NAME AS DISPLAY_NAME, M.CREATED_AT AS JOINED_AT
               FROM REPOSITORY_MEMBERS M JOIN APP_USERS U ON U.USER_ID = M.USER_ID
               WHERE M.REPOSITORY_ID=?""",
            (repository_id,),
        ).fetchall()
        first_commit_rows = conn.execute(
            "SELECT AUTHOR_USER_ID AS USER_ID, MIN(CREATED_AT) AS FIRST_COMMIT_AT FROM COMMITS WHERE REPOSITORY_ID=? GROUP BY AUTHOR_USER_ID",
            (repository_id,),
        ).fetchall()
    finally:
        conn.close()

    first_commit_by_user = {row["USER_ID"]: row["FIRST_COMMIT_AT"] for row in first_commit_rows}
    now = datetime.now(timezone.utc)

    members: list[dict[str, Any]] = []
    for row in member_rows:
        user_id = row["USER_ID"]
        joined_dt = _parse_iso(row["JOINED_AT"])
        first_commit_at = first_commit_by_user.get(user_id)
        first_commit_dt = _parse_iso(first_commit_at)
        days_since_joined = round((now - joined_dt).total_seconds() / 86400, 1) if joined_dt else None
        ramp_days = None
        if joined_dt and first_commit_dt:
            ramp_days = round(max(0.0, (first_commit_dt - joined_dt).total_seconds() / 86400), 1)
        stalled = (
            first_commit_at is None and days_since_joined is not None and days_since_joined >= stalled_after_days
        )
        members.append({
            "user_id": user_id, "email": row["EMAIL"], "display_name": row["DISPLAY_NAME"] or row["EMAIL"],
            "joined_at": row["JOINED_AT"], "first_commit_at": first_commit_at,
            "ramp_days": ramp_days, "days_since_joined": days_since_joined, "stalled": stalled,
        })

    members.sort(key=lambda item: (item["ramp_days"] is None, item["ramp_days"] if item["ramp_days"] is not None else 0.0))
    completed = [item for item in members if item["ramp_days"] is not None]
    average_ramp_days = round(sum(item["ramp_days"] for item in completed) / len(completed), 1) if completed else None

    return {
        "repository_id": repository_id, "members": members,
        "average_ramp_days": average_ramp_days,
        "stalled_count": sum(1 for item in members if item["stalled"]),
    }


# --------------------------------------------------- behavioral anomalies --
def detect_behavioral_anomalies(repository_id: str, baseline_days: int = 30) -> dict[str, Any]:
    """Flags a member whose most recent day of commit activity is a large,
    genuine outlier against their OWN trailing history — not against
    anyone else's. Requires at least 3 days of prior history to establish a
    baseline (so a brand-new contributor's very first busy day is never
    flagged), and requires BOTH a >=3x ratio AND a non-trivial absolute
    change count, so ordinary variance (2 changes one day, 6 the next)
    never trips this."""
    since = (datetime.now(timezone.utc) - timedelta(days=baseline_days)).isoformat()
    conn = database._get_connection()
    try:
        commit_rows = conn.execute(
            """SELECT AUTHOR_USER_ID AS USER_ID, AUTHOR_EMAIL AS EMAIL, DATE(CREATED_AT) AS DAY,
                      COUNT(*) AS COMMITS, COALESCE(SUM(CHANGE_COUNT),0) AS CHANGES
               FROM COMMITS WHERE REPOSITORY_ID=? AND CREATED_AT>=? GROUP BY AUTHOR_USER_ID, DAY
               ORDER BY DAY ASC""",
            (repository_id, since),
        ).fetchall()
        display_name_rows = conn.execute("SELECT USER_ID, DISPLAY_NAME FROM APP_USERS").fetchall()
    finally:
        conn.close()

    display_names = {row["USER_ID"]: row["DISPLAY_NAME"] for row in display_name_rows if row["DISPLAY_NAME"]}

    by_user: dict[str, list[dict[str, Any]]] = {}
    for row in commit_rows:
        by_user.setdefault(row["USER_ID"], []).append(
            {"day": row["DAY"], "commits": row["COMMITS"], "changes": row["CHANGES"], "email": row["EMAIL"]}
        )

    anomalies: list[dict[str, Any]] = []
    for user_id, days in by_user.items():
        if len(days) < 4:
            continue  # not enough prior history for a meaningful baseline
        latest = days[-1]
        baseline = days[:-1]
        avg_changes = sum(item["changes"] for item in baseline) / len(baseline)
        if avg_changes <= 0 or latest["changes"] < _ANOMALY_MIN_ABSOLUTE_CHANGES:
            continue
        ratio = latest["changes"] / avg_changes
        if ratio < _ANOMALY_RATIO_THRESHOLD:
            continue
        anomalies.append({
            "user_id": user_id, "email": latest["email"], "display_name": display_names.get(user_id) or latest["email"],
            "day": latest["day"], "metric": "change_volume",
            "observed_changes": latest["changes"], "baseline_average_changes": round(avg_changes, 1),
            "ratio": round(ratio, 1), "baseline_sample_days": len(baseline),
            "message": (
                f"{latest['changes']} cell changes on {latest['day']} — {round(ratio, 1)}x their own "
                f"{len(baseline)}-day average of {round(avg_changes, 1)}."
            ),
        })
    anomalies.sort(key=lambda item: item["ratio"], reverse=True)
    return {"repository_id": repository_id, "since": since, "anomalies": anomalies}


# ------------------------------------------------------ team health digest --
async def team_health_digest(
    organization_id: str, actor_user_id: str, repository_id: str, *, use_ai: bool = True,
) -> dict[str, Any]:
    """Deterministic team-health observations (presence, bus-factor risk on
    single-expert sheets, onboarding stalls, activity anomalies), with an
    optional grounded AI narrative over exactly those facts — the same
    Manager-Digest pattern already used in Insights, pointed at team
    dynamics instead of raw KPIs."""
    from ..store.presence_store import repository_activity_overview

    activity = repository_activity_overview(repository_id)
    expertise = repository_expertise(repository_id)
    ramp = onboarding_ramp(repository_id)
    anomaly_result = detect_behavioral_anomalies(repository_id)

    online_count = sum(1 for item in activity if item.get("status") == "ONLINE")
    need_help_count = sum(1 for item in activity if item.get("status") == "NEED_HELP")
    single_expert_sheets = [
        sheet for sheet in expertise["sheets"] if len([e for e in sheet["experts"] if e["touches"] > 0]) == 1
    ]
    busiest = max(expertise["experts"], key=lambda item: item["open_workload_days"], default=None)

    observations = [
        f"{online_count} of {len(activity)} member(s) online in Excel right now; {need_help_count} flagged Need Help.",
    ]
    if single_expert_sheets:
        names = ", ".join(sheet["sheet_name"] for sheet in single_expert_sheets[:3])
        more = f" and {len(single_expert_sheets) - 3} more" if len(single_expert_sheets) > 3 else ""
        observations.append(
            f"{len(single_expert_sheets)} worksheet(s) have only one real expert ({names}{more}) — a bus-factor risk "
            "if that person is unavailable."
        )
    if ramp["stalled_count"]:
        observations.append(
            f"{ramp['stalled_count']} member(s) were onboarded over {_STALLED_AFTER_DAYS} days ago and still "
            "haven't made a first commit."
        )
    if anomaly_result["anomalies"]:
        top = anomaly_result["anomalies"][0]
        observations.append(
            f"{len(anomaly_result['anomalies'])} unusual activity spike(s) detected, the largest being "
            f"{top['display_name']}: {top['message']}"
        )
    if busiest and busiest["open_workload_days"] > 0:
        observations.append(
            f"{busiest['display_name']} currently carries the heaviest open-review workload "
            f"({busiest['open_workload_days']} workload-day(s) across {busiest['open_merge_requests']} request(s))."
        )
    if len(observations) == 1:
        observations.append("No workload, onboarding, or activity concerns detected this period.")

    result: dict[str, Any] = {
        "repository_id": repository_id,
        "observations": observations,
        "online_count": online_count, "need_help_count": need_help_count, "team_size": len(activity),
        "bus_factor_sheets": len(single_expert_sheets),
        "stalled_onboarding": ramp["stalled_count"],
        "anomaly_count": len(anomaly_result["anomalies"]),
        "narrative": None,
    }

    if use_ai:
        try:
            result["narrative"] = await _explain_team_health(organization_id, actor_user_id, repository_id, observations)
        except Exception:
            result["narrative"] = None  # advisory only — the deterministic observations above always stand alone
    return result


async def _explain_team_health(
    organization_id: str, actor_user_id: str, repository_id: str, observations: list[str],
) -> str | None:
    from ..ai.gateway import ai_gateway

    evidence = [
        EvidenceItem(
            type="TEAM_OBSERVATION", id=f"OBS_{index}", title=f"Observation {index + 1}",
            summary=observation, data={"observation": observation},
        ).serializable()
        for index, observation in enumerate(observations)
    ]
    question = (
        "These are real, already-computed observations about this team's current health (presence, workload "
        "concentration, onboarding, and activity anomalies). Write a short, plain-language paragraph (2-4 "
        "sentences) summarizing them for the repository owner, as if briefing a manager. Do not invent any fact, "
        "number, or name not already present in the observations."
    )
    result = await ai_gateway.generate(
        organization_id=organization_id, user_id=actor_user_id, feature="team_health_digest",
        question=question, evidence=evidence,
        context_hash=_hash({"repository_id": repository_id, "observations": observations}),
        classification="INTERNAL",
    )
    # A free model occasionally echoes its entire structured response back
    # into `answer` instead of one prose paragraph -- sanitized so that
    # never reaches a user; the deterministic observations always stand on
    # their own even if this narration comes back empty.
    return sanitize_free_text(result.get("answer")) or None


# --------------------------------------------- reactive notification checks --
def _recently_notified(conn, user_id: str, notification_type: str, resource_id: str) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=_NOTIFICATION_COOLDOWN_HOURS)).isoformat()
    row = conn.execute(
        """SELECT 1 FROM NOTIFICATIONS WHERE USER_ID=? AND TYPE=? AND RESOURCE_ID=? AND CREATED_AT>=? LIMIT 1""",
        (user_id, notification_type, resource_id, cutoff),
    ).fetchone()
    return row is not None


def check_team_signals(repository_id: str, owner_user_id: str, organization_id: str | None = None) -> None:
    """Best-effort reactive side-check, run alongside a normal Team Activity
    read (this product has no background scheduler by design — every
    time-sensitive check is computed at read time instead). Never raises
    and never blocks the caller; each individual signal is independently
    de-duplicated so a real, ongoing problem is surfaced once per cooldown
    window rather than re-notified on every page refresh."""
    try:
        _check_review_queue_backlog(repository_id, owner_user_id, organization_id)
    except Exception as exc:
        structured_log(
            logging.WARNING, "team_signal_check_failed", check="review_queue_backlog",
            repository_id=repository_id, error=str(exc), error_type=type(exc).__name__,
        )
    try:
        _check_onboarding_stalled(repository_id, owner_user_id, organization_id)
    except Exception as exc:
        structured_log(
            logging.WARNING, "team_signal_check_failed", check="onboarding_stalled",
            repository_id=repository_id, error=str(exc), error_type=type(exc).__name__,
        )
    try:
        _check_behavioral_anomalies(repository_id, owner_user_id, organization_id)
    except Exception as exc:
        structured_log(
            logging.WARNING, "team_signal_check_failed", check="behavioral_anomalies",
            repository_id=repository_id, error=str(exc), error_type=type(exc).__name__,
        )


def _check_review_queue_backlog(repository_id: str, owner_user_id: str, organization_id: str | None) -> None:
    expertise = repository_expertise(repository_id)
    conn = database._get_connection()
    try:
        for expert in expertise["experts"]:
            if expert["open_workload_days"] < 10.0:  # a real, sustained backlog — not just one fresh request
                continue
            resource_id = f"{repository_id}:{expert['user_id']}"
            if _recently_notified(conn, owner_user_id, "REVIEW_QUEUE_BACKLOG", resource_id):
                continue
            database.create_notification(
                owner_user_id, "REVIEW_QUEUE_BACKLOG",
                f"{expert['display_name']}'s review queue is backing up",
                f"{expert['open_merge_requests']} open merge request(s), {expert['open_workload_days']} "
                "combined workload-day(s) — the oldest have been waiting a while.",
                resource_type="REPOSITORY_MEMBER", resource_id=resource_id, organization_id=organization_id,
            )
    finally:
        conn.close()


def _check_onboarding_stalled(repository_id: str, owner_user_id: str, organization_id: str | None) -> None:
    ramp = onboarding_ramp(repository_id)
    conn = database._get_connection()
    try:
        for member in ramp["members"]:
            if not member["stalled"]:
                continue
            resource_id = f"{repository_id}:{member['user_id']}"
            if _recently_notified(conn, owner_user_id, "ONBOARDING_STALLED", resource_id):
                continue
            database.create_notification(
                owner_user_id, "ONBOARDING_STALLED",
                f"{member['display_name']} hasn't committed yet",
                f"Onboarded {member['days_since_joined']} day(s) ago with no commits so far — "
                "they may be stuck or need a nudge.",
                resource_type="REPOSITORY_MEMBER", resource_id=resource_id, organization_id=organization_id,
            )
    finally:
        conn.close()


def _check_behavioral_anomalies(repository_id: str, owner_user_id: str, organization_id: str | None) -> None:
    result = detect_behavioral_anomalies(repository_id)
    conn = database._get_connection()
    try:
        for anomaly in result["anomalies"]:
            resource_id = f"{repository_id}:{anomaly['user_id']}:{anomaly['day']}"
            if _recently_notified(conn, owner_user_id, "BEHAVIORAL_ANOMALY", resource_id):
                continue
            database.create_notification(
                owner_user_id, "BEHAVIORAL_ANOMALY",
                f"Unusual activity from {anomaly['display_name']}",
                anomaly["message"],
                resource_type="REPOSITORY_MEMBER", resource_id=resource_id, organization_id=organization_id,
            )
    finally:
        conn.close()
