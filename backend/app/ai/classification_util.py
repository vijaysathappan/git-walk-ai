"""Shared helper for deriving the real data-classification of a repository,
instead of every AI feature hardcoding `"INTERNAL"`. Mirrors the lookup
`EvidenceRetriever.retrieve` already does against `WORKBOOK_REPOSITORIES`.
"""

from __future__ import annotations

from .. import database


def repository_classification(repository_id: str | None) -> str:
    if not repository_id:
        return "INTERNAL"
    conn = database._get_connection()
    try:
        row = conn.execute(
            "SELECT DATA_CLASSIFICATION FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?",
            (repository_id,),
        ).fetchone()
        return str(row["DATA_CLASSIFICATION"] or "INTERNAL").upper() if row else "INTERNAL"
    finally:
        conn.close()


def euc_classification(euc_id: str | None) -> str:
    if not euc_id:
        return "INTERNAL"
    conn = database._get_connection()
    try:
        row = conn.execute(
            """SELECT R.DATA_CLASSIFICATION FROM EUC_ASSETS A
               JOIN WORKBOOK_REPOSITORIES R ON R.REPOSITORY_ID=A.REPOSITORY_ID WHERE A.EUC_ID=?""",
            (euc_id,),
        ).fetchone()
        return str(row["DATA_CLASSIFICATION"] or "INTERNAL").upper() if row else "INTERNAL"
    finally:
        conn.close()
