"""Continuous Macro Assurance -- re-validates every runnable macro's
assumptions against main every time a merge lands, instead of waiting for
someone to click Run and discover a reference is now stale. Mirrors
``app.euc.continuous_assurance.rescore_repository_after_merge`` exactly:
opt-in only (a repository with no runnable macros is untouched, never
auto-started), one check per runnable macro, and a notification only on a
genuine OK -> STALE *flip* -- not on every merge, and not on a macro that
was already known to be stale.

Schema-level only: this checks that a macro's referenced columns/sheets
still exist, using the exact same missing-column detection
``app.macros.execution`` already does for real runs (factored out here so
neither module needs the other's write path). It does not re-execute the
macro speculatively -- that would be both expensive on every merge and a
correctness risk in its own right (an interpreted-lane macro could have
side effects worth avoiding outside an explicit, reviewed Run).
"""

from __future__ import annotations

from typing import Any

from .. import database
from .execution import ExecutionError, _column_map, _first_sheet_id
from .macro_explainer import structural_facts
from .parser.statement_parser import ParseError, parse_sub


def _id(prefix: str) -> str:
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _latest_runnable_macros(conn, repository_id: str) -> list[Any]:
    latest_extraction = conn.execute(
        """SELECT EXTRACTION_RUN_ID FROM MACRO_EXTRACTION_RUNS WHERE REPOSITORY_ID=? AND STATUS='COMPLETED'
           ORDER BY CREATED_AT DESC LIMIT 1""",
        (repository_id,),
    ).fetchone()
    if not latest_extraction:
        return []
    return conn.execute(
        "SELECT * FROM MACRO_DEFINITIONS WHERE EXTRACTION_RUN_ID=? AND STATIC_RISK='RUNNABLE'",
        (latest_extraction["EXTRACTION_RUN_ID"],),
    ).fetchall()


def _check_still_resolvable(conn, branch_id: str, facts: dict[str, Any]) -> list[str]:
    """Schema-identity check only -- no row data is fetched. Returns a list
    of human-readable reasons the macro's assumptions no longer hold;
    empty means everything it references still exists."""
    reasons: list[str] = []
    try:
        sheet_id = _first_sheet_id(conn, branch_id)
    except ExecutionError as exc:
        return [str(exc)]
    columns = _column_map(conn, branch_id, sheet_id)
    for column_index in facts["columns_referenced"]:
        if column_index not in columns:
            reasons.append(f"column {column_index} no longer exists on the default sheet")
    if facts["sheets_referenced"]:
        sheet_names = {
            row["SHEET_NAME"] for row in conn.execute(
                "SELECT SHEET_NAME FROM BRANCH_SHEETS WHERE BRANCH_ID=? AND STATUS='ACTIVE'", (branch_id,)
            ).fetchall()
        }
        for name in facts["sheets_referenced"]:
            if name not in sheet_names:
                reasons.append(f"worksheet '{name}' no longer exists")
    return reasons


def _previous_check(conn, repository_id: str, module_name: str, proc_name: str):
    return conn.execute(
        """SELECT * FROM MACRO_ASSURANCE_CHECKS WHERE REPOSITORY_ID=? AND MODULE_NAME=? AND PROC_NAME=?
           ORDER BY CHECKED_AT DESC LIMIT 1""",
        (repository_id, module_name, proc_name),
    ).fetchone()


def rescore_macros_after_merge(repository_id: str, table_id: str, branch_id: str, actor_user_id: str) -> dict[str, Any] | None:
    conn = database._get_connection()
    try:
        macros = _latest_runnable_macros(conn, repository_id)
        if not macros:
            return None

        results = []
        newly_stale = []
        for macro in macros:
            source = None
            try:
                from ..services.semantic_ledger_service import ledger_for_connection
                source = ledger_for_connection(conn).objects.get_bytes(conn, macro["SOURCE_OBJECT_HASH"]).decode("utf-8")
                sub_ast = parse_sub(source)
                facts = structural_facts(sub_ast)
                reasons = _check_still_resolvable(conn, branch_id, facts)
            except ParseError as exc:
                reasons = [f"macro no longer parses: {exc}"]
            except Exception as exc:  # noqa: BLE001 -- a check failure is itself reported as STALE, never silently dropped
                reasons = [f"assurance check failed: {exc}"]

            status = "STALE" if reasons else "OK"
            previous = _previous_check(conn, repository_id, macro["MODULE_NAME"], macro["PROC_NAME"])
            flipped_to_stale = status == "STALE" and (not previous or previous["STATUS"] == "OK")

            check_id = _id("MASR")
            conn.execute(
                """INSERT INTO MACRO_ASSURANCE_CHECKS
                    (CHECK_ID, REPOSITORY_ID, MACRO_ID, MODULE_NAME, PROC_NAME, BRANCH_ID, STATUS, STALE_REASON, CHECKED_AT)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (check_id, repository_id, macro["MACRO_ID"], macro["MODULE_NAME"], macro["PROC_NAME"],
                 branch_id, status, "; ".join(reasons) or None, database._utcnow()),
            )
            results.append({"proc_name": macro["PROC_NAME"], "module_name": macro["MODULE_NAME"], "status": status, "reasons": reasons})
            if flipped_to_stale:
                newly_stale.append({"proc_name": macro["PROC_NAME"], "module_name": macro["MODULE_NAME"], "reasons": reasons})
        conn.commit()

        if newly_stale:
            owner_row = conn.execute(
                "SELECT CREATED_BY, REPOSITORY_NAME FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (repository_id,)
            ).fetchone()
            if owner_row:
                names = ", ".join(f"{item['proc_name']} ({item['module_name']})" for item in newly_stale)
                try:
                    database.create_notification(
                        owner_row["CREATED_BY"], "MACRO_ASSURANCE_STALE",
                        "A macro's assumptions are now stale",
                        f"{names} in '{owner_row['REPOSITORY_NAME']}' no longer matches the current data -- "
                        "review it in Virtual Run before running it again.",
                        "REPOSITORY", repository_id,
                    )
                except Exception:
                    pass  # a failed notification must never fail the merge this runs alongside

        return {"checked_count": len(results), "results": results, "newly_stale_count": len(newly_stale)}
    finally:
        conn.close()
