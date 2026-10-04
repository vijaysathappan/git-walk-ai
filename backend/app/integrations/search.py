"""Security-trimmed information catalogue, search, and operations rollups."""

from __future__ import annotations

import json
from typing import Any

from .. import database
from ..access_control.engine import ResourceContext, authorization_engine


def index_item(conn, organization_id: str, item_type: str, item_id: str, title: str,
               description: str | None, search_values: list[Any], facets: dict[str, Any],
               security_resource_type: str = "ORGANIZATION", security_resource_id: str | None = None,
               freshness_at: str | None = None) -> None:
    now = database._utcnow(); catalogue_id = f"CATI_{item_type}_{item_id}"
    search_text = " ".join(str(value) for value in [title, description, *search_values] if value is not None).lower()
    conn.execute(
        """INSERT INTO INFORMATION_CATALOGUE
           (CATALOGUE_ID,ORGANIZATION_ID,ITEM_TYPE,ITEM_ID,TITLE,DESCRIPTION,SEARCH_TEXT,FACETS_JSON,SECURITY_RESOURCE_TYPE,SECURITY_RESOURCE_ID,FRESHNESS_AT,STATUS,UPDATED_AT)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,'ACTIVE',?)
           ON CONFLICT(ORGANIZATION_ID,ITEM_TYPE,ITEM_ID) DO UPDATE SET TITLE=excluded.TITLE,DESCRIPTION=excluded.DESCRIPTION,
             SEARCH_TEXT=excluded.SEARCH_TEXT,FACETS_JSON=excluded.FACETS_JSON,FRESHNESS_AT=excluded.FRESHNESS_AT,STATUS='ACTIVE',UPDATED_AT=excluded.UPDATED_AT""",
        (catalogue_id, organization_id, item_type, item_id, title, description, search_text,
         json.dumps(facets, sort_keys=True, default=str), security_resource_type, security_resource_id,
         freshness_at or now, now),
    )


def search_catalogue(organization_id: str, actor_id: str, query: str = "", item_type: str | None = None,
                     limit: int = 100) -> dict[str, Any]:
    authorization_engine.require(actor_id, "catalogue.read", ResourceContext("ORGANIZATION", organization_id, organization_id=organization_id))
    conn = database._get_connection()
    try:
        sql = "SELECT * FROM INFORMATION_CATALOGUE WHERE ORGANIZATION_ID=? AND STATUS='ACTIVE'"; params: list[Any] = [organization_id]
        if query.strip(): sql += " AND SEARCH_TEXT LIKE ?"; params.append(f"%{query.strip().lower()}%")
        if item_type: sql += " AND ITEM_TYPE=?"; params.append(item_type.upper())
        sql += " ORDER BY FRESHNESS_AT DESC,TITLE LIMIT ?"; params.append(min(limit, 500))
        items = []
        for row in conn.execute(sql, params):
            resource_type = row["SECURITY_RESOURCE_TYPE"]
            resource_id = row["SECURITY_RESOURCE_ID"] or organization_id
            resource = ResourceContext(resource_type, resource_id, organization_id=organization_id,
                                       repository_id=resource_id if resource_type == "REPOSITORY" else None)
            permission = "repository.read" if resource_type == "REPOSITORY" else "catalogue.read"
            if authorization_engine.authorize(actor_id, permission, resource, conn=conn).allowed:
                node = conn.execute(
                    "SELECT NODE_ID FROM DIGITAL_THREAD_NODES WHERE ORGANIZATION_ID=? AND EXTERNAL_KEY=? ORDER BY CREATED_AT DESC LIMIT 1",
                    (organization_id, row["ITEM_ID"]),
                ).fetchone()
                items.append({**{key.lower(): row[key] for key in row.keys()}, "facets": json.loads(row["FACETS_JSON"]),
                              "node_id": node[0] if node else None})
        facets: dict[str, dict[str, int]] = {}
        for item in items:
            for key, value in item["facets"].items(): facets.setdefault(key, {})[str(value)] = facets.setdefault(key, {}).get(str(value), 0) + 1
        return {"query": query, "count": len(items), "items": items, "facets": facets}
    finally: conn.close()


def operations_dashboard(organization_id: str, actor_id: str) -> dict[str, Any]:
    authorization_engine.require(actor_id, "integration.read", ResourceContext("ORGANIZATION", organization_id, organization_id=organization_id))
    conn = database._get_connection()
    try:
        connection = conn.execute("SELECT COUNT(*),SUM(CASE WHEN HEALTH_STATUS='HEALTHY' THEN 1 ELSE 0 END),SUM(CASE WHEN HEALTH_STATUS IN ('DEGRADED','UNHEALTHY') THEN 1 ELSE 0 END) FROM INTEGRATION_CONNECTIONS WHERE ORGANIZATION_ID=? AND STATUS='ACTIVE'", (organization_id,)).fetchone()
        runs = conn.execute("SELECT COUNT(*),SUM(CASE WHEN STATUS='COMPLETED' THEN 1 ELSE 0 END),SUM(CASE WHEN STATUS='FAILED' THEN 1 ELSE 0 END),COALESCE(SUM(RECORDS_READ),0),COALESCE(SUM(RECORDS_WRITTEN),0),COALESCE(SUM(BYTES_PROCESSED),0) FROM INTEGRATION_RUNS WHERE ORGANIZATION_ID=?", (organization_id,)).fetchone()
        canonical = conn.execute("SELECT COUNT(*),COALESCE(SUM(CURRENT_VERSION),0) FROM CANONICAL_OBJECTS WHERE ORGANIZATION_ID=? AND STATUS='ACTIVE'", (organization_id,)).fetchone()
        thread = conn.execute("SELECT (SELECT COUNT(*) FROM DIGITAL_THREAD_NODES WHERE ORGANIZATION_ID=?),(SELECT COUNT(*) FROM DIGITAL_THREAD_EDGES WHERE ORGANIZATION_ID=? AND VALID_TO IS NULL)", (organization_id, organization_id)).fetchone()
        queues = conn.execute("SELECT (SELECT COUNT(*) FROM CANONICAL_QUARANTINE WHERE ORGANIZATION_ID=? AND STATUS='OPEN'),(SELECT COUNT(*) FROM INTEGRATION_DEAD_LETTERS WHERE ORGANIZATION_ID=? AND STATUS='OPEN'),(SELECT COUNT(*) FROM INTEGRATION_CONFLICTS WHERE ORGANIZATION_ID=? AND STATUS='OPEN')", (organization_id, organization_id, organization_id)).fetchone()
        latest = [{key.lower(): row[key] for key in row.keys()} for row in conn.execute("SELECT RUN_ID,CONNECTION_ID,OPERATION,STATUS,STARTED_AT,COMPLETED_AT,RECORDS_READ,ERROR_COUNT,ERROR_CODE FROM INTEGRATION_RUNS WHERE ORGANIZATION_ID=? ORDER BY STARTED_AT DESC LIMIT 20", (organization_id,))]
        return {"connections": {"total": connection[0], "healthy": connection[1] or 0, "attention": connection[2] or 0},
                "runs": {"total": runs[0], "successful": runs[1] or 0, "failed": runs[2] or 0, "records_read": runs[3], "records_written": runs[4], "bytes": runs[5]},
                "canonical": {"objects": canonical[0], "versions": canonical[1]}, "thread": {"nodes": thread[0], "edges": thread[1]},
                "queues": {"quarantine": queues[0], "dead_letters": queues[1], "conflicts": queues[2]}, "latest_runs": latest}
    finally: conn.close()
