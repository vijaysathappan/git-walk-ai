"""Stage 3.1 access-control schema, seed data, and legacy membership migration."""

from __future__ import annotations

import hashlib
import sqlite3


PERMISSIONS = (
    "organization.admin", "organization.audit", "repository.read", "repository.update",
    "repository.delete", "repository.manage_access", "branch.read", "branch.create",
    "branch.update", "branch.delete", "branch.sync", "commit.read", "commit.create",
    "commit.revert", "merge_request.read", "merge_request.create", "merge_request.resolve",
    "merge_request.review", "merge_request.approve", "merge_request.merge", "workflow.manage",
    "euc.read", "euc.ingest", "euc.analyze", "application_model.read",
    "application_model.build", "application_model.review", "application_model.approve",
    "application_model.generate", "audit.read", "service_account.manage", "session.manage",
    "integration.read", "integration.manage", "integration.execute", "integration.replay",
    "canonical.read", "canonical.manage", "canonical.map", "thread.read", "thread.manage",
    "synchronization.read", "synchronization.manage", "synchronization.execute",
    "catalogue.read", "incident.read", "incident.manage",
    "ai.read", "ai.use", "ai.recommend", "ai.agent.run", "ai.action.confirm",
    "ai.admin", "ai.audit",
    "macro.view", "macro.run",
)

ROLE_PERMISSIONS = {
    "ORG_ADMIN": PERMISSIONS,
    "REPOSITORY_OWNER": tuple(permission for permission in PERMISSIONS if permission != "organization.admin"),
    "MAKER": ("repository.read", "branch.read", "branch.create", "branch.update", "branch.sync",
              "commit.read", "commit.create", "merge_request.read", "merge_request.create",
              "euc.read", "euc.ingest", "euc.analyze", "application_model.read", "application_model.build",
              "integration.read", "integration.execute", "canonical.read", "thread.read", "catalogue.read",
              "ai.read", "ai.use", "ai.recommend", "ai.agent.run"),
    "CHECKER": ("repository.read", "branch.read", "commit.read", "merge_request.read",
                "merge_request.review", "audit.read", "euc.read", "application_model.read",
                "application_model.review", "integration.read", "canonical.read", "thread.read", "catalogue.read",
                "ai.read", "ai.use", "ai.recommend"),
    "APPROVER": ("repository.read", "branch.read", "commit.read", "merge_request.read",
                 "merge_request.review", "merge_request.approve", "audit.read",
                 "application_model.read", "application_model.approve", "integration.read", "canonical.read",
                 "thread.read", "catalogue.read", "incident.read", "ai.read", "ai.use", "ai.recommend"),
    "EDITOR": ("repository.read", "repository.update", "branch.read", "branch.create",
               "branch.update", "branch.delete", "branch.sync", "commit.read", "commit.create",
               "commit.revert", "merge_request.read", "merge_request.create",
               "merge_request.resolve", "euc.read", "euc.ingest", "euc.analyze",
               "application_model.read", "application_model.build", "integration.read", "integration.manage",
               "integration.execute", "canonical.read", "canonical.map", "thread.read", "catalogue.read",
               "synchronization.read", "synchronization.execute", "ai.read", "ai.use", "ai.recommend",
               "ai.agent.run", "ai.action.confirm", "macro.view", "macro.run"),
    "VIEWER": ("repository.read", "branch.read", "commit.read", "merge_request.read",
               "euc.read", "application_model.read", "integration.read", "canonical.read", "thread.read", "catalogue.read",
               "ai.read", "ai.use", "macro.view"),
    "AUDITOR": ("repository.read", "branch.read", "commit.read", "merge_request.read",
                "audit.read", "euc.read", "application_model.read", "integration.read", "canonical.read",
                "thread.read", "catalogue.read", "incident.read", "synchronization.read", "ai.read", "ai.audit"),
    "CONTROL_OWNER": ("repository.read", "branch.read", "commit.read", "merge_request.read",
                      "merge_request.review", "audit.read", "euc.read", "euc.analyze",
                      "application_model.read", "application_model.review", "integration.read", "canonical.read",
                      "canonical.manage", "thread.read", "catalogue.read", "incident.read", "incident.manage",
                      "synchronization.read", "synchronization.manage", "ai.read", "ai.use", "ai.recommend",
                      "ai.agent.run", "ai.action.confirm", "ai.audit"),
    "MIGRATION_ADMIN": ("repository.read", "branch.read", "commit.read", "euc.read", "euc.analyze",
                        "application_model.read", "application_model.build", "application_model.generate",
                        "integration.read", "integration.manage", "integration.execute", "canonical.read",
                        "canonical.manage", "canonical.map", "thread.read", "thread.manage", "catalogue.read",
                        "synchronization.read", "synchronization.manage", "synchronization.execute",
                        "ai.read", "ai.use", "ai.recommend", "ai.agent.run", "ai.action.confirm", "ai.audit"),
}


