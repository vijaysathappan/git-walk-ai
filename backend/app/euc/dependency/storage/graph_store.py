"""Hybrid SQLite/object-store persistence for deterministic dependency graphs."""

import hashlib
import json
import uuid
from typing import Any

from ....config import settings
from ....database import _utcnow
from ....services.semantic_ledger_service import ledger_for_connection
from ..graph.models import DependencyGraph


class GraphStore:
    def __init__(self, conn):
        self.conn = conn
        self.ledger = ledger_for_connection(conn)

    def persist(self, dependency_run_id: str, analysis_id: str, graph: DependencyGraph,
                ast_by_pattern: dict[str, dict[str, Any]], node_metrics: dict[str, dict],
                cycles: list[list[str]], sheet_edges: list[dict], summary: dict,
                pattern_breaks: list[dict], source_commit_id: str | None,
                euc_id: str) -> dict[str, Any]:
        upstream, downstream = graph.adjacency()
        ast_hashes = []
        for pattern_id, ast_record in sorted(ast_by_pattern.items()):
            stored = self.ledger.objects.put(self.conn, "FORMULA_AST", ast_record["ast"])
            ast_hashes.append(stored["object_hash"])
            self.conn.execute(
                """INSERT INTO EUC_FORMULA_ASTS
                   (DEPENDENCY_RUN_ID,PATTERN_ID,NORMALIZED_HASH,AST_OBJECT_HASH,AST_DEPTH,
                    FUNCTION_COUNT,REFERENCE_COUNT,RANGE_COUNT,DYNAMIC_REFERENCE_COUNT)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (dependency_run_id, pattern_id, ast_record["normalized_hash"],
                 stored["object_hash"], ast_record["ast_depth"], ast_record["function_count"],
                 ast_record["reference_count"], ast_record["range_count"], ast_record["dynamic_reference_count"]),
            )
        upstream_object = self.ledger.objects.put(self.conn, "DEPENDENCY_UPSTREAM_INDEX", upstream)
        downstream_object = self.ledger.objects.put(self.conn, "DEPENDENCY_DOWNSTREAM_INDEX", downstream)
        metrics_object = self.ledger.objects.put(self.conn, "DEPENDENCY_NODE_METRICS", node_metrics)
        sheet_object = self.ledger.objects.put(self.conn, "DEPENDENCY_SHEET_GRAPH", sheet_edges)
        breaks_object = self.ledger.objects.put(self.conn, "FORMULA_PATTERN_BREAKS", pattern_breaks)
        cycle_records = []
        cycle_hashes = []
        for position, members in enumerate(cycles, 1):
            stored = self.ledger.objects.put(self.conn, "DEPENDENCY_CYCLE_MEMBERS", members)
            cycle_hashes.append(stored["object_hash"])
            sheets = sorted({graph.nodes[node].sheet_id for node in members if node in graph.nodes and graph.nodes[node].sheet_id})
            cycle_identity = f"{dependency_run_id}|{'|'.join(members)}"
            cycle_id = f"CYC_{hashlib.sha256(cycle_identity.encode()).hexdigest()[:20].upper()}"
            cycle_records.append((cycle_id, dependency_run_id, len(members),
                                  "HIGH" if len(members) > 10 else "MEDIUM" if len(members) > 2 else "LOW",
                                  stored["object_hash"], json.dumps(sheets)))
        self.conn.executemany("INSERT INTO EUC_DEPENDENCY_CYCLES VALUES (?,?,?,?,?,?)", cycle_records)

        for direction, adjacency, object_hash in (
            ("UPSTREAM", upstream, upstream_object["object_hash"]),
            ("DOWNSTREAM", downstream, downstream_object["object_hash"]),
        ):
            self.conn.execute(
                "INSERT INTO EUC_DEPENDENCY_BLOCKS VALUES (?,?,?,?,?,?,?,?)",
                (f"DBL_{uuid.uuid4().hex[:20].upper()}", dependency_run_id, None, direction,
                 len(adjacency), sum(len(values) for values in adjacency.values()), object_hash, _utcnow()),
            )

        node_rows = []
        for node_id, node in sorted(graph.nodes.items()):
            metric = node_metrics.get(node_id, {})
            node_rows.append((
                dependency_run_id, node_id, node.node_type, node.sheet_id, node.row_id,
                node.column_id, node.cell_address, node.display_name,
                metric.get("upstream_count", 0), metric.get("downstream_count", 0),
                metric.get("direct_upstream_count", 0), metric.get("direct_downstream_count", 0),
                metric.get("max_upstream_depth", 0), metric.get("max_downstream_depth", 0),
                metric.get("sheet_spread", 0), metric.get("hub_score", 0),
                metric.get("technical_criticality", 0), metric.get("node_role", "SOURCE"),
                metric.get("component_id"), json.dumps(node.metadata, default=str, sort_keys=True),
            ))
        for start in range(0, len(node_rows), 2000):
            self.conn.executemany(
                "INSERT INTO EUC_DEPENDENCY_NODES VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                node_rows[start:start + 2000],
            )

        indexed_edges = sorted(graph.edges.values(), key=lambda edge: edge.key)[:settings.dependency_sqlite_edge_limit]
        edge_rows = []
        for edge in indexed_edges:
            source = graph.nodes[edge.source_node_id]
            target = graph.nodes[edge.target_node_id]
            identity = "|".join((dependency_run_id, *edge.key))
            edge_rows.append((
                f"DGE_{hashlib.sha256(identity.encode()).hexdigest()[:24].upper()}",
                dependency_run_id, analysis_id, edge.source_node_id, source.node_type,
                edge.target_node_id, target.node_type, edge.dependency_type, edge.category,
                edge.resolution_status, edge.logical_cardinality, edge.metadata_json, _utcnow(),
            ))
        for start in range(0, len(edge_rows), 2000):
            self.conn.executemany(
                """INSERT INTO EUC_DEPENDENCY_EDGES VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                edge_rows[start:start + 2000],
            )
        self.conn.executemany(
            "INSERT INTO EUC_SHEET_DEPENDENCIES VALUES (?,?,?,?,?,?)",
            [(dependency_run_id, edge["source_sheet_id"], edge["target_sheet_id"], edge["edge_weight"],
              edge.get("source_role"), edge.get("target_role"))
             for edge in sheet_edges],
        )

        manifest = {
            "graph_version": settings.dependency_engine_version,
            "euc_id": euc_id, "source_commit_id": source_commit_id,
            "node_count": len(graph.nodes), "logical_edge_count": summary["logical_edge_count"],
            "upstream_index_hash": upstream_object["object_hash"],
            "downstream_index_hash": downstream_object["object_hash"],
            "sheet_graph_hash": sheet_object["object_hash"],
            "cycle_manifest_hashes": cycle_hashes,
            "metrics_manifest_hash": metrics_object["object_hash"],
            "pattern_breaks_hash": breaks_object["object_hash"],
            "formula_ast_hashes": sorted(ast_hashes), "summary": summary,
        }
        root = self.ledger.objects.put(self.conn, "DEPENDENCY_GRAPH_MANIFEST", manifest)
        child_hashes = [upstream_object["object_hash"], downstream_object["object_hash"],
                        metrics_object["object_hash"], sheet_object["object_hash"],
                        breaks_object["object_hash"], *cycle_hashes, *ast_hashes]
        for position, child_hash in enumerate(sorted(set(child_hashes))):
            self.conn.execute(
                "INSERT OR IGNORE INTO OBJECT_REFERENCES VALUES (?,?,?,?)",
                (root["object_hash"], child_hash, "DEPENDENCY_GRAPH_CHILD", position),
            )
        return {
            "graph_manifest_hash": root["object_hash"],
            "upstream_index_hash": upstream_object["object_hash"],
            "downstream_index_hash": downstream_object["object_hash"],
            "new_object_count": sum(1 for item in (upstream_object, downstream_object, metrics_object,
                                                     sheet_object, breaks_object, root) if item["created"]),
        }

    def load_adjacency(self, dependency_run_id: str, direction: str) -> dict[str, list[str]]:
        row = self.conn.execute(
            "SELECT OBJECT_HASH FROM EUC_DEPENDENCY_BLOCKS WHERE DEPENDENCY_RUN_ID=? AND DIRECTION=?",
            (dependency_run_id, direction.upper()),
        ).fetchone()
        if not row:
            raise KeyError("Dependency adjacency index does not exist")
        return self.ledger.objects.get(self.conn, row[0])
