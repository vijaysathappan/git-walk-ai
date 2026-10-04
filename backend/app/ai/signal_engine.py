"""The Signal Registry: a data-driven replacement for the Controls tab's
former hardcoded checks, plus an internal MCP-style tool catalogue for them.

Design summary (see the conversation/spec this was built from):
  - A "signal" (``AI_SIGNAL_DEFINITIONS``) pairs a whitelisted ``source``
    (``signal_sources.SOURCES``) with a small, validated ``condition`` --
    never raw SQL. ``run_signal_scan`` is the single execution path for
    every signal, built-in or admin-created.
  - The five checks that used to be hardocded directly inside
    ``AIService.generate_controls()`` are migrated here as ``SYSTEM_BUILTIN``
    definitions with byte-identical dedupe keys, insight types, and
    severities, so re-running a scan after this migration neither duplicates
    nor drops any already-open insight (verified in
    ``tests/test_signal_engine.py``).
  - Every enabled definition also registers itself as an ``AI_TOOLS`` row
    (``signal.<key>``), so Git Walk's own agents can discover and invoke it
    the same way they already discover the 14 hardcoded tools -- this is the
    "act like an MCP for our product" half of the feature. Custom (per-
    organization) tool rows carry that organization's id so one tenant's
    signal catalogue is never visible to another (see
    ``AIService.administration()``'s tool query).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .. import database
from ..access_control.engine import ResourceContext, authorization_engine
from ..observability import record_audit_event
from . import signal_sources
from .retrieval import EvidenceItem

_ALLOWED_CATEGORIES_DEFAULT = "CUSTOM"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _row(row) -> dict[str, Any]:
    return {key.lower(): row[key] for key in row.keys()}


# (signal_key, name, description, category, source, insight_type, condition, severity)
# Mirrors the five checks AIService.generate_controls() used to run inline,
# in the same order, with the same dedupe-key shape and severities.
_BUILTIN_SIGNALS: tuple[tuple[str, str, str, str, str, str, dict[str, Any], str], ...] = (
    ("SYSTEM_INTEGRATION_UNHEALTHY", "Integration connection unhealthy",
     "Flags connections reporting degraded or unhealthy status.", "INTEGRATIONS",
     "INTEGRATION_HEALTH", "INTEGRATION_HEALTH",
     {"field": "health_status", "op": "in", "value": ["UNHEALTHY", "DEGRADED"]}, "HIGH"),
    ("SYSTEM_DEAD_LETTER_QUEUE", "Dead-letter items awaiting replay",
     "Flags integration records parked in the dead-letter queue.", "INTEGRATIONS",
     "INTEGRATION_QUEUE", "DEAD_LETTER",
     {"field": "queue", "op": "equals", "value": "DEAD_LETTER"}, "HIGH"),
    ("SYSTEM_CANONICAL_QUARANTINE", "Canonical records in quarantine",
     "Flags incoming records that failed canonical validation.", "INTEGRATIONS",
     "INTEGRATION_QUEUE", "CANONICAL_QUARANTINE",
     {"field": "queue", "op": "equals", "value": "CANONICAL_QUARANTINE"}, "HIGH"),
    ("SYSTEM_SYNC_CONFLICT", "Unresolved synchronization conflicts",
     "Flags integration records where two systems disagree.", "INTEGRATIONS",
     "INTEGRATION_QUEUE", "SYNC_CONFLICT",
     {"field": "queue", "op": "equals", "value": "SYNC_CONFLICT"}, "CRITICAL"),
    ("SYSTEM_EUC_HIGH_RISK", "High/critical EUC risk findings",
     "Flags open spreadsheet-risk findings at HIGH or CRITICAL severity for the selected repository.", "EUC_RISK",
     "EUC_FINDING", "EUC_RISK",
     {"field": "severity", "op": "in", "value": ["HIGH", "CRITICAL"]}, "HIGH"),
)


def ensure_builtin_signals(conn, organization_id: str, now: str) -> None:
    """Idempotent per-organization seed, mirroring AIService._ensure_settings's
    lazy INSERT OR IGNORE pattern -- signal definitions are org-scoped data,
    not global catalogue rows, so they can't be seeded once at schema-init
    time the way AI_TOOLS/AI_AGENTS are."""
    for signal_key, name, description, category, source, insight_type, condition, severity in _BUILTIN_SIGNALS:
        conn.execute(
            """INSERT OR IGNORE INTO AI_SIGNAL_DEFINITIONS
               (SIGNAL_ID, ORGANIZATION_ID, SIGNAL_KEY, NAME, DESCRIPTION, CATEGORY, SOURCE, INSIGHT_TYPE,
                CONDITION_JSON, SEVERITY, ORIGIN, ENABLED, CREATED_BY, CREATED_AT, UPDATED_AT)
               VALUES (?,?,?,?,?,?,?,?,?,?,'SYSTEM_BUILTIN',1,'USR_SYSTEM',?,?)""",
            (_id("AISIG"), organization_id, signal_key, name, description, category, source, insight_type,
             json.dumps(condition), severity, now, now),
        )
        # organization_id=None: a built-in is shared/global, same visibility as the 14 hardcoded tools.
        _register_signal_tool(conn, organization_id=None, signal_key=signal_key, name=name, description=description, now=now)


def _register_signal_tool(conn, *, organization_id: str | None, signal_key: str, name: str, description: str, now: str) -> None:
    """Registers (or refreshes) this signal's AI_TOOLS row. organization_id
    is None for the shared system built-ins (visible to every tenant, same
    as the 14 hardcoded tools) and set for a tenant's own custom signal
    (visible only to that tenant -- see AIService.administration())."""
    tool_name = f"signal.{signal_key.lower()}"
    description_text = (description or name)[:500]
    conn.execute(
        """INSERT INTO AI_TOOLS
           (TOOL_ID, TOOL_NAME, DESCRIPTION, INPUT_SCHEMA_JSON, REQUIRED_PERMISSION, RISK_LEVEL,
            APPROVAL_MODE, IDEMPOTENT, READ_ONLY, STATUS, CREATED_AT, UPDATED_AT, ORGANIZATION_ID)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(TOOL_NAME) DO UPDATE SET
               DESCRIPTION=excluded.DESCRIPTION, STATUS=excluded.STATUS, UPDATED_AT=excluded.UPDATED_AT""",
        (_id("AIT"), tool_name, description_text, "{}", "ai.recommend", "LOW", "AUTO", 1, 1,
         "ACTIVE", now, now, organization_id),
    )


def list_sources() -> list[dict[str, Any]]:
    return signal_sources.list_sources()


def list_signal_definitions(organization_id: str, user_id: str) -> list[dict[str, Any]]:
    from .service import ai_service  # deferred: service.py imports this module for generate_controls()

    ai_service._require(user_id, "ai.read", organization_id)
    conn = database._get_connection()
    now = database._utcnow()
    try:
        ensure_builtin_signals(conn, organization_id, now)
        conn.commit()
        rows = conn.execute(
            """SELECT D.*,
                      (SELECT COUNT(*) FROM AI_INSIGHTS I
                        WHERE I.ORGANIZATION_ID=D.ORGANIZATION_ID AND I.GENERATED_BY=D.SIGNAL_KEY AND I.STATUS='OPEN'
                      ) AS OPEN_INSIGHT_COUNT,
                      (SELECT R.STARTED_AT FROM AI_SIGNAL_RUNS R WHERE R.SIGNAL_ID=D.SIGNAL_ID ORDER BY R.STARTED_AT DESC LIMIT 1) AS LAST_RUN_AT,
                      (SELECT R.MATCHED_COUNT FROM AI_SIGNAL_RUNS R WHERE R.SIGNAL_ID=D.SIGNAL_ID ORDER BY R.STARTED_AT DESC LIMIT 1) AS LAST_RUN_MATCHED,
                      (SELECT R.STATUS FROM AI_SIGNAL_RUNS R WHERE R.SIGNAL_ID=D.SIGNAL_ID ORDER BY R.STARTED_AT DESC LIMIT 1) AS LAST_RUN_STATUS
               FROM AI_SIGNAL_DEFINITIONS D WHERE D.ORGANIZATION_ID=?
               ORDER BY (D.ORIGIN='SYSTEM_BUILTIN') DESC, D.CREATED_AT""",
            (organization_id,),
        ).fetchall()
        items = []
        for row in rows:
            item = _row(row)
            item["condition"] = json.loads(item.pop("condition_json"))
            items.append(item)
        return items
    finally:
        conn.close()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    return (slug or "SIGNAL")[:40]


def create_signal_definition(organization_id: str, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from .service import ai_service

    ai_service._require(user_id, "ai.admin", organization_id)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("Signal name is required")
    source_key = str(payload.get("source") or "").strip()
    source = signal_sources.SOURCES.get(source_key)
    if source is None:
        raise ValueError(f"Unknown signal source '{source_key}'")
    condition = payload.get("condition") or {}
    signal_sources.validate_condition(source_key, condition)
    severity = signal_sources.validate_severity(payload.get("severity") or source.default_severity)
    description = str(payload.get("description") or "").strip() or source.description
    category = str(payload.get("category") or _ALLOWED_CATEGORIES_DEFAULT).strip().upper()[:40]
    insight_type = str(payload.get("insight_type") or source_key).strip().upper()[:60]
    signal_key = f"CUSTOM_{_slugify(name)}_{uuid.uuid4().hex[:6].upper()}"

    now = database._utcnow()
    signal_id = _id("AISIG")
    conn = database._get_connection()
    try:
        ensure_builtin_signals(conn, organization_id, now)
        conn.execute(
            """INSERT INTO AI_SIGNAL_DEFINITIONS
               (SIGNAL_ID, ORGANIZATION_ID, SIGNAL_KEY, NAME, DESCRIPTION, CATEGORY, SOURCE, INSIGHT_TYPE,
                CONDITION_JSON, SEVERITY, ORIGIN, ENABLED, CREATED_BY, CREATED_AT, UPDATED_AT)
               VALUES (?,?,?,?,?,?,?,?,?,?,'CUSTOM',1,?,?,?)""",
            (signal_id, organization_id, signal_key, name, description, category, source_key, insight_type,
             json.dumps(condition), severity, user_id, now, now),
        )
        _register_signal_tool(conn, organization_id=organization_id, signal_key=signal_key, name=name, description=description, now=now)
        conn.commit()
    finally:
        conn.close()
    record_audit_event(
        "AI_SIGNAL_DEFINITION_CREATED", actor_user_id=user_id,
        payload={"organization_id": organization_id, "signal_id": signal_id, "signal_key": signal_key, "source": source_key, "severity": severity},
    )
    return {"signal_id": signal_id, "signal_key": signal_key}


def update_signal_definition(organization_id: str, user_id: str, signal_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from .service import ai_service

    ai_service._require(user_id, "ai.admin", organization_id)
    conn = database._get_connection()
    now = database._utcnow()
    try:
        row = conn.execute(
            "SELECT * FROM AI_SIGNAL_DEFINITIONS WHERE SIGNAL_ID=? AND ORGANIZATION_ID=?", (signal_id, organization_id),
        ).fetchone()
        if not row:
            raise KeyError("Signal definition does not exist")
        definition = _row(row)
        is_builtin = definition["origin"] == "SYSTEM_BUILTIN"

        updates: dict[str, Any] = {}
        if "enabled" in payload:
            updates["ENABLED"] = 1 if payload["enabled"] else 0
        if not is_builtin:
            if "name" in payload:
                name = str(payload["name"]).strip()
                if not name:
                    raise ValueError("Signal name cannot be empty")
                updates["NAME"] = name
            if "description" in payload:
                updates["DESCRIPTION"] = str(payload["description"]).strip() or definition["description"]
            if "severity" in payload:
                updates["SEVERITY"] = signal_sources.validate_severity(payload["severity"])
            if "condition" in payload:
                signal_sources.validate_condition(definition["source"], payload["condition"])
                updates["CONDITION_JSON"] = json.dumps(payload["condition"])
        elif any(key in payload for key in ("name", "description", "severity", "condition", "source")):
            raise ValueError("Built-in signals can only be enabled or disabled, not edited")

        if not updates:
            item = definition
            item["condition"] = json.loads(item.pop("condition_json"))
            return item

        updates["UPDATED_AT"] = now
        set_clause = ", ".join(f"{column}=?" for column in updates)
        conn.execute(
            f"UPDATE AI_SIGNAL_DEFINITIONS SET {set_clause} WHERE SIGNAL_ID=? AND ORGANIZATION_ID=?",
            (*updates.values(), signal_id, organization_id),
        )
        if "NAME" in updates or "DESCRIPTION" in updates:
            _register_signal_tool(
                conn, organization_id=organization_id, signal_key=definition["signal_key"],
                name=updates.get("NAME", definition["name"]), description=updates.get("DESCRIPTION", definition["description"]), now=now,
            )
        conn.commit()
        refreshed = _row(conn.execute("SELECT * FROM AI_SIGNAL_DEFINITIONS WHERE SIGNAL_ID=?", (signal_id,)).fetchone())
        refreshed["condition"] = json.loads(refreshed.pop("condition_json"))
        return refreshed
    finally:
        conn.close()


def delete_signal_definition(organization_id: str, user_id: str, signal_id: str) -> None:
    from .service import ai_service

    ai_service._require(user_id, "ai.admin", organization_id)
    conn = database._get_connection()
    try:
        row = conn.execute(
            "SELECT ORIGIN, SIGNAL_KEY FROM AI_SIGNAL_DEFINITIONS WHERE SIGNAL_ID=? AND ORGANIZATION_ID=?",
            (signal_id, organization_id),
        ).fetchone()
        if not row:
            raise KeyError("Signal definition does not exist")
        if row["ORIGIN"] == "SYSTEM_BUILTIN":
            raise ValueError("Built-in signals cannot be deleted; disable them instead")
        conn.execute("DELETE FROM AI_SIGNAL_DEFINITIONS WHERE SIGNAL_ID=? AND ORGANIZATION_ID=?", (signal_id, organization_id))
        conn.execute("DELETE FROM AI_SIGNAL_RUNS WHERE SIGNAL_ID=?", (signal_id,))
        conn.execute("DELETE FROM AI_TOOLS WHERE TOOL_NAME=?", (f"signal.{row['SIGNAL_KEY'].lower()}",))
        conn.commit()
    finally:
        conn.close()
    record_audit_event("AI_SIGNAL_DEFINITION_DELETED", actor_user_id=user_id,
                       payload={"organization_id": organization_id, "signal_id": signal_id})


def signal_run_history(organization_id: str, user_id: str, signal_id: str, limit: int = 30) -> list[dict[str, Any]]:
    from .service import ai_service

    ai_service._require(user_id, "ai.read", organization_id)
    conn = database._get_connection()
    try:
        owner = conn.execute(
            "SELECT 1 FROM AI_SIGNAL_DEFINITIONS WHERE SIGNAL_ID=? AND ORGANIZATION_ID=?", (signal_id, organization_id),
        ).fetchone()
        if not owner:
            raise KeyError("Signal definition does not exist")
        rows = conn.execute(
            "SELECT * FROM AI_SIGNAL_RUNS WHERE SIGNAL_ID=? ORDER BY STARTED_AT DESC LIMIT ?",
            (signal_id, max(1, min(limit, 200))),
        ).fetchall()
        return [_row(row) for row in rows]
    finally:
        conn.close()


def _can_read_repository(conn, user_id: str, organization_id: str, repository_id: str) -> bool:
    try:
        authorization_engine.require(
            user_id, "repository.read",
            ResourceContext("REPOSITORY", repository_id, organization_id=organization_id, repository_id=repository_id),
            conn=conn,
        )
        return True
    except PermissionError:
        return False


def run_signal_scan(organization_id: str, user_id: str, repository_id: str | None = None, signal_id: str | None = None) -> dict[str, Any]:
    """The single execution path for every signal, built-in or custom.
    Backward-compatible with the return shape of the original
    AIService.generate_controls() ({"generated", "insight_ids"}) plus a new
    "signals_scanned" count, so the existing Controls tab/endpoint keeps
    working unchanged while gaining every enabled custom signal for free."""
    from .service import ai_service

    ai_service._require(user_id, "ai.recommend", organization_id)
    conn = database._get_connection()
    now = database._utcnow()
    generated: list[str] = []
    signals_scanned = 0
    try:
        ai_service._ensure_settings(conn, organization_id, user_id)
        ensure_builtin_signals(conn, organization_id, now)
        if repository_id:
            authorization_engine.require(
                user_id, "repository.read",
                ResourceContext("REPOSITORY", repository_id, organization_id=organization_id, repository_id=repository_id),
                conn=conn,
            )
        query = "SELECT * FROM AI_SIGNAL_DEFINITIONS WHERE ORGANIZATION_ID=? AND ENABLED=1"
        params: list[Any] = [organization_id]
        if signal_id:
            query += " AND SIGNAL_ID=?"
            params.append(signal_id)
        definitions = conn.execute(query, params).fetchall()

        for row in definitions:
            definition = _row(row)
            source = signal_sources.SOURCES.get(definition["source"])
            if source is None:
                continue  # a source was retired/renamed after this definition was saved -- skip, don't crash the scan
            if source.scope == "REPOSITORY" and not repository_id:
                continue  # matches legacy behavior: repository-scoped signals only run once a repository is selected

            run_id = _id("ASR")
            started_at = database._utcnow()
            try:
                condition = json.loads(definition["condition_json"])
                matches = source.evaluate(conn, organization_id, condition, repository_id)
                if not repository_id and source.scope != "ORGANIZATION":
                    # Org-wide scan (no repository selected): a repository-
                    # scoped source still returns matches across every
                    # repository in the org, gated so far only by the
                    # org-level "ai.recommend" permission checked above --
                    # drop any match on a repository this caller can't
                    # actually read, so branch/merge-request titles never
                    # leak to someone without repository access.
                    matches = [
                        match for match in matches
                        if not match.repository_id or _can_read_repository(conn, user_id, organization_id, match.repository_id)
                    ]
                for match in matches:
                    insight_id = ai_service._upsert_insight(
                        conn, organization_id, definition["insight_type"],
                        match.severity or definition["severity"], match.title, match.summary,
                        match.resource_type, match.resource_id, match.evidence, match.dedupe_key, now,
                        generated_by=definition["signal_key"],
                    )
                    generated.append(insight_id)
                conn.execute(
                    "INSERT INTO AI_SIGNAL_RUNS VALUES (?,?,?,?,?,?,?)",
                    (run_id, definition["signal_id"], len(matches), "COMPLETED", None, started_at, database._utcnow()),
                )
            except Exception as exc:
                # One broken signal (e.g. a stale condition referencing a
                # value a since-changed source no longer accepts) must never
                # take down every other signal's scan.
                conn.execute(
                    "INSERT INTO AI_SIGNAL_RUNS VALUES (?,?,?,?,?,?,?)",
                    (run_id, definition["signal_id"], 0, "FAILED", str(exc)[:500], started_at, database._utcnow()),
                )
            signals_scanned += 1
        conn.commit()
        return {"generated": len(generated), "insight_ids": generated, "signals_scanned": signals_scanned}
    finally:
        conn.close()


def _hash(value: Any) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


async def explain_insight(organization_id: str, user_id: str, insight_id: str) -> dict[str, Any]:
    """Wires the previously-dormant AIService.recommendation() into a real
    generation call: one grounded explanation of an already-deterministic
    insight, never an invented one -- the insight's own title/summary/
    severity ARE the evidence, the model only explains and recommends."""
    from .gateway import ai_gateway
    from .response_parsing import sanitize_free_text
    from .service import ai_service

    ai_service._require(user_id, "ai.recommend", organization_id)
    conn = database._get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM AI_INSIGHTS WHERE INSIGHT_ID=? AND ORGANIZATION_ID=?", (insight_id, organization_id),
        ).fetchone()
        if not row:
            raise KeyError("AI insight does not exist")
        insight = _row(row)
    finally:
        conn.close()

    evidence = [EvidenceItem(
        type=insight["resource_type"] or insight["insight_type"], id=str(insight["resource_id"] or insight_id),
        title=insight["title"], summary=insight["summary"],
        data={"insight_type": insight["insight_type"], "severity": insight["severity"], "generated_by": insight["generated_by"]},
    ).serializable()]
    question = (
        "A deterministic control has already flagged the exception below -- do not invent additional facts or "
        "change its severity, only explain it in plain language for an operator and recommend the safest next "
        "step.\n"
        f"Severity: {insight['severity']}. Title: {insight['title']}. Detail: {insight['summary']}."
    )
    result = await ai_gateway.generate(
        organization_id=organization_id, user_id=user_id, feature="control_explanation",
        question=question, evidence=evidence,
        context_hash=_hash({"insight_id": insight_id, "evidence": evidence}),
        classification="INTERNAL", agent_key="CONTROL_REMEDIATION_AGENT",
    )
    explanation = sanitize_free_text(result.get("answer"), fallback="No explanation could be generated for this insight.")
    stored = ai_service.recommendation(organization_id, user_id, insight_id, {
        "ai_request_id": result.get("ai_request_id"),
        "title": f"Explanation: {insight['title']}",
        "recommendation": explanation,
        "priority": insight["severity"],
    })
    return {**stored, "explanation": explanation, "confidence": result.get("confidence"), "warnings": result.get("warnings") or []}