def _stable(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16].upper()}"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info('{table}')")}


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _seed_roles(conn: sqlite3.Connection, now: str) -> None:
    for permission in PERMISSIONS:
        permission_id = _stable("PRM", permission)
        conn.execute(
            "INSERT OR IGNORE INTO SECURITY_PERMISSIONS (PERMISSION_ID,PERMISSION_KEY,DESCRIPTION,CREATED_AT) VALUES (?,?,?,?)",
            (permission_id, permission, permission.replace("_", " ").replace(".", " / ").title(), now),
        )
    for role_key, permissions in ROLE_PERMISSIONS.items():
        role_id = f"ROL_{role_key}"
        conn.execute(
            "INSERT OR IGNORE INTO SECURITY_ROLES (ROLE_ID,ROLE_KEY,DISPLAY_NAME,IS_SYSTEM,CREATED_AT,UPDATED_AT) VALUES (?,?,?,1,?,?)",
            (role_id, role_key, role_key.replace("_", " ").title(), now, now),
        )
        for permission in permissions:
            conn.execute(
                "INSERT OR IGNORE INTO SECURITY_ROLE_PERMISSIONS (ROLE_ID,PERMISSION_ID,CREATED_AT) VALUES (?,?,?)",
                (role_id, _stable("PRM", permission), now),
            )


