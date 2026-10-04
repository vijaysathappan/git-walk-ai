"""Agentic RBAC Anomaly detection.

Cross-references three signals this product already records independently
but has never checked against each other for one repository: role
assignments (``SECURITY_USER_ROLE_ASSIGNMENTS``), device trust state
(``list_repository_devices``), and real observed activity
(``repository_expertise`` commit history, ``repository_activity_overview``
presence). Four anomaly types, every one of them DETECTED deterministically
-- the model is never asked to invent or classify an anomaly, only to
explain an already-detected one in plain language and draft a recommended-
action sentence that points at controls this product already has (role
revoke, device block); it never proposes a new write path of its own:

- ``DORMANT_GRANT``: an editor/owner-level role granted long ago with zero
  commit or presence activity since.
- ``ROLE_ACTIVITY_MISMATCH``: a documented viewer with real commit history
  -- since only the COMMITS table (real, authenticated writes) can produce
  that history, this is never a guess.
- ``UNTRUSTED_DEVICE_ACTIVE``: a device never explicitly trusted or
  blocked that has been active recently.
- ``MULTI_DEVICE_BURST``: several distinct, previously-unseen devices for
  one account clustering within a short window.

Grounded via ``ai.rbac_knowledge``'s FTS5 RAG corpus -- crucially including
the organization's OWN past dismissals (``record_rbac_decision``), so a
pattern an admin has already reviewed and explained doesn't get re-flagged
as a fresh mystery every time this runs.

PLAN -> DETECT -> EXPLAIN loop, audited via the same
AI_AGENT_RUNS/AI_AGENT_STEPS primitives every other agent in this codebase
uses. Findings persist to ``RBAC_ANOMALY_FINDINGS`` with the same
OPEN/ACKNOWLEDGED/DISMISSED/RESOLVED lifecycle shape as the EUC findings
register, deduplicated so re-running this doesn't spam duplicate rows for
a still-open condition.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import database
from ..observability import record_audit_event
from ..services.expertise_service import _parse_iso, repository_expertise
from . import agent_runtime
from .classification_util import repository_classification
from .gateway import ai_gateway
from .rbac_knowledge import record_rbac_decision, retrieve_rbac_precedents
from .response_parsing import sanitize_free_text
from .retrieval import EvidenceItem
from .service import ai_service

_DORMANT_AFTER_DAYS = 21.0
_UNTRUSTED_RECENT_DAYS = 7.0
_BURST_WINDOW_HOURS = 48.0
_BURST_DEVICE_COUNT = 3


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _detect_dormant_grants(conn, repository_id: str, commits_by_user: dict[str, int], activity_by_user: dict[str, dict], now: datetime) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT A.USER_ID, A.CREATED_AT, R.ROLE_KEY, U.EMAIL, U.DISPLAY_NAME
           FROM SECURITY_USER_ROLE_ASSIGNMENTS A
           JOIN SECURITY_ROLES R ON R.ROLE_ID = A.ROLE_ID
           JOIN APP_USERS U ON U.USER_ID = A.USER_ID
           WHERE A.SCOPE_TYPE='REPOSITORY' AND A.SCOPE_ID=? AND R.ROLE_KEY IN ('EDITOR','REPOSITORY_OWNER')""",
        (repository_id,),
    ).fetchall()
    findings = []
    for row in rows:
        granted_at = _parse_iso(row["CREATED_AT"])
        if not granted_at:
            continue
        age_days = (now - granted_at).total_seconds() / 86400
        if age_days < _DORMANT_AFTER_DAYS or commits_by_user.get(row["USER_ID"], 0) > 0:
            continue
        entry = activity_by_user.get(row["USER_ID"]) or {}
        if entry.get("last_seen_at"):
            continue
        findings.append({
            "anomaly_type": "DORMANT_GRANT", "subject_user_id": row["USER_ID"], "severity": "MEDIUM",
            "evidence": {"role": row["ROLE_KEY"], "granted_at": row["CREATED_AT"], "age_days": round(age_days, 1)},
            "scenario_text": f"{row['DISPLAY_NAME'] or row['EMAIL']} was granted {row['ROLE_KEY'].lower().replace('_', ' ')} "
                              f"access {round(age_days)} day(s) ago with zero commits and no recorded presence since.",
            "deduplication_key": f"DORMANT_GRANT:{row['USER_ID']}",
        })
    return findings


