"""Portfolio Risk Command Center — org-wide roll-up of every EUC asset's
latest Stage 2.3 score, so a risk owner sees the whole portfolio's risk
posture in one place instead of opening each asset individually."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .. import database
from .intelligence.scoring.models import classification


def portfolio_risk_overview(user_id: str, organization_id: str | None = None) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        filters = ["R.STATUS = 'ACTIVE'", "(R.CREATED_BY = ? OR M.USER_ID = ?)"]
        params: list[Any] = [user_id, user_id, user_id]
        if organization_id:
            filters.append("R.ORGANIZATION_ID = ?")
            params.append(organization_id)
        rows = conn.execute(
            f"""
            SELECT A.EUC_ID, A.ORIGINAL_FILENAME, A.REPOSITORY_ID, R.REPOSITORY_NAME,
                   U.EMAIL AS OWNER_EMAIL, U.DISPLAY_NAME AS OWNER_NAME,
                   I.INTELLIGENCE_RUN_ID, I.COMPLEXITY_SCORE, I.INHERENT_RISK_SCORE,
                   I.CONTROL_SCORE, I.RESIDUAL_RISK_SCORE, I.FINDING_COUNT,
                   I.CRITICAL_FINDING_COUNT, I.COMPLETED_AT, I.SOURCE_COMMIT_ID,
                   B.HEAD_COMMIT_ID AS CURRENT_HEAD_COMMIT_ID
            FROM EUC_ASSETS A
            JOIN WORKBOOK_REPOSITORIES R ON R.REPOSITORY_ID = A.REPOSITORY_ID
            LEFT JOIN REPOSITORY_MEMBERS M ON M.REPOSITORY_ID = R.REPOSITORY_ID AND M.USER_ID = ?
            LEFT JOIN APP_USERS U ON U.USER_ID = R.CREATED_BY
            LEFT JOIN BRANCHES B ON B.BRANCH_ID = R.DEFAULT_BRANCH_ID
            LEFT JOIN EUC_INTELLIGENCE_RUNS I ON I.INTELLIGENCE_RUN_ID = (
                SELECT INTELLIGENCE_RUN_ID FROM EUC_INTELLIGENCE_RUNS
                WHERE EUC_ID = A.EUC_ID AND STATUS = 'COMPLETED'
                ORDER BY STARTED_AT DESC, ROWID DESC LIMIT 1
            )
            WHERE {' AND '.join(filters)}
            ORDER BY A.UPDATED_AT DESC
            """,
            params,
        ).fetchall()

        run_ids = [row["INTELLIGENCE_RUN_ID"] for row in rows if row["INTELLIGENCE_RUN_ID"]]
        severity_by_run: dict[str, Counter] = {}
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            for occurrence in conn.execute(
                f"""SELECT O.INTELLIGENCE_RUN_ID, O.SEVERITY FROM EUC_FINDING_OCCURRENCES O
                    JOIN EUC_FINDINGS F ON F.FINDING_ID = O.FINDING_ID
                    WHERE O.INTELLIGENCE_RUN_ID IN ({placeholders})
                    AND F.STATUS NOT IN ('RESOLVED','FALSE_POSITIVE','SUPPRESSED')""",
                run_ids,
            ):
                severity_by_run.setdefault(occurrence["INTELLIGENCE_RUN_ID"], Counter())[occurrence["SEVERITY"]] += 1

        assets = []
        for row in rows:
            has_run = bool(row["INTELLIGENCE_RUN_ID"])
            residual_risk = row["RESIDUAL_RISK_SCORE"] if has_run else None
            assets.append({
                "euc_id": row["EUC_ID"], "filename": row["ORIGINAL_FILENAME"],
                "repository_id": row["REPOSITORY_ID"], "repository_name": row["REPOSITORY_NAME"],
                "owner_email": row["OWNER_EMAIL"], "owner_name": row["OWNER_NAME"],
                "status": "SCORED" if has_run else "NOT_SCORED",
                "complexity": row["COMPLEXITY_SCORE"], "inherent_risk": row["INHERENT_RISK_SCORE"],
                "control_strength": row["CONTROL_SCORE"], "residual_risk": residual_risk,
                "residual_risk_classification": classification(residual_risk) if has_run else None,
                "finding_count": row["FINDING_COUNT"] or 0, "critical_finding_count": row["CRITICAL_FINDING_COUNT"] or 0,
                "finding_severities": dict(severity_by_run.get(row["INTELLIGENCE_RUN_ID"], {})),
                "last_scored_at": row["COMPLETED_AT"],
                "freshness": ("CURRENT" if row["SOURCE_COMMIT_ID"] == row["CURRENT_HEAD_COMMIT_ID"] else "STALE") if has_run else None,
            })

        assets.sort(key=lambda item: (item["status"] != "SCORED", -(item["residual_risk"] or 0)))
        scored = [item for item in assets if item["status"] == "SCORED"]
        summary = {
            "total_assets": len(assets),
            "scored_assets": len(scored),
            "not_scored_assets": len(assets) - len(scored),
            "high_risk_count": sum(1 for item in scored if item["residual_risk_classification"] in ("HIGH", "VERY_HIGH")),
            "critical_finding_total": sum(item["critical_finding_count"] for item in scored),
            "average_residual_risk": round(sum(item["residual_risk"] or 0 for item in scored) / len(scored), 1) if scored else 0,
            "stale_count": sum(1 for item in scored if item["freshness"] == "STALE"),
        }
        return {"summary": summary, "assets": assets}
    finally:
        conn.close()


def portfolio_open_findings(user_id: str, organization_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Every open HIGH/CRITICAL EUC finding the user can see, across every
    repository, newest first -- the grounding detail behind the aggregate
    scores in ``portfolio_risk_overview`` (used by the compliance copilot
    to cite specific findings, not just repeat a summary number)."""
    conn = database._get_connection()
    try:
        filters = ["R.STATUS = 'ACTIVE'", "(R.CREATED_BY = ? OR M.USER_ID = ?)",
                   "F.STATUS NOT IN ('RESOLVED','FALSE_POSITIVE','SUPPRESSED')", "O.SEVERITY IN ('CRITICAL','HIGH')"]
        params: list[Any] = [user_id, user_id, user_id]
        if organization_id:
            filters.append("R.ORGANIZATION_ID = ?")
            params.append(organization_id)
        params.append(max(1, min(limit, 100)))
        rows = conn.execute(
            f"""
            SELECT F.FINDING_ID, F.TITLE, F.RULE_ID, F.STATUS, F.UPDATED_AT, O.SEVERITY, O.DESCRIPTION,
                   O.SHEET_ID, O.CELL_ADDRESS, A.EUC_ID, A.ORIGINAL_FILENAME, R.REPOSITORY_ID, R.REPOSITORY_NAME
            FROM EUC_FINDINGS F
            JOIN EUC_ASSETS A ON A.EUC_ID = F.EUC_ID
            JOIN WORKBOOK_REPOSITORIES R ON R.REPOSITORY_ID = A.REPOSITORY_ID
            LEFT JOIN REPOSITORY_MEMBERS M ON M.REPOSITORY_ID = R.REPOSITORY_ID AND M.USER_ID = ?
            LEFT JOIN EUC_FINDING_OCCURRENCES O ON O.FINDING_ID = F.FINDING_ID AND O.INTELLIGENCE_RUN_ID = F.LAST_RUN_ID
            WHERE {' AND '.join(filters)}
            ORDER BY CASE O.SEVERITY WHEN 'CRITICAL' THEN 0 ELSE 1 END, F.UPDATED_AT DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [{
            "finding_id": row["FINDING_ID"], "title": row["TITLE"], "rule_id": row["RULE_ID"],
            "status": row["STATUS"], "severity": row["SEVERITY"], "description": row["DESCRIPTION"],
            "sheet_id": row["SHEET_ID"], "cell_address": row["CELL_ADDRESS"], "euc_id": row["EUC_ID"],
            "filename": row["ORIGINAL_FILENAME"], "repository_id": row["REPOSITORY_ID"],
            "repository_name": row["REPOSITORY_NAME"], "updated_at": row["UPDATED_AT"],
        } for row in rows]
    finally:
        conn.close()
