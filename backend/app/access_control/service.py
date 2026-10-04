"""Administration operations for organizations, groups, roles, policies, and service identities."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from typing import Any

from .. import database
from ..observability import record_audit_event, record_security_event
from .bootstrap import bump_authorization_revision
from .engine import ResourceContext, authorization_engine


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _row(row) -> dict[str, Any]:
    return {key.lower(): row[key] for key in row.keys()}


def primary_organization(user_id: str) -> str | None:
    conn = database._get_connection()
    try:
        row = conn.execute(
            "SELECT ORGANIZATION_ID FROM ORGANIZATION_MEMBERS WHERE USER_ID=? AND STATUS='ACTIVE' ORDER BY JOINED_AT LIMIT 1",
            (user_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _require_admin(conn, organization_id: str, actor_id: str) -> None:
    authorization_engine.require(
        actor_id, "organization.admin",
        ResourceContext("ORGANIZATION", organization_id, organization_id=organization_id),
        conn=conn,
    )


def security_overview(organization_id: str, actor_id: str) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        _require_admin(conn, organization_id, actor_id)
        organization = conn.execute("SELECT * FROM ORGANIZATIONS WHERE ORGANIZATION_ID=?", (organization_id,)).fetchone()
        if not organization:
            raise KeyError("Organization does not exist")
        users = [_row(row) for row in conn.execute(
            """SELECT U.USER_ID,U.EMAIL,U.DISPLAY_NAME,U.EMPLOYEE_ID,U.STATUS,M.STATUS AS MEMBERSHIP_STATUS,M.JOINED_AT
               FROM ORGANIZATION_MEMBERS M JOIN APP_USERS U ON U.USER_ID=M.USER_ID
               WHERE M.ORGANIZATION_ID=? ORDER BY U.EMAIL""", (organization_id,)
        )]
        groups = [_row(row) for row in conn.execute(
            """SELECT G.*,COUNT(GM.USER_ID) AS MEMBER_COUNT FROM SECURITY_GROUPS G
               LEFT JOIN SECURITY_GROUP_MEMBERS GM ON GM.GROUP_ID=G.GROUP_ID
               WHERE G.ORGANIZATION_ID=? GROUP BY G.GROUP_ID ORDER BY G.NAME""", (organization_id,)
        )]
        roles = [_row(row) for row in conn.execute(
            """SELECT R.ROLE_ID,R.ROLE_KEY,R.DISPLAY_NAME,R.IS_SYSTEM,COUNT(RP.PERMISSION_ID) AS PERMISSION_COUNT
               FROM SECURITY_ROLES R LEFT JOIN SECURITY_ROLE_PERMISSIONS RP ON RP.ROLE_ID=R.ROLE_ID
               GROUP BY R.ROLE_ID ORDER BY R.ROLE_KEY"""
        )]
        # Repository-scoped assignments are excluded once their repository
        # is deleted (soft-deleted rows drop out of STATUS='ACTIVE') so a
        # removed repository's stale grants don't linger in the Security
        # Center view or its repository filter. Organization-scoped rows
        # (no WORKBOOK_REPOSITORIES match) are untouched by this filter.
        assignments = [_row(row) for row in conn.execute(
            """SELECT A.ASSIGNMENT_ID,A.USER_ID,U.EMAIL,R.ROLE_KEY,A.SCOPE_TYPE,A.SCOPE_ID,A.CREATED_AT,
                      W.REPOSITORY_NAME AS SCOPE_NAME
               FROM SECURITY_USER_ROLE_ASSIGNMENTS A JOIN SECURITY_ROLES R ON R.ROLE_ID=A.ROLE_ID
               LEFT JOIN APP_USERS U ON U.USER_ID=A.USER_ID
               LEFT JOIN WORKBOOK_REPOSITORIES W ON A.SCOPE_TYPE='REPOSITORY' AND A.SCOPE_ID=W.REPOSITORY_ID
               WHERE A.ORGANIZATION_ID=? AND (A.SCOPE_TYPE!='REPOSITORY' OR W.STATUS='ACTIVE')
               ORDER BY A.CREATED_AT DESC""",
            (organization_id,),
        )]
        policies = [_row(row) for row in conn.execute(
            "SELECT * FROM ACCESS_POLICIES WHERE ORGANIZATION_ID=? ORDER BY PRIORITY,NAME", (organization_id,)
        )]
        service_accounts = [_row(row) for row in conn.execute(
            """SELECT SERVICE_ACCOUNT_ID,NAME,ROLE_ID,SCOPE_TYPE,SCOPE_ID,STATUS,CREATED_AT,LAST_USED_AT,EXPIRES_AT
               FROM SERVICE_ACCOUNTS WHERE ORGANIZATION_ID=? ORDER BY NAME""", (organization_id,)
        )]
        protections = [_row(row) for row in conn.execute(
            "SELECT * FROM BRANCH_PROTECTION_RULES WHERE ORGANIZATION_ID=?", (organization_id,)
        )]
        sessions = [_row(row) for row in conn.execute(
            """SELECT S.SESSION_ID,S.USER_ID,U.EMAIL,S.CREATED_AT,S.EXPIRES_AT,S.LAST_SEEN_AT,S.REVOKED_AT
               FROM AUTH_SESSIONS S JOIN APP_USERS U ON U.USER_ID=S.USER_ID
               JOIN ORGANIZATION_MEMBERS M ON M.USER_ID=S.USER_ID AND M.ORGANIZATION_ID=?
               ORDER BY S.CREATED_AT DESC LIMIT 100""", (organization_id,)
        )]
        return {
            "organization": _row(organization), "users": users, "groups": groups,
            "roles": roles, "assignments": assignments, "policies": policies,
            "service_accounts": service_accounts, "branch_protections": protections,
            "sessions": sessions,
        }
    finally:
        conn.close()


def add_organization_member(organization_id: str, email: str, actor_id: str) -> dict[str, Any]:
    user = database.get_or_create_user(email)
    conn = database._get_connection()
    try:
        _require_admin(conn, organization_id, actor_id)
        now = database._utcnow()
        conn.execute(
            "INSERT INTO ORGANIZATION_MEMBERS (ORGANIZATION_ID,USER_ID,STATUS,JOINED_AT,UPDATED_AT) VALUES (?,?,'ACTIVE',?,?) ON CONFLICT(ORGANIZATION_ID,USER_ID) DO UPDATE SET STATUS='ACTIVE',UPDATED_AT=excluded.UPDATED_AT",
            (organization_id, user["user_id"], now, now),
        )
        bump_authorization_revision(conn, now); conn.commit()
    finally:
        conn.close()
    record_audit_event("ORGANIZATION_MEMBER_ADDED", actor_user_id=actor_id, payload={"organization_id": organization_id, "user_id": user["user_id"], "email": user["email"]})
    return {"user_id": user["user_id"], "email": user["email"], "status": "ACTIVE"}


def create_group(organization_id: str, name: str, description: str | None, actor_id: str) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        _require_admin(conn, organization_id, actor_id)
        now = database._utcnow(); group_id = _id("GRP")
        conn.execute(
            "INSERT INTO SECURITY_GROUPS VALUES (?,?,?,?,'ACTIVE',?,?,?)",
            (group_id, organization_id, name.strip(), description, actor_id, now, now),
        )
        bump_authorization_revision(conn, now); conn.commit()
        result = {"group_id": group_id, "organization_id": organization_id, "name": name.strip(), "description": description, "status": "ACTIVE"}
    finally:
        conn.close()
    record_audit_event("SECURITY_GROUP_CREATED", actor_user_id=actor_id, payload=result)
    return result


def add_group_member(organization_id: str, group_id: str, user_id: str, actor_id: str) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        _require_admin(conn, organization_id, actor_id)
        valid = conn.execute(
            "SELECT 1 FROM SECURITY_GROUPS G JOIN ORGANIZATION_MEMBERS M ON M.ORGANIZATION_ID=G.ORGANIZATION_ID WHERE G.GROUP_ID=? AND G.ORGANIZATION_ID=? AND M.USER_ID=? AND M.STATUS='ACTIVE'",
            (group_id, organization_id, user_id),
        ).fetchone()
        if not valid: raise ValueError("Group and active organization member are required")
        now = database._utcnow()
        conn.execute("INSERT OR IGNORE INTO SECURITY_GROUP_MEMBERS VALUES (?,?,?,?)", (group_id, user_id, actor_id, now))
        bump_authorization_revision(conn, now); conn.commit()
    finally:
        conn.close()
    record_audit_event("SECURITY_GROUP_MEMBER_ADDED", actor_user_id=actor_id, payload={"organization_id": organization_id, "group_id": group_id, "user_id": user_id})
    return {"group_id": group_id, "user_id": user_id, "status": "ACTIVE"}


def assign_role(organization_id: str, role_key: str, scope_type: str, scope_id: str, actor_id: str, *, user_id: str | None = None, group_id: str | None = None) -> dict[str, Any]:
    if bool(user_id) == bool(group_id):
        raise ValueError("Assign a role to exactly one user or group")
    conn = database._get_connection()
    try:
        _require_admin(conn, organization_id, actor_id)
        role = conn.execute("SELECT ROLE_ID FROM SECURITY_ROLES WHERE ROLE_KEY=?", (role_key.upper(),)).fetchone()
        if not role: raise ValueError("Unknown role")
        now = database._utcnow(); assignment_id = _id("URA" if user_id else "GRA")
        if user_id:
            member = conn.execute("SELECT 1 FROM ORGANIZATION_MEMBERS WHERE ORGANIZATION_ID=? AND USER_ID=? AND STATUS='ACTIVE'", (organization_id, user_id)).fetchone()
            if not member: raise ValueError("User is not an active organization member")
            conn.execute("INSERT INTO SECURITY_USER_ROLE_ASSIGNMENTS VALUES (?,?,?,?,?,?,?,?)", (assignment_id, organization_id, user_id, role[0], scope_type.upper(), scope_id, actor_id, now))
        else:
            group = conn.execute("SELECT 1 FROM SECURITY_GROUPS WHERE ORGANIZATION_ID=? AND GROUP_ID=? AND STATUS='ACTIVE'", (organization_id, group_id)).fetchone()
            if not group: raise ValueError("Group does not exist")
            conn.execute("INSERT INTO SECURITY_GROUP_ROLE_ASSIGNMENTS VALUES (?,?,?,?,?,?,?,?)", (assignment_id, organization_id, group_id, role[0], scope_type.upper(), scope_id, actor_id, now))
        bump_authorization_revision(conn, now); conn.commit()
    finally:
        conn.close()
    payload = {"assignment_id": assignment_id, "role_key": role_key.upper(), "scope_type": scope_type.upper(), "scope_id": scope_id, "user_id": user_id, "group_id": group_id}
    record_audit_event("SECURITY_ROLE_ASSIGNED", actor_user_id=actor_id, payload=payload)
    return payload


def revoke_assignment(organization_id: str, assignment_id: str, actor_id: str) -> None:
    conn = database._get_connection()
    try:
        _require_admin(conn, organization_id, actor_id)
        deleted = conn.execute("DELETE FROM SECURITY_USER_ROLE_ASSIGNMENTS WHERE ORGANIZATION_ID=? AND ASSIGNMENT_ID=?", (organization_id, assignment_id)).rowcount
        deleted += conn.execute("DELETE FROM SECURITY_GROUP_ROLE_ASSIGNMENTS WHERE ORGANIZATION_ID=? AND ASSIGNMENT_ID=?", (organization_id, assignment_id)).rowcount
        if not deleted: raise KeyError("Role assignment does not exist")
        now = database._utcnow(); bump_authorization_revision(conn, now); conn.commit()
    finally:
        conn.close()
    record_audit_event("SECURITY_ROLE_REVOKED", actor_user_id=actor_id, payload={"organization_id": organization_id, "assignment_id": assignment_id})


def create_policy(organization_id: str, payload: dict[str, Any], actor_id: str) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        _require_admin(conn, organization_id, actor_id)
        now = database._utcnow(); policy_id = _id("POL")
        effect = payload["effect"].upper()
        if effect not in {"ALLOW", "DENY"}: raise ValueError("Policy effect must be ALLOW or DENY")
        conn.execute(
            """INSERT INTO ACCESS_POLICIES
               (POLICY_ID,ORGANIZATION_ID,NAME,EFFECT,PERMISSION_KEY,SUBJECT_TYPE,SUBJECT_ID,RESOURCE_TYPE,RESOURCE_ID,CONDITIONS_JSON,PRIORITY,ENABLED,CREATED_BY,CREATED_AT,UPDATED_AT)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
            (policy_id, organization_id, payload["name"].strip(), effect, payload["permission_key"],
             payload["subject_type"].upper(), payload.get("subject_id"), payload["resource_type"].upper(),
             payload.get("resource_id"), json.dumps(payload.get("conditions") or {}, sort_keys=True),
             int(payload.get("priority", 100)), actor_id, now, now),
        )
        bump_authorization_revision(conn, now); conn.commit()
        result = {"policy_id": policy_id, **payload, "effect": effect, "organization_id": organization_id}
    finally:
        conn.close()
    record_audit_event("ACCESS_POLICY_CREATED", actor_user_id=actor_id, payload=result)
    return result


