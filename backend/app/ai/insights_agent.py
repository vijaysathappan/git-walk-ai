"""Data Insights Agent -- the "Power BI via AI" USP.

Answers an open-ended analytical question about a repository's live data
with a chosen chart, a data table, and a plain-English explanation grounded
in real computed numbers. Follows the same PLAN -> INVESTIGATE -> EXPLAIN
discipline as every other agent in this codebase (see formula_explainer.py),
extended to two LLM calls either side of a deterministic query execution:

    PLAN            (deterministic)  -- discover the table's real schema
    QUERY PLAN      (LLM call #1)    -- question + schema -> a query spec
    VALIDATE+COMPILE(deterministic)  -- fail-closed allow-list, never raw SQL
    EXECUTE         (deterministic)  -- real live SQL, real numbers, optional
                                         linear-trend estimate (no invented
                                         numbers, ever)
    NARRATE         (LLM call #2)    -- real numbers -> headline, bullets,
                                         key terms, recommended actions

The model never supplies SQL text and never states a number that didn't
come from the executed query -- the second call is given the actual result
rows as evidence and is only asked to label and phrase them (grounding is
enforced the same way `ai_gateway.generate()` already enforces it for every
other feature: citation coverage against the evidence bundle).

Durably cached in AI_DATA_INSIGHT_RESULTS keyed by (table, branch HEAD
commit, question) -- identical question against unchanged data is served
instantly; a new commit on the branch invalidates the cache automatically.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .. import database
from ..repositories.commit_store import reconstruct_branch
from ..repositories.governance_store import repository_insights
from . import agent_runtime
from .classification_util import repository_classification
from .gateway import ai_gateway
from .insights_query_engine import (
    QuerySpecError,
    activity_schema_payload,
    build_activity_rows,
    compile_to_sql,
    discover_schema,
    execute,
    linear_trend,
    schema_evidence_payload,
    validate_activity_spec,
    validate_query_spec,
)
from .response_parsing import sanitize_free_text
from .retrieval import EvidenceItem
from .service import ai_service


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _resolve_branch_table(branch_id: str) -> tuple[str, str]:
    """Returns (sheet_id, data_table_id) for the branch's primary sheet."""
    conn = database._get_connection()
    try:
        row = conn.execute(
            "SELECT SHEET_ID, DATA_TABLE_ID FROM BRANCH_SHEET_TABLES WHERE BRANCH_ID=? ORDER BY IS_PRIMARY DESC LIMIT 1",
            (branch_id,),
        ).fetchone()
        if not row:
            raise ValueError("This branch has no data table yet")
        return row["SHEET_ID"], row["DATA_TABLE_ID"]
    finally:
        conn.close()


def _branch_head_commit(branch_id: str) -> str:
    conn = database._get_connection()
    try:
        row = conn.execute("SELECT HEAD_COMMIT_ID FROM BRANCHES WHERE BRANCH_ID=?", (branch_id,)).fetchone()
        return row["HEAD_COMMIT_ID"] if row else ""
    finally:
        conn.close()


def get_cached_result(table_id: str, branch_id: str, source_commit_id: str, question_hash: str) -> dict[str, Any] | None:
    conn = database._get_connection()
    try:
        row = conn.execute(
            """SELECT * FROM AI_DATA_INSIGHT_RESULTS
               WHERE TABLE_ID=? AND BRANCH_ID=? AND SOURCE_COMMIT_ID=? AND QUESTION_HASH=?""",
            (table_id, branch_id, source_commit_id, question_hash),
        ).fetchone()
        if not row:
            return None
        return _decode_result_row(row)
    finally:
        conn.close()


def get_result_by_id(insight_result_id: str) -> dict[str, Any] | None:
    conn = database._get_connection()
    try:
        row = conn.execute("SELECT * FROM AI_DATA_INSIGHT_RESULTS WHERE INSIGHT_RESULT_ID=?", (insight_result_id,)).fetchone()
        return _decode_result_row(row) if row else None
    finally:
        conn.close()


def _decode_result_row(row) -> dict[str, Any]:
    item = {key.lower(): row[key] for key in row.keys()}
    item["query_spec"] = json.loads(item.pop("query_spec_json") or "{}")
    item["result_data"] = json.loads(item.pop("result_data_json") or "[]")
    item["bullets"] = json.loads(item.pop("bullets_json") or "[]")
    item["key_terms"] = json.loads(item.pop("key_terms_json") or "[]")
    item["forecast"] = json.loads(item.pop("forecast_json")) if item.get("forecast_json") else None
    item.pop("forecast_json", None)
    item["recommended_actions"] = json.loads(item.pop("recommended_actions_json") or "[]")
    item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
    return item


