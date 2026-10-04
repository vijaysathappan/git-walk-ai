"""Continuous Assurance — re-scores the EUC asset already tracked for a
repository every time a merge lands on main, instead of waiting for the
next scheduled audit to notice a regression. Only acts on repositories
that have already opted in to EUC governance (i.e. already have at least
one analyzed asset); it never silently starts tracking a repository
nobody registered for EUC intelligence. Reuses the exact Stage 2.1 -> 2.2
-> 2.3 pipeline already proven by the Risk Drift Radar's branch snapshot
helper — this just points it at main after every merge instead of at a
branch on demand."""

from __future__ import annotations

from typing import Any

from .. import database
from .attestation import is_attestation_overdue
from .branch_comparison import snapshot_branch_for_comparison


_SEVERE_SEVERITIES = ("CRITICAL", "HIGH")


def _severe_finding_count(conn, intelligence_run_id: str) -> int:
    if not intelligence_run_id:
        return 0
    placeholders = ",".join("?" for _ in _SEVERE_SEVERITIES)
    row = conn.execute(
        f"""SELECT COUNT(*) FROM EUC_FINDING_OCCURRENCES O JOIN EUC_FINDINGS F ON F.FINDING_ID = O.FINDING_ID
            WHERE O.INTELLIGENCE_RUN_ID=? AND O.SEVERITY IN ({placeholders})
            AND F.STATUS NOT IN ('RESOLVED','FALSE_POSITIVE','SUPPRESSED')""",
        (intelligence_run_id, *_SEVERE_SEVERITIES),
    ).fetchone()
    return row[0] if row else 0


def rescore_repository_after_merge(
    repository_id: str, table_id: str, branch_id: str, branch_name: str, actor_user_id: str,
) -> dict[str, Any] | None:
    conn = database._get_connection()
    try:
        tracked = conn.execute(
            "SELECT EUC_ID FROM EUC_ASSETS WHERE REPOSITORY_ID=? ORDER BY UPDATED_AT DESC LIMIT 1",
            (repository_id,),
        ).fetchone()
        if not tracked:
            return None
        euc_id = tracked["EUC_ID"]
        previous_run = conn.execute(
            """SELECT INTELLIGENCE_RUN_ID FROM EUC_INTELLIGENCE_RUNS
               WHERE EUC_ID=? AND STATUS='COMPLETED' ORDER BY STARTED_AT DESC, ROWID DESC LIMIT 1""",
            (euc_id,),
        ).fetchone()
        previous_severe = _severe_finding_count(conn, previous_run["INTELLIGENCE_RUN_ID"] if previous_run else None)
        owner_row = conn.execute(
            "SELECT CREATED_BY, REPOSITORY_NAME FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?",
            (repository_id,),
        ).fetchone()
    finally:
        conn.close()

    result = snapshot_branch_for_comparison(repository_id, branch_id, branch_name, table_id, actor_user_id)

    conn = database._get_connection()
    try:
        new_severe = _severe_finding_count(conn, result["intelligence_run_id"])
    finally:
        conn.close()
    regressed = new_severe > previous_severe

    if regressed and owner_row:
        try:
            database.create_notification(
                owner_row["CREATED_BY"], "EUC_RISK_DRIFT",
                f"New high-severity risk in {owner_row['REPOSITORY_NAME']} after merge",
                f"Continuous assurance found {new_severe} open high/critical finding(s), up from "
                f"{previous_severe}, after the latest merge to main.",
                "REPOSITORY", repository_id,
            )
        except Exception:
            pass

    attestation_overdue = False
    if owner_row:
        conn = database._get_connection()
        try:
            attestation_overdue = is_attestation_overdue(conn, repository_id)
        finally:
            conn.close()
        if attestation_overdue:
            try:
                database.create_notification(
                    owner_row["CREATED_BY"], "EUC_ATTESTATION_OVERDUE",
                    f"Attestation overdue for {owner_row['REPOSITORY_NAME']}",
                    "This EUC's periodic risk attestation is overdue. Review its current findings "
                    "and submit a new attestation from the Risk & controls tab.",
                    "REPOSITORY", repository_id,
                )
            except Exception:
                pass

    return {
        "euc_id": euc_id, "intelligence_run_id": result["intelligence_run_id"],
        "previous_severe_finding_count": previous_severe,
        "severe_finding_count": new_severe, "regressed": regressed,
        "attestation_overdue": attestation_overdue,
    }