def set_user_status(organization_id: str, user_id: str, status: str, actor_id: str) -> dict[str, Any]:
    normalized = status.upper()
    if normalized not in {"ACTIVE", "SUSPENDED"}: raise ValueError("Status must be ACTIVE or SUSPENDED")
    conn = database._get_connection()
    try:
        _require_admin(conn, organization_id, actor_id)
        now = database._utcnow()
        if not conn.execute("SELECT 1 FROM ORGANIZATION_MEMBERS WHERE ORGANIZATION_ID=? AND USER_ID=?", (organization_id, user_id)).fetchone():
            raise KeyError("Organization member does not exist")
        conn.execute("UPDATE APP_USERS SET STATUS=?,AUTHORIZATION_REVISION=AUTHORIZATION_REVISION+1 WHERE USER_ID=?", (normalized, user_id))
        if normalized == "SUSPENDED":
            conn.execute("UPDATE AUTH_SESSIONS SET REVOKED_AT=? WHERE USER_ID=? AND REVOKED_AT IS NULL", (now, user_id))
        bump_authorization_revision(conn, now); conn.commit()
    finally:
        conn.close()
    record_security_event("USER_STATUS_CHANGED", severity="WARNING", user_id=user_id, details={"organization_id": organization_id, "status": normalized, "changed_by": actor_id})
    record_audit_event("USER_STATUS_CHANGED", actor_user_id=actor_id, payload={"organization_id": organization_id, "user_id": user_id, "status": normalized})
    return {"user_id": user_id, "status": normalized}


