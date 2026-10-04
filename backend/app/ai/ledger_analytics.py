"""The AI Token Ledger: per-agent burn, success/failure, budget, and
cost-avoidance analytics.

``AIService.usage()`` (``ai/service.py``) already returns fixed all-time
totals with no per-agent breakdown, no time series, and no success/failure
rate — and ``AI_TOKEN_LEDGER.ESTIMATED_COST_USD`` has always been hardcoded
to 0 at write time (``gateway.py``), so "cost" has never actually been
computed anywhere. This module is the fix: it reads the same ``AI_REQUESTS``
table (now that ``AIGateway.generate()`` finally populates the
previously-dead ``AGENT_KEY`` column and a new ``AGENT_RUN_ID`` column) to
build a real, drillable ledger — one row per agent "transaction"
(``AI_AGENT_RUNS``), each priced by summing its own requests' tokens.

Since Git Walk routes exclusively to free-tier models, real spend is always
$0 — that's a true fact about this product's architecture, not something to
hide. Instead of a hollow $0.00 panel, every cost figure here is a
**reference cost**: what the same token volume would have cost at a
documented mid-tier commercial rate (``config.ai_reference_cost_per_1k_*``),
framed as cost avoidance / savings. It is informational, computed from real
token counts, and never presented as an actual bill.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .. import database
from ..config import settings
from .service import _row, ai_service

_COPILOT_CHAT_KEY = "__COPILOT_CHAT__"


def _reference_cost(input_tokens: int, output_tokens: int) -> float:
    return round(
        (input_tokens / 1000) * settings.ai_reference_cost_per_1k_input_tokens
        + (output_tokens / 1000) * settings.ai_reference_cost_per_1k_output_tokens,
        4,
    )


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))).isoformat()


def agent_leaderboard(organization_id: str, user_id: str, days: int = 30) -> list[dict[str, Any]]:
    """Per-agent (plus a synthetic "Copilot chat" bucket for direct,
    non-agentic calls) token burn and success/failure rollup over the
    trailing ``days`` window."""
    ai_service._require(user_id, "ai.audit", organization_id)
    since = _since(days)
    conn = database._get_connection()
    try:
        rows = conn.execute(
            """
            SELECT COALESCE(AGENT_KEY,?) AS AGENT_KEY, COUNT(*) AS REQUESTS,
                   SUM(CASE WHEN STATUS='COMPLETED' THEN 1 ELSE 0 END) AS SUCCEEDED,
                   SUM(CASE WHEN STATUS='FAILED' THEN 1 ELSE 0 END) AS FAILED,
                   COALESCE(SUM(INPUT_TOKENS),0) AS INPUT_TOKENS, COALESCE(SUM(OUTPUT_TOKENS),0) AS OUTPUT_TOKENS,
                   COALESCE(SUM(REASONING_TOKENS),0) AS REASONING_TOKENS,
                   COALESCE(AVG(LATENCY_MS),0) AS AVG_LATENCY_MS, MAX(CREATED_AT) AS LAST_RUN_AT
            FROM AI_REQUESTS WHERE ORGANIZATION_ID=? AND CREATED_AT>=?
            GROUP BY AGENT_KEY ORDER BY (INPUT_TOKENS+OUTPUT_TOKENS+REASONING_TOKENS) DESC
            """,
            (_COPILOT_CHAT_KEY, organization_id, since),
        ).fetchall()
        agent_names = {row["AGENT_KEY"]: row["NAME"] for row in conn.execute("SELECT AGENT_KEY,NAME FROM AI_AGENTS")}
        last_errors = {}
        for row in conn.execute(
            """SELECT COALESCE(AGENT_KEY,?) AS AGENT_KEY, ERROR_CODE FROM AI_REQUESTS
               WHERE ORGANIZATION_ID=? AND CREATED_AT>=? AND STATUS='FAILED' ORDER BY CREATED_AT DESC""",
            (_COPILOT_CHAT_KEY, organization_id, since),
        ):
            last_errors.setdefault(row["AGENT_KEY"], row["ERROR_CODE"])
        result = []
        for row in rows:
            item = _row(row)
            key = item["agent_key"]
            tokens = item["input_tokens"] + item["output_tokens"] + item["reasoning_tokens"]
            item.update({
                "agent_name": agent_names.get(key, "Copilot chat (non-agent)"),
                "is_agent": key != _COPILOT_CHAT_KEY,
                "total_tokens": tokens,
                "success_rate": round(item["succeeded"] / item["requests"], 3) if item["requests"] else 0.0,
                "reference_cost_usd": _reference_cost(item["input_tokens"], item["output_tokens"]),
                "last_error_code": last_errors.get(key),
                "avg_latency_ms": round(item.pop("avg_latency_ms"), 2),
            })
            result.append(item)
        return result
    finally:
        conn.close()


def daily_trend(organization_id: str, user_id: str, days: int = 30) -> list[dict[str, Any]]:
    """Requests, failures, tokens, and reference cost per calendar day over
    the trailing ``days`` window — powers the ledger's trend chart."""
    ai_service._require(user_id, "ai.audit", organization_id)
    since = _since(days)
    conn = database._get_connection()
    try:
        rows = conn.execute(
            """
            SELECT substr(CREATED_AT,1,10) AS DAY, COUNT(*) AS REQUESTS,
                   SUM(CASE WHEN STATUS='FAILED' THEN 1 ELSE 0 END) AS FAILURES,
                   COALESCE(SUM(INPUT_TOKENS),0) AS INPUT_TOKENS, COALESCE(SUM(OUTPUT_TOKENS),0) AS OUTPUT_TOKENS
            FROM AI_REQUESTS WHERE ORGANIZATION_ID=? AND CREATED_AT>=?
            GROUP BY DAY ORDER BY DAY ASC
            """,
            (organization_id, since),
        ).fetchall()
        result = []
        for row in rows:
            item = _row(row)
            item["total_tokens"] = item["input_tokens"] + item["output_tokens"]
            item["reference_cost_usd"] = _reference_cost(item["input_tokens"], item["output_tokens"])
            result.append(item)
        return result
    finally:
        conn.close()


