"""Presence (who's actively viewing/editing right now), notifications, and
repository activity-overview aggregation."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .schema import _get_connection, _utcnow


def get_notification_preferences(user_id: str) -> dict[str, bool]:
    """Effective preference for every known notification type — a missing
    row means enabled (opt-out, not opt-in), so this is additive and never
    silently changes behavior for a user who's never touched Settings."""
    conn = _get_connection()
    try:
        rows = {row["TYPE"]: bool(row["ENABLED"]) for row in conn.execute(
            "SELECT TYPE, ENABLED FROM NOTIFICATION_PREFERENCES WHERE USER_ID=?", (user_id,)
        )}
    finally:
        conn.close()
    return {
        notification_type: rows.get(notification_type, True)
        for notification_type in (
            "MERGE_REQUEST_CONFLICTED", "MERGE_RISK_FLAGGED", "DEVICE_BLOCKED", "EUC_RISK_DRIFT",
            "NEED_HELP_REQUESTED", "EUC_ATTESTATION_OVERDUE",
            "REVIEW_QUEUE_BACKLOG", "ONBOARDING_STALLED", "BEHAVIORAL_ANOMALY",
        )
    }


def set_notification_preference(user_id: str, notification_type: str, enabled: bool) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            """INSERT INTO NOTIFICATION_PREFERENCES (USER_ID, TYPE, ENABLED, UPDATED_AT) VALUES (?,?,?,?)
               ON CONFLICT(USER_ID, TYPE) DO UPDATE SET ENABLED=excluded.ENABLED, UPDATED_AT=excluded.UPDATED_AT""",
            (user_id, notification_type, 1 if enabled else 0, _utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


def _notification_enabled(user_id: str, notification_type: str) -> bool:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT ENABLED FROM NOTIFICATION_PREFERENCES WHERE USER_ID=? AND TYPE=?", (user_id, notification_type)
        ).fetchone()
        return bool(row[0]) if row else True
    finally:
        conn.close()


