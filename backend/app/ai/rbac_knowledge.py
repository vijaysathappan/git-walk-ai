"""RAG knowledge base for the RBAC Anomaly agent.

Same zero-heavy-dependency FTS5 approach as ``ai.merge_knowledge`` and
``ai.formula_knowledge``. The corpus starts from a small synthetic set of
common access-pattern scenarios and grows every time an admin actually
DISMISSES a flagged anomaly with a reason -- the exact pattern is recorded
as a precedent, so the same recurring, already-explained situation (e.g.
"this service account is expected to be dormant") is never re-flagged as a
mystery the next time it's seen; retrieval instead surfaces the admin's own
past explanation as grounding for a lower-confidence, calmer narration.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .. import database

SYNTHETIC_SCENARIOS: list[dict[str, str]] = [
    {
        "doc_id": "SYN_RBAC_DORMANT_001", "anomaly_type": "DORMANT_GRANT",
        "title": "Editor role granted but never used",
        "scenario_text": "A user was granted an editor role on a repository weeks ago and has no commits, merge requests, or presence activity on it since.",
        "resolution": "FLAG_FOR_REVIEW",
        "rationale": "A long-dormant edit grant is low-signal but worth a periodic look -- either the person no longer needs access (least-privilege) or they're blocked on something upstream of actually using it.",
    },
    {
        "doc_id": "SYN_RBAC_DORMANT_002", "anomaly_type": "DORMANT_GRANT",
        "title": "Owner role dormant during a known leave period",
        "scenario_text": "A repository owner has had no activity for an extended period, but organizational context (a role transition, leave, or handoff) explains the gap.",
        "resolution": "ACKNOWLEDGE",
        "rationale": "Not every dormant grant is a risk; a known, explainable gap should be acknowledged rather than repeatedly re-flagged.",
    },
    {
        "doc_id": "SYN_RBAC_MISMATCH_001", "anomaly_type": "ROLE_ACTIVITY_MISMATCH",
        "title": "Viewer role with editor-level activity volume",
        "scenario_text": "A user holding only a viewer role shows commit or merge-request activity comparable to or exceeding the repository's actual editors.",
        "resolution": "FLAG_FOR_REVIEW",
        "rationale": "This usually means the role model is stale (their real responsibilities outgrew the assigned role) or that write access is somehow happening through a path other than the recorded role -- either way it's worth reconciling the role with actual behavior.",
    },
    {
        "doc_id": "SYN_RBAC_DEVICE_001", "anomaly_type": "UNTRUSTED_DEVICE_ACTIVE",
        "title": "UNKNOWN-trust device with recent, ongoing activity",
        "scenario_text": "A device fingerprint that has never been explicitly trusted or blocked is actively being used to access repository data.",
        "resolution": "FLAG_FOR_REVIEW",
        "rationale": "An UNKNOWN device isn't inherently malicious -- it may just be a new laptop -- but it hasn't been reviewed, and this product's whole per-device trust model exists precisely so an owner can make that call deliberately rather than by default.",
    },
    {
        "doc_id": "SYN_RBAC_DEVICE_002", "anomaly_type": "UNTRUSTED_DEVICE_ACTIVE",
        "title": "Shared kiosk or lab machine used by many known accounts",
        "scenario_text": "One device fingerprint shows activity from several different legitimate user accounts in the same organization, consistent with a shared or lab machine rather than a personal one.",
        "resolution": "ACKNOWLEDGE",
        "rationale": "A genuinely shared machine used by multiple legitimate accounts is expected in some environments and shouldn't be treated the same as one account's credentials showing up on an unfamiliar device.",
    },
    {
        "doc_id": "SYN_RBAC_BURST_001", "anomaly_type": "MULTI_DEVICE_BURST",
        "title": "Same account, several new devices in a short window",
        "scenario_text": "One user account is seen from multiple distinct, previously-unseen device fingerprints within a short span of time.",
        "resolution": "FLAG_FOR_REVIEW",
        "rationale": "This is consistent with either the person legitimately switching machines quickly (new laptop setup, multiple work devices) or with credential sharing/compromise -- the pattern alone can't distinguish the two, which is exactly why it should be surfaced for a human to confirm rather than resolved automatically.",
    },
]


def seed_synthetic_rbac_corpus(conn=None) -> int:
    owns_connection = conn is None
    conn = conn or database._get_connection()
    now = database._utcnow()
    inserted = 0
    try:
        for doc in SYNTHETIC_SCENARIOS:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO RBAC_ANOMALY_KNOWLEDGE
                    (DOC_ID, ANOMALY_TYPE, TITLE, SCENARIO_TEXT, RESOLUTION, RATIONALE, SOURCE, CREATED_AT)
                VALUES (?,?,?,?,?,?,'SYNTHETIC',?)
                """,
                (doc["doc_id"], doc["anomaly_type"], doc["title"], doc["scenario_text"], doc["resolution"], doc["rationale"], now),
            )
            inserted += 1 if cursor.rowcount and cursor.rowcount > 0 else 0
        if owns_connection:
            conn.commit()
    finally:
        if owns_connection:
            conn.close()
    return inserted


