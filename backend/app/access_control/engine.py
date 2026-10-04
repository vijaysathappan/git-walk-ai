"""Default-deny RBAC and constrained ABAC authorization engine."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .. import database


@dataclass(frozen=True)
class ResourceContext:
    resource_type: str
    resource_id: str
    organization_id: str | None = None
    repository_id: str | None = None
    branch_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason_code: str
    reason: str
    matched_roles: tuple[str, ...] = ()
    matched_policy_id: str | None = None


def _lookup(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _conditions_match(conditions: dict[str, Any], facts: dict[str, Any]) -> bool:
    """Evaluate a small declarative operator set; arbitrary expressions are never run."""
    for path, requirement in conditions.items():
        actual = _lookup(facts, path)
        rule = requirement if isinstance(requirement, dict) else {"eq": requirement}
        for operator, expected in rule.items():
            if operator == "eq" and actual != expected:
                return False
            if operator == "ne" and actual == expected:
                return False
            if operator == "in" and actual not in expected:
                return False
            if operator == "not_in" and actual in expected:
                return False
            if operator == "contains" and (actual is None or expected not in actual):
                return False
            if operator == "gte" and (actual is None or actual < expected):
                return False
            if operator == "lte" and (actual is None or actual > expected):
                return False
            if operator not in {"eq", "ne", "in", "not_in", "contains", "gte", "lte"}:
                return False
    return True


class AuthorizationEngine:
    def _organization_id(self, conn, resource: ResourceContext) -> str | None:
        if resource.organization_id:
            return resource.organization_id
        repository_id = resource.repository_id
        if not repository_id and resource.resource_type == "REPOSITORY":
            repository_id = resource.resource_id
        if not repository_id and resource.branch_id:
            row = conn.execute("SELECT REPOSITORY_ID FROM BRANCHES WHERE BRANCH_ID=?", (resource.branch_id,)).fetchone()
            repository_id = row[0] if row else None
        if repository_id:
            row = conn.execute("SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (repository_id,)).fetchone()
            return row[0] if row else None
        return None

    def authorize(
        self,
        actor_id: str,
        permission: str,
        resource: ResourceContext,
        context: dict[str, Any] | None = None,
        *,
        conn=None,
    ) -> AuthorizationDecision:
        owns_connection = conn is None
        conn = conn or database._get_connection()
        try:
            if actor_id == "USR_SYSTEM":
                return AuthorizationDecision(True, "SYSTEM_ACTOR", "Trusted system actor")
            organization_id = self._organization_id(conn, resource)
            if not organization_id:
                return AuthorizationDecision(False, "RESOURCE_TENANT_MISSING", "Resource has no organization boundary")
            user = conn.execute("SELECT * FROM APP_USERS WHERE USER_ID=?", (actor_id,)).fetchone()
            service = None
            if not user:
                service = conn.execute(
                    "SELECT * FROM SERVICE_ACCOUNTS WHERE SERVICE_ACCOUNT_ID=? AND STATUS='ACTIVE'",
                    (actor_id,),
                ).fetchone()
            if not user and not service:
                return AuthorizationDecision(False, "ACTOR_UNKNOWN", "Actor is not active")
            if user and user["STATUS"] != "ACTIVE":
                return AuthorizationDecision(False, "ACTOR_SUSPENDED", "User account is suspended")
            if service and service["ORGANIZATION_ID"] != organization_id:
                return AuthorizationDecision(False, "CROSS_TENANT_DENIED", "Service account belongs to another organization")
            if user:
                member = conn.execute(
                    "SELECT STATUS FROM ORGANIZATION_MEMBERS WHERE ORGANIZATION_ID=? AND USER_ID=?",
                    (organization_id, actor_id),
                ).fetchone()
                if not member or member["STATUS"] != "ACTIVE":
                    return AuthorizationDecision(False, "CROSS_TENANT_DENIED", "Actor is not an active organization member")

            facts = {
                "user": {"id": actor_id, "email": user["EMAIL"] if user else None,
                         "employee_id": user["EMPLOYEE_ID"] if user else None},
                "resource": {"type": resource.resource_type, "id": resource.resource_id,
                             "organization_id": organization_id, "repository_id": resource.repository_id,
                             "branch_id": resource.branch_id, **resource.attributes},
                "context": context or {},
            }
            policies = conn.execute(
                """SELECT * FROM ACCESS_POLICIES WHERE ORGANIZATION_ID=? AND ENABLED=1
                   AND (PERMISSION_KEY=? OR PERMISSION_KEY='*')
                   AND (RESOURCE_TYPE=? OR RESOURCE_TYPE='*')
                   AND (RESOURCE_ID IS NULL OR RESOURCE_ID=?) ORDER BY PRIORITY ASC""",
                (organization_id, permission, resource.resource_type, resource.resource_id),
            ).fetchall()
            group_ids = {
                row[0] for row in conn.execute(
                    "SELECT GM.GROUP_ID FROM SECURITY_GROUP_MEMBERS GM JOIN SECURITY_GROUPS G ON G.GROUP_ID=GM.GROUP_ID WHERE GM.USER_ID=? AND G.ORGANIZATION_ID=? AND G.STATUS='ACTIVE'",
                    (actor_id, organization_id),
                )
            } if user else set()
            for policy in policies:
                subject_matches = (
                    policy["SUBJECT_TYPE"] == "ANY"
                    or (policy["SUBJECT_TYPE"] == "USER" and policy["SUBJECT_ID"] == actor_id)
                    or (policy["SUBJECT_TYPE"] == "GROUP" and policy["SUBJECT_ID"] in group_ids)
                    or (policy["SUBJECT_TYPE"] == "SERVICE_ACCOUNT" and policy["SUBJECT_ID"] == actor_id)
                )
                if subject_matches and _conditions_match(json.loads(policy["CONDITIONS_JSON"] or "{}"), facts):
                    if policy["EFFECT"] == "DENY":
                        return AuthorizationDecision(False, "EXPLICIT_POLICY_DENY", "An explicit access policy denied this action", matched_policy_id=policy["POLICY_ID"])

            role_rows = []
            if service:
                if self._scope_matches(service["SCOPE_TYPE"], service["SCOPE_ID"], organization_id, resource):
                    role_rows = [service]
            else:
                role_rows = list(conn.execute(
                    """SELECT A.ROLE_ID FROM SECURITY_USER_ROLE_ASSIGNMENTS A
                       WHERE A.ORGANIZATION_ID=? AND A.USER_ID=?""",
                    (organization_id, actor_id),
                ))
                if group_ids:
                    placeholders = ",".join("?" for _ in group_ids)
                    role_rows += list(conn.execute(
                        f"SELECT ROLE_ID,SCOPE_TYPE,SCOPE_ID FROM SECURITY_GROUP_ROLE_ASSIGNMENTS WHERE ORGANIZATION_ID=? AND GROUP_ID IN ({placeholders})",
                        (organization_id, *sorted(group_ids)),
                    ))
                direct_rows = conn.execute(
                    "SELECT ROLE_ID,SCOPE_TYPE,SCOPE_ID FROM SECURITY_USER_ROLE_ASSIGNMENTS WHERE ORGANIZATION_ID=? AND USER_ID=?",
                    (organization_id, actor_id),
                ).fetchall()
                role_rows = list(direct_rows) + [row for row in role_rows if "SCOPE_TYPE" in row.keys()]

            matched_roles: list[str] = []
            for assignment in role_rows:
                if not self._scope_matches(assignment["SCOPE_TYPE"], assignment["SCOPE_ID"], organization_id, resource):
                    continue
                allowed = conn.execute(
                    """SELECT R.ROLE_KEY FROM SECURITY_ROLES R
                       JOIN SECURITY_ROLE_PERMISSIONS RP ON RP.ROLE_ID=R.ROLE_ID
                       JOIN SECURITY_PERMISSIONS P ON P.PERMISSION_ID=RP.PERMISSION_ID
                       WHERE R.ROLE_ID=? AND P.PERMISSION_KEY=?""",
                    (assignment["ROLE_ID"], permission),
                ).fetchone()
                if allowed:
                    matched_roles.append(allowed[0])
            if matched_roles:
                return AuthorizationDecision(True, "ROLE_PERMISSION_ALLOWED", "Permission granted by scoped role", tuple(sorted(set(matched_roles))))

            for policy in policies:
                subject_matches = policy["SUBJECT_TYPE"] == "ANY" or policy["SUBJECT_ID"] == actor_id or policy["SUBJECT_ID"] in group_ids
                if policy["EFFECT"] == "ALLOW" and subject_matches and _conditions_match(json.loads(policy["CONDITIONS_JSON"] or "{}"), facts):
                    return AuthorizationDecision(True, "EXPLICIT_POLICY_ALLOW", "An access policy allowed this action", matched_policy_id=policy["POLICY_ID"])
            return AuthorizationDecision(False, "DEFAULT_DENY", "No scoped role or policy grants this permission")
        finally:
            if owns_connection:
                conn.close()

    @staticmethod
    def _scope_matches(scope_type: str, scope_id: str, organization_id: str, resource: ResourceContext) -> bool:
        scope_type = scope_type.upper()
        if scope_type == "ORGANIZATION":
            return scope_id == organization_id
        if scope_type == "REPOSITORY":
            target = resource.repository_id or (resource.resource_id if resource.resource_type == "REPOSITORY" else None)
            return scope_id == target
        if scope_type == "BRANCH":
            return scope_id == (resource.branch_id or (resource.resource_id if resource.resource_type == "BRANCH" else None))
        if scope_type in {"APPLICATION", "WORKFLOW"}:
            return scope_id == resource.resource_id
        return False

    def require(self, actor_id: str, permission: str, resource: ResourceContext, context: dict[str, Any] | None = None, *, conn=None) -> AuthorizationDecision:
        decision = self.authorize(actor_id, permission, resource, context, conn=conn)
        if not decision.allowed:
            raise PermissionError(f"{decision.reason} [{decision.reason_code}]")
        return decision


authorization_engine = AuthorizationEngine()


def repository_resource(repository_id: str, *, organization_id: str | None = None, attributes: dict[str, Any] | None = None) -> ResourceContext:
    return ResourceContext("REPOSITORY", repository_id, organization_id=organization_id,
                           repository_id=repository_id, attributes=attributes or {})


def branch_resource(repository_id: str, branch_id: str, *, organization_id: str | None = None, attributes: dict[str, Any] | None = None) -> ResourceContext:
    return ResourceContext("BRANCH", branch_id, organization_id=organization_id,
                           repository_id=repository_id, branch_id=branch_id, attributes=attributes or {})
