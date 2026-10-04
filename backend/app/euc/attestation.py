"""Automated Attestation & Certification.

A formal, immutable sign-off that a repository owner has reviewed an EUC
asset's current findings and residual risk -- the literal deliverable a
SOX / model-risk-management audit asks for. Each submission snapshots the
findings state it was made against (so "I attested when there were 3 open
findings" stays true even after later re-analysis changes the count) and
is recorded through the same tamper-evident audit ledger every other
governed action in this codebase already uses.

Attestation history is keyed by REPOSITORY_ID, not EUC_ID. EUC_ASSETS are
content-addressed -- a new row is created every time the workbook's content
changes (including every Continuous Assurance re-ingest after a merge), so
an attestation keyed by EUC_ID would be silently orphaned the moment
anyone merges again. Each row still records which exact euc_id/commit it
was made against, but "what's the latest attestation for this workbook"
is always resolved through the repository.

Due/overdue state is computed reactively from the latest submission's age
against a fixed review interval -- the same "no background job, TTL
computed at read time" pattern already established for Excel presence
status, rather than a scheduler this codebase has no infrastructure for.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import database
from ..observability import record_audit_event
from .service import _repository_access

ATTESTATION_INTERVAL_DAYS = 90


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20].upper()}"


def _add_days(iso_timestamp: str, days: int) -> str:
    return (datetime.fromisoformat(iso_timestamp) + timedelta(days=days)).isoformat()


def _lower(row) -> dict[str, Any]:
    return {key.lower(): row[key] for key in row.keys()}


def _asset_and_repository(conn, euc_id: str, user_id: str, edit: bool = False):
    asset = conn.execute("SELECT * FROM EUC_ASSETS WHERE EUC_ID=?", (euc_id,)).fetchone()
    if not asset:
        raise KeyError("EUC asset does not exist")
    repository = _repository_access(conn, asset["REPOSITORY_ID"], user_id, edit=edit)
    return asset, repository


def _findings_snapshot(conn, euc_id: str) -> dict[str, Any]:
    run = conn.execute(
        """SELECT INTELLIGENCE_RUN_ID, RESIDUAL_RISK_SCORE, SOURCE_COMMIT_ID FROM EUC_INTELLIGENCE_RUNS
           WHERE EUC_ID=? AND STATUS='COMPLETED' ORDER BY STARTED_AT DESC, ROWID DESC LIMIT 1""",
        (euc_id,),
    ).fetchone()
    if not run:
        return {"open_finding_count": 0, "open_critical_high_count": 0, "residual_risk": None, "source_commit_id": None}
    counts = conn.execute(
        """SELECT O.SEVERITY, COUNT(*) AS N FROM EUC_FINDING_OCCURRENCES O
           JOIN EUC_FINDINGS F ON F.FINDING_ID = O.FINDING_ID
           WHERE O.INTELLIGENCE_RUN_ID=? AND F.STATUS NOT IN ('RESOLVED','FALSE_POSITIVE','SUPPRESSED')
           GROUP BY O.SEVERITY""",
        (run["INTELLIGENCE_RUN_ID"],),
    ).fetchall()
    total = sum(row["N"] for row in counts)
    severe = sum(row["N"] for row in counts if row["SEVERITY"] in ("CRITICAL", "HIGH"))
    return {"open_finding_count": total, "open_critical_high_count": severe,
            "residual_risk": run["RESIDUAL_RISK_SCORE"], "source_commit_id": run["SOURCE_COMMIT_ID"]}


def _latest_for_repository(conn, repository_id: str):
    return conn.execute(
        "SELECT * FROM EUC_ATTESTATIONS WHERE REPOSITORY_ID=? ORDER BY SUBMITTED_AT DESC, ROWID DESC LIMIT 1",
        (repository_id,),
    ).fetchone()


def submit_attestation(euc_id: str, user_id: str, statement: str) -> dict[str, Any]:
    statement = (statement or "").strip()
    if len(statement) < 10:
        raise ValueError("A meaningful attestation statement is required (at least 10 characters)")
    conn = database._get_connection()
    try:
        asset, repository = _asset_and_repository(conn, euc_id, user_id, edit=True)
        if repository["ACCESS_ROLE"] != "owner":
            raise PermissionError("Only the repository owner can submit an attestation")
        snapshot = _findings_snapshot(conn, euc_id)
        attestation_id = _id("ATT")
        now = database._utcnow()
        conn.execute(
            """INSERT INTO EUC_ATTESTATIONS
               (ATTESTATION_ID, EUC_ID, REPOSITORY_ID, STATEMENT, OPEN_FINDING_COUNT,
                OPEN_CRITICAL_HIGH_COUNT, RESIDUAL_RISK, SOURCE_COMMIT_ID, SUBMITTED_BY, SUBMITTED_AT)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (attestation_id, euc_id, asset["REPOSITORY_ID"], statement, snapshot["open_finding_count"],
             snapshot["open_critical_high_count"], snapshot["residual_risk"], snapshot["source_commit_id"],
             user_id, now),
        )
        conn.commit()
        record_audit_event(
            "EUC_ATTESTATION_SUBMITTED", actor_user_id=user_id, repository_id=asset["REPOSITORY_ID"],
            payload={"euc_id": euc_id, "attestation_id": attestation_id, "statement": statement,
                     "open_finding_count": snapshot["open_finding_count"],
                     "open_critical_high_count": snapshot["open_critical_high_count"],
                     "residual_risk": snapshot["residual_risk"]},
        )
        row = conn.execute("SELECT * FROM EUC_ATTESTATIONS WHERE ATTESTATION_ID=?", (attestation_id,)).fetchone()
        return _lower(row)
    finally:
        conn.close()