def _query_plan_question(
    question: str, schema_payload: dict[str, Any], activity_payload: dict[str, Any], repair_reason: str | None = None,
) -> str:
    repair_block = (
        f"\nYour previous proposal was rejected: {repair_reason}. Propose a corrected plan using ONLY the real "
        "columns and enumerated choices listed below.\n" if repair_reason else ""
    )
    return (
        "A user asked an analytical question. It may be about (A) this repository's own DATA TABLE (the business "
        "data rows it stores -- sales, inventory, whatever the sheet contains), or (B) this repository's own "
        "GOVERNANCE/ACTIVITY (how it is used as a version-controlled asset -- commit counts, merge success rate, "
        "conflict rate, validation health, and similar already-computed metrics -- e.g. \"what percentage of our "
        "merges succeed\" or \"how much of our activity is formula changes\"). Decide which one the question is "
        "about, then propose a plan to answer it -- you never write SQL and never invent a number; you only "
        "choose from the exact options below, and the real answer is computed deterministically from your "
        "choices.\n"
        f"Question: {question}\n"
        f"Data table schema (JSON): {json.dumps(schema_payload)}\n"
        f"Repository activity metrics -- name, label, and REAL current value (JSON): {json.dumps(activity_payload)}\n"
        + repair_block +
        "First, add exactly one entry with action_type 'DOMAIN', title = 'TABLE_DATA' or 'REPOSITORY_ACTIVITY', "
        "rationale = one short reason for the choice.\n"
        "If DOMAIN is TABLE_DATA, also encode: 'GROUP_BY' (title = the column name to group by, omit if no "
        "grouping is needed), 'AGGREGATION' (title = one of SUM/AVG/COUNT/MIN/MAX/COUNT_DISTINCT, rationale = the "
        "column name it applies to, or the word 'none' for COUNT), 'FILTER' (title = column name, rationale = "
        "'<op> <value>' where op is one of =,!=,>,<,>=,<=,IN,BETWEEN), 'TIME_BUCKET' (title = one of day/week/"
        "month, only if the question is about a trend over time and a date-like column exists). Never reference a "
        "column that is not in the data table schema above.\n"
        "If DOMAIN is REPOSITORY_ACTIVITY, instead add one 'METRIC' entry per metric you want to show (up to 6), "
        "title = the exact metric name from the list above (e.g. 'merge_success_rate'), rationale = one short "
        "reason it's relevant. Never reference a metric name that is not in the list above.\n"
        "Always add one 'CHART_TYPE' entry (title = one of BAR/PIE/LINE/TABLE_ONLY, rationale = why it fits). "
        "`rationale` is REQUIRED and must never be empty on any entry. Put one sentence of plain reasoning in "
        "`answer`."
    )