def list_organization_devices(organization_id: str, actor_id: str) -> list[dict[str, Any]]:
    conn = database._get_connection()
    try:
        _require_admin(conn, organization_id, actor_id)
    finally:
        conn.close()
    return database.list_organization_devices(organization_id)


def set_organization_device_trust(organization_id: str, actor_id: str, fingerprint_id: str, trust_status: str) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        _require_admin(conn, organization_id, actor_id)
    finally:
        conn.close()
    result = database.set_device_trust_status(fingerprint_id, trust_status)
    record_security_event("DEVICE_TRUST_CHANGED", severity="WARNING" if trust_status == "BLOCKED" else "INFO",
                          user_id=None, details={"organization_id": organization_id, "fingerprint_id": fingerprint_id,
                                                  "trust_status": trust_status, "changed_by": actor_id})
    record_audit_event("DEVICE_TRUST_CHANGED", actor_user_id=actor_id,
                       payload={"organization_id": organization_id, "fingerprint_id": fingerprint_id, "trust_status": trust_status})
    return result


def revoke_organization_session(organization_id: str, actor_id: str, session_id: str) -> None:
    conn = database._get_connection()
    try:
        _require_admin(conn, organization_id, actor_id)
    finally:
        conn.close()
    database.revoke_session(session_id, organization_id)
    record_security_event("SESSION_REVOKED", severity="WARNING", user_id=None,
                          details={"organization_id": organization_id, "session_id": session_id, "revoked_by": actor_id})
    record_audit_event("SESSION_REVOKED", actor_user_id=actor_id, payload={"organization_id": organization_id, "session_id": session_id})


