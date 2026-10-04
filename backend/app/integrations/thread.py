"""Temporal digital-thread graph for cross-system provenance and impact."""

from __future__ import annotations

import json
import uuid
from collections import deque
from typing import Any

from .. import database


def _id(prefix: str) -> str: return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def upsert_node(conn, organization_id: str, node_type: str, external_key: str, display_name: str,
                object_reference: str | None = None, attributes: dict[str, Any] | None = None) -> str:
    now = database._utcnow()
    existing = conn.execute("SELECT NODE_ID FROM DIGITAL_THREAD_NODES WHERE ORGANIZATION_ID=? AND NODE_TYPE=? AND EXTERNAL_KEY=?", (organization_id, node_type, external_key)).fetchone()
    if existing:
        conn.execute("UPDATE DIGITAL_THREAD_NODES SET DISPLAY_NAME=?,OBJECT_REFERENCE=?,ATTRIBUTES_JSON=? WHERE NODE_ID=?",
                     (display_name, object_reference, json.dumps(attributes or {}, sort_keys=True, default=str), existing[0]))
        return existing[0]
    node_id = _id("DTN")
    conn.execute("INSERT INTO DIGITAL_THREAD_NODES VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (node_id, organization_id, node_type, external_key, display_name, object_reference,
                  json.dumps(attributes or {}, sort_keys=True, default=str), now, None, now))
    return node_id


def link_nodes(conn, organization_id: str, source_node_id: str, target_node_id: str, edge_type: str,
               confidence: float = 1.0, evidence_type: str = "DETERMINISTIC",
               evidence_reference: str | None = None, attributes: dict[str, Any] | None = None) -> str:
    now = database._utcnow(); edge_id = _id("DTE")
    conn.execute(
        """INSERT INTO DIGITAL_THREAD_EDGES
           (EDGE_ID,ORGANIZATION_ID,SOURCE_NODE_ID,TARGET_NODE_ID,EDGE_TYPE,CONFIDENCE,EVIDENCE_TYPE,EVIDENCE_REFERENCE,ATTRIBUTES_JSON,VALID_FROM,VALID_TO,CREATED_AT)
           VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?) ON CONFLICT(ORGANIZATION_ID,SOURCE_NODE_ID,TARGET_NODE_ID,EDGE_TYPE,EVIDENCE_REFERENCE)
           DO UPDATE SET CONFIDENCE=excluded.CONFIDENCE,ATTRIBUTES_JSON=excluded.ATTRIBUTES_JSON""",
        (edge_id, organization_id, source_node_id, target_node_id, edge_type, max(0.0, min(1.0, confidence)),
         evidence_type, evidence_reference, json.dumps(attributes or {}, sort_keys=True, default=str), now, now),
    )
    row = conn.execute("SELECT EDGE_ID FROM DIGITAL_THREAD_EDGES WHERE ORGANIZATION_ID=? AND SOURCE_NODE_ID=? AND TARGET_NODE_ID=? AND EDGE_TYPE=? AND EVIDENCE_REFERENCE IS ?",
                       (organization_id, source_node_id, target_node_id, edge_type, evidence_reference)).fetchone()
    return row[0] if row else edge_id


def record_thread_event(conn, organization_id: str, node_id: str, event_type: str, *, actor_id: str | None = None,
                        source_system: str | None = None, evidence_reference: str | None = None,
                        payload: dict[str, Any] | None = None, event_time: str | None = None) -> str:
    event_id = _id("DTV"); now = database._utcnow()
    conn.execute("INSERT INTO DIGITAL_THREAD_EVENTS VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (event_id, organization_id, node_id, event_type, event_time or now, actor_id, source_system,
                  evidence_reference, json.dumps(payload or {}, sort_keys=True, default=str), now))
    return event_id