def create_notification(
    user_id: str, notification_type: str, title: str, body: str | None = None,
    resource_type: str | None = None, resource_id: str | None = None, organization_id: str | None = None,
) -> dict[str, Any] | None:
    """The single reusable insert path for every notification the product
    raises — merge conflicts, AI risk flags, device blocks, EUC risk drift.
    Callers should wrap this in try/except: a notification failing to
    write must never break the action that triggered it. Returns None
    (not an error) when the user has opted out of this notification type."""
    if not _notification_enabled(user_id, notification_type):
        return None
    conn = _get_connection()
    try:
        notification_id = f"NTF_{uuid.uuid4().hex[:20].upper()}"
        now = _utcnow()
        conn.execute(
            """INSERT INTO NOTIFICATIONS
               (NOTIFICATION_ID,USER_ID,ORGANIZATION_ID,TYPE,TITLE,BODY,RESOURCE_TYPE,RESOURCE_ID,CREATED_AT)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (notification_id, user_id, organization_id, notification_type, title, body, resource_type, resource_id, now),
        )
        conn.commit()
        return {"notification_id": notification_id, "user_id": user_id, "type": notification_type, "title": title,
                "body": body, "resource_type": resource_type, "resource_id": resource_id, "read_at": None, "created_at": now}
    finally:
        conn.close()


def list_notifications(user_id: str, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    conn = _get_connection()
    try:
        clause = "USER_ID=?" + (" AND READ_AT IS NULL" if unread_only else "")
        rows = conn.execute(
            f"SELECT * FROM NOTIFICATIONS WHERE {clause} ORDER BY CREATED_AT DESC LIMIT ?",
            (user_id, max(1, min(limit, 200))),
        ).fetchall()
        return [{key.lower(): row[key] for key in row.keys()} for row in rows]
    finally:
        conn.close()


def mark_notification_read(notification_id: str, user_id: str) -> None:
    conn = _get_connection()
    try:
        updated = conn.execute(
            "UPDATE NOTIFICATIONS SET READ_AT=? WHERE NOTIFICATION_ID=? AND USER_ID=? AND READ_AT IS NULL",
            (_utcnow(), notification_id, user_id),
        ).rowcount
        conn.commit()
        if not updated:
            existing = conn.execute(
                "SELECT 1 FROM NOTIFICATIONS WHERE NOTIFICATION_ID=? AND USER_ID=?", (notification_id, user_id)
            ).fetchone()
            if not existing:
                raise KeyError("Notification does not exist")
    finally:
        conn.close()


def mark_all_notifications_read(user_id: str) -> int:
    conn = _get_connection()
    try:
        updated = conn.execute(
            "UPDATE NOTIFICATIONS SET READ_AT=? WHERE USER_ID=? AND READ_AT IS NULL", (_utcnow(), user_id)
        ).rowcount
        conn.commit()
        return updated
    finally:
        conn.close()


_PRESENCE_TTL_SECONDS = 45
_WORKING_COPY_ACTIVE_TTL_SECONDS = 300
# Excel's own taskpane heartbeat fires every 60s (see TaskpaneUI.jsx); this
# TTL needs enough margin over that interval that "LIVE" doesn't flicker
# off between two consecutive heartbeats.
_EXCEL_PRESENCE_TTL_SECONDS = 150


def repository_activity_overview(repository_id: str) -> list[dict[str, Any]]:
    """One row per user with membership on this repository: RBAC role, a
    Teams-style live status (ONLINE / NEED_HELP / OFFLINE) driven purely by
    whether the Excel taskpane's heartbeat is fresh — a browser tab being
    open is never "live" here — plus real accumulated active time for
    today (REPOSITORY_DAILY_ACTIVITY, incremented heartbeat-to-heartbeat so
    gaps longer than the heartbeat TTL are never counted as active) and
    their most recently seen device."""
    conn = _get_connection()
    try:
        repository = conn.execute(
            "SELECT TABLE_ID, CREATED_BY FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (repository_id,)
        ).fetchone()
        if not repository:
            raise ValueError("Repository does not exist")
        # The taskpane heartbeats against whichever data table is actually
        # open — the repository's own TABLE_ID when on main, or a branch's
        # own DATA_TABLE_ID when on a branch. Presence must be checked
        # across every one of those, not just the repository's main table,
        # or a user working on a branch always reads as OFFLINE here.
        branch_table_ids = [
            row["DATA_TABLE_ID"] for row in conn.execute(
                "SELECT DATA_TABLE_ID FROM BRANCHES WHERE REPOSITORY_ID=?", (repository_id,)
            )
        ]
        table_ids = list({repository["TABLE_ID"], *branch_table_ids})
        table_id_placeholders = ",".join("?" for _ in table_ids)
        now = _utcnow()
        members = conn.execute(
            """
            SELECT USER_ID, ROLE FROM REPOSITORY_MEMBERS WHERE REPOSITORY_ID=?
            UNION
            SELECT CREATED_BY, 'owner' FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?
            """,
            (repository_id, repository_id),
        ).fetchall()
        overview = []
        for member in members:
            user_id = member["USER_ID"]
            user = conn.execute("SELECT EMAIL, DISPLAY_NAME, LAST_LOGIN_AT FROM APP_USERS WHERE USER_ID=?", (user_id,)).fetchone()
            if not user:
                continue
            presence = conn.execute(
                f"""SELECT MAX(CASE WHEN SURFACE='excel' THEN LAST_SEEN END) AS EXCEL_LAST_SEEN,
                          MAX(LAST_SEEN) AS LAST_SEEN
                   FROM DATASET_PRESENCE WHERE TABLE_ID IN ({table_id_placeholders}) AND USER_ID=? AND SURFACE='excel'""",
                (*table_ids, user_id),
            ).fetchone()
            excel_status_row = conn.execute(
                f"""SELECT STATUS FROM DATASET_PRESENCE WHERE TABLE_ID IN ({table_id_placeholders}) AND USER_ID=? AND SURFACE='excel'
                   ORDER BY LAST_SEEN DESC LIMIT 1""",
                (*table_ids, user_id),
            ).fetchone()
            working_copy = conn.execute(
                "SELECT MAX(LAST_SEEN_AT) AS LAST_SEEN FROM WORKING_COPIES WHERE REPOSITORY_ID=? AND USER_ID=? AND STATUS='ACTIVE'",
                (repository_id, user_id),
            ).fetchone()
            last_seen_candidates = [value for value in (presence["LAST_SEEN"], working_copy["LAST_SEEN"]) if value]
            last_seen_at = max(last_seen_candidates) if last_seen_candidates else None
            # "LIVE" means the Excel taskpane's own heartbeat is fresh right
            # now — closing Excel simply lets the heartbeat go stale and the
            # TTL below flips status back to OFFLINE automatically.
            is_excel_live = bool(
                presence["EXCEL_LAST_SEEN"] and _seconds_since(presence["EXCEL_LAST_SEEN"], now) <= _EXCEL_PRESENCE_TTL_SECONDS
            )
            is_active_now = is_excel_live
            if not is_excel_live:
                status = "OFFLINE"
            elif excel_status_row and excel_status_row["STATUS"] == "NEED_HELP":
                status = "NEED_HELP"
            else:
                status = "ONLINE"
            today_activity = conn.execute(
                "SELECT LAST_SEEN_AT, ACTIVE_SECONDS FROM REPOSITORY_DAILY_ACTIVITY WHERE USER_ID=? AND REPOSITORY_ID=? AND ACTIVITY_DATE=?",
                (user_id, repository_id, now[:10]),
            ).fetchone()
            active_seconds_today = today_activity["ACTIVE_SECONDS"] if today_activity else 0
            # Top up with the still-running session so the number visibly
            # ticks up between heartbeats instead of only updating every 60s.
            if is_excel_live and today_activity:
                active_seconds_today += min(_seconds_since(today_activity["LAST_SEEN_AT"], now), _EXCEL_PRESENCE_TTL_SECONDS)
            device = conn.execute(
                "SELECT FINGERPRINT_ID, IP_ADDRESS, MACHINE_ID, USER_AGENT, TRUST_STATUS, LAST_SEEN_AT FROM DEVICE_FINGERPRINTS WHERE USER_ID=? ORDER BY LAST_SEEN_AT DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            overview.append({
                "user_id": user_id, "email": user["EMAIL"], "display_name": user["DISPLAY_NAME"],
                "role": member["ROLE"], "is_active_now": is_active_now, "status": status,
                "last_seen_at": last_seen_at, "last_login_at": user["LAST_LOGIN_AT"],
                "hours_active_today": round(active_seconds_today / 3600, 2),
                "active_seconds_today": round(active_seconds_today),
                "device": {key.lower(): device[key] for key in device.keys()} if device else None,
            })
        overview.sort(key=lambda item: (item["status"] != "NEED_HELP", item["status"] == "OFFLINE", item["email"] or ""))
        return overview
    finally:
        conn.close()


def repository_activity_matrix(repository_id: str, days: int = 14) -> dict[str, Any]:
    """Per-user, per-day active-seconds matrix for the last ``days`` days —
    powers the Team Activity matrix view. Real accumulated time from
    REPOSITORY_DAILY_ACTIVITY, not an estimate.

    Each day cell also carries a ``focus`` ratio (0-1): accumulated active
    seconds divided by the wall-clock span from first to last heartbeat that
    day. A ratio near 1.0 is one continuous stretch of engagement; a low
    ratio means the same amount of active time was spread thin across a
    much longer span — someone popping in and out all day rather than
    settling in. This distinguishes volume from focus using only data
    already recorded (FIRST_SEEN_AT/LAST_SEEN_AT), no new schema."""
    conn = _get_connection()
    try:
        repository = conn.execute(
            "SELECT REPOSITORY_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (repository_id,)
        ).fetchone()
        if not repository:
            raise ValueError("Repository does not exist")
        days = max(1, min(days, 60))
        today = datetime.now(timezone.utc)
        dates = [(today - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(days - 1, -1, -1)]
        since_date = dates[0]
        members = conn.execute(
            """
            SELECT USER_ID, ROLE FROM REPOSITORY_MEMBERS WHERE REPOSITORY_ID=?
            UNION
            SELECT CREATED_BY, 'owner' FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?
            """,
            (repository_id, repository_id),
        ).fetchall()
        rows = conn.execute(
            """SELECT USER_ID, ACTIVITY_DATE, ACTIVE_SECONDS, FIRST_SEEN_AT, LAST_SEEN_AT
               FROM REPOSITORY_DAILY_ACTIVITY WHERE REPOSITORY_ID=? AND ACTIVITY_DATE>=?""",
            (repository_id, since_date),
        ).fetchall()
        by_user: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            active_seconds = row["ACTIVE_SECONDS"]
            span_seconds = 0.0
            first_seen, last_seen = row["FIRST_SEEN_AT"], row["LAST_SEEN_AT"]
            if first_seen and last_seen:
                try:
                    first_dt = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
                    last_dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                    span_seconds = max(0.0, (last_dt - first_dt).total_seconds())
                except ValueError:
                    span_seconds = 0.0
            focus = 1.0 if active_seconds > 0 and span_seconds <= 0 else (
                round(min(1.0, active_seconds / span_seconds), 2) if span_seconds > 0 else 0.0
            )
            by_user.setdefault(row["USER_ID"], {})[row["ACTIVITY_DATE"]] = {
                "seconds": active_seconds, "focus": focus,
            }
        users = []
        for member in members:
            user = conn.execute("SELECT EMAIL, DISPLAY_NAME FROM APP_USERS WHERE USER_ID=?", (member["USER_ID"],)).fetchone()
            if not user:
                continue
            per_day = by_user.get(member["USER_ID"], {})
            cells = [per_day.get(date, {"seconds": 0, "focus": 0.0}) for date in dates]
            total_seconds = sum(cell["seconds"] for cell in cells)
            active_days = [cell for cell in cells if cell["seconds"] > 0]
            average_focus = round(sum(cell["focus"] for cell in active_days) / len(active_days), 2) if active_days else None
            users.append({
                "user_id": member["USER_ID"], "email": user["EMAIL"], "display_name": user["DISPLAY_NAME"],
                "role": member["ROLE"], "cells": cells, "total_seconds": total_seconds,
                "average_focus": average_focus,
            })
        users.sort(key=lambda item: item["total_seconds"], reverse=True)
        return {"dates": dates, "users": users}
    finally:
        conn.close()


def _seconds_since(earlier_iso: str, later_iso: str) -> float:
    earlier = datetime.fromisoformat(earlier_iso)
    later = datetime.fromisoformat(later_iso)
    return max(0.0, (later - earlier).total_seconds())


def _touch_daily_activity(conn, user_id: str, now: str | None = None) -> None:
    """Upsert today's first/last-seen for a user; ``hours_active_today`` is
    approximated downstream as LAST_SEEN_AT - FIRST_SEEN_AT for today's row.
    Caller owns the transaction (no commit here) so this composes cheaply
    with presence/working-copy heartbeats that already write in the same
    connection/transaction."""
    now = now or _utcnow()
    today = now[:10]
    conn.execute(
        """
        INSERT INTO USER_DAILY_ACTIVITY (USER_ID, ACTIVITY_DATE, FIRST_SEEN_AT, LAST_SEEN_AT, HEARTBEAT_COUNT)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(USER_ID, ACTIVITY_DATE) DO UPDATE SET
            LAST_SEEN_AT=excluded.LAST_SEEN_AT,
            HEARTBEAT_COUNT=HEARTBEAT_COUNT+1
        """,
        (user_id, today, now, now),
    )


def _accumulate_repository_activity(conn, user_id: str, repository_id: str, now: str) -> None:
    """Adds the gap since this user's last heartbeat in this repository to
    today's running total — but only when that gap is no longer than the
    Excel presence TTL. A longer gap means Excel was actually closed (or
    the machine slept) in between, so that idle stretch is correctly never
    counted as active time; the next heartbeat just starts accumulating
    from zero again. This is how "start (active) - close, start (active) -
    close" sessions add up to a real total instead of a first-to-last
    estimate that would count idle time as active."""
    today = now[:10]
    row = conn.execute(
        "SELECT LAST_SEEN_AT FROM REPOSITORY_DAILY_ACTIVITY WHERE USER_ID=? AND REPOSITORY_ID=? AND ACTIVITY_DATE=?",
        (user_id, repository_id, today),
    ).fetchone()
    increment = 0.0
    if row and row["LAST_SEEN_AT"]:
        gap = _seconds_since(row["LAST_SEEN_AT"], now)
        if gap <= _EXCEL_PRESENCE_TTL_SECONDS:
            increment = gap
    conn.execute(
        """
        INSERT INTO REPOSITORY_DAILY_ACTIVITY (USER_ID, REPOSITORY_ID, ACTIVITY_DATE, FIRST_SEEN_AT, LAST_SEEN_AT, ACTIVE_SECONDS, HEARTBEAT_COUNT)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(USER_ID, REPOSITORY_ID, ACTIVITY_DATE) DO UPDATE SET
            LAST_SEEN_AT=excluded.LAST_SEEN_AT,
            ACTIVE_SECONDS=ACTIVE_SECONDS+?,
            HEARTBEAT_COUNT=HEARTBEAT_COUNT+1
        """,
        (user_id, repository_id, today, now, now, round(increment), round(increment)),
    )


def touch_dataset_presence(
    table_id: str, user_id: str, client_id: str, surface: str, activity: str, status: str = "ONLINE"
) -> None:
    """Records a presence heartbeat. ``status`` is the Excel taskpane's own
    Teams-style status — "ONLINE" (default) or "NEED_HELP" — and only means
    anything for ``surface="excel"``; "OFFLINE" is never stored here, it is
    purely the absence of a fresh heartbeat (see repository_activity_overview).
    A transition into NEED_HELP notifies the repository owner once, not on
    every subsequent heartbeat while the status remains NEED_HELP."""
    conn = _get_connection()
    notify_owner = None
    try:
        now = _utcnow()
        previous = conn.execute(
            "SELECT STATUS FROM DATASET_PRESENCE WHERE TABLE_ID=? AND USER_ID=? AND CLIENT_ID=?",
            (table_id, user_id, client_id),
        ).fetchone()
        previous_status = previous["STATUS"] if previous else None
        conn.execute(
            """
            INSERT INTO DATASET_PRESENCE
                (TABLE_ID, USER_ID, CLIENT_ID, SURFACE, ACTIVITY, LAST_SEEN, STATUS)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(TABLE_ID, USER_ID, CLIENT_ID) DO UPDATE SET
                SURFACE=excluded.SURFACE,
                ACTIVITY=excluded.ACTIVITY,
                LAST_SEEN=excluded.LAST_SEEN,
                STATUS=excluded.STATUS
            """,
            (table_id, user_id, client_id, surface, activity, now, status),
        )
        _touch_daily_activity(conn, user_id, now)
        if surface == "excel":
            # table_id is whatever data table the taskpane actually opened —
            # the repository's own TABLE_ID on main, or a branch's own
            # DATA_TABLE_ID on a branch. Resolve through either so activity
            # accumulation and Need Help notifications still fire for users
            # working on a branch, not just on main.
            repo_row = conn.execute(
                "SELECT REPOSITORY_ID, CREATED_BY, REPOSITORY_NAME FROM WORKBOOK_REPOSITORIES WHERE TABLE_ID=?",
                (table_id,),
            ).fetchone()
            if not repo_row:
                branch_row = conn.execute(
                    """SELECT W.REPOSITORY_ID, W.CREATED_BY, W.REPOSITORY_NAME
                       FROM BRANCHES B JOIN WORKBOOK_REPOSITORIES W ON W.REPOSITORY_ID = B.REPOSITORY_ID
                       WHERE B.DATA_TABLE_ID=?""",
                    (table_id,),
                ).fetchone()
                repo_row = branch_row
            if repo_row:
                _accumulate_repository_activity(conn, user_id, repo_row["REPOSITORY_ID"], now)
                if status == "NEED_HELP" and previous_status != "NEED_HELP" and repo_row["CREATED_BY"] != user_id:
                    requester = conn.execute("SELECT EMAIL FROM APP_USERS WHERE USER_ID=?", (user_id,)).fetchone()
                    notify_owner = {
                        "owner_id": repo_row["CREATED_BY"], "repository_id": repo_row["REPOSITORY_ID"],
                        "repository_name": repo_row["REPOSITORY_NAME"],
                        "requester_email": requester["EMAIL"] if requester else user_id,
                    }
        conn.commit()
    finally:
        conn.close()
    if notify_owner:
        try:
            create_notification(
                notify_owner["owner_id"], "NEED_HELP_REQUESTED",
                f"{notify_owner['requester_email']} needs help in {notify_owner['repository_name']}",
                "They set their status to Need Help while working in Excel.",
                "REPOSITORY", notify_owner["repository_id"],
            )
        except Exception:
            pass


def remove_dataset_presence(table_id: str, user_id: str, client_id: str) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "DELETE FROM DATASET_PRESENCE WHERE TABLE_ID=? AND USER_ID=? AND CLIENT_ID=?",
            (table_id, user_id, client_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_active_presence(table_id: str, ttl_seconds: int = 45) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc).timestamp() - ttl_seconds)
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM DATASET_PRESENCE WHERE LAST_SEEN<?", (cutoff_iso,))
        rows = conn.execute(
            """
            SELECT P.USER_ID, U.EMAIL, U.DISPLAY_NAME,
                   MAX(P.LAST_SEEN) AS LAST_SEEN,
                   GROUP_CONCAT(DISTINCT P.SURFACE) AS SURFACES,
                   GROUP_CONCAT(DISTINCT P.ACTIVITY) AS ACTIVITIES,
                   COUNT(*) AS CLIENT_COUNT
            FROM DATASET_PRESENCE P
            JOIN APP_USERS U ON U.USER_ID=P.USER_ID
            WHERE P.TABLE_ID=? AND P.LAST_SEEN>=?
            GROUP BY P.USER_ID, U.EMAIL, U.DISPLAY_NAME
            ORDER BY LAST_SEEN DESC
            """,
            (table_id, cutoff_iso),
        ).fetchall()
        conn.commit()
        return [
            {
                "user_id": row["USER_ID"],
                "email": row["EMAIL"],
                "display_name": row["DISPLAY_NAME"],
                "last_seen": row["LAST_SEEN"],
                "surfaces": (row["SURFACES"] or "").split(","),
                "activities": (row["ACTIVITIES"] or "").split(","),
                "client_count": row["CLIENT_COUNT"],
            }
            for row in rows
        ]
    finally:
        conn.close()


