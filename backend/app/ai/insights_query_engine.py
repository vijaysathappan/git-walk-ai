"""Deterministic, closed-world query planning for the Data Insights agent.

Mirrors the discipline `app.macros.sql_lane` already established for Virtual
Run: the AI model never supplies raw SQL text, only an enumerated choice of
columns/aggregation/filters/grouping; this module validates that choice
against the target table's REAL schema and compiles it to parametrized SQL.
A column name or operator the model invents is rejected, never executed --
fail closed, exactly like `macros.static_gate`.

Schema discovery uses `PRAGMA table_info` directly against the physical
per-branch sheet table (the same approach `dataset_store.get_table_page`
already uses), not `excel.identity.semantic_snapshot` -- the snapshot
reconstructs every row's identity individually (N+1 queries) and is far too
slow for live aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import database
from ..store.identifiers import SafeIdentifier

AGGREGATIONS = {"SUM", "AVG", "COUNT", "MIN", "MAX", "COUNT_DISTINCT"}
FILTER_OPERATORS = {"=", "!=", ">", "<", ">=", "<=", "IN", "BETWEEN"}
CHART_TYPES = {"BAR", "PIE", "LINE", "TABLE_ONLY"}
TIME_BUCKETS = {"day", "week", "month", None}
_MAX_CATEGORICAL_SAMPLES = 5
_CATEGORICAL_CARDINALITY_LIMIT = 20
_MAX_ACTIVITY_METRICS = 6

# The allow-listed, read-only surface onto `governance_store.repository_insights()`
# -- a fixed set of already-computed, already-vetted metrics (percentages
# like merge_success_rate/conflict_rate are computed once, deterministically,
# by governance_store itself; this module never re-derives or re-computes
# them, it only ever selects and labels a subset of what's already there).
ACTIVITY_METRICS: dict[str, tuple[str, str, str, bool]] = {
    # key: (section, field, label, is_percentage)
    "commits": ("version_control", "commits", "Commits", False),
    "changes": ("version_control", "changes", "Total changes", False),
    "cell_changes": ("version_control", "cell_changes", "Cell value/format changes", False),
    "formula_changes": ("version_control", "formula_changes", "Formula changes", False),
    "reverts": ("version_control", "reverts", "Reverted commits", False),
    "merge_requests": ("workflow", "merge_requests", "Merge requests", False),
    "merged": ("workflow", "merged", "Merged requests", False),
    "merge_success_rate": ("workflow", "merge_success_rate", "Merge success rate", True),
    "conflict_rate": ("workflow", "conflict_rate", "Conflict rate", True),
    "average_review_hours": ("workflow", "average_review_hours", "Average review time (hours)", False),
    "average_branch_lifetime_days": ("workflow", "average_branch_lifetime_days", "Average branch lifetime (days)", False),
    "active_branches": ("workflow", "active_branches", "Active branches", False),
    "active_working_copies": ("workflow", "active_working_copies", "Active working copies", False),
    "validation_runs": ("data_quality", "validation_runs", "Validation runs", False),
    "failed_runs": ("data_quality", "failed_runs", "Failed validation runs", False),
    "errors": ("data_quality", "errors", "Validation errors", False),
    "warnings": ("data_quality", "warnings", "Validation warnings", False),
    "rows": ("workbook", "rows", "Rows", False),
    "columns": ("workbook", "columns", "Columns", False),
    "sheets": ("workbook", "sheets", "Sheets", False),
}


def activity_schema_payload(stats: dict[str, Any]) -> dict[str, Any]:
    """A plain-dict, LLM-facing view of every allow-listed repository
    activity/governance metric and its REAL current value -- shown as
    evidence alongside the table schema so the model can choose to answer
    from either domain."""
    metrics = []
    for key, (section, field_name, label, is_percentage) in ACTIVITY_METRICS.items():
        value = (stats.get(section) or {}).get(field_name)
        if value is None:
            continue
        metrics.append({"name": key, "label": label, "value": value, "is_percentage": is_percentage})
    return {"metrics": metrics}


def validate_activity_spec(raw: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed validation for the REPOSITORY_ACTIVITY domain: every
    metric name must be in the fixed ACTIVITY_METRICS allow-list -- never a
    field name the model invented."""
    names = raw.get("metrics") or []
    if not isinstance(names, list) or not names:
        raise QuerySpecError("REPOSITORY_ACTIVITY requires at least one metric name")
    resolved: list[str] = []
    for name in names[:_MAX_ACTIVITY_METRICS]:
        key = str(name).strip().lower()
        if key not in ACTIVITY_METRICS:
            raise QuerySpecError(f"unknown activity metric {name!r} -- must be one of {sorted(ACTIVITY_METRICS)}")
        resolved.append(key)
    chart_type = str(raw.get("chart_type") or "BAR").upper()
    if chart_type not in CHART_TYPES:
        raise QuerySpecError(f"unknown chart_type {chart_type!r} -- must be one of {sorted(CHART_TYPES)}")
    return {"metrics": resolved, "chart_type": chart_type}