def get_attestation_status(euc_id: str, user_id: str) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        asset, repository = _asset_and_repository(conn, euc_id, user_id)
        latest = _latest_for_repository(conn, asset["REPOSITORY_ID"])
        now = database._utcnow()
        baseline = latest["SUBMITTED_AT"] if latest else asset["CREATED_AT"]
        due_at = _add_days(baseline, ATTESTATION_INTERVAL_DAYS)
        return {
            "euc_id": euc_id, "repository_id": asset["REPOSITORY_ID"],
            "latest": _lower(latest) if latest else None,
            "due_at": due_at, "is_overdue": now > due_at,
            "interval_days": ATTESTATION_INTERVAL_DAYS,
            "can_attest": repository["ACCESS_ROLE"] == "owner",
        }
    finally:
        conn.close()


def list_attestations(euc_id: str, user_id: str) -> list[dict[str, Any]]:
    """Full attestation history for this workbook's repository -- spans
    every re-ingested EUC_ASSETS snapshot, since the history is a property
    of the governed repository, not of one immutable content hash."""
    conn = database._get_connection()
    try:
        asset, _ = _asset_and_repository(conn, euc_id, user_id)
        rows = conn.execute(
            "SELECT * FROM EUC_ATTESTATIONS WHERE REPOSITORY_ID=? ORDER BY SUBMITTED_AT DESC",
            (asset["REPOSITORY_ID"],),
        ).fetchall()
        return [_lower(row) for row in rows]
    finally:
        conn.close()


def is_attestation_overdue(conn, repository_id: str, asset_created_at: str | None = None) -> bool:
    """Lightweight overdue check reused by Continuous Assurance's merge
    hook -- takes an already-open connection so it composes cheaply with
    the rescore transaction instead of opening a second one."""
    latest = _latest_for_repository(conn, repository_id)
    if latest:
        baseline = latest["SUBMITTED_AT"]
    elif asset_created_at:
        baseline = asset_created_at
    else:
        row = conn.execute(
            "SELECT MIN(CREATED_AT) AS FIRST_SEEN FROM EUC_ASSETS WHERE REPOSITORY_ID=?", (repository_id,)
        ).fetchone()
        if not row or not row["FIRST_SEEN"]:
            return False
        baseline = row["FIRST_SEEN"]
    return database._utcnow() > _add_days(baseline, ATTESTATION_INTERVAL_DAYS)


def portfolio_attestation_overview(user_id: str, organization_id: str | None = None) -> dict[str, Any]:
    """Org-wide roll-up of attestation due/overdue state, one row per
    repository the user can access that has at least one EUC asset -- the
    program-management view: which attestations need chasing right now."""
    conn = database._get_connection()
    try:
        filters = ["R.STATUS = 'ACTIVE'", "(R.CREATED_BY = ? OR M.USER_ID = ?)"]
        params: list[Any] = [user_id, user_id, user_id]
        if organization_id:
            filters.append("R.ORGANIZATION_ID = ?")
            params.append(organization_id)
        rows = conn.execute(
            f"""
            SELECT R.REPOSITORY_ID, R.REPOSITORY_NAME, U.EMAIL AS OWNER_EMAIL,
                   MIN(A.CREATED_AT) AS FIRST_ASSET_CREATED_AT,
                   (SELECT A2.EUC_ID FROM EUC_ASSETS A2 WHERE A2.REPOSITORY_ID = R.REPOSITORY_ID
                    ORDER BY A2.UPDATED_AT DESC LIMIT 1) AS LATEST_EUC_ID,
                   (SELECT A2.ORIGINAL_FILENAME FROM EUC_ASSETS A2 WHERE A2.REPOSITORY_ID = R.REPOSITORY_ID
                    ORDER BY A2.UPDATED_AT DESC LIMIT 1) AS LATEST_FILENAME
            FROM WORKBOOK_REPOSITORIES R
            JOIN EUC_ASSETS A ON A.REPOSITORY_ID = R.REPOSITORY_ID
            LEFT JOIN REPOSITORY_MEMBERS M ON M.REPOSITORY_ID = R.REPOSITORY_ID AND M.USER_ID = ?
            LEFT JOIN APP_USERS U ON U.USER_ID = R.CREATED_BY
            WHERE {' AND '.join(filters)}
            GROUP BY R.REPOSITORY_ID
            ORDER BY R.REPOSITORY_NAME
            """,
            params,
        ).fetchall()
        now = database._utcnow()
        items = []
        for row in rows:
            latest = _latest_for_repository(conn, row["REPOSITORY_ID"])
            baseline = latest["SUBMITTED_AT"] if latest else row["FIRST_ASSET_CREATED_AT"]
            due_at = _add_days(baseline, ATTESTATION_INTERVAL_DAYS)
            items.append({
                "euc_id": row["LATEST_EUC_ID"], "filename": row["LATEST_FILENAME"],
                "repository_id": row["REPOSITORY_ID"], "repository_name": row["REPOSITORY_NAME"],
                "owner_email": row["OWNER_EMAIL"],
                "last_submitted_at": latest["SUBMITTED_AT"] if latest else None,
                "last_submitted_by": latest["SUBMITTED_BY"] if latest else None,
                "due_at": due_at, "is_overdue": now > due_at,
            })
        items.sort(key=lambda item: (not item["is_overdue"], item["due_at"]))
        summary = {
            "total_assets": len(items),
            "overdue_count": sum(1 for item in items if item["is_overdue"]),
            "never_attested_count": sum(1 for item in items if not item["last_submitted_at"]),
        }
        return {"summary": summary, "items": items}
    finally:
        conn.close()