def model_breakdown(organization_id: str, user_id: str, days: int = 30) -> list[dict[str, Any]]:
    """Token burn and reference cost per model over the trailing ``days``
    window."""
    ai_service._require(user_id, "ai.audit", organization_id)
    since = _since(days)
    conn = database._get_connection()
    try:
        rows = conn.execute(
            """
            SELECT R.MODEL_ID, M.DISPLAY_NAME, M.MODEL_SLUG, COUNT(*) AS REQUESTS,
                   COALESCE(SUM(R.INPUT_TOKENS),0) AS INPUT_TOKENS, COALESCE(SUM(R.OUTPUT_TOKENS),0) AS OUTPUT_TOKENS
            FROM AI_REQUESTS R LEFT JOIN AI_MODELS M ON M.MODEL_ID=R.MODEL_ID
            WHERE R.ORGANIZATION_ID=? AND R.CREATED_AT>=?
            GROUP BY R.MODEL_ID ORDER BY (INPUT_TOKENS+OUTPUT_TOKENS) DESC
            """,
            (organization_id, since),
        ).fetchall()
        result = []
        for row in rows:
            item = _row(row)
            item["total_tokens"] = item["input_tokens"] + item["output_tokens"]
            item["reference_cost_usd"] = _reference_cost(item["input_tokens"], item["output_tokens"])
            result.append(item)
        return result
    finally:
        conn.close()


