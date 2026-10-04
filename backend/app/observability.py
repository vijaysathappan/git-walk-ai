"""Stage 4 audit, security logging, metrics, and request correlation."""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .database import _get_connection, _utcnow


logger = logging.getLogger("gitwalk")
request_id_var = contextvars.ContextVar("request_id", default="REQ_SYSTEM")
trace_id_var = contextvars.ContextVar("trace_id", default="TRC_SYSTEM")


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    trace_id: str


def new_request_context(request_id: str | None = None, trace_id: str | None = None) -> RequestContext:
    safe = re.compile(r"^[A-Za-z0-9_-]{4,80}$")
    request_id = request_id if request_id and safe.fullmatch(request_id) else f"REQ_{uuid.uuid4().hex[:16].upper()}"
    trace_id = trace_id if trace_id and safe.fullmatch(trace_id) else f"TRC_{uuid.uuid4().hex[:24].upper()}"
    request_id_var.set(request_id)
    trace_id_var.set(trace_id)
    return RequestContext(request_id, trace_id)


def current_request_id() -> str:
    return request_id_var.get()


def current_trace_id() -> str:
    return trace_id_var.get()


def structured_log(level: int, message: str, **context: Any) -> None:
    payload = {
        "message": message,
        "request_id": current_request_id(),
        "trace_id": current_trace_id(),
        **{key: value for key, value in context.items() if value is not None},
    }
    logger.log(level, json.dumps(payload, default=str, sort_keys=True))


def record_audit_event(
    event_type: str,
    *,
    actor_user_id: str = "USR_SYSTEM",
    actor_type: str = "USER",
    repository_id: str | None = None,
    branch_id: str | None = None,
    working_copy_id: str | None = None,
    commit_id: str | None = None,
    merge_request_id: str | None = None,
    payload: dict[str, Any] | None = None,
    status: str = "SUCCESS",
    failure_reason: str | None = None,
) -> str:
    """Append an integrity-chained event. Database triggers prevent mutation."""
    conn = _get_connection()
    now = _utcnow()
    event_id = f"EVT_{uuid.uuid4().hex[:20].upper()}"
    payload_json = json.dumps(payload or {}, default=str, sort_keys=True, separators=(",", ":"))
    try:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT EVENT_HASH FROM AUDIT_EVENTS ORDER BY ROWID DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous[0] if previous else "GENESIS"
        canonical = "|".join(
            [event_id, now, actor_user_id, actor_type, event_type, payload_json,
             current_request_id(), current_trace_id(), status, failure_reason or "", previous_hash]
        )
        event_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        conn.execute(
            """
            INSERT INTO AUDIT_EVENTS
                (EVENT_ID,CREATED_AT,ACTOR_USER_ID,ACTOR_TYPE,REPOSITORY_ID,
                 BRANCH_ID,WORKING_COPY_ID,COMMIT_ID,MERGE_REQUEST_ID,EVENT_TYPE,
                 EVENT_PAYLOAD,REQUEST_ID,TRACE_ID,STATUS,FAILURE_REASON,
                 PREVIOUS_EVENT_HASH,EVENT_HASH)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (event_id, now, actor_user_id, actor_type, repository_id, branch_id,
             working_copy_id, commit_id, merge_request_id, event_type, payload_json,
             current_request_id(), current_trace_id(), status, failure_reason,
             previous_hash, event_hash),
        )
        conn.commit()
        return event_id
    finally:
        conn.close()


def record_metric(
    name: str,
    value: float,
    unit: str,
    *,
    status: str = "SUCCESS",
    user_id: str | None = None,
    repository_id: str | None = None,
    branch_id: str | None = None,
    tags: dict[str, Any] | None = None,
) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO OPERATION_METRICS VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"MET_{uuid.uuid4().hex[:20].upper()}", _utcnow(), name, float(value), unit,
             status, current_request_id(), current_trace_id(), user_id, repository_id,
             branch_id, json.dumps(tags or {}, default=str, sort_keys=True)),
        )
        conn.commit()
    finally:
        conn.close()


def record_security_event(
    event_type: str,
    *,
    severity: str = "WARNING",
    user_id: str | None = None,
    client_ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO SECURITY_EVENTS VALUES (?,?,?,?,?,?,?,?,?)",
            (f"SEC_{uuid.uuid4().hex[:20].upper()}", _utcnow(), event_type, severity,
             user_id, current_request_id(), current_trace_id(), client_ip,
             json.dumps(details or {}, default=str, sort_keys=True)),
        )
        conn.commit()
    finally:
        conn.close()


class metric_timer:
    def __init__(self, name: str, **context: Any):
        self.name = name
        self.context = context
        self.started = 0.0

    def __enter__(self):
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, _exc, _traceback):
        record_metric(
            self.name,
            (time.perf_counter() - self.started) * 1000,
            "ms",
            status="FAILED" if exc_type else "SUCCESS",
            **self.context,
        )
        return False