def build_activity_rows(metric_names: list[str], stats: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key in metric_names:
        section, field_name, label, is_percentage = ACTIVITY_METRICS[key]
        value = (stats.get(section) or {}).get(field_name)
        rows.append({"group_key": label, "value": value, "unit": "%" if is_percentage else ""})
    return rows
_RESULT_ROW_LIMIT = 50


class QuerySpecError(ValueError):
    """Raised with a precise, user-facing reason -- never silently caught
    and turned into a generic message. Callers feed this back to the model
    as a repair instruction (see insights_agent.py)."""


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    sql_type: str
    is_numeric: bool
    sample_values: list[Any] = field(default_factory=list)
    distinct_count: int | None = None


@dataclass(frozen=True)
class TableSchema:
    table_id: str
    columns: list[ColumnInfo]
    row_count: int

    def column_names(self) -> set[str]:
        return {column.name for column in self.columns}

    def find(self, name: str) -> ColumnInfo | None:
        target = name.strip().upper()
        return next((column for column in self.columns if column.name.upper() == target), None)


@dataclass(frozen=True)
class Filter:
    column: str
    op: str
    value: Any


@dataclass(frozen=True)
class QuerySpec:
    columns: list[str]           # non-aggregated columns to read (e.g. for TABLE_ONLY)
    aggregation: str | None      # one of AGGREGATIONS, or None for a raw row listing
    aggregation_column: str | None
    group_by: str | None
    filters: list[Filter]
    time_bucket: str | None
    chart_type: str


def discover_schema(table_id: str) -> TableSchema:
    """Reads the live schema of a physical repository table -- one bulk
    PRAGMA + one bulk COUNT + a bounded sample per low-cardinality column,
    never a per-row reconstruction."""
    safe_table = SafeIdentifier(table_id)
    conn = database._get_connection()
    try:
        raw_columns = conn.execute(f'PRAGMA table_info("{safe_table}")').fetchall()
        if not raw_columns:
            raise QuerySpecError(f"Table {table_id} does not exist")
        row_count = conn.execute(f'SELECT COUNT(*) FROM "{safe_table}"').fetchone()[0]
        columns: list[ColumnInfo] = []
        for raw in raw_columns:
            name = raw["name"]
            if name in ("ROW_ID",):
                continue
            declared_type = (raw["type"] or "").upper()
            is_numeric = any(marker in declared_type for marker in ("INT", "REAL", "FLOA", "DOUB", "NUM"))
            distinct_count = None
            sample_values: list[Any] = []
            if not is_numeric:
                safe_column = SafeIdentifier(name)
                distinct_count = conn.execute(
                    f'SELECT COUNT(DISTINCT "{safe_column}") FROM "{safe_table}"'
                ).fetchone()[0]
                if distinct_count and distinct_count <= _CATEGORICAL_CARDINALITY_LIMIT:
                    sample_values = [
                        row[0] for row in conn.execute(
                            f'SELECT DISTINCT "{safe_column}" FROM "{safe_table}" '
                            f'WHERE "{safe_column}" IS NOT NULL LIMIT {_MAX_CATEGORICAL_SAMPLES}'
                        ).fetchall()
                    ]
            columns.append(ColumnInfo(
                name=name, sql_type=declared_type or "TEXT", is_numeric=is_numeric,
                sample_values=sample_values, distinct_count=distinct_count,
            ))
        return TableSchema(table_id=str(safe_table), columns=columns, row_count=row_count)
    finally:
        conn.close()


def schema_evidence_payload(schema: TableSchema) -> dict[str, Any]:
    """A plain-dict, LLM-facing description of the schema -- what the query
    planning prompt actually shows the model."""
    return {
        "table_id": schema.table_id,
        "row_count": schema.row_count,
        "columns": [
            {
                "name": column.name,
                "type": "NUMBER" if column.is_numeric else "TEXT",
                **({"sample_values": column.sample_values} if column.sample_values else {}),
                **({"distinct_count": column.distinct_count} if column.distinct_count is not None else {}),
            }
            for column in schema.columns
        ],
    }


def validate_query_spec(raw: dict[str, Any], schema: TableSchema) -> QuerySpec:
    """Fail-closed validation of the model's proposed query plan against the
    REAL schema. Raises QuerySpecError with a specific, repair-able reason
    on any violation -- never silently substitutes or guesses."""
    columns_field = raw.get("columns") or []
    if not isinstance(columns_field, list):
        raise QuerySpecError("'columns' must be a list of column names")
    for name in columns_field:
        if not schema.find(str(name)):
            raise QuerySpecError(f"unknown column {name!r} -- must be one of {sorted(schema.column_names())}")

    aggregation = raw.get("aggregation")
    if aggregation is not None:
        aggregation = str(aggregation).upper()
        if aggregation not in AGGREGATIONS:
            raise QuerySpecError(f"unknown aggregation {aggregation!r} -- must be one of {sorted(AGGREGATIONS)}")

    aggregation_column = raw.get("aggregation_column")
    if aggregation and aggregation != "COUNT":
        if not aggregation_column:
            raise QuerySpecError(f"aggregation {aggregation!r} requires 'aggregation_column'")
        column = schema.find(str(aggregation_column))
        if not column:
            raise QuerySpecError(f"unknown aggregation_column {aggregation_column!r}")
        if aggregation in ("SUM", "AVG") and not column.is_numeric:
            raise QuerySpecError(f"{aggregation} requires a numeric column, but {aggregation_column!r} is not numeric")
        aggregation_column = column.name

    group_by = raw.get("group_by")
    if group_by:
        column = schema.find(str(group_by))
        if not column:
            raise QuerySpecError(f"unknown group_by column {group_by!r}")
        group_by = column.name

    filters: list[Filter] = []
    for entry in raw.get("filters") or []:
        if not isinstance(entry, dict):
            raise QuerySpecError("each filter must be an object with column/op/value")
        column = schema.find(str(entry.get("column", "")))
        if not column:
            raise QuerySpecError(f"unknown filter column {entry.get('column')!r}")
        op = str(entry.get("op", "")).upper() if str(entry.get("op", "")).upper() == "IN" else entry.get("op")
        if op not in FILTER_OPERATORS:
            raise QuerySpecError(f"unknown filter operator {entry.get('op')!r} -- must be one of {sorted(FILTER_OPERATORS)}")
        filters.append(Filter(column=column.name, op=op, value=entry.get("value")))

    time_bucket = raw.get("time_bucket")
    if time_bucket not in TIME_BUCKETS:
        raise QuerySpecError(f"unknown time_bucket {time_bucket!r} -- must be one of {sorted(b for b in TIME_BUCKETS if b)} or null")

    chart_type = str(raw.get("chart_type") or "TABLE_ONLY").upper()
    if chart_type not in CHART_TYPES:
        raise QuerySpecError(f"unknown chart_type {chart_type!r} -- must be one of {sorted(CHART_TYPES)}")

    return QuerySpec(
        columns=[schema.find(str(name)).name for name in columns_field],
        aggregation=aggregation, aggregation_column=aggregation_column, group_by=group_by,
        filters=filters, time_bucket=time_bucket, chart_type=chart_type,
    )


def compile_to_sql(spec: QuerySpec, schema: TableSchema) -> tuple[str, list[Any]]:
    """Compiles a validated QuerySpec into parametrized SQL. Every
    identifier embedded here has already been checked against the real
    schema by validate_query_spec -- values are always bound as
    parameters, never interpolated."""
    safe_table = SafeIdentifier(schema.table_id)
    select_parts: list[str] = []
    group_parts: list[str] = []
    params: list[Any] = []

    if spec.group_by:
        safe_group = SafeIdentifier(spec.group_by)
        select_parts.append(f'"{safe_group}" AS group_key')
        group_parts.append(f'"{safe_group}"')

    if spec.aggregation:
        if spec.aggregation == "COUNT":
            select_parts.append("COUNT(*) AS value")
        elif spec.aggregation == "COUNT_DISTINCT":
            safe_agg = SafeIdentifier(spec.aggregation_column)
            select_parts.append(f'COUNT(DISTINCT "{safe_agg}") AS value')
        else:
            safe_agg = SafeIdentifier(spec.aggregation_column)
            select_parts.append(f'{spec.aggregation}("{safe_agg}") AS value')
    elif spec.columns:
        select_parts.extend(f'"{SafeIdentifier(name)}"' for name in spec.columns)
    else:
        select_parts.append("*")

    where_parts: list[str] = []
    for item in spec.filters:
        safe_column = SafeIdentifier(item.column)
        if item.op == "IN":
            values = item.value if isinstance(item.value, list) else [item.value]
            placeholders = ",".join(["?"] * len(values))
            where_parts.append(f'"{safe_column}" IN ({placeholders})')
            params.extend(values)
        elif item.op == "BETWEEN":
            low, high = item.value
            where_parts.append(f'"{safe_column}" BETWEEN ? AND ?')
            params.extend([low, high])
        else:
            where_parts.append(f'"{safe_column}" {item.op} ?')
            params.append(item.value)

    sql = f'SELECT {", ".join(select_parts)} FROM "{safe_table}"'
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    if group_parts:
        sql += " GROUP BY " + ", ".join(group_parts)
        sql += " ORDER BY value DESC" if spec.aggregation else ""
    sql += f" LIMIT {_RESULT_ROW_LIMIT}"
    return sql, params


def execute(sql: str, params: list[Any]) -> list[dict[str, Any]]:
    conn = database._get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]
    finally:
        conn.close()


def linear_trend(rows: list[dict[str, Any]], key_field: str = "group_key", value_field: str = "value") -> dict[str, Any] | None:
    """A simple, disclosed linear-trend estimate over an ordered series --
    no external ML dependency, no invented numbers. Returns None when there
    aren't enough points to say anything meaningful."""
    points = [
        (index, float(row[value_field])) for index, row in enumerate(rows)
        if row.get(value_field) is not None
    ]
    if len(points) < 3:
        return None
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if denominator == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
    intercept = mean_y - slope * mean_x
    next_index = n
    projected = slope * next_index + intercept
    direction = "up" if slope > 0 else "down" if slope < 0 else "flat"
    return {
        "method": "linear_trend",
        "slope": round(slope, 4),
        "direction": direction,
        "next_period_projection": round(projected, 4),
        "labels": [str(row.get(key_field)) for row in rows],
        "disclaimer": "AI-assisted trend estimate from a simple linear fit over the computed series -- not a trained forecasting model.",
    }
