"""Read models for Stage 4 audit, observability, analytics, and workbook blame."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from ..database import _get_connection
from .commit_store import reconstruct_branch
from .merge_store import branch_context


def _decode(row) -> dict[str, Any]:
    item = {key.lower(): row[key] for key in row.keys()}
    for key in (
        "event_payload", "tags_json", "details_json", "metadata_json",
        "old_value", "new_value",
    ):
        if item.get(key):
            item[key.removesuffix("_json")] = json.loads(item.pop(key))
    return item


def workbook_change_activity(
    branch_id: str,
    sheet_id: str | None = None,
    operation: str | None = None,
    sort: str = "desc",
    limit: int = 1000,
) -> dict[str, Any]:
    """Return only committed workbook mutations, never unchanged imported cells."""
    where = ["X.BRANCH_ID=?"]
    params: list[Any] = [branch_id]
    if sheet_id:
        where.append("X.SHEET_ID=?")
        params.append(sheet_id)
    if operation:
        where.append("X.OPERATION_TYPE=?")
        params.append(operation.strip().upper())
    direction = "ASC" if sort.lower() == "asc" else "DESC"
    params.append(max(1, min(limit, 5000)))
    conn = _get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT X.*,C.MESSAGE,C.AUTHOR_USER_ID,C.AUTHOR_EMAIL,C.COMMIT_HASH,
                   C.CREATED_AT AS COMMIT_CREATED_AT,
                   S.SHEET_NAME,SC.COLUMN_NAME,SR.ROW_POSITION,SC.COLUMN_POSITION,
                   M.MERGE_REQUEST_ID
            FROM COMMIT_CHANGES X
            JOIN COMMITS C ON C.COMMIT_ID=X.COMMIT_ID
            LEFT JOIN BRANCH_SHEETS S
              ON S.BRANCH_ID=X.BRANCH_ID AND S.SHEET_ID=X.SHEET_ID
            LEFT JOIN SHEET_COLUMNS SC
              ON SC.BRANCH_ID=X.BRANCH_ID AND SC.SHEET_ID=X.SHEET_ID
             AND SC.COLUMN_ID=X.COLUMN_ID
            LEFT JOIN SHEET_ROWS SR
              ON SR.BRANCH_ID=X.BRANCH_ID AND SR.SHEET_ID=X.SHEET_ID
             AND SR.ROW_ID=X.ROW_ID
            LEFT JOIN MERGE_REQUESTS M ON M.MERGE_COMMIT_ID=C.COMMIT_ID
            WHERE {' AND '.join(where)}
            ORDER BY C.CREATED_AT {direction},X.CHANGE_ID {direction}
            LIMIT ?
            """,
            params,
        ).fetchall()
        events = [_decode(row) for row in rows]
        for event in events:
            event["occurred_at"] = event.pop("commit_created_at")
            event["action"] = (
                "created" if event["operation_type"].endswith("INSERT")
                or event["operation_type"].endswith("CREATE")
                else "deleted" if event["operation_type"].endswith("DELETE")
                else "updated"
            )
        read_rows = conn.execute(
            f"""
            SELECT A.EVENT_ID,A.ACTOR_USER_ID,U.EMAIL AS AUTHOR_EMAIL,
                   A.EVENT_PAYLOAD,A.CREATED_AT,A.COMMIT_ID,A.MERGE_REQUEST_ID
            FROM AUDIT_EVENTS A
            LEFT JOIN APP_USERS U ON U.USER_ID=A.ACTOR_USER_ID
            WHERE A.BRANCH_ID=? AND A.EVENT_TYPE='CELL_READ'
            ORDER BY A.CREATED_AT {direction}
            LIMIT ?
            """,
            (branch_id, max(1, min(limit, 5000))),
        ).fetchall()
        for row in read_rows:
            payload = json.loads(row["EVENT_PAYLOAD"] or "{}")
            if sheet_id and payload.get("sheet_id") != sheet_id:
                continue
            if operation and operation.strip().upper() != "CELL_READ":
                continue
            events.append({
                "change_id": row["EVENT_ID"],
                "operation_type": "CELL_READ",
                "action": "read",
                "sheet_id": payload.get("sheet_id"),
                "sheet_name": payload.get("sheet_name"),
                "row_id": payload.get("row_id"),
                "column_id": payload.get("column_id"),
                "column_name": payload.get("column_name"),
                "row_position": payload.get("row_position"),
                "column_position": payload.get("column_position"),
                "old_value": None,
                "new_value": payload.get("value"),
                "author_user_id": row["ACTOR_USER_ID"],
                "author_email": row["AUTHOR_EMAIL"],
                "commit_id": row["COMMIT_ID"],
                "merge_request_id": row["MERGE_REQUEST_ID"],
                "occurred_at": row["CREATED_AT"],
            })
        events.sort(
            key=lambda item: item.get("occurred_at") or "",
            reverse=direction == "DESC",
        )
        events = events[: max(1, min(limit, 5000))]
        return {
            "branch_id": branch_id,
            "events": events,
            "sort": direction.lower(),
            "event_count": len(events),
        }
    finally:
        conn.close()