def ensure_repository_security(conn: sqlite3.Connection, repository_id: str, now: str) -> str:
    repository = conn.execute(
        "SELECT REPOSITORY_ID,CREATED_BY,ORGANIZATION_ID,DEFAULT_BRANCH_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?",
        (repository_id,),
    ).fetchone()
    if not repository:
        raise ValueError("Repository does not exist")
    organization_id = repository["ORGANIZATION_ID"] or _stable("ORG", repository["CREATED_BY"])
    owner = conn.execute("SELECT EMAIL,DISPLAY_NAME FROM APP_USERS WHERE USER_ID=?", (repository["CREATED_BY"],)).fetchone()
    organization_name = ((owner["DISPLAY_NAME"] if owner else None) or (owner["EMAIL"] if owner else None) or "Git Walk") + " Workspace"
    conn.execute(
        "INSERT OR IGNORE INTO ORGANIZATIONS (ORGANIZATION_ID,NAME,SLUG,STATUS,CREATED_BY,CREATED_AT,UPDATED_AT) VALUES (?,?,?,'ACTIVE',?,?,?)",
        (organization_id, organization_name, organization_id.lower(), repository["CREATED_BY"], now, now),
    )
    conn.execute("UPDATE WORKBOOK_REPOSITORIES SET ORGANIZATION_ID=? WHERE REPOSITORY_ID=?", (organization_id, repository_id))
    members = conn.execute(
        "SELECT USER_ID,ROLE FROM REPOSITORY_MEMBERS WHERE REPOSITORY_ID=? UNION SELECT CREATED_BY,'owner' FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?",
        (repository_id, repository_id),
    ).fetchall()
    role_map = {"owner": "REPOSITORY_OWNER", "editor": "EDITOR", "viewer": "VIEWER"}
    for member in members:
        conn.execute(
            "INSERT INTO ORGANIZATION_MEMBERS (ORGANIZATION_ID,USER_ID,STATUS,JOINED_AT,UPDATED_AT) VALUES (?,?,'ACTIVE',?,?) ON CONFLICT(ORGANIZATION_ID,USER_ID) DO UPDATE SET UPDATED_AT=excluded.UPDATED_AT",
            (organization_id, member["USER_ID"], now, now),
        )
        role_key = role_map.get(member["ROLE"], "VIEWER")
        conn.execute(
            "DELETE FROM SECURITY_USER_ROLE_ASSIGNMENTS WHERE ORGANIZATION_ID=? AND USER_ID=? AND SCOPE_TYPE='REPOSITORY' AND SCOPE_ID=? AND ROLE_ID IN ('ROL_REPOSITORY_OWNER','ROL_EDITOR','ROL_VIEWER')",
            (organization_id, member["USER_ID"], repository_id),
        )
        assignment_id = _stable("URA", f"{repository_id}:{member['USER_ID']}:{role_key}")
        conn.execute(
            "INSERT OR IGNORE INTO SECURITY_USER_ROLE_ASSIGNMENTS (ASSIGNMENT_ID,ORGANIZATION_ID,USER_ID,ROLE_ID,SCOPE_TYPE,SCOPE_ID,GRANTED_BY,CREATED_AT) VALUES (?,?,?,?, 'REPOSITORY',?,?,?)",
            (assignment_id, organization_id, member["USER_ID"], f"ROL_{role_key}", repository_id, repository["CREATED_BY"], now),
        )
    owner_assignment = _stable("URA", f"{organization_id}:{repository['CREATED_BY']}:ORG_ADMIN")
    conn.execute(
        "INSERT OR IGNORE INTO SECURITY_USER_ROLE_ASSIGNMENTS (ASSIGNMENT_ID,ORGANIZATION_ID,USER_ID,ROLE_ID,SCOPE_TYPE,SCOPE_ID,GRANTED_BY,CREATED_AT) VALUES (?,?,?,?, 'ORGANIZATION',?,?,?)",
        (owner_assignment, organization_id, repository["CREATED_BY"], "ROL_ORG_ADMIN", organization_id, repository["CREATED_BY"], now),
    )
    if repository["DEFAULT_BRANCH_ID"]:
        conn.execute(
            "INSERT OR IGNORE INTO BRANCH_PROTECTION_RULES (RULE_ID,ORGANIZATION_ID,REPOSITORY_ID,BRANCH_ID,ALLOW_DIRECT_COMMITS,REQUIRED_APPROVALS,REQUIRE_VALIDATION,CREATED_BY,CREATED_AT,UPDATED_AT) VALUES (?,?,?,?,0,1,1,?,?,?)",
            (_stable("BPR", repository["DEFAULT_BRANCH_ID"]), organization_id, repository_id,
             repository["DEFAULT_BRANCH_ID"], repository["CREATED_BY"], now, now),
        )
    return organization_id