def thread_graph(organization_id: str, node_id: str, depth: int = 3, limit: int = 500) -> dict[str, Any]:
    depth = max(0, min(depth, 8)); limit = max(1, min(limit, 2000)); conn = database._get_connection()
    try:
        root = conn.execute("SELECT * FROM DIGITAL_THREAD_NODES WHERE NODE_ID=? AND ORGANIZATION_ID=?", (node_id, organization_id)).fetchone()
        if not root: raise KeyError("Digital-thread node does not exist")
        visited = {node_id: 0}; queue = deque([node_id]); edge_rows = []
        while queue and len(visited) < limit:
            current = queue.popleft(); current_depth = visited[current]
            if current_depth >= depth: continue
            edges = conn.execute("SELECT * FROM DIGITAL_THREAD_EDGES WHERE ORGANIZATION_ID=? AND VALID_TO IS NULL AND (SOURCE_NODE_ID=? OR TARGET_NODE_ID=?) ORDER BY CREATED_AT", (organization_id, current, current)).fetchall()
            for edge in edges:
                edge_rows.append(edge); neighbor = edge["TARGET_NODE_ID"] if edge["SOURCE_NODE_ID"] == current else edge["SOURCE_NODE_ID"]
                if neighbor not in visited and len(visited) < limit: visited[neighbor] = current_depth + 1; queue.append(neighbor)
        placeholders = ",".join("?" for _ in visited)
        nodes = conn.execute(f"SELECT * FROM DIGITAL_THREAD_NODES WHERE NODE_ID IN ({placeholders})", tuple(visited)).fetchall()
        unique_edges = {row["EDGE_ID"]: row for row in edge_rows}
        return {"root_node_id": node_id, "depth": depth,
                "nodes": [{**{key.lower(): row[key] for key in row.keys()}, "attributes": json.loads(row["ATTRIBUTES_JSON"]), "depth": visited[row["NODE_ID"]]} for row in nodes],
                "edges": [{**{key.lower(): row[key] for key in row.keys()}, "attributes": json.loads(row["ATTRIBUTES_JSON"])} for row in unique_edges.values()]}
    finally: conn.close()


def node_timeline(organization_id: str, node_id: str, limit: int = 200) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        node = conn.execute("SELECT * FROM DIGITAL_THREAD_NODES WHERE NODE_ID=? AND ORGANIZATION_ID=?", (node_id, organization_id)).fetchone()
        if not node: raise KeyError("Digital-thread node does not exist")
        events = conn.execute("SELECT * FROM DIGITAL_THREAD_EVENTS WHERE NODE_ID=? AND ORGANIZATION_ID=? ORDER BY EVENT_TIME DESC LIMIT ?", (node_id, organization_id, min(limit, 500))).fetchall()
        return {"node": {key.lower(): node[key] for key in node.keys()},
                "events": [{**{key.lower(): row[key] for key in row.keys()}, "payload": json.loads(row["PAYLOAD_JSON"])} for row in events]}
    finally: conn.close()


def impact(organization_id: str, node_id: str, depth: int = 5) -> dict[str, Any]:
    graph = thread_graph(organization_id, node_id, depth)
    downstream_types = {"PRODUCED", "MAPS_TO", "UPDATED", "GENERATED", "TRIGGERED", "DEPENDS_ON", "RELATED_TO"}
    adjacency: dict[str, list[str]] = {}
    for edge in graph["edges"]:
        if edge["edge_type"] in downstream_types: adjacency.setdefault(edge["source_node_id"], []).append(edge["target_node_id"])
    visited = {node_id: 0}; queue = deque([node_id])
    while queue:
        current = queue.popleft()
        for target in adjacency.get(current, []):
            if target not in visited: visited[target] = visited[current] + 1; queue.append(target)
    nodes = [node for node in graph["nodes"] if node["node_id"] in visited and node["node_id"] != node_id]
    return {"node_id": node_id, "affected_count": len(nodes), "maximum_depth": max(visited.values(), default=0),
            "by_type": {node_type: sum(1 for node in nodes if node["node_type"] == node_type) for node_type in sorted({node["node_type"] for node in nodes})},
            "affected_nodes": sorted(nodes, key=lambda node: (visited[node["node_id"]], node["display_name"]))}