def _detect_role_activity_mismatches(conn, repository_id: str, commits_by_user: dict[str, int]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT A.USER_ID, U.EMAIL, U.DISPLAY_NAME
           FROM SECURITY_USER_ROLE_ASSIGNMENTS A
           JOIN SECURITY_ROLES R ON R.ROLE_ID = A.ROLE_ID
           JOIN APP_USERS U ON U.USER_ID = A.USER_ID
           WHERE A.SCOPE_TYPE='REPOSITORY' AND A.SCOPE_ID=? AND R.ROLE_KEY='VIEWER'""",
        (repository_id,),
    ).fetchall()
    findings = []
    for row in rows:
        commits = commits_by_user.get(row["USER_ID"], 0)
        if commits <= 0:
            continue
        findings.append({
            "anomaly_type": "ROLE_ACTIVITY_MISMATCH", "subject_user_id": row["USER_ID"], "severity": "HIGH",
            "evidence": {"role": "viewer", "commit_count": commits},
            "scenario_text": f"{row['DISPLAY_NAME'] or row['EMAIL']} is currently a viewer on this repository but "
                              f"has {commits} commit(s) on record -- their access level doesn't match their observed activity.",
            "deduplication_key": f"ROLE_ACTIVITY_MISMATCH:{row['USER_ID']}",
        })
    return findings


def _detect_untrusted_devices(devices: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    # Security signal, not a per-repository display: an untrusted device's
    # GLOBAL last-seen (any repository) is what matters here, not whether it
    # was active on this specific repository -- see list_repository_devices'
    # docstring for why the two are kept as separate fields.
    findings = []
    for device in devices:
        if device.get("trust_status") != "UNKNOWN":
            continue
        last_seen = _parse_iso(device.get("global_last_seen_at"))
        if not last_seen or (now - last_seen).total_seconds() / 86400 > _UNTRUSTED_RECENT_DAYS:
            continue
        findings.append({
            "anomaly_type": "UNTRUSTED_DEVICE_ACTIVE", "subject_user_id": device.get("user_id"), "severity": "MEDIUM",
            "evidence": {"fingerprint_id": device.get("fingerprint_id"), "ip_address": device.get("ip_address"),
                        "machine_id": device.get("machine_id"), "last_seen_at": device.get("global_last_seen_at")},
            "scenario_text": f"A device used by {device.get('display_name') or device.get('email')} has never been "
                              f"explicitly trusted or blocked, and was active within the last {_UNTRUSTED_RECENT_DAYS:.0f} day(s).",
            "deduplication_key": f"UNTRUSTED_DEVICE_ACTIVE:{device.get('fingerprint_id')}",
        })
    return findings


def _detect_multi_device_bursts(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_user: dict[str, list[dict[str, Any]]] = {}
    for device in devices:
        by_user.setdefault(device.get("user_id"), []).append(device)
    findings = []
    for user_id, user_devices in by_user.items():
        if len(user_devices) < _BURST_DEVICE_COUNT:
            continue
        timestamps = sorted(filter(None, (_parse_iso(item.get("first_seen_at")) for item in user_devices)))
        if len(timestamps) < _BURST_DEVICE_COUNT:
            continue
        span_hours = (timestamps[-1] - timestamps[0]).total_seconds() / 3600
        if span_hours > _BURST_WINDOW_HOURS:
            continue
        sample = user_devices[0]
        findings.append({
            "anomaly_type": "MULTI_DEVICE_BURST", "subject_user_id": user_id, "severity": "HIGH",
            "evidence": {"device_count": len(user_devices), "span_hours": round(span_hours, 1)},
            "scenario_text": f"{sample.get('display_name') or sample.get('email')}'s account was seen from "
                              f"{len(user_devices)} distinct new devices within {round(span_hours, 1)} hour(s).",
            "deduplication_key": f"MULTI_DEVICE_BURST:{user_id}",
        })
    return findings


def _question_for_findings(findings: list[dict[str, Any]]) -> str:
    lines = "\n".join(f"- [{item['deduplication_key']}] ({item['anomaly_type']}) {item['scenario_text']}" for item in findings)
    return (
        "These access-pattern anomalies were already detected deterministically -- your job is ONLY to explain "
        "each one in plain language for a repository owner and, where a resolution precedent below is relevant, "
        "note whether this looks like the same kind of pattern that was previously reviewed. Do not invent a new "
        "anomaly, and do not change the anomaly type or severity already assigned. For EACH finding below, return "
        "one entry in recommended_actions with action_type set to EXACTLY the bracketed key shown for that finding "
        "(this key is unique per finding, not per anomaly type -- two findings of the same type must get two "
        "separate entries with two different keys), title set to the subject's name, and rationale explaining the "
        "concern and a suggested next step (e.g. 'consider revoking this role' or 'consider blocking this device "
        "from Team Activity' -- reference controls this product already has, don't invent a new one).\n"
        f"Findings:\n{lines}"
    )


def list_findings(repository_id: str, status: str | None = None) -> list[dict[str, Any]]:
    conn = database._get_connection()
    try:
        query = "SELECT * FROM RBAC_ANOMALY_FINDINGS WHERE REPOSITORY_ID=?"
        params: list[Any] = [repository_id]
        if status:
            query += " AND STATUS=?"
            params.append(status.upper())
        query += " ORDER BY CASE SEVERITY WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, LAST_DETECTED_AT DESC"
        rows = conn.execute(query, params).fetchall()
        output = []
        for row in rows:
            item = {key.lower(): row[key] for key in row.keys()}
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            output.append(item)
        return output
    finally:
        conn.close()


def resolve_finding(repository_id: str, finding_id: str, user_id: str, decision: str, reason: str) -> dict[str, Any]:
    """Plain, non-agentic lifecycle mutation an admin uses to acknowledge,
    dismiss, or resolve a flagged anomaly -- mirrors the EUC finding
    lifecycle exactly. Feeds a DISMISSED/ACKNOWLEDGED decision back into
    the precedent corpus so the same reviewed pattern doesn't get
    re-flagged as a fresh mystery next time."""
    decision = decision.upper()
    if decision not in {"ACKNOWLEDGED", "DISMISSED", "RESOLVED"}:
        raise ValueError("decision must be ACKNOWLEDGED, DISMISSED, or RESOLVED")
    conn = database._get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM RBAC_ANOMALY_FINDINGS WHERE FINDING_ID=? AND REPOSITORY_ID=?", (finding_id, repository_id)
        ).fetchone()
        if not row:
            raise KeyError("RBAC anomaly finding does not exist")
        conn.execute(
            "UPDATE RBAC_ANOMALY_FINDINGS SET STATUS=?,DECIDED_BY=?,DECISION_REASON=?,DECIDED_AT=? WHERE FINDING_ID=?",
            (decision, user_id, reason, database._utcnow(), finding_id),
        )
        conn.commit()
        anomaly_type, scenario = row["ANOMALY_TYPE"], row["EXPLANATION"] or ""
    finally:
        conn.close()
    if decision in {"ACKNOWLEDGED", "DISMISSED"}:
        try:
            organization_id = database.get_repository_organization_id(repository_id)
            record_rbac_decision(anomaly_type, scenario, decision, reason, organization_id=organization_id)
        except Exception:
            pass
    record_audit_event(
        "RBAC_ANOMALY_FINDING_DECIDED", actor_user_id=user_id, repository_id=repository_id,
        payload={"finding_id": finding_id, "decision": decision, "reason": reason},
    )
    return {"finding_id": finding_id, "status": decision}


async def _run_rbac_anomaly_scan(run_id: str, organization_id: str, user_id: str, repository_id: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    expertise = repository_expertise(repository_id)
    commits_by_user = {expert["user_id"]: expert["commits"] for expert in expertise["experts"]}

    from ..store.presence_store import repository_activity_overview
    activity = repository_activity_overview(repository_id)
    activity_by_user = {item["user_id"]: item for item in activity}
    devices = database.list_repository_devices(repository_id)

    conn = database._get_connection()
    try:
        detected = (
            _detect_dormant_grants(conn, repository_id, commits_by_user, activity_by_user, now)
            + _detect_role_activity_mismatches(conn, repository_id, commits_by_user)
            + _detect_untrusted_devices(devices, now)
            + _detect_multi_device_bursts(devices)
        )
    finally:
        conn.close()
    ai_service._record_agent_step(
        run_id, 1, "PLAN", "COMPLETED",
        f"Cross-referenced role assignments, {len(devices)} known device(s), and activity for "
        f"{len(commits_by_user)} contributor(s)",
    )

    tool_call = ai_service._record_read_tool_call(
        organization_id, user_id, run_id, None, "get_rbac_context",
        {"repository_id": repository_id}, {"anomalies_detected": len(detected)},
    )
    ai_service._record_agent_step(
        run_id, 2, "DETECT", "COMPLETED",
        f"Detected {len(detected)} anomal{'y' if len(detected) == 1 else 'ies'} across 4 pattern checks",
        tool_call["tool_call_id"],
    )

    explanations: dict[str, str] = {}
    ai_request_id = None
    confidence = 0.0
    warnings: list[str] = []
    if detected:
        precedents_by_type: dict[str, list[dict[str, Any]]] = {}
        evidence: list[dict[str, Any]] = []
        for finding in detected:
            precedents = precedents_by_type.setdefault(
                finding["anomaly_type"],
                retrieve_rbac_precedents(finding["anomaly_type"], limit=2, organization_id=organization_id),
            )
            for doc in precedents:
                evidence.append(EvidenceItem(
                    type="rbac_precedent", id=doc["doc_id"], title=doc["title"], summary=doc["rationale"], data=doc,
                ).serializable())
        evidence = list({item["id"]: item for item in evidence}.values())  # dedupe shared precedents across findings
        evidence += [
            EvidenceItem(
                type="rbac_anomaly", id=finding["deduplication_key"], title=finding["anomaly_type"],
                summary=finding["scenario_text"], data=finding,
            ).serializable()
            for finding in detected
        ]
        result = await ai_gateway.generate(
            organization_id=organization_id, user_id=user_id, feature="rbac_anomaly_explanation",
            question=_question_for_findings(detected), evidence=evidence,
            context_hash=_hash({"repository_id": repository_id, "findings": [f["deduplication_key"] for f in detected]}),
            classification=repository_classification(repository_id), agent_key="RBAC_ANOMALY_AGENT", agent_run_id=run_id,
        )
        confidence = float(result.get("confidence") or 0.0)
        warnings = result.get("warnings") or []
        ai_request_id = result.get("ai_request_id")
        for action in result.get("recommended_actions") or []:
            explanations[str(action.get("action_type", ""))] = sanitize_free_text(action.get("rationale"))

    ai_service._record_agent_step(
        run_id, 3, "EXPLAIN", "COMPLETED",
        f"Explained {len(explanations)} finding(s)" if detected else "No anomalies found this scan",
    )

    now_iso = database._utcnow()
    stored_findings = []
    conn = database._get_connection()
    try:
        for finding in detected:
            explanation = explanations.get(finding["deduplication_key"]) or finding["scenario_text"]
            existing = conn.execute(
                "SELECT FINDING_ID, FIRST_DETECTED_AT FROM RBAC_ANOMALY_FINDINGS WHERE REPOSITORY_ID=? AND DEDUPLICATION_KEY=?",
                (repository_id, finding["deduplication_key"]),
            ).fetchone()
            finding_id = existing["FINDING_ID"] if existing else _id("RBACF")
            first_detected = existing["FIRST_DETECTED_AT"] if existing else now_iso
            conn.execute(
                """
                INSERT INTO RBAC_ANOMALY_FINDINGS
                    (FINDING_ID, REPOSITORY_ID, ANOMALY_TYPE, SUBJECT_USER_ID, SEVERITY, EVIDENCE_JSON, EXPLANATION,
                     RECOMMENDED_ACTION, CONFIDENCE, STATUS, DEDUPLICATION_KEY, AI_REQUEST_ID, FIRST_DETECTED_AT, LAST_DETECTED_AT)
                VALUES (?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?)
                ON CONFLICT(REPOSITORY_ID, DEDUPLICATION_KEY) DO UPDATE SET
                    SEVERITY=excluded.SEVERITY, EVIDENCE_JSON=excluded.EVIDENCE_JSON, EXPLANATION=excluded.EXPLANATION,
                    RECOMMENDED_ACTION=excluded.RECOMMENDED_ACTION, CONFIDENCE=excluded.CONFIDENCE,
                    AI_REQUEST_ID=excluded.AI_REQUEST_ID, LAST_DETECTED_AT=excluded.LAST_DETECTED_AT,
                    STATUS=CASE WHEN RBAC_ANOMALY_FINDINGS.STATUS='RESOLVED' THEN 'OPEN' ELSE RBAC_ANOMALY_FINDINGS.STATUS END
                """,
                (
                    finding_id, repository_id, finding["anomaly_type"], finding["subject_user_id"], finding["severity"],
                    json.dumps(finding["evidence"]), explanation, explanation, confidence,
                    finding["deduplication_key"], ai_request_id, first_detected, now_iso,
                ),
            )
            stored_findings.append({**finding, "finding_id": finding_id, "explanation": explanation})
        conn.commit()
    finally:
        conn.close()

    record_audit_event(
        "AI_RBAC_ANOMALY_SCAN_COMPLETED", actor_user_id=user_id, repository_id=repository_id,
        payload={"agent_run_id": run_id, "findings": len(stored_findings)},
    )
    return {"repository_id": repository_id, "findings": stored_findings, "confidence": confidence, "warnings": warnings}


async def scan_rbac_anomalies(organization_id: str, user_id: str, repository_id: str) -> dict[str, Any]:
    """Fully synchronous entrypoint used by tests and any server-to-server caller."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    run_id, _ = agent_runtime.create_run(
        organization_id, user_id, "RBAC_ANOMALY_AGENT", "Scan for RBAC/device anomalies", "REPOSITORY", repository_id,
    )
    try:
        result = await _run_rbac_anomaly_scan(run_id, organization_id, user_id, repository_id)
        agent_runtime.finish_run(run_id, 3)
        return {"agent_run_id": run_id, **result}
    except Exception as exc:
        agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))
        raise


def start_rbac_anomaly_scan(organization_id: str, user_id: str, repository_id: str) -> dict[str, Any]:
    """Background-launching entrypoint for the live-progress UI."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    run_id, _ = agent_runtime.create_run(
        organization_id, user_id, "RBAC_ANOMALY_AGENT", "Scan for RBAC/device anomalies", "REPOSITORY", repository_id,
    )

    async def _body() -> None:
        try:
            await _run_rbac_anomaly_scan(run_id, organization_id, user_id, repository_id)
            agent_runtime.finish_run(run_id, 3)
        except Exception as exc:
            agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))

    agent_runtime.launch(_body())
    return {"agent_run_id": run_id, "status": "RUNNING"}
