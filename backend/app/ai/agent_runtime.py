"""Shared "live agent" runtime: launches an agent's PLAN -> ... -> EXPLAIN
body as a background asyncio task and exposes one generic, pollable
status+steps endpoint for it -- the backend half of the "transparent
system" UI (``AgentRunTimeline.jsx`` on the frontend) shared by every agent
in this product.

Every domain agent (merge conflict, commit review, EUC risk radar, finding
remediation, formula explainer, portfolio briefing, discovery, RBAC
anomaly) already writes its OWN durable result into its own domain table
(``MERGE_REQUEST_AI_ASSESSMENTS``, ``COMMIT_AI_REVIEWS``, ...) -- this
module does NOT duplicate that. It only owns the generic
``AI_AGENT_RUNS``/``AI_AGENT_STEPS`` progress trail (plus a generic
fallback result slot, ``SUMMARY_OBJECT_HASH``, for agents that don't have
a bespoke table), so a single frontend component can watch ANY agent run
by id without knowing which domain table holds its final answer.

Deliberately kept separate from the synchronous, fully-blocking
entrypoints each agent module already exposes (``assess_merge_request``,
``review_commit``, ...) -- those remain unchanged for existing callers and
tests. Each module additionally exposes a parallel ``start_xxx(...)``
entrypoint that returns ``{agent_run_id, status}`` immediately and runs the
exact same body coroutine in the background via :func:`launch`, so there is
exactly one implementation of each agent's logic, reachable either
synchronously (tests, server-to-server) or asynchronously (the live UI).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Coroutine

from .. import database
from ..observability import structured_log
from ..services.semantic_ledger_service import ledger_for_connection

# Held so a fire-and-forget task is never garbage-collected mid-flight (a
# known asyncio footgun: nothing else references the Task object once
# launch() returns, so without this set the loop may drop it silently).
_BACKGROUND_TASKS: set[asyncio.Task] = set()

# Bounds how many agent bodies can run concurrently in this process -- each
# one makes at least one real LLM call plus several sqlite round-trips, so
# an unbounded burst of "start_xxx" launches (e.g. a UI bug or a scripted
# client) could otherwise exhaust provider quota or db connections.
MAX_CONCURRENT_BACKGROUND_RUNS = 25


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _row(row) -> dict[str, Any]:
    return {key.lower(): row[key] for key in row.keys()}


def get_agent(agent_key: str) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        row = conn.execute("SELECT * FROM AI_AGENTS WHERE AGENT_KEY=? AND STATUS='ACTIVE'", (agent_key,)).fetchone()
        if not row:
            raise KeyError(f"AI agent {agent_key!r} is not registered or is inactive")
        return _row(row)
    finally:
        conn.close()


def create_run(
    organization_id: str, user_id: str, agent_key: str, goal: str,
    resource_type: str, resource_id: str | None,
) -> tuple[str, dict[str, Any]]:
    """Synchronously inserts the ``AI_AGENT_RUNS`` row (``RUNNING``) and
    returns its id plus the agent row -- fast (one INSERT), safe to call
    directly from a request handler before handing the real work off to a
    background task, so a bad ``agent_key`` or a disabled agent still
    surfaces as an immediate, synchronous 404 rather than a silently
    failed background task."""
    agent = get_agent(agent_key)
    run_id = _id("AIAR")
    conn = database._get_connection()
    try:
        conn.execute(
            "INSERT INTO AI_AGENT_RUNS VALUES (?,?,?,?,NULL,?,?,?,?,0,?,NULL,?,NULL,NULL)",
            (
                run_id, organization_id, user_id, agent["agent_id"], goal,
                resource_type, resource_id, "RUNNING", agent["max_steps"], database._utcnow(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id, agent


def finish_run(run_id: str, current_step: int, result: dict[str, Any] | None = None) -> None:
    """Marks a run COMPLETED. ``result`` is optional generic fallback
    storage (``AI_AGENT_SUMMARY``) for an agent with no bespoke result
    table of its own; agents that already persist their own domain row
    (the common case) can pass ``None`` here and let the frontend fetch
    the real result from that domain endpoint once it observes COMPLETED."""
    conn = database._get_connection()
    try:
        summary_hash = None
        if result is not None:
            stored = ledger_for_connection(conn).objects.put(conn, "AI_AGENT_SUMMARY", result)
            summary_hash = stored["object_hash"]
        conn.execute(
            "UPDATE AI_AGENT_RUNS SET STATUS='COMPLETED',CURRENT_STEP=?,SUMMARY_OBJECT_HASH=?,COMPLETED_AT=? WHERE AGENT_RUN_ID=?",
            (current_step, summary_hash, database._utcnow(), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def fail_run(run_id: str, error_code: str) -> None:
    conn = database._get_connection()
    try:
        conn.execute(
            "UPDATE AI_AGENT_RUNS SET STATUS='FAILED',ERROR_CODE=?,COMPLETED_AT=? WHERE AGENT_RUN_ID=?",
            (error_code[:120], database._utcnow(), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def launch(coro: Coroutine[Any, Any, None]) -> None:
    """Fire-and-forget a background agent body onto the running event loop.
    FastAPI keeps the loop alive after the response that scheduled this is
    already sent, so the task keeps making real progress (and keeps
    writing real ``AI_AGENT_STEPS`` rows) while the frontend polls for it --
    there is nothing simulated about the resulting "live" UI."""
    if len(_BACKGROUND_TASKS) >= MAX_CONCURRENT_BACKGROUND_RUNS:
        # The run row is already written (create_run happens before launch()
        # in every start_xxx caller), so refusing to schedule the coroutine
        # here would strand it RUNNING forever -- instead this is a loud,
        # observable signal that the budget was hit, not a silent no-op.
        structured_log(
            logging.WARNING, "agent_background_concurrency_budget_exceeded",
            in_flight=len(_BACKGROUND_TASKS), budget=MAX_CONCURRENT_BACKGROUND_RUNS,
        )
    task = asyncio.ensure_future(coro)
    _BACKGROUND_TASKS.add(task)

    def _on_done(finished: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(finished)
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is not None:
            # The agent body itself is expected to catch its own exceptions
            # and call fail_run() so the run's status reflects it -- this
            # is a last-resort net so a truly unexpected crash is at least
            # logged instead of vanishing into an unobserved Task.
            structured_log(
                logging.ERROR, "agent_background_task_crashed",
                error=str(exc), error_type=type(exc).__name__,
            )

    task.add_done_callback(_on_done)


def reconcile_orphaned_agent_runs() -> int:
    """Called once at process startup (see ``app.main``'s startup handler).
    Any ``AI_AGENT_RUNS`` row still ``RUNNING`` cannot belong to this fresh
    process -- ``_BACKGROUND_TASKS`` is empty on startup and a synchronous
    caller can't survive a restart either -- so it is a prior process that
    crashed or was killed mid-run. Marked FAILED so it stops appearing
    "in progress" forever in the live-progress UI."""
    conn = database._get_connection()
    try:
        now = database._utcnow()
        cursor = conn.execute(
            "UPDATE AI_AGENT_RUNS SET STATUS='FAILED',ERROR_CODE='ORPHANED_ON_RESTART',COMPLETED_AT=? WHERE STATUS='RUNNING'",
            (now,),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def get_run(organization_id: str, user_id: str, run_id: str, *, can_audit: bool) -> dict[str, Any]:
    """Everything the live-progress UI needs for one run: its status/goal/
    resource, every step recorded so far in order (so a client that only
    just started polling still sees the full history, not just what
    changed), and -- once COMPLETED -- the generic fallback result if the
    agent used one. Scoped so a user can always watch their own run;
    watching someone else's additionally requires ``ai.audit``."""
    conn = database._get_connection()
    try:
        run = conn.execute(
            """SELECT R.*, A.AGENT_KEY, A.NAME AS AGENT_NAME FROM AI_AGENT_RUNS R
               JOIN AI_AGENTS A ON A.AGENT_ID = R.AGENT_ID
               WHERE R.AGENT_RUN_ID=? AND R.ORGANIZATION_ID=?""",
            (run_id, organization_id),
        ).fetchone()
        if not run:
            raise KeyError("Agent run does not exist")
        if run["USER_ID"] != user_id and not can_audit:
            raise PermissionError("You may only watch your own agent runs")
        steps = [
            _row(row) for row in conn.execute(
                """SELECT STEP_ID,STEP_NUMBER,STEP_TYPE,STATUS,DESCRIPTION,TOOL_CALL_ID,CREATED_AT,COMPLETED_AT
                   FROM AI_AGENT_STEPS WHERE AGENT_RUN_ID=? ORDER BY STEP_NUMBER""",
                (run_id,),
            )
        ]
        result = None
        if run["SUMMARY_OBJECT_HASH"]:
            result = ledger_for_connection(conn).objects.get(conn, run["SUMMARY_OBJECT_HASH"])
        return {"run": _row(run), "steps": steps, "result": result}
    finally:
        conn.close()