def initialize_access_control(conn: sqlite3.Connection, now: str) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ORGANIZATIONS (
            ORGANIZATION_ID TEXT PRIMARY KEY, NAME TEXT NOT NULL, SLUG TEXT NOT NULL UNIQUE,
            STATUS TEXT NOT NULL DEFAULT 'ACTIVE', CREATED_BY TEXT NOT NULL, CREATED_AT TEXT NOT NULL, UPDATED_AT TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ORGANIZATION_MEMBERS (
            ORGANIZATION_ID TEXT NOT NULL, USER_ID TEXT NOT NULL, STATUS TEXT NOT NULL DEFAULT 'ACTIVE',
            JOINED_AT TEXT NOT NULL, UPDATED_AT TEXT NOT NULL, PRIMARY KEY (ORGANIZATION_ID,USER_ID)
        );
        CREATE TABLE IF NOT EXISTS SECURITY_GROUPS (
            GROUP_ID TEXT PRIMARY KEY, ORGANIZATION_ID TEXT NOT NULL, NAME TEXT NOT NULL, DESCRIPTION TEXT,
            STATUS TEXT NOT NULL DEFAULT 'ACTIVE', CREATED_BY TEXT NOT NULL, CREATED_AT TEXT NOT NULL, UPDATED_AT TEXT NOT NULL,
            UNIQUE(ORGANIZATION_ID,NAME)
        );
        CREATE TABLE IF NOT EXISTS SECURITY_GROUP_MEMBERS (
            GROUP_ID TEXT NOT NULL, USER_ID TEXT NOT NULL, ADDED_BY TEXT NOT NULL, CREATED_AT TEXT NOT NULL,
            PRIMARY KEY (GROUP_ID,USER_ID)
        );
        CREATE TABLE IF NOT EXISTS SECURITY_ROLES (
            ROLE_ID TEXT PRIMARY KEY, ROLE_KEY TEXT NOT NULL UNIQUE, DISPLAY_NAME TEXT NOT NULL,
            DESCRIPTION TEXT, IS_SYSTEM INTEGER NOT NULL DEFAULT 0, CREATED_AT TEXT NOT NULL, UPDATED_AT TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS SECURITY_PERMISSIONS (
            PERMISSION_ID TEXT PRIMARY KEY, PERMISSION_KEY TEXT NOT NULL UNIQUE, DESCRIPTION TEXT, CREATED_AT TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS SECURITY_ROLE_PERMISSIONS (
            ROLE_ID TEXT NOT NULL, PERMISSION_ID TEXT NOT NULL, CREATED_AT TEXT NOT NULL,
            PRIMARY KEY (ROLE_ID,PERMISSION_ID)
        );
        CREATE TABLE IF NOT EXISTS SECURITY_USER_ROLE_ASSIGNMENTS (
            ASSIGNMENT_ID TEXT PRIMARY KEY, ORGANIZATION_ID TEXT NOT NULL, USER_ID TEXT NOT NULL, ROLE_ID TEXT NOT NULL,
            SCOPE_TYPE TEXT NOT NULL, SCOPE_ID TEXT NOT NULL, GRANTED_BY TEXT NOT NULL, CREATED_AT TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS SECURITY_GROUP_ROLE_ASSIGNMENTS (
            ASSIGNMENT_ID TEXT PRIMARY KEY, ORGANIZATION_ID TEXT NOT NULL, GROUP_ID TEXT NOT NULL, ROLE_ID TEXT NOT NULL,
            SCOPE_TYPE TEXT NOT NULL, SCOPE_ID TEXT NOT NULL, GRANTED_BY TEXT NOT NULL, CREATED_AT TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ACCESS_POLICIES (
            POLICY_ID TEXT PRIMARY KEY, ORGANIZATION_ID TEXT NOT NULL, NAME TEXT NOT NULL, EFFECT TEXT NOT NULL,
            PERMISSION_KEY TEXT NOT NULL, SUBJECT_TYPE TEXT NOT NULL, SUBJECT_ID TEXT,
            RESOURCE_TYPE TEXT NOT NULL, RESOURCE_ID TEXT, CONDITIONS_JSON TEXT NOT NULL DEFAULT '{}',
            PRIORITY INTEGER NOT NULL DEFAULT 100, ENABLED INTEGER NOT NULL DEFAULT 1,
            CREATED_BY TEXT NOT NULL, CREATED_AT TEXT NOT NULL, UPDATED_AT TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS SERVICE_ACCOUNTS (
            SERVICE_ACCOUNT_ID TEXT PRIMARY KEY, ORGANIZATION_ID TEXT NOT NULL, NAME TEXT NOT NULL,
            SECRET_HASH TEXT NOT NULL, ROLE_ID TEXT NOT NULL, SCOPE_TYPE TEXT NOT NULL, SCOPE_ID TEXT NOT NULL,
            STATUS TEXT NOT NULL DEFAULT 'ACTIVE', CREATED_BY TEXT NOT NULL, CREATED_AT TEXT NOT NULL,
            LAST_USED_AT TEXT, EXPIRES_AT TEXT, UNIQUE(ORGANIZATION_ID,NAME)
        );
        CREATE TABLE IF NOT EXISTS IDENTITY_PROVIDERS (
            PROVIDER_ID TEXT PRIMARY KEY, ORGANIZATION_ID TEXT NOT NULL, PROVIDER_TYPE TEXT NOT NULL,
            NAME TEXT NOT NULL, CONFIG_JSON TEXT NOT NULL DEFAULT '{}', STATUS TEXT NOT NULL DEFAULT 'DISABLED',
            CREATED_BY TEXT NOT NULL, CREATED_AT TEXT NOT NULL, UPDATED_AT TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS BRANCH_PROTECTION_RULES (
            RULE_ID TEXT PRIMARY KEY, ORGANIZATION_ID TEXT NOT NULL, REPOSITORY_ID TEXT NOT NULL, BRANCH_ID TEXT NOT NULL,
            ALLOW_DIRECT_COMMITS INTEGER NOT NULL DEFAULT 0, REQUIRED_APPROVALS INTEGER NOT NULL DEFAULT 1,
            REQUIRE_VALIDATION INTEGER NOT NULL DEFAULT 1, CREATED_BY TEXT NOT NULL, CREATED_AT TEXT NOT NULL, UPDATED_AT TEXT NOT NULL,
            UNIQUE(REPOSITORY_ID,BRANCH_ID)
        );
        CREATE TABLE IF NOT EXISTS SECURITY_AUTHZ_STATE (
            SINGLETON_ID INTEGER PRIMARY KEY CHECK(SINGLETON_ID=1), REVISION INTEGER NOT NULL, UPDATED_AT TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS IDX_ORG_MEMBER_USER ON ORGANIZATION_MEMBERS(USER_ID,STATUS);
        CREATE INDEX IF NOT EXISTS IDX_USER_ROLE_LOOKUP ON SECURITY_USER_ROLE_ASSIGNMENTS(USER_ID,ORGANIZATION_ID,SCOPE_TYPE,SCOPE_ID);
        CREATE INDEX IF NOT EXISTS IDX_GROUP_ROLE_LOOKUP ON SECURITY_GROUP_ROLE_ASSIGNMENTS(GROUP_ID,ORGANIZATION_ID,SCOPE_TYPE,SCOPE_ID);
        CREATE INDEX IF NOT EXISTS IDX_POLICY_LOOKUP ON ACCESS_POLICIES(ORGANIZATION_ID,PERMISSION_KEY,ENABLED,PRIORITY);
        """
    )
    _add_column(conn, "APP_USERS", "STATUS", "TEXT NOT NULL DEFAULT 'ACTIVE'")
    _add_column(conn, "APP_USERS", "AUTHORIZATION_REVISION", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "AUTH_SESSIONS", "ORGANIZATION_ID", "TEXT")
    _add_column(conn, "AUTH_SESSIONS", "LAST_SEEN_AT", "TEXT")
    _add_column(conn, "WORKBOOK_REPOSITORIES", "ORGANIZATION_ID", "TEXT")
    _add_column(conn, "EUC_ASSETS", "ORGANIZATION_ID", "TEXT")
    conn.execute("INSERT OR IGNORE INTO SECURITY_AUTHZ_STATE VALUES (1,1,?)", (now,))
    _seed_roles(conn, now)
    repositories = conn.execute("SELECT REPOSITORY_ID FROM WORKBOOK_REPOSITORIES").fetchall()
    for repository in repositories:
        organization_id = ensure_repository_security(conn, repository["REPOSITORY_ID"], now)
        conn.execute(
            "UPDATE EUC_ASSETS SET ORGANIZATION_ID=? WHERE REPOSITORY_ID=? AND ORGANIZATION_ID IS NULL",
            (organization_id, repository["REPOSITORY_ID"]),
        )


def bump_authorization_revision(conn: sqlite3.Connection, now: str) -> None:
    conn.execute("UPDATE SECURITY_AUTHZ_STATE SET REVISION=REVISION+1,UPDATED_AT=? WHERE SINGLETON_ID=1", (now,))