def create_service_account(organization_id: str, name: str, role_key: str, scope_type: str, scope_id: str, actor_id: str) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        _require_admin(conn, organization_id, actor_id)
        role = conn.execute("SELECT ROLE_ID FROM SECURITY_ROLES WHERE ROLE_KEY=?", (role_key.upper(),)).fetchone()
        if not role: raise ValueError("Unknown role")
        service_id = _id("SVC"); secret = secrets.token_urlsafe(32)
        secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest(); now = database._utcnow()
        conn.execute(
            "INSERT INTO SERVICE_ACCOUNTS (SERVICE_ACCOUNT_ID,ORGANIZATION_ID,NAME,SECRET_HASH,ROLE_ID,SCOPE_TYPE,SCOPE_ID,STATUS,CREATED_BY,CREATED_AT) VALUES (?,?,?,?,?,?,?,'ACTIVE',?,?)",
            (service_id, organization_id, name.strip(), secret_hash, role[0], scope_type.upper(), scope_id, actor_id, now),
        )
        conn.commit()
    finally:
        conn.close()
    record_audit_event("SERVICE_ACCOUNT_CREATED", actor_user_id=actor_id, actor_type="USER", payload={"organization_id": organization_id, "service_account_id": service_id, "name": name})
    return {"service_account_id": service_id, "name": name.strip(), "credential": f"{service_id}.{secret}", "credential_notice": "This secret is shown once."}


def verify_service_credential(credential: str) -> dict[str, Any] | None:
    try: service_id, secret = credential.split(".", 1)
    except ValueError: return None
    conn = database._get_connection()
    try:
        row = conn.execute("SELECT * FROM SERVICE_ACCOUNTS WHERE SERVICE_ACCOUNT_ID=? AND STATUS='ACTIVE' AND (EXPIRES_AT IS NULL OR EXPIRES_AT>?)", (service_id, database._utcnow())).fetchone()
        if not row or not hmac.compare_digest(row["SECRET_HASH"], hashlib.sha256(secret.encode("utf-8")).hexdigest()):
            return None
        conn.execute("UPDATE SERVICE_ACCOUNTS SET LAST_USED_AT=? WHERE SERVICE_ACCOUNT_ID=?", (database._utcnow(), service_id)); conn.commit()
        return dict(row)
    finally:
        conn.close()