def repository_id_for_table(table_id: str) -> str | None:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT REPOSITORY_ID FROM WORKBOOK_REPOSITORIES WHERE TABLE_ID=?",
            (table_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def list_audit_events(
    *, repository_id: str | None = None, actor_user_id: str | None = None,
    event_type: str | None = None, status: str | None = None, limit: int = 200,
    since: str | None = None, until: str | None = None,
) -> list[dict[str, Any]]:
    where, params = [], []
    for column, value in (
        ("REPOSITORY_ID", repository_id), ("ACTOR_USER_ID", actor_user_id),
        ("EVENT_TYPE", event_type), ("STATUS", status),
    ):
        if value:
            where.append(f"{column}=?")
            params.append(value)
    if since:
        where.append("CREATED_AT>=?")
        params.append(since)
    if until:
        where.append("CREATED_AT<=?")
        params.append(until)
    sql = "SELECT * FROM AUDIT_EVENTS"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ROWID DESC LIMIT ?"
    params.append(max(1, min(limit, 1000)))
    conn = _get_connection()
    try:
        return [_decode(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def verify_audit_integrity() -> dict[str, Any]:
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT * FROM AUDIT_EVENTS ORDER BY ROWID").fetchall()
    finally:
        conn.close()
    previous_hash = "GENESIS"
    for position, row in enumerate(rows, start=1):
        canonical = "|".join(
            [row["EVENT_ID"], row["CREATED_AT"], row["ACTOR_USER_ID"],
             row["ACTOR_TYPE"], row["EVENT_TYPE"], row["EVENT_PAYLOAD"],
             row["REQUEST_ID"], row["TRACE_ID"], row["STATUS"],
             row["FAILURE_REASON"] or "", previous_hash]
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if row["PREVIOUS_EVENT_HASH"] != previous_hash or row["EVENT_HASH"] != expected:
            return {"valid": False, "event_count": len(rows), "broken_at": position,
                    "event_id": row["EVENT_ID"]}
        previous_hash = row["EVENT_HASH"]
    return {"valid": True, "event_count": len(rows), "head_hash": previous_hash}


def operational_metrics(hours: int = 24) -> dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(hours=max(1, min(hours, 24 * 90)))).isoformat()
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM OPERATION_METRICS WHERE CREATED_AT>=? ORDER BY CREATED_AT DESC",
            (since,),
        ).fetchall()
        security = conn.execute(
            "SELECT SEVERITY,COUNT(*) AS COUNT FROM SECURITY_EVENTS WHERE CREATED_AT>=? GROUP BY SEVERITY",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    groups: dict[str, list[float]] = {}
    failures = Counter()
    for row in rows:
        groups.setdefault(row["METRIC_NAME"], []).append(float(row["METRIC_VALUE"]))
        if row["STATUS"] == "FAILED":
            failures[row["METRIC_NAME"]] += 1
    summary = {}
    for name, values in groups.items():
        ordered = sorted(values)
        summary[name] = {
            "count": len(values), "average": round(sum(values) / len(values), 2),
            "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * .95))], 2),
            "maximum": round(max(values), 2), "failures": failures[name],
        }
    return {
        "window_hours": hours, "samples": len(rows), "metrics": summary,
        "security_events": {row["SEVERITY"].lower(): row["COUNT"] for row in security},
    }


def operational_metrics_trend(hours: int = 24 * 14) -> list[dict[str, Any]]:
    """Day-grouped trend of the same OPERATION_METRICS operational_metrics()
    flattens to one all-time average — gives the Insights hub's trend charts
    real day-over-day movement instead of a single snapshot number."""
    since = (datetime.now(timezone.utc) - timedelta(hours=max(1, min(hours, 24 * 180)))).isoformat()
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT DATE(CREATED_AT) AS DAY, METRIC_NAME, METRIC_VALUE, STATUS "
            "FROM OPERATION_METRICS WHERE CREATED_AT>=? ORDER BY DAY ASC",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = row["DAY"]
        bucket = by_day.setdefault(day, {"day": day, "samples": 0, "failures": 0, "values": []})
        bucket["samples"] += 1
        bucket["values"].append(float(row["METRIC_VALUE"]))
        if row["STATUS"] == "FAILED":
            bucket["failures"] += 1
    trend = []
    for day, bucket in sorted(by_day.items()):
        values = bucket.pop("values")
        bucket["average"] = round(sum(values) / len(values), 2) if values else 0
        trend.append(bucket)
    return trend


def _macro_governance(conn, repository_id: str) -> dict[str, Any]:
    """Virtual Run's own insight block: macro-population health (from the
    most recently completed extraction -- an older, superseded upload's
    macro list is not what "how many macros does this repo have today"
    should mean) plus run activity (across ALL extractions ever, since a
    macro from an older upload can still be this repo's most-run one)."""
    latest_extraction = conn.execute(
        """SELECT * FROM MACRO_EXTRACTION_RUNS WHERE REPOSITORY_ID=? AND STATUS='COMPLETED'
           ORDER BY CREATED_AT DESC LIMIT 1""",
        (repository_id,),
    ).fetchone()
    empty = {
        "has_macros": False, "total_macros": 0, "runnable": 0,
        "blocked_external": 0, "blocked_unsupported": 0,
        "sql_lane": 0, "interpreted_lane": 0,
        "runs_last_30d": 0, "most_run_macro": None, "last_run_at": None,
    }
    if not latest_extraction:
        return empty

    macros = conn.execute(
        "SELECT STATIC_RISK, EXECUTION_LANE FROM MACRO_DEFINITIONS WHERE EXTRACTION_RUN_ID=?",
        (latest_extraction["EXTRACTION_RUN_ID"],),
    ).fetchall()
    runs = conn.execute(
        """SELECT MR.MACRO_ID, MR.STATUS, MR.CREATED_AT, MD.PROC_NAME, MD.MODULE_NAME
           FROM MACRO_RUNS MR JOIN MACRO_DEFINITIONS MD ON MD.MACRO_ID = MR.MACRO_ID
           WHERE MR.REPOSITORY_ID=?""",
        (repository_id,),
    ).fetchall()
    executed = [row for row in runs if row["STATUS"] == "EXECUTED"]
    thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    most_run_macro = None
    run_counts = Counter(row["MACRO_ID"] for row in executed)
    top = run_counts.most_common(1)
    if top:
        macro_id, count = top[0]
        sample = next(row for row in executed if row["MACRO_ID"] == macro_id)
        most_run_macro = {"proc_name": sample["PROC_NAME"], "module_name": sample["MODULE_NAME"], "run_count": count}

    return {
        "has_macros": True,
        "total_macros": len(macros),
        "runnable": sum(row["STATIC_RISK"] == "RUNNABLE" for row in macros),
        "blocked_external": sum(row["STATIC_RISK"] == "BLOCKED_EXTERNAL" for row in macros),
        "blocked_unsupported": sum(row["STATIC_RISK"] == "BLOCKED_UNSUPPORTED" for row in macros),
        "sql_lane": sum(row["EXECUTION_LANE"] == "SQL" for row in macros),
        "interpreted_lane": sum(row["EXECUTION_LANE"] == "INTERPRETED" for row in macros),
        "runs_last_30d": sum(row["CREATED_AT"] >= thirty_days_ago for row in executed),
        "most_run_macro": most_run_macro,
        "last_run_at": max((row["CREATED_AT"] for row in executed), default=None),
    }


def repository_insights(repository_id: str) -> dict[str, Any]:
    conn = _get_connection()
    try:
        repository = conn.execute(
            """SELECT R.*,D.ROW_COUNT,D.COLUMN_COUNT FROM WORKBOOK_REPOSITORIES R
               JOIN DATASET_REGISTRY D ON D.TABLE_ID=R.TABLE_ID WHERE R.REPOSITORY_ID=?""",
            (repository_id,),
        ).fetchone()
        if not repository:
            raise ValueError("Repository does not exist")
        commits = conn.execute(
            "SELECT * FROM COMMITS WHERE REPOSITORY_ID=? AND STATUS!='CHECKPOINT'",
            (repository_id,),
        ).fetchall()
        changes = conn.execute(
            """SELECT X.OPERATION_TYPE,X.SHEET_ID FROM COMMIT_CHANGES X
               JOIN COMMITS C ON C.COMMIT_ID=X.COMMIT_ID WHERE C.REPOSITORY_ID=?""",
            (repository_id,),
        ).fetchall()
        requests = conn.execute(
            "SELECT * FROM MERGE_REQUESTS WHERE REPOSITORY_ID=?", (repository_id,)
        ).fetchall()
        conflicted_request_ids = {
            row[0] for row in conn.execute(
                """SELECT DISTINCT C.MERGE_REQUEST_ID FROM MERGE_CONFLICTS C
                   JOIN MERGE_REQUESTS M ON M.MERGE_REQUEST_ID=C.MERGE_REQUEST_ID
                   WHERE M.REPOSITORY_ID=?""",
                (repository_id,),
            ).fetchall()
        }
        branch_rows = conn.execute(
            "SELECT CREATED_AT,MERGED_AT,ARCHIVED_AT,STATUS FROM BRANCHES WHERE REPOSITORY_ID=? AND BRANCH_TYPE='USER'",
            (repository_id,),
        ).fetchall()
        validations = conn.execute(
            "SELECT STATUS,ERROR_COUNT,WARNING_COUNT FROM VALIDATION_RUNS WHERE REPOSITORY_ID=?",
            (repository_id,),
        ).fetchall()
        active_branches = conn.execute(
            "SELECT COUNT(*) FROM BRANCHES WHERE REPOSITORY_ID=? AND STATUS='ACTIVE'",
            (repository_id,),
        ).fetchone()[0]
        active_copies = conn.execute(
            "SELECT COUNT(*) FROM WORKING_COPIES WHERE REPOSITORY_ID=? AND STATUS='ACTIVE'",
            (repository_id,),
        ).fetchone()[0]
        sheet_count = conn.execute(
            "SELECT COUNT(*) FROM WORKBOOK_SHEETS WHERE REPOSITORY_ID=? AND STATUS='ACTIVE'",
            (repository_id,),
        ).fetchone()[0]
        top_sheets = Counter(row["SHEET_ID"] for row in changes if row["SHEET_ID"])
        sheet_names = {
            row["SHEET_ID"]: row["SHEET_NAME"] for row in conn.execute(
                "SELECT SHEET_ID,SHEET_NAME FROM WORKBOOK_SHEETS WHERE REPOSITORY_ID=?",
                (repository_id,),
            ).fetchall()
        }
        macro_governance = _macro_governance(conn, repository_id)
    finally:
        conn.close()
    merged = [item for item in requests if item["STATUS"] == "MERGED"]
    conflicted = [item for item in requests if item["MERGE_REQUEST_ID"] in conflicted_request_ids]
    review_hours = []
    for item in merged:
        if item["MERGED_AT"]:
            review_hours.append(
                (datetime.fromisoformat(item["MERGED_AT"]) - datetime.fromisoformat(item["CREATED_AT"])).total_seconds() / 3600
            )
    most_changed = top_sheets.most_common(1)
    now = datetime.now(timezone.utc)
    branch_lifetimes = []
    for item in branch_rows:
        created = datetime.fromisoformat(item["CREATED_AT"])
        ended_at = item["MERGED_AT"] or item["ARCHIVED_AT"]
        ended = datetime.fromisoformat(ended_at) if ended_at else now
        branch_lifetimes.append((ended - created).total_seconds() / 86400)
    return {
        "repository_id": repository_id,
        "repository_name": repository["REPOSITORY_NAME"],
        "version_control": {
            "commits": len(commits), "changes": len(changes),
            "cell_changes": sum(row["OPERATION_TYPE"].startswith("CELL_") for row in changes),
            "formula_changes": sum(row["OPERATION_TYPE"] == "CELL_FORMULA_UPDATE" for row in changes),
            "reverts": sum(row["STATUS"] == "REVERT" for row in commits),
            "most_changed_sheet": sheet_names.get(most_changed[0][0], most_changed[0][0]) if most_changed else None,
        },
        "workflow": {
            "merge_requests": len(requests), "merged": len(merged),
            "merge_success_rate": round(len(merged) * 100 / len(requests), 1) if requests else 100.0,
            "conflict_rate": round(len(conflicted) * 100 / len(requests), 1) if requests else 0.0,
            "average_review_hours": round(sum(review_hours) / len(review_hours), 2) if review_hours else 0.0,
            "average_branch_lifetime_days": round(sum(branch_lifetimes) / len(branch_lifetimes), 2) if branch_lifetimes else 0.0,
            "active_branches": active_branches, "active_working_copies": active_copies,
        },
        "data_quality": {
            "validation_runs": len(validations),
            "failed_runs": sum(row["STATUS"] == "FAILED" for row in validations),
            "errors": sum(row["ERROR_COUNT"] for row in validations),
            "warnings": sum(row["WARNING_COUNT"] for row in validations),
        },
        "workbook": {"rows": repository["ROW_COUNT"], "columns": repository["COLUMN_COUNT"], "sheets": sheet_count},
        "macro_governance": macro_governance,
    }


def row_history(branch_id: str, sheet_id: str, row_id: str, limit: int = 200) -> list[dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            """SELECT X.*,C.MESSAGE,C.AUTHOR_USER_ID,C.AUTHOR_EMAIL,C.COMMIT_HASH,C.CREATED_AT AS COMMIT_CREATED_AT,
                      M.MERGE_REQUEST_ID
               FROM COMMIT_CHANGES X JOIN COMMITS C ON C.COMMIT_ID=X.COMMIT_ID
               LEFT JOIN MERGE_REQUESTS M ON M.MERGE_COMMIT_ID=C.COMMIT_ID
               WHERE X.BRANCH_ID=? AND X.SHEET_ID=? AND X.ROW_ID=?
               ORDER BY C.CREATED_AT DESC LIMIT ?""",
            (branch_id, sheet_id, row_id, max(1, min(limit, 1000))),
        ).fetchall()
        return [_decode(row) for row in rows]
    finally:
        conn.close()


def workbook_blame(branch_id: str, sheet_id: str | None = None, limit: int = 5000) -> dict[str, Any]:
    context = branch_context(branch_id)
    if not context:
        raise ValueError("Branch does not exist")
    state = reconstruct_branch(branch_id)
    selected = [sheet for sheet in state.get("sheets", []) if not sheet_id or sheet["sheet_id"] == sheet_id]
    conn = _get_connection()
    try:
        history_rows = conn.execute(
            """SELECT X.SHEET_ID,X.ROW_ID,X.COLUMN_ID,X.COMMIT_ID,C.AUTHOR_USER_ID,
                      C.AUTHOR_EMAIL,C.CREATED_AT,C.MESSAGE,M.MERGE_REQUEST_ID
               FROM COMMIT_CHANGES X JOIN COMMITS C ON C.COMMIT_ID=X.COMMIT_ID
               LEFT JOIN MERGE_REQUESTS M ON M.MERGE_COMMIT_ID=C.COMMIT_ID
               WHERE X.BRANCH_ID=? AND X.COLUMN_ID IS NOT NULL
               ORDER BY C.CREATED_AT DESC""",
            (branch_id,),
        ).fetchall()
        last_change = {}
        for item in history_rows:
            last_change.setdefault(
                (item["SHEET_ID"], item["ROW_ID"], item["COLUMN_ID"]), item
            )
        output = []
        for sheet in selected:
            columns = {item["column_id"]: item for item in sheet.get("columns", [])}
            for row in sheet.get("rows", []):
                for column_id, value in row.get("values", {}).items():
                    last = last_change.get((sheet["sheet_id"], row["row_id"], column_id))
                    output.append({
                        "sheet_id": sheet["sheet_id"], "sheet_name": sheet["name"],
                        "row_id": row["row_id"], "row_position": row["position"],
                        "column_id": column_id,
                        "column_name": columns.get(column_id, {}).get("name", column_id),
                        "value": value, "formula": row.get("formulas", {}).get(column_id),
                        "last_commit_id": last["COMMIT_ID"] if last else context["base_commit_id"],
                        "last_author_user_id": last["AUTHOR_USER_ID"] if last else context["created_by"],
                        "last_author_email": last["AUTHOR_EMAIL"] if last else None,
                        "last_modified_at": last["CREATED_AT"] if last else context["created_at"],
                        "commit_message": last["MESSAGE"] if last else "Initial workbook",
                        "merge_request_id": last["MERGE_REQUEST_ID"] if last else None,
                    })
                    if len(output) >= limit:
                        return {"branch_id": branch_id, "head_commit_id": context["head_commit_id"], "cells": output, "truncated": True}
        return {"branch_id": branch_id, "head_commit_id": context["head_commit_id"], "cells": output, "truncated": False}
    finally:
        conn.close()


def cell_traceability(branch_id: str, sheet_id: str, row_id: str, column_id: str) -> dict[str, Any]:
    blame = workbook_blame(branch_id, sheet_id, limit=100000)
    cell = next(
        (item for item in blame["cells"] if item["row_id"] == row_id and item["column_id"] == column_id),
        None,
    )
    if not cell:
        raise ValueError("Cell does not exist in the current workbook state")
    cell["history"] = row_history(branch_id, sheet_id, row_id)
    return cell