def retrieve_rbac_precedents(anomaly_type: str, limit: int = 4, organization_id: str | None = None) -> list[dict[str, Any]]:
    """Scoped first to the anomaly's own type -- a precedent for a
    different anomaly type is never useful regardless of keyword overlap.
    Also scoped to `organization_id` when given: a real admin's dismissal
    reasoning (SOURCE='HISTORICAL') is specific to their own
    organization's context and must never leak into another org's
    evidence, whereas the built-in SYNTHETIC seed rows (ORGANIZATION_ID
    IS NULL) are generic and meant to be shared by everyone."""
    limit = max(1, min(limit, 20))
    org_filter = " AND (K.ORGANIZATION_ID IS NULL OR K.ORGANIZATION_ID=?)" if organization_id else " AND K.ORGANIZATION_ID IS NULL"
    org_params = (organization_id,) if organization_id else ()
    conn = database._get_connection()
    try:
        try:
            rows = conn.execute(
                f"""
                SELECT K.* FROM RBAC_ANOMALY_KNOWLEDGE_FTS F
                JOIN RBAC_ANOMALY_KNOWLEDGE K ON K.DOC_ID = F.doc_id
                WHERE K.ANOMALY_TYPE = ? AND RBAC_ANOMALY_KNOWLEDGE_FTS MATCH ?{org_filter}
                ORDER BY bm25(RBAC_ANOMALY_KNOWLEDGE_FTS) LIMIT ?
                """,
                (anomaly_type, f'"{anomaly_type}"', *org_params, limit),
            ).fetchall()
        except Exception:
            rows = conn.execute(
                f"SELECT * FROM RBAC_ANOMALY_KNOWLEDGE K WHERE K.ANOMALY_TYPE=?{org_filter} LIMIT ?",
                (anomaly_type, *org_params, limit),
            ).fetchall()
        return [{key.lower(): row[key] for key in row.keys()} for row in rows]
    finally:
        conn.close()


def record_rbac_decision(anomaly_type: str, scenario_text: str, resolution: str, reason: str, organization_id: str | None = None) -> None:
    """Append one self-grown HISTORICAL precedent from a real admin
    decision (acknowledge/dismiss with a reason). Never blocks or fails the
    decision itself; callers wrap this in try/except."""
    doc_id = f"HST_RBAC_{hashlib.sha256((anomaly_type + scenario_text + resolution).encode()).hexdigest()[:20].upper()}"
    conn = database._get_connection()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO RBAC_ANOMALY_KNOWLEDGE
                (DOC_ID, ANOMALY_TYPE, TITLE, SCENARIO_TEXT, RESOLUTION, RATIONALE, SOURCE, CREATED_AT, ORGANIZATION_ID)
            VALUES (?,?,?,?,?,?,'HISTORICAL',?,?)
            """,
            (doc_id, anomaly_type, f"Previously {resolution.lower()}", scenario_text[:2000], resolution, reason[:2000], database._utcnow(), organization_id),
        )
        conn.commit()
    finally:
        conn.close()
