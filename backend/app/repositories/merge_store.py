"""Persistence helpers for Stage 3 merge requests and validation."""

import json
from datetime import datetime, timezone
from typing import Any

from ..database import _get_connection
from ..excel.identity import stable_id


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def branch_context(branch_id: str) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT B.*, R.TABLE_ID AS MAIN_TABLE_ID, R.DEFAULT_BRANCH_ID,
                   R.CREATED_BY AS REPOSITORY_OWNER_ID, R.REPOSITORY_NAME
            FROM BRANCHES B JOIN WORKBOOK_REPOSITORIES R
              ON R.REPOSITORY_ID=B.REPOSITORY_ID
            WHERE B.BRANCH_ID=?
            """,
            (branch_id,),
        ).fetchone()
        return {key.lower(): row[key] for key in row.keys()} if row else None
    finally:
        conn.close()


def workbook_filename(table_id: str | None) -> str | None:
    if not table_id:
        return None
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT ORIGINAL_FILENAME FROM DATASET_REGISTRY WHERE TABLE_ID=?", (table_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def repository_role(repository_id: str, user_id: str) -> str | None:
    conn = _get_connection()
    try:
        if user_id == "USR_SYSTEM":
            return "owner"
        member = conn.execute(
            """
            SELECT M.ROLE FROM REPOSITORY_MEMBERS M
            JOIN WORKBOOK_REPOSITORIES R ON R.REPOSITORY_ID=M.REPOSITORY_ID
            WHERE M.REPOSITORY_ID=? AND M.USER_ID=? AND R.STATUS='ACTIVE'
            """,
            (repository_id, user_id),
        ).fetchone()
        return member[0] if member else None
    finally:
        conn.close()


def _parents(conn, commit_id: str) -> list[str]:
    return [
        row[0] for row in conn.execute(
            "SELECT PARENT_COMMIT_ID FROM COMMIT_PARENTS WHERE COMMIT_ID=? ORDER BY PARENT_ORDER",
            (commit_id,),
        ).fetchall()
    ]


def ancestors(commit_id: str | None) -> dict[str, int]:
    if not commit_id:
        return {}
    conn = _get_connection()
    try:
        distances = {commit_id: 0}
        queue = [commit_id]
        while queue:
            current = queue.pop(0)
            for parent in _parents(conn, current):
                distance = distances[current] + 1
                if parent not in distances or distance < distances[parent]:
                    distances[parent] = distance
                    queue.append(parent)
        return distances
    finally:
        conn.close()


def merge_base(source_head: str, target_head: str) -> str:
    source = ancestors(source_head)
    target = ancestors(target_head)
    common = set(source) & set(target)
    if not common:
        raise ValueError("Branches do not share a commit ancestor")
    return min(common, key=lambda commit_id: source[commit_id] + target[commit_id])


def ahead_behind(source_branch_id: str, target_branch_id: str) -> dict[str, Any]:
    source = branch_context(source_branch_id)
    target = branch_context(target_branch_id)
    if not source or not target or source["repository_id"] != target["repository_id"]:
        raise ValueError("Branches must exist in the same repository")
    base = merge_base(source["head_commit_id"], target["head_commit_id"])
    source_ancestors = ancestors(source["head_commit_id"])
    target_ancestors = ancestors(target["head_commit_id"])
    return {
        "source_branch_id": source_branch_id,
        "source_branch_name": source["branch_name"],
        "target_branch_id": target_branch_id,
        "target_branch_name": target["branch_name"],
        "merge_base_commit_id": base,
        "ahead": source_ancestors.get(base, 0),
        "behind": target_ancestors.get(base, 0),
        "source_head_commit_id": source["head_commit_id"],
        "target_head_commit_id": target["head_commit_id"],
        "source_status": source["status"],
        "target_status": target["status"],
    }


def save_validation_run(
    *, repository_id: str, branch_id: str, validation: dict[str, Any],
    merge_request_id: str | None = None, commit_id: str | None = None,
) -> dict[str, Any]:
    conn = _get_connection()
    now = utcnow()
    run_id = stable_id("VAL")
    try:
        conn.execute(
            "INSERT INTO VALIDATION_RUNS VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, merge_request_id, repository_id, branch_id, commit_id,
                validation["status"], validation["error_count"],
                validation["warning_count"], now, now,
            ),
        )
        for result in validation["results"]:
            conn.execute(
                "INSERT INTO VALIDATION_RESULTS VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stable_id("VR"), run_id, result["rule_code"], result["severity"],
                    result["status"], result.get("sheet_id"), result.get("row_id"),
                    result.get("column_id"), result["message"],
                    json.dumps(result.get("details") or {}, default=str), now,
                ),
            )
        if merge_request_id:
            conn.execute(
                "UPDATE MERGE_REQUESTS SET VALIDATION_STATUS=?, UPDATED_AT=? WHERE MERGE_REQUEST_ID=?",
                (validation["status"], now, merge_request_id),
            )
        conn.commit()
        return {"validation_run_id": run_id, **validation}
    finally:
        conn.close()


def create_merge_request_record(
    *, repository_id: str, source_branch_id: str, target_branch_id: str,
    source_head: str, target_head: str, base_commit: str, created_by: str,
    title: str, description: str | None, conflicts: list[dict[str, Any]],
) -> str:
    conn = _get_connection()
    now = utcnow()
    request_id = stable_id("MR")
    conflict_status = "CONFLICTED" if conflicts else "CLEAN"
    status = "CONFLICTED" if conflicts else "READY_FOR_REVIEW"
    try:
        conn.execute(
            """
            INSERT INTO MERGE_REQUESTS
                (MERGE_REQUEST_ID,REPOSITORY_ID,SOURCE_BRANCH_ID,TARGET_BRANCH_ID,
                 SOURCE_HEAD_COMMIT_ID,TARGET_HEAD_COMMIT_ID,MERGE_BASE_COMMIT_ID,
                 CREATED_BY,TITLE,DESCRIPTION,STATUS,CONFLICT_STATUS,
                 VALIDATION_STATUS,CREATED_AT,UPDATED_AT)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'PENDING',?,?)
            """,
            (
                request_id, repository_id, source_branch_id, target_branch_id,
                source_head, target_head, base_commit, created_by, title,
                description, status, conflict_status, now, now,
            ),
        )
        for conflict in conflicts:
            conn.execute(
                """
                INSERT INTO MERGE_CONFLICTS
                    (CONFLICT_ID,MERGE_REQUEST_ID,SHEET_ID,ROW_ID,COLUMN_ID,
                     CONFLICT_TYPE,BASE_STATE,MAIN_STATE,BRANCH_STATE,STATUS,CREATED_AT)
                VALUES (?,?,?,?,?,?,?,?,?,'OPEN',?)
                """,
                (
                    stable_id("CNF"), request_id, conflict.get("sheet_id"),
                    conflict.get("row_id"), conflict.get("column_id"),
                    conflict["conflict_type"], json.dumps(conflict.get("base_state"), default=str),
                    json.dumps(conflict.get("main_state"), default=str),
                    json.dumps(conflict.get("branch_state"), default=str), now,
                ),
            )
        conn.commit()
        return request_id
    finally:
        conn.close()


def _decoded(row) -> dict[str, Any]:
    result = {key.lower(): row[key] for key in row.keys()}
    for key in ("base_state", "main_state", "branch_state", "resolved_state", "details_json"):
        if key in result and result[key] is not None:
            result[key] = json.loads(result[key])
    return result


def get_merge_request(merge_request_id: str) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT M.*, S.BRANCH_NAME AS SOURCE_BRANCH_NAME,
                   T.BRANCH_NAME AS TARGET_BRANCH_NAME, U.EMAIL AS CREATED_BY_EMAIL
            FROM MERGE_REQUESTS M
            JOIN BRANCHES S ON S.BRANCH_ID=M.SOURCE_BRANCH_ID
            JOIN BRANCHES T ON T.BRANCH_ID=M.TARGET_BRANCH_ID
            LEFT JOIN APP_USERS U ON U.USER_ID=M.CREATED_BY
            WHERE M.MERGE_REQUEST_ID=?
            """,
            (merge_request_id,),
        ).fetchone()
        if not row:
            return None
        result = _decoded(row)
        result["conflicts"] = [
            _decoded(item) for item in conn.execute(
                "SELECT * FROM MERGE_CONFLICTS WHERE MERGE_REQUEST_ID=? ORDER BY CREATED_AT, CONFLICT_ID",
                (merge_request_id,),
            ).fetchall()
        ]
        result["reviews"] = [
            _decoded(item) for item in conn.execute(
                """
                SELECT V.*, U.EMAIL AS REVIEWER_EMAIL FROM MERGE_REQUEST_REVIEWS V
                LEFT JOIN APP_USERS U ON U.USER_ID=V.REVIEWER_USER_ID
                WHERE V.MERGE_REQUEST_ID=? ORDER BY V.CREATED_AT
                """,
                (merge_request_id,),
            ).fetchall()
        ]
        run = conn.execute(
            "SELECT * FROM VALIDATION_RUNS WHERE MERGE_REQUEST_ID=? ORDER BY STARTED_AT DESC LIMIT 1",
            (merge_request_id,),
        ).fetchone()
        result["validation"] = _decoded(run) if run else None
        if run:
            result["validation"]["results"] = [
                _decoded(item) for item in conn.execute(
                    "SELECT * FROM VALIDATION_RESULTS WHERE VALIDATION_RUN_ID=? ORDER BY SEVERITY, RULE_CODE",
                    (run["VALIDATION_RUN_ID"],),
                ).fetchall()
            ]
        return result
    finally:
        conn.close()