def run_ledger(
    organization_id: str, user_id: str, agent_key: str | None = None,
    status: str | None = None, limit: int = 50, cursor: int = 0,
) -> dict[str, Any]:
    """The literal ledger book: one row per agent run (a "transaction"),
    each priced by summing its own ``AI_REQUESTS`` (matched via the new
    ``AGENT_RUN_ID`` column)."""
    ai_service._require(user_id, "ai.audit", organization_id)
    limit = max(1, min(limit, 200))
    clauses, params = ["R.ORGANIZATION_ID=?"], [organization_id]
    if agent_key:
        clauses.append("A.AGENT_KEY=?"); params.append(agent_key)
    if status:
        clauses.append("R.STATUS=?"); params.append(status.upper())
    conn = database._get_connection()
    try:
        rows = conn.execute(
            f"""SELECT R.*, A.AGENT_KEY, A.NAME AS AGENT_NAME FROM AI_AGENT_RUNS R
                JOIN AI_AGENTS A ON A.AGENT_ID=R.AGENT_ID
                WHERE {' AND '.join(clauses)} ORDER BY R.CREATED_AT DESC LIMIT ? OFFSET ?""",
            [*params, limit + 1, cursor],
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            item = _row(row)
            totals = conn.execute(
                """SELECT COUNT(*), COALESCE(SUM(INPUT_TOKENS),0), COALESCE(SUM(OUTPUT_TOKENS),0),
                          SUM(CASE WHEN STATUS='FAILED' THEN 1 ELSE 0 END)
                   FROM AI_REQUESTS WHERE AGENT_RUN_ID=?""",
                (item["agent_run_id"],),
            ).fetchone()
            item["requests"], input_tokens, output_tokens, item["failed_requests"] = totals
            item["total_tokens"] = input_tokens + output_tokens
            item["reference_cost_usd"] = _reference_cost(input_tokens, output_tokens)
            items.append(item)
        return {"items": items, "next_cursor": cursor + limit if has_more else None}
    finally:
        conn.close()


def run_receipt(organization_id: str, user_id: str, agent_run_id: str) -> dict[str, Any]:
    """A single agent run's full "receipt": its step-by-step trace plus
    each underlying AI request with its own token/cost line."""
    ai_service._require(user_id, "ai.audit", organization_id)
    conn = database._get_connection()
    try:
        run = conn.execute(
            """SELECT R.*, A.AGENT_KEY, A.NAME AS AGENT_NAME FROM AI_AGENT_RUNS R
               JOIN AI_AGENTS A ON A.AGENT_ID=R.AGENT_ID WHERE R.AGENT_RUN_ID=? AND R.ORGANIZATION_ID=?""",
            (agent_run_id, organization_id),
        ).fetchone()
        if not run:
            raise KeyError("Agent run does not exist")
        steps = [_row(row) for row in conn.execute(
            "SELECT * FROM AI_AGENT_STEPS WHERE AGENT_RUN_ID=? ORDER BY STEP_NUMBER", (agent_run_id,)
        )]
        requests = []
        total_input = total_output = 0
        for row in conn.execute(
            "SELECT * FROM AI_REQUESTS WHERE AGENT_RUN_ID=? ORDER BY CREATED_AT", (agent_run_id,)
        ):
            item = _row(row)
            item["reference_cost_usd"] = _reference_cost(item.get("input_tokens") or 0, item.get("output_tokens") or 0)
            total_input += item.get("input_tokens") or 0
            total_output += item.get("output_tokens") or 0
            requests.append(item)
        return {
            "run": _row(run), "steps": steps, "requests": requests,
            "total_tokens": total_input + total_output,
            "reference_cost_usd": _reference_cost(total_input, total_output),
        }
    finally:
        conn.close()


def budget_status(organization_id: str, user_id: str) -> dict[str, Any]:
    """Today's usage against the org's daily token quota, an intraday burn
    rate, a projected end-of-day usage figure, and a deterministic alert —
    pure arithmetic over real rows, no AI call. Also flags that agent runs
    (unlike copilot chat) are not yet subject to ``AIGateway.enforce_quota``,
    a real asymmetry in today's enforcement worth surfacing rather than
    implying quota coverage that doesn't exist."""
    ai_service._require(user_id, "ai.audit", organization_id)
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    conn = database._get_connection()
    try:
        settings_row = conn.execute(
            "SELECT * FROM AI_ORGANIZATION_SETTINGS WHERE ORGANIZATION_ID=?", (organization_id,)
        ).fetchone()
        quota = _row(settings_row) if settings_row else {"daily_token_quota": 1000000, "user_daily_token_quota": 150000}
        daily_quota = quota.get("daily_token_quota") or 1000000

        today_totals = conn.execute(
            """SELECT COALESCE(SUM(INPUT_TOKENS),0), COALESCE(SUM(OUTPUT_TOKENS),0), COALESCE(SUM(REASONING_TOKENS),0)
               FROM AI_REQUESTS WHERE ORGANIZATION_ID=? AND CREATED_AT>=?""",
            (organization_id, today_start.isoformat()),
        ).fetchone()
        today_usage = sum(today_totals)

        trailing = conn.execute(
            """SELECT substr(CREATED_AT,1,10) AS DAY, COALESCE(SUM(INPUT_TOKENS+OUTPUT_TOKENS+REASONING_TOKENS),0) AS TOKENS
               FROM AI_REQUESTS WHERE ORGANIZATION_ID=? AND CREATED_AT>=? AND CREATED_AT<?
               GROUP BY DAY""",
            (organization_id, (today_start - timedelta(days=7)).isoformat(), today_start.isoformat()),
        ).fetchall()
        trailing_avg = round(sum(row[1] for row in trailing) / 7, 2)

        hours_elapsed = max((now - today_start).total_seconds() / 3600, 0.25)
        projected_end_of_day_usage = round(today_usage / hours_elapsed * 24, 2)
        quota_pct_used = round(min(1.0, today_usage / daily_quota), 4) if daily_quota else 0.0

        alerts = []
        if trailing_avg > 0 and today_usage > trailing_avg * 2:
            alerts.append({
                "type": "USAGE_SPIKE",
                "message": f"Today's usage ({today_usage:,} tokens) is more than double the trailing 7-day "
                           f"average ({trailing_avg:,.0f} tokens/day).",
            })
        if projected_end_of_day_usage > daily_quota:
            alerts.append({
                "type": "PROJECTED_QUOTA_BREACH",
                "message": f"At today's current pace, usage is projected to reach {projected_end_of_day_usage:,.0f} "
                           f"tokens by end of day — above the {daily_quota:,} token daily quota.",
            })

        thirty_day_since = (now - timedelta(days=30)).isoformat()
        month_totals = conn.execute(
            """SELECT COALESCE(SUM(INPUT_TOKENS),0), COALESCE(SUM(OUTPUT_TOKENS),0)
               FROM AI_REQUESTS WHERE ORGANIZATION_ID=? AND CREATED_AT>=?""",
            (organization_id, thirty_day_since),
        ).fetchone()
        reference_savings_30d = _reference_cost(month_totals[0], month_totals[1])

        return {
            "daily_token_quota": daily_quota, "today_usage": today_usage, "quota_pct_used": quota_pct_used,
            "trailing_7day_avg_tokens_per_day": trailing_avg, "projected_end_of_day_usage": projected_end_of_day_usage,
            "reference_savings_30d_usd": reference_savings_30d, "alerts": alerts,
            "agent_runs_subject_to_quota": False,
        }
    finally:
        conn.close()