def _parse_query_plan(result: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = {"filters": [], "metrics": [], "domain": "TABLE_DATA"}
    for item in result.get("recommended_actions") or []:
        action_type = str(item.get("action_type", "")).upper()
        title = (item.get("title") or "").strip()
        rationale = (item.get("rationale") or "").strip()
        if action_type == "DOMAIN" and title:
            spec["domain"] = title.upper()
        elif action_type == "GROUP_BY" and title:
            spec["group_by"] = title
        elif action_type == "AGGREGATION" and title:
            spec["aggregation"] = title
            if rationale and rationale.lower() != "none":
                spec["aggregation_column"] = rationale
        elif action_type == "FILTER" and title and rationale:
            parts = rationale.split(" ", 1)
            if len(parts) == 2:
                spec["filters"].append({"column": title, "op": parts[0], "value": parts[1].strip()})
        elif action_type == "TIME_BUCKET" and title:
            spec["time_bucket"] = title.lower()
        elif action_type == "METRIC" and title:
            spec["metrics"].append(title)
        elif action_type == "CHART_TYPE" and title:
            spec["chart_type"] = title.upper()
    return spec


def _narrate_question(question: str, spec_summary: str, result_rows: list[dict[str, Any]], forecast: dict[str, Any] | None) -> str:
    forecast_block = (
        f"\nA simple trend estimate was also computed: {json.dumps(forecast)}.\n" if forecast else ""
    )
    return (
        "A user asked an analytical question about this repository's data. The query below was already executed "
        "deterministically -- these are the REAL, computed numbers. Explain them; never state a number that is "
        "not in this evidence.\n"
        f"Question: {question}\n"
        f"Query executed: {spec_summary}\n"
        f"Result rows (JSON, at most 50): {json.dumps(result_rows)}\n"
        + forecast_block +
        "Put a one-sentence headline finding in `answer`. Encode the explanation as entries in "
        "recommended_actions: one or more entries with action_type 'BULLET' (title = a short key term this "
        "bullet is about, rationale = the bullet's explanation, written for a Data Analyst -- clear, specific, "
        "references the actual numbers), and up to 3 entries with action_type 'ACTION' (title = a short "
        "recommended next step, rationale = why) if a genuinely useful follow-up action exists -- omit ACTION "
        "entries entirely if there is nothing worth recommending. Never invent a number, trend, or category not "
        "present in the result rows above."
    )


def _parse_narrative(result: dict[str, Any]) -> dict[str, Any]:
    actions = result.get("recommended_actions") or []
    bullets = [
        {"key_term": item.get("title") or "", "text": sanitize_free_text(item.get("rationale"))}
        for item in actions if str(item.get("action_type", "")).upper() == "BULLET"
    ]
    bullets = [b for b in bullets if b["text"]]
    recommended = [
        {"title": item.get("title") or "", "rationale": sanitize_free_text(item.get("rationale"))}
        for item in actions if str(item.get("action_type", "")).upper() == "ACTION"
    ]
    recommended = [item for item in recommended if item["title"] and item["rationale"]]
    return {
        "headline": sanitize_free_text(result.get("answer")) or "Here's what the data shows.",
        "bullets": [b["text"] for b in bullets],
        "key_terms": [b["key_term"] for b in bullets if b["key_term"]],
        "recommended_actions": recommended,
        "confidence": float(result.get("confidence") or 0.0),
        "warnings": result.get("warnings") or [],
    }


async def _run_data_insight(
    run_id: str, organization_id: str, user_id: str, repository_id: str, table_id: str,
    branch_id: str, question: str,
) -> dict[str, Any]:
    sheet_id, data_table_id = _resolve_branch_table(branch_id)
    schema = discover_schema(data_table_id)
    schema_payload = schema_evidence_payload(schema)
    stats = repository_insights(repository_id)
    activity_payload = activity_schema_payload(stats)
    ai_service._record_agent_step(
        run_id, 1, "PLAN", "COMPLETED",
        f"Discovered {len(schema.columns)} column(s) across {schema.row_count} row(s) on sheet {sheet_id}, "
        f"plus {len(activity_payload['metrics'])} repository activity metric(s)",
    )

    plan_evidence = [
        EvidenceItem(type="table_schema", id=data_table_id, title="Table schema", summary=f"{len(schema.columns)} column(s)", data=schema_payload).serializable(),
        EvidenceItem(type="repository_activity", id=repository_id, title="Repository activity metrics", summary="Commit, merge, and validation governance metrics", data=activity_payload).serializable(),
    ]

    domain: str | None = None
    query_spec = None
    activity_spec = None
    repair_reason: str | None = None
    for attempt in range(2):
        plan_result = await ai_gateway.generate(
            organization_id=organization_id, user_id=user_id, feature="DATA_INSIGHTS_QUERY_PLAN",
            question=_query_plan_question(question, schema_payload, activity_payload, repair_reason),
            evidence=plan_evidence, context_hash=_hash({"schema": schema_payload, "activity": activity_payload, "attempt": attempt}),
            classification=repository_classification(repository_id), agent_key="DATA_INSIGHTS_AGENT", agent_run_id=run_id,
        )
        raw_spec = _parse_query_plan(plan_result)
        domain = raw_spec.get("domain") or "TABLE_DATA"
        try:
            if domain == "REPOSITORY_ACTIVITY":
                activity_spec = validate_activity_spec(raw_spec, stats)
            else:
                domain = "TABLE_DATA"
                query_spec = validate_query_spec(raw_spec, schema)
            break
        except QuerySpecError as exc:
            repair_reason = str(exc)
    if query_spec is None and activity_spec is None:
        # Fail-closed default: never silently fail the whole request over a
        # model that couldn't propose a valid plan twice in a row. Falls
        # back within whichever domain it last attempted.
        if domain == "REPOSITORY_ACTIVITY":
            available = {item["name"] for item in activity_payload["metrics"]}
            preferred = ["merge_success_rate", "conflict_rate", "cell_changes", "formula_changes"]
            default_metrics = [key for key in preferred if key in available] or [activity_payload["metrics"][0]["name"]]
            activity_spec = validate_activity_spec({"metrics": default_metrics, "chart_type": "BAR"}, stats)
        else:
            fallback_group = next((c.name for c in schema.columns if c.sample_values), None)
            query_spec = validate_query_spec(
                {"group_by": fallback_group, "aggregation": "COUNT", "chart_type": "BAR" if fallback_group else "TABLE_ONLY"},
                schema,
            )

    if activity_spec is not None:
        chart_type = activity_spec["chart_type"]
        tool_call = ai_service._record_read_tool_call(
            organization_id, user_id, run_id, None, "query_repository_data",
            {"domain": "REPOSITORY_ACTIVITY", "metrics": activity_spec["metrics"]}, {"chart_type": chart_type},
        )
        ai_service._record_agent_step(
            run_id, 2, "QUERY_PLANNED", "COMPLETED",
            f"Planned a repository activity summary of {len(activity_spec['metrics'])} metric(s): {', '.join(activity_spec['metrics'])}",
            tool_call["tool_call_id"],
        )
        result_rows = build_activity_rows(activity_spec["metrics"], stats)
        forecast = None
        ai_service._record_agent_step(
            run_id, 3, "EXECUTED", "COMPLETED",
            f"Read {len(result_rows)} real, already-computed governance metric(s) -- no query execution needed",
        )
        spec_summary = f"Repository activity metrics: {', '.join(activity_spec['metrics'])}"
    else:
        chart_type = query_spec.chart_type
        tool_call = ai_service._record_read_tool_call(
            organization_id, user_id, run_id, None, "query_repository_data",
            {"domain": "TABLE_DATA", "table_id": data_table_id, "group_by": query_spec.group_by, "aggregation": query_spec.aggregation},
            {"chart_type": chart_type},
        )
        ai_service._record_agent_step(
            run_id, 2, "QUERY_PLANNED", "COMPLETED",
            f"Planned a {query_spec.aggregation or 'row listing'} query"
            + (f" grouped by {query_spec.group_by}" if query_spec.group_by else ""),
            tool_call["tool_call_id"],
        )
        sql, params = compile_to_sql(query_spec, schema)
        result_rows = execute(sql, params)
        forecast = linear_trend(result_rows) if query_spec.time_bucket or (query_spec.group_by and len(result_rows) >= 3) else None
        ai_service._record_agent_step(
            run_id, 3, "EXECUTED", "COMPLETED",
            f"Executed the query live against {data_table_id} -- {len(result_rows)} row(s) returned"
            + (" with a trend estimate" if forecast else ""),
        )
        spec_summary = (
            f"{query_spec.aggregation or 'SELECT'}"
            + (f"({query_spec.aggregation_column})" if query_spec.aggregation_column else "")
            + (f" GROUP BY {query_spec.group_by}" if query_spec.group_by else "")
        )
    narrative_evidence = [
        EvidenceItem(type="query_result", id=data_table_id, title="Computed result", summary=spec_summary, data={"rows": result_rows}).serializable(),
    ]
    if forecast:
        narrative_evidence.append(EvidenceItem(type="trend_estimate", id=data_table_id, title="Trend estimate", summary=forecast["direction"], data=forecast).serializable())

    narrate_result = await ai_gateway.generate(
        organization_id=organization_id, user_id=user_id, feature="DATA_INSIGHTS_NARRATIVE",
        question=_narrate_question(question, spec_summary, result_rows, forecast),
        evidence=narrative_evidence, context_hash=_hash({"result_rows": result_rows, "forecast": forecast}),
        classification=repository_classification(repository_id), agent_key="DATA_INSIGHTS_AGENT", agent_run_id=run_id,
    )
    narrative = _parse_narrative(narrate_result)
    ai_service._record_agent_step(
        run_id, 4, "NARRATED", "COMPLETED",
        f"Explained the result in {len(narrative['bullets'])} bullet(s)",
    )

    if activity_spec is not None:
        stored_spec = {"domain": "REPOSITORY_ACTIVITY", "metrics": activity_spec["metrics"], "chart_type": chart_type}
    else:
        stored_spec = {
            "domain": "TABLE_DATA", "columns": query_spec.columns, "aggregation": query_spec.aggregation,
            "aggregation_column": query_spec.aggregation_column, "group_by": query_spec.group_by,
            "filters": [f.__dict__ for f in query_spec.filters], "time_bucket": query_spec.time_bucket,
            "chart_type": chart_type,
        }

    insight_result_id = _id("AIDI")
    question_hash = _hash(question.strip().lower())
    source_commit_id = _branch_head_commit(branch_id)
    conn = database._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO AI_DATA_INSIGHT_RESULTS
                (INSIGHT_RESULT_ID, AGENT_RUN_ID, REPOSITORY_ID, TABLE_ID, BRANCH_ID, SOURCE_COMMIT_ID,
                 QUESTION, QUESTION_HASH, QUERY_SPEC_JSON, RESULT_DATA_JSON, CHART_TYPE, HEADLINE,
                 BULLETS_JSON, KEY_TERMS_JSON, FORECAST_JSON, RECOMMENDED_ACTIONS_JSON, CONFIDENCE,
                 WARNINGS_JSON, CREATED_BY, CREATED_AT)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(TABLE_ID, BRANCH_ID, SOURCE_COMMIT_ID, QUESTION_HASH) DO UPDATE SET
                RESULT_DATA_JSON=excluded.RESULT_DATA_JSON, HEADLINE=excluded.HEADLINE,
                BULLETS_JSON=excluded.BULLETS_JSON, KEY_TERMS_JSON=excluded.KEY_TERMS_JSON,
                FORECAST_JSON=excluded.FORECAST_JSON, RECOMMENDED_ACTIONS_JSON=excluded.RECOMMENDED_ACTIONS_JSON,
                CONFIDENCE=excluded.CONFIDENCE, WARNINGS_JSON=excluded.WARNINGS_JSON, CREATED_AT=excluded.CREATED_AT
            """,
            (
                insight_result_id, run_id, repository_id, data_table_id, branch_id, source_commit_id,
                question, question_hash, json.dumps(stored_spec),
                json.dumps(result_rows, default=str), chart_type, narrative["headline"],
                json.dumps(narrative["bullets"]), json.dumps(narrative["key_terms"]),
                json.dumps(forecast) if forecast else None, json.dumps(narrative["recommended_actions"]),
                narrative["confidence"], json.dumps(narrative["warnings"]), user_id, database._utcnow(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {"cache_hit": False, **get_result_by_id(insight_result_id)}


def start_data_insight(
    organization_id: str, user_id: str, repository_id: str, table_id: str, branch_id: str, question: str,
) -> dict[str, Any]:
    """Background-launching entrypoint: returns immediately with
    {agent_run_id, status}; the frontend polls GET /ai-platform/agent-runs/{id}
    for live steps, then fetches the result by insight_result_id once
    COMPLETED. Serves a cached result directly (status COMPLETED, no run)
    when this exact question against this exact branch HEAD is already
    answered."""
    ai_service._require(user_id, "ai.agent.run", organization_id)
    sheet_id, data_table_id = _resolve_branch_table(branch_id)
    source_commit_id = _branch_head_commit(branch_id)
    question_hash = _hash(question.strip().lower())
    cached = get_cached_result(data_table_id, branch_id, source_commit_id, question_hash)
    if cached:
        return {"status": "COMPLETED", "cache_hit": True, **cached}

    run_id, _ = agent_runtime.create_run(
        organization_id, user_id, "DATA_INSIGHTS_AGENT", f"Answer: {question}",
        "DATA_INSIGHT", f"{table_id}:{branch_id}",
    )

    async def _body() -> None:
        try:
            outcome = await _run_data_insight(run_id, organization_id, user_id, repository_id, table_id, branch_id, question)
            agent_runtime.finish_run(run_id, 4, result={"insight_result_id": outcome["insight_result_id"]})
        except Exception as exc:
            agent_runtime.fail_run(run_id, getattr(exc, "code", type(exc).__name__))

    agent_runtime.launch(_body())
    return {"agent_run_id": run_id, "status": "RUNNING"}