def list_merge_requests(repository_id: str) -> list[dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT M.*, S.BRANCH_NAME AS SOURCE_BRANCH_NAME,
                   T.BRANCH_NAME AS TARGET_BRANCH_NAME, U.EMAIL AS CREATED_BY_EMAIL,
                   (SELECT COUNT(*) FROM MERGE_CONFLICTS C WHERE C.MERGE_REQUEST_ID=M.MERGE_REQUEST_ID AND C.STATUS='OPEN') AS OPEN_CONFLICTS,
                   (SELECT COUNT(*) FROM MERGE_REQUEST_REVIEWS V WHERE V.MERGE_REQUEST_ID=M.MERGE_REQUEST_ID AND V.DECISION='APPROVED') AS APPROVALS
            FROM MERGE_REQUESTS M
            JOIN BRANCHES S ON S.BRANCH_ID=M.SOURCE_BRANCH_ID
            JOIN BRANCHES T ON T.BRANCH_ID=M.TARGET_BRANCH_ID
            LEFT JOIN APP_USERS U ON U.USER_ID=M.CREATED_BY
            WHERE M.REPOSITORY_ID=? ORDER BY M.CREATED_AT DESC
            """,
            (repository_id,),
        ).fetchall()
        return [_decoded(row) for row in rows]
    finally:
        conn.close()


def resolve_conflict_record(
    conflict_id: str, user_id: str, resolution_type: str, resolved_state: Any
) -> str:
    conn = _get_connection()
    now = utcnow()
    try:
        conflict = conn.execute(
            "SELECT * FROM MERGE_CONFLICTS WHERE CONFLICT_ID=?", (conflict_id,)
        ).fetchone()
        if not conflict:
            raise ValueError("Merge conflict does not exist")
        conn.execute(
            """
            UPDATE MERGE_CONFLICTS SET RESOLUTION_TYPE=?,RESOLVED_STATE=?,
                RESOLVED_BY=?,RESOLVED_AT=?,STATUS='RESOLVED' WHERE CONFLICT_ID=?
            """,
            (resolution_type, json.dumps(resolved_state, default=str), user_id, now, conflict_id),
        )
        remaining = conn.execute(
            "SELECT COUNT(*) FROM MERGE_CONFLICTS WHERE MERGE_REQUEST_ID=? AND STATUS='OPEN'",
            (conflict["MERGE_REQUEST_ID"],),
        ).fetchone()[0]
        conn.execute(
            "UPDATE MERGE_REQUESTS SET CONFLICT_STATUS=?,STATUS=?,UPDATED_AT=? WHERE MERGE_REQUEST_ID=?",
            (
                "RESOLVED" if not remaining else "CONFLICTED",
                "READY_FOR_REVIEW" if not remaining else "CONFLICTED",
                now, conflict["MERGE_REQUEST_ID"],
            ),
        )
        conn.commit()
        return conflict["MERGE_REQUEST_ID"]
    finally:
        conn.close()


def save_review(
    merge_request_id: str, reviewer_user_id: str, decision: str, comment: str | None
) -> None:
    conn = _get_connection()
    now = utcnow()
    try:
        conn.execute(
            """
            INSERT INTO MERGE_REQUEST_REVIEWS VALUES (?,?,?,?,?,?)
            ON CONFLICT(MERGE_REQUEST_ID,REVIEWER_USER_ID) DO UPDATE SET
                DECISION=excluded.DECISION,COMMENT_TEXT=excluded.COMMENT_TEXT,
                CREATED_AT=excluded.CREATED_AT
            """,
            (stable_id("RVW"), merge_request_id, reviewer_user_id, decision, comment, now),
        )
        status = "APPROVED" if decision == "APPROVED" else "REJECTED"
        conn.execute(
            "UPDATE MERGE_REQUESTS SET STATUS=?,UPDATED_AT=? WHERE MERGE_REQUEST_ID=?",
            (status, now, merge_request_id),
        )
        conn.commit()
    finally:
        conn.close()


def create_reviewer_request(
    merge_request_id: str, repository_id: str, requested_by_user_id: str,
    reviewer_user_id: str, reviewer_email: str, expires_at: str,
) -> dict[str, Any]:
    conn = _get_connection()
    now = utcnow()
    request_id = stable_id("RVQ")
    try:
        conn.execute(
            """
            INSERT INTO MERGE_REVIEWER_REQUESTS
                (REQUEST_ID,MERGE_REQUEST_ID,REPOSITORY_ID,REQUESTED_BY_USER_ID,
                 REVIEWER_USER_ID,REVIEWER_EMAIL,STATUS,CREATED_AT,EXPIRES_AT)
            VALUES (?,?,?,?,?,?,'PENDING',?,?)
            """,
            (request_id, merge_request_id, repository_id, requested_by_user_id,
             reviewer_user_id, reviewer_email, now, expires_at),
        )
        conn.commit()
    finally:
        conn.close()
    return get_reviewer_request(request_id)


def get_reviewer_request(request_id: str) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM MERGE_REVIEWER_REQUESTS WHERE REQUEST_ID=?", (request_id,)
        ).fetchone()
        return {key.lower(): row[key] for key in row.keys()} if row else None
    finally:
        conn.close()


def find_pending_reviewer_request(merge_request_id: str, reviewer_user_id: str) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        row = conn.execute(
            """SELECT * FROM MERGE_REVIEWER_REQUESTS
               WHERE MERGE_REQUEST_ID=? AND REVIEWER_USER_ID=? AND STATUS='PENDING'""",
            (merge_request_id, reviewer_user_id),
        ).fetchone()
        return {key.lower(): row[key] for key in row.keys()} if row else None
    finally:
        conn.close()


def list_reviewer_requests(merge_request_id: str) -> list[dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM MERGE_REVIEWER_REQUESTS WHERE MERGE_REQUEST_ID=? ORDER BY CREATED_AT DESC",
            (merge_request_id,),
        ).fetchall()
        return [{key.lower(): row[key] for key in row.keys()} for row in rows]
    finally:
        conn.close()


def update_reviewer_request_response(request_id: str, status: str, decision: str | None, comment: str | None) -> None:
    conn = _get_connection()
    now = utcnow()
    try:
        conn.execute(
            """UPDATE MERGE_REVIEWER_REQUESTS
               SET STATUS=?,DECISION=?,COMMENT_TEXT=?,RESPONDED_AT=?
               WHERE REQUEST_ID=?""",
            (status, decision, comment, now, request_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_merged(
    merge_request_id: str, source_branch_id: str, merge_commit_id: str, user_id: str
) -> None:
    conn = _get_connection()
    now = utcnow()
    try:
        conn.execute(
            "UPDATE MERGE_REQUESTS SET STATUS='MERGED',MERGED_AT=?,MERGED_BY=?,MERGE_COMMIT_ID=?,UPDATED_AT=? WHERE MERGE_REQUEST_ID=?",
            (now, user_id, merge_commit_id, now, merge_request_id),
        )
        conn.execute(
            "UPDATE BRANCHES SET STATUS='MERGED',MERGED_AT=?,UPDATED_AT=? WHERE BRANCH_ID=?",
            (now, now, source_branch_id),
        )
        conn.execute(
            "UPDATE WORKING_COPIES SET STATUS='REVOKED',LAST_SEEN_AT=? WHERE BRANCH_ID=? AND STATUS='ACTIVE'",
            (now, source_branch_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_branch_base(branch_id: str, base_commit_id: str) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE BRANCHES SET BASE_COMMIT_ID=?,UPDATED_AT=? WHERE BRANCH_ID=?",
            (base_commit_id, utcnow(), branch_id),
        )
        conn.commit()
    finally:
        conn.close()
