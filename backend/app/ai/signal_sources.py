"""The whitelisted "signal source" catalogue behind the Signal Registry.

A signal definition (``AI_SIGNAL_DEFINITIONS``) never stores a fragment of
SQL -- only a ``source`` key and a small ``condition`` dict (``{field, op,
value}``). Each source below owns a fixed, single configurable field with a
fixed set of operators and (for enum fields) a fixed set of legal values, and
its evaluator function builds its query with those values only ever bound as
parameters, never interpolated into SQL text. This is the actual security
boundary for "let an admin add a signal from the UI": the admin picks *which*
values to filter on, never *what SQL runs*.

Every evaluator is read-only and organization/repository scoped exactly like
the deterministic checks in ``generate_controls()`` were before this module
existed (see ``ai/signal_engine.py`` for the migration) -- a signal can only
ever surface facts the caller's own organization/repository already owns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


@dataclass(frozen=True)
class SignalField:
    key: str
    label: str
    type: str  # "enum" | "number"
    operators: list[str]
    enum_values: list[str] | None = None
    min_value: int | None = None
    max_value: int | None = None


@dataclass(frozen=True)
class SignalMatch:
    resource_type: str
    resource_id: str
    title: str
    summary: str
    evidence: list[dict[str, Any]]
    dedupe_key: str
    severity: str | None = None  # overrides the definition's own severity when the source carries its own (e.g. EUC_FINDING)
    repository_id: str | None = None  # set by repository-scoped sources so an org-wide scan can filter by repository.read


@dataclass(frozen=True)
class SignalSource:
    key: str
    label: str
    description: str
    scope: str  # "ORGANIZATION" | "REPOSITORY" | "ORGANIZATION_OR_REPOSITORY"
    default_severity: str
    field: SignalField
    evaluate: Callable[[Any, str, dict[str, Any], str | None], list["SignalMatch"]]


def _cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


# ---------------------------------------------------------------------------
# Evaluators. Each mirrors, byte-for-byte where applicable, the deterministic
# query it replaces from the original hardcoded generate_controls().
# ---------------------------------------------------------------------------

def _evaluate_integration_health(conn, organization_id: str, condition: dict[str, Any], repository_id: str | None) -> list[SignalMatch]:
    values = _as_list(condition["value"])
    placeholders = ",".join("?" for _ in values)
    rows = conn.execute(
        f"""SELECT CONNECTION_ID, NAME, HEALTH_STATUS FROM INTEGRATION_CONNECTIONS
            WHERE ORGANIZATION_ID=? AND STATUS='ACTIVE' AND HEALTH_STATUS IN ({placeholders})""",
        (organization_id, *values),
    ).fetchall()
    return [
        SignalMatch(
            resource_type="INTEGRATION_CONNECTION", resource_id=row["CONNECTION_ID"],
            title=f"{row['NAME']} requires attention",
            summary=f"Connection health is {row['HEALTH_STATUS']}; deterministic run evidence should be reviewed.",
            evidence=[{"type": "CONNECTION", "id": row["CONNECTION_ID"]}],
            dedupe_key=f"CONNECTION_HEALTH:{row['CONNECTION_ID']}",
        )
        for row in rows
    ]


# queue value -> (table, id column). Interpolated into SQL below, but only
# ever from these fixed keys -- validate_condition() rejects anything else
# before a definition can be saved, and a lookup miss here raises KeyError
# (caught per-signal by the engine) rather than ever touching raw input.
_QUEUE_TABLES = {
    "DEAD_LETTER": ("INTEGRATION_DEAD_LETTERS", "DEAD_LETTER_ID"),
    "CANONICAL_QUARANTINE": ("CANONICAL_QUARANTINE", "QUARANTINE_ID"),
    "SYNC_CONFLICT": ("INTEGRATION_CONFLICTS", "CONFLICT_ID"),
}
_QUEUE_TITLES = {
    "DEAD_LETTER": "Dead-letter replay requires review",
    "CANONICAL_QUARANTINE": "Canonical record failed validation",
    "SYNC_CONFLICT": "Synchronization conflict is unresolved",
}


def _evaluate_integration_queue(conn, organization_id: str, condition: dict[str, Any], repository_id: str | None) -> list[SignalMatch]:
    queue = condition["value"]
    table, id_column = _QUEUE_TABLES[queue]
    rows = conn.execute(
        f"SELECT {id_column} AS ITEM_ID FROM {table} WHERE ORGANIZATION_ID=? AND STATUS='OPEN' LIMIT 100",
        (organization_id,),
    ).fetchall()
    return [
        SignalMatch(
            resource_type=queue, resource_id=row["ITEM_ID"], title=_QUEUE_TITLES[queue],
            summary="A deterministic exception remains open and may affect downstream freshness.",
            evidence=[{"type": queue, "id": row["ITEM_ID"]}],
            dedupe_key=f"{queue}:{row['ITEM_ID']}",
        )
        for row in rows
    ]


def _evaluate_euc_finding(conn, organization_id: str, condition: dict[str, Any], repository_id: str | None) -> list[SignalMatch]:
    if not repository_id:
        return []  # repository-scoped: only meaningful once a repository is selected, same as the original check
    values = _as_list(condition["value"])
    placeholders = ",".join("?" for _ in values)
    rows = conn.execute(
        f"""SELECT F.FINDING_ID, F.TITLE, O.SEVERITY, O.DESCRIPTION FROM EUC_ASSETS A
            JOIN EUC_FINDINGS F ON F.EUC_ID=A.EUC_ID
            LEFT JOIN EUC_FINDING_OCCURRENCES O ON O.FINDING_ID=F.FINDING_ID AND O.INTELLIGENCE_RUN_ID=F.LAST_RUN_ID
            WHERE A.REPOSITORY_ID=? AND F.STATUS='OPEN' AND O.SEVERITY IN ({placeholders}) LIMIT 100""",
        (repository_id, *values),
    ).fetchall()
    return [
        SignalMatch(
            resource_type="EUC_FINDING", resource_id=row["FINDING_ID"], title=row["TITLE"],
            summary=row["DESCRIPTION"] or "", severity=row["SEVERITY"],
            evidence=[{"type": "EUC_FINDING", "id": row["FINDING_ID"]}],
            dedupe_key=f"EUC_FINDING:{row['FINDING_ID']}",
        )
        for row in rows
    ]


def _evaluate_branch_activity(conn, organization_id: str, condition: dict[str, Any], repository_id: str | None) -> list[SignalMatch]:
    days = int(condition["value"])
    cutoff = _cutoff_iso(days)
    params: list[Any] = [organization_id]
    repo_filter = ""
    if repository_id:
        repo_filter = "AND B.REPOSITORY_ID=?"
        params.append(repository_id)
    rows = conn.execute(
        f"""SELECT B.BRANCH_ID, B.BRANCH_NAME, B.CREATED_AT, B.REPOSITORY_ID, MAX(C.CREATED_AT) AS LAST_COMMIT_AT
            FROM BRANCHES B
            JOIN WORKBOOK_REPOSITORIES W ON W.REPOSITORY_ID = B.REPOSITORY_ID
            LEFT JOIN COMMITS C ON C.BRANCH_ID = B.BRANCH_ID
            WHERE W.ORGANIZATION_ID=? AND B.STATUS='ACTIVE' AND B.BRANCH_TYPE='USER' {repo_filter}
            GROUP BY B.BRANCH_ID""",
        params,
    ).fetchall()
    matches = []
    for row in rows:
        last_activity = row["LAST_COMMIT_AT"] or row["CREATED_AT"]
        if last_activity < cutoff:
            matches.append(SignalMatch(
                resource_type="BRANCH", resource_id=row["BRANCH_ID"],
                title=f"Branch \"{row['BRANCH_NAME']}\" has been idle for {days}+ days",
                summary="No commit activity recorded in this window; review or archive the branch.",
                evidence=[{"type": "BRANCH", "id": row["BRANCH_ID"]}],
                dedupe_key=f"BRANCH_ACTIVITY:{row['BRANCH_ID']}",
                repository_id=row["REPOSITORY_ID"],
            ))
    return matches


def _evaluate_merge_request_stale(conn, organization_id: str, condition: dict[str, Any], repository_id: str | None) -> list[SignalMatch]:
    days = int(condition["value"])
    cutoff = _cutoff_iso(days)
    params: list[Any] = [organization_id, cutoff]
    repo_filter = ""
    if repository_id:
        repo_filter = "AND M.REPOSITORY_ID=?"
        params.append(repository_id)
    rows = conn.execute(
        f"""SELECT M.MERGE_REQUEST_ID, M.TITLE, M.REPOSITORY_ID,
                   (SELECT COUNT(*) FROM MERGE_CONFLICTS C WHERE C.MERGE_REQUEST_ID=M.MERGE_REQUEST_ID AND C.STATUS='OPEN') AS OPEN_CONFLICTS
            FROM MERGE_REQUESTS M
            JOIN WORKBOOK_REPOSITORIES W ON W.REPOSITORY_ID = M.REPOSITORY_ID
            WHERE W.ORGANIZATION_ID=? AND M.STATUS='OPEN' AND M.CREATED_AT < ? {repo_filter}""",
        params,
    ).fetchall()
    return [
        SignalMatch(
            resource_type="MERGE_REQUEST", resource_id=row["MERGE_REQUEST_ID"],
            title=f'Merge request "{row["TITLE"]}" has been conflicted for {days}+ days',
            summary="This merge request has unresolved conflicts and has stayed open past the review window.",
            evidence=[{"type": "MERGE_REQUEST", "id": row["MERGE_REQUEST_ID"]}],
            dedupe_key=f"MERGE_REQUEST_STALE:{row['MERGE_REQUEST_ID']}",
            repository_id=row["REPOSITORY_ID"],
        )
        for row in rows if row["OPEN_CONFLICTS"] > 0
    ]


def _evaluate_device_trust(conn, organization_id: str, condition: dict[str, Any], repository_id: str | None) -> list[SignalMatch]:
    values = _as_list(condition["value"])
    placeholders = ",".join("?" for _ in values)
    rows = conn.execute(
        f"""SELECT DISTINCT D.FINGERPRINT_ID, D.TRUST_STATUS, D.MACHINE_ID, D.LAST_SEEN_AT
            FROM DEVICE_FINGERPRINTS D
            JOIN ORGANIZATION_MEMBERS M ON M.USER_ID = D.USER_ID AND M.STATUS='ACTIVE'
            WHERE M.ORGANIZATION_ID=? AND D.TRUST_STATUS IN ({placeholders})""",
        (organization_id, *values),
    ).fetchall()
    return [
        SignalMatch(
            resource_type="DEVICE", resource_id=row["FINGERPRINT_ID"],
            title=f"Device in {row['TRUST_STATUS']} trust state is active",
            summary=f"Machine {row['MACHINE_ID'] or 'unknown'} last seen {row['LAST_SEEN_AT']}; review its trust status.",
            evidence=[{"type": "DEVICE", "id": row["FINGERPRINT_ID"]}],
            dedupe_key=f"DEVICE_TRUST:{row['FINGERPRINT_ID']}",
        )
        for row in rows
    ]


SOURCES: dict[str, SignalSource] = {
    "INTEGRATION_HEALTH": SignalSource(
        key="INTEGRATION_HEALTH", label="Integration health",
        description="Flags connected external systems reporting unhealthy or degraded status.",
        scope="ORGANIZATION", default_severity="HIGH",
        field=SignalField(key="health_status", label="Health status", type="enum",
                           operators=["in"], enum_values=["UNHEALTHY", "DEGRADED"]),
        evaluate=_evaluate_integration_health,
    ),
    "INTEGRATION_QUEUE": SignalSource(
        key="INTEGRATION_QUEUE", label="Integration exception queue",
        description="Flags unresolved items sitting in a dead-letter, quarantine, or sync-conflict queue.",
        scope="ORGANIZATION", default_severity="HIGH",
        field=SignalField(key="queue", label="Queue", type="enum", operators=["equals"],
                           enum_values=["DEAD_LETTER", "CANONICAL_QUARANTINE", "SYNC_CONFLICT"]),
        evaluate=_evaluate_integration_queue,
    ),
    "EUC_FINDING": SignalSource(
        key="EUC_FINDING", label="EUC risk finding",
        description="Flags open spreadsheet-risk findings at a chosen severity within one repository.",
        scope="REPOSITORY", default_severity="HIGH",
        field=SignalField(key="severity", label="Severity", type="enum", operators=["in"],
                           enum_values=["LOW", "MEDIUM", "HIGH", "CRITICAL"]),
        evaluate=_evaluate_euc_finding,
    ),
    "BRANCH_ACTIVITY": SignalSource(
        key="BRANCH_ACTIVITY", label="Idle branch",
        description="Flags personal branches with no commit activity for longer than a chosen number of days.",
        scope="ORGANIZATION_OR_REPOSITORY", default_severity="MEDIUM",
        field=SignalField(key="idle_days", label="Idle for at least (days)", type="number",
                           operators=["older_than_days"], min_value=1, max_value=365),
        evaluate=_evaluate_branch_activity,
    ),
    "MERGE_REQUEST_STALE": SignalSource(
        key="MERGE_REQUEST_STALE", label="Stale conflicted merge request",
        description="Flags open merge requests that still have unresolved conflicts after a chosen number of days.",
        scope="ORGANIZATION_OR_REPOSITORY", default_severity="HIGH",
        field=SignalField(key="stale_days", label="Open & conflicted for at least (days)", type="number",
                           operators=["older_than_days"], min_value=1, max_value=180),
        evaluate=_evaluate_merge_request_stale,
    ),
    "DEVICE_TRUST": SignalSource(
        key="DEVICE_TRUST", label="Untrusted device activity",
        description="Flags devices in a chosen trust state associated with any member of the organization.",
        scope="ORGANIZATION", default_severity="HIGH",
        field=SignalField(key="trust_status", label="Trust status", type="enum", operators=["in"],
                           enum_values=["BLOCKED", "UNKNOWN"]),
        evaluate=_evaluate_device_trust,
    ),
}

_VALID_SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def validate_condition(source_key: str, condition: dict[str, Any]) -> None:
    """Raises ``ValueError`` (never lets an invalid condition reach storage)
    with a message safe to show directly to the admin who submitted it."""
    source = SOURCES.get(source_key)
    if source is None:
        raise ValueError(f"Unknown signal source '{source_key}'")
    if not isinstance(condition, dict):
        raise ValueError("Condition must be an object with field/op/value")
    field_spec = source.field
    if condition.get("field") != field_spec.key:
        raise ValueError(f"Condition field must be '{field_spec.key}' for source {source_key}")
    op = condition.get("op")
    if op not in field_spec.operators:
        raise ValueError(f"Operator '{op}' is not valid for field '{field_spec.key}' (allowed: {field_spec.operators})")
    value = condition.get("value")
    if field_spec.type == "enum":
        values = _as_list(value)
        if not values:
            raise ValueError("At least one value must be selected")
        invalid = [item for item in values if item not in (field_spec.enum_values or [])]
        if invalid:
            raise ValueError(f"Invalid value(s) for {field_spec.key}: {invalid} (allowed: {field_spec.enum_values})")
        if op == "equals" and len(values) != 1:
            raise ValueError("'equals' requires exactly one value")
    elif field_spec.type == "number":
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field_spec.key} must be a whole number") from None
        lo = field_spec.min_value if field_spec.min_value is not None else 1
        hi = field_spec.max_value if field_spec.max_value is not None else 3650
        if not (lo <= number <= hi):
            raise ValueError(f"{field_spec.key} must be between {lo} and {hi}")
    else:  # pragma: no cover - every current field is enum or number
        raise ValueError(f"Unsupported field type '{field_spec.type}'")


def validate_severity(severity: str) -> str:
    normalized = str(severity or "").strip().upper()
    if normalized not in _VALID_SEVERITIES:
        raise ValueError(f"Severity must be one of {sorted(_VALID_SEVERITIES)}")
    return normalized


def list_sources() -> list[dict[str, Any]]:
    """Schema-described catalogue the frontend's "Add signal" wizard renders
    its source dropdown and field inputs from -- the wizard never hardcodes
    a source list, so a new source added here appears there automatically."""
    return [
        {
            "source": source.key, "label": source.label, "description": source.description,
            "scope": source.scope, "default_severity": source.default_severity,
            "field": {
                "key": source.field.key, "label": source.field.label, "type": source.field.type,
                "operators": source.field.operators, "enum_values": source.field.enum_values,
                "min_value": source.field.min_value, "max_value": source.field.max_value,
            },
        }
        for source in SOURCES.values()
    ]
