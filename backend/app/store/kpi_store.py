"""Dashboard KPI aggregation (commit counts, risk scores, top contributors)."""

from typing import Any

from .schema import _get_connection


def get_kpis(user_id: str, table_id: str | None = None) -> dict[str, Any]:
    conn = _get_connection()
    try:
        if table_id:
            where = "WHERE A.TABLE_ID=?"
            params: tuple[Any, ...] = (table_id,)
        else:
            where = """
                WHERE A.TABLE_ID IN (
                    SELECT D.TABLE_ID FROM DATASET_REGISTRY D
                    WHERE D.OWNER_USER_ID IN (?, 'USR_SYSTEM') OR EXISTS (
                        SELECT 1 FROM DATASET_MEMBERS M
                        WHERE M.TABLE_ID=D.TABLE_ID AND M.USER_ID=?
                    )
                )
            """
            params = (user_id, user_id)
        summary = conn.execute(
            f"""
            SELECT COUNT(*) AS CELL_EVENTS,
                   COUNT(DISTINCT BATCH_ID) AS COMMITS,
                   COUNT(DISTINCT USER_ID) AS CONTRIBUTORS,
                   SUM(CASE WHEN STATUS IN
                       ('UPDATED', 'INSERTED', 'DELETED', 'COLUMN_ADDED',
                        'COLUMN_DELETED', 'SNAPSHOT_RESTORED')
                       THEN 1 ELSE 0 END) AS SUCCESSES,
                   SUM(CASE WHEN STATUS='NO_CHANGE' THEN 1 ELSE 0 END) AS NO_CHANGES,
                   AVG(RISK_SCORE) AS AVG_RISK,
                   MAX(RISK_SCORE) AS MAX_RISK
            FROM AUDIT_COMMITS A {where}
            """,
            params,
        ).fetchone()
        datasets = conn.execute(
            """
            SELECT COUNT(*) FROM DATASET_REGISTRY D
            WHERE D.OWNER_USER_ID IN (?, 'USR_SYSTEM') OR EXISTS (
                SELECT 1 FROM DATASET_MEMBERS M
                WHERE M.TABLE_ID=D.TABLE_ID AND M.USER_ID=?
            )
            """,
            (user_id, user_id),
        ).fetchone()[0]
        top_users = conn.execute(
            f"""
            SELECT USER_ID, USER_EMAIL, COUNT(DISTINCT BATCH_ID) AS COMMITS
            FROM AUDIT_COMMITS A {where}
            GROUP BY USER_ID, USER_EMAIL ORDER BY COMMITS DESC LIMIT 5
            """,
            params,
        ).fetchall()
        return {
            "datasets": datasets,
            "cell_events": summary["CELL_EVENTS"] or 0,
            "commits": summary["COMMITS"] or 0,
            "contributors": summary["CONTRIBUTORS"] or 0,
            "successes": summary["SUCCESSES"] or 0,
            "no_changes": summary["NO_CHANGES"] or 0,
            "avg_risk": round(summary["AVG_RISK"] or 0, 1),
            "max_risk": summary["MAX_RISK"] or 0,
            "top_contributors": [
                {"user_id": row[0], "email": row[1], "commits": row[2]}
                for row in top_users
            ],
        }
    finally:
        conn.close()
