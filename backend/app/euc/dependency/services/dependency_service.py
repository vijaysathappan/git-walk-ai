"""Stage 2.2 graph orchestration and bounded query services."""

from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from typing import Any

from .... import database
from ....config import settings
from ....observability import record_audit_event, record_metric
from ....services.semantic_ledger_service import ledger_for_connection
from ...service import _repository_access
from ..graph import DependencyGraphBuilder, ResolverCatalog
from ..graph.analytics import calculate_node_metrics, graph_summary, sheet_graph
from ..graph.cycles import find_cycles
from ..graph.traversal import traverse
from ..storage import GraphStore


def _run_id() -> str:
    return f"DEP_{uuid.uuid4().hex[:20].upper()}"


def _json(row, name: str, fallback):
    return json.loads(row[name] or json.dumps(fallback))


class DependencyService:
    def build(self, euc_id: str, user_id: str) -> dict[str, Any]:
        conn = database._get_connection()
        run_id = _run_id()
        started = time.perf_counter()
        asset = None
        try:
            asset = conn.execute(
                """SELECT A.*,X.SOURCE_COMMIT_ID,X.STATUS AS ANALYSIS_STATUS
                   FROM EUC_ASSETS A LEFT JOIN EUC_ANALYSIS_RUNS X ON X.ANALYSIS_ID=A.LATEST_ANALYSIS_ID
                   WHERE A.EUC_ID=?""",
                (euc_id,),
            ).fetchone()
            if not asset:
                raise KeyError("EUC asset does not exist")
            _repository_access(conn, asset["REPOSITORY_ID"], user_id, edit=True)
            if not asset["LATEST_ANALYSIS_ID"] or asset["ANALYSIS_STATUS"] not in {"COMPLETED", "COMPLETED_WITH_WARNINGS"}:
                raise ValueError("A completed Stage 2.1 inventory is required before dependency analysis")
            now = database._utcnow()
            conn.execute(
                """INSERT INTO EUC_DEPENDENCY_RUNS
                   (DEPENDENCY_RUN_ID,ANALYSIS_ID,EUC_ID,SOURCE_COMMIT_ID,ENGINE_VERSION,
                    STATUS,PROGRESS,CURRENT_STEP,STARTED_AT)
                   VALUES (?,?,?,?,?,'PARSING_FORMULAS',10,'FORMULA_PARSER',?)""",
                (run_id, asset["LATEST_ANALYSIS_ID"], euc_id, asset["SOURCE_COMMIT_ID"],
                 settings.dependency_engine_version, now),
            )
            conn.commit()
            record_audit_event("EUC_DEPENDENCY_ANALYSIS_STARTED", actor_user_id=user_id,
                               repository_id=asset["REPOSITORY_ID"],
                               payload={"euc_id": euc_id, "dependency_run_id": run_id,
                                        "engine_version": settings.dependency_engine_version})
            catalog = self._catalog(conn, asset, user_id)
            occurrences = [dict(row) for row in conn.execute(
                """SELECT O.PATTERN_ID AS pattern_id,O.SHEET_ID AS sheet_id,
                          O.CELL_ADDRESS AS cell_address,O.RAW_FORMULA AS raw_formula,
                          P.NORMALIZED_HASH AS normalized_hash
                   FROM EUC_FORMULA_OCCURRENCES O JOIN EUC_FORMULA_PATTERNS P
                     ON P.PATTERN_ID=O.PATTERN_ID
                   WHERE O.ANALYSIS_ID=? ORDER BY O.SHEET_ID,O.CELL_ADDRESS""",
                (asset["LATEST_ANALYSIS_ID"],),
            )]
            conn.execute("UPDATE EUC_DEPENDENCY_RUNS SET STATUS='RESOLVING_REFERENCES',PROGRESS=25,CURRENT_STEP='REFERENCE_RESOLVER' WHERE DEPENDENCY_RUN_ID=?", (run_id,))
            conn.commit()
            graph, ast_by_pattern, pattern_breaks = DependencyGraphBuilder(catalog).build(occurrences)
            self._timeout(started)
            conn.execute("UPDATE EUC_DEPENDENCY_RUNS SET STATUS='DETECTING_CYCLES',PROGRESS=55,CURRENT_STEP='TARJAN_SCC' WHERE DEPENDENCY_RUN_ID=?", (run_id,))
            conn.commit()
            upstream, _ = graph.adjacency()
            cycles = find_cycles(upstream)
            conn.execute("UPDATE EUC_DEPENDENCY_RUNS SET STATUS='CALCULATING_METRICS',PROGRESS=68,CURRENT_STEP='GRAPH_ANALYTICS' WHERE DEPENDENCY_RUN_ID=?", (run_id,))
            conn.commit()
            node_metrics = calculate_node_metrics(
                graph, settings.dependency_max_depth, settings.dependency_max_response_nodes
            )
            sheet_edges = sheet_graph(graph)
            summary = graph_summary(graph, node_metrics, cycles)
            summary["formula_pattern_breaks"] = len(pattern_breaks)
            summary["parse_warning_count"] = len(graph.warnings)
            summary["static_resolvability"] = self._resolvability(summary)
            sheet_roles = self._sheet_roles(conn, asset["LATEST_ANALYSIS_ID"], sheet_edges)
            for edge in sheet_edges:
                edge["source_role"] = sheet_roles.get(edge["source_sheet_id"], "MIXED")
                edge["target_role"] = sheet_roles.get(edge["target_sheet_id"], "MIXED")
            conn.execute("UPDATE EUC_DEPENDENCY_RUNS SET STATUS='PERSISTING',PROGRESS=82,CURRENT_STEP='GRAPH_OBJECTS' WHERE DEPENDENCY_RUN_ID=?", (run_id,))
            store = GraphStore(conn)
            persisted = store.persist(run_id, asset["LATEST_ANALYSIS_ID"], graph, ast_by_pattern,
                                      node_metrics, cycles, sheet_edges, summary, pattern_breaks,
                                      asset["SOURCE_COMMIT_ID"], euc_id)
            duration_ms = round((time.perf_counter() - started) * 1000)
            status = "COMPLETED_WITH_WARNINGS" if graph.warnings else "COMPLETED"
            conn.execute(
                """UPDATE EUC_DEPENDENCY_RUNS SET STATUS=?,PROGRESS=100,CURRENT_STEP='COMPLETED',
                   NODE_COUNT=?,EDGE_COUNT=?,LOGICAL_EDGE_COUNT=?,CYCLE_COUNT=?,UNRESOLVED_COUNT=?,
                   DYNAMIC_COUNT=?,BROKEN_COUNT=?,GRAPH_MANIFEST_HASH=?,UPSTREAM_INDEX_HASH=?,
                   DOWNSTREAM_INDEX_HASH=?,METRICS_JSON=?,WARNINGS_JSON=?,COMPLETED_AT=?,DURATION_MS=?
                   WHERE DEPENDENCY_RUN_ID=?""",
                (status, summary["node_count"], summary["edge_count"], summary["logical_edge_count"],
                 summary["cycle_count"], summary["unresolved_count"], summary["dynamic_count"],
                 summary["broken_count"], persisted["graph_manifest_hash"], persisted["upstream_index_hash"],
                 persisted["downstream_index_hash"], json.dumps(summary, sort_keys=True),
                 json.dumps(graph.warnings, sort_keys=True), database._utcnow(), duration_ms, run_id),
            )
            conn.commit()
            record_metric("dependency_analysis_duration", duration_ms, "ms", user_id=user_id,
                          repository_id=asset["REPOSITORY_ID"], status=status,
                          tags={"nodes": summary["node_count"], "edges": summary["edge_count"]})
            record_audit_event("EUC_DEPENDENCY_ANALYSIS_COMPLETED", actor_user_id=user_id,
                               repository_id=asset["REPOSITORY_ID"],
                               payload={"euc_id": euc_id, "dependency_run_id": run_id,
                                        "graph_manifest_hash": persisted["graph_manifest_hash"],
                                        "summary": summary})
            return self.overview(euc_id, user_id, run_id)
        except Exception as exc:
            conn.rollback()
            try:
                conn.execute(
                    "UPDATE EUC_DEPENDENCY_RUNS SET STATUS='FAILED',CURRENT_STEP='FAILED',COMPLETED_AT=?,WARNINGS_JSON=? WHERE DEPENDENCY_RUN_ID=?",
                    (database._utcnow(), json.dumps([{"code": "DEPENDENCY_BUILD_FAILED", "message": str(exc)}]), run_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
            record_audit_event("EUC_DEPENDENCY_ANALYSIS_FAILED", actor_user_id=user_id,
                               repository_id=asset["REPOSITORY_ID"] if asset else None,
                               payload={"euc_id": euc_id, "dependency_run_id": run_id},
                               status="FAILED", failure_reason=str(exc))
            raise
        finally:
            conn.close()

    def overview(self, euc_id: str, user_id: str, run_id: str | None = None) -> dict[str, Any]:
        conn = database._get_connection()
        try:
            asset = self._asset_access(conn, euc_id, user_id)
            run = self._run(conn, euc_id, run_id)
            if not run:
                return {"euc_id": euc_id, "status": "NOT_BUILT", "freshness": "MISSING"}
            current_head = conn.execute(
                """SELECT B.HEAD_COMMIT_ID FROM WORKBOOK_REPOSITORIES R
                   LEFT JOIN BRANCHES B ON B.BRANCH_ID=R.DEFAULT_BRANCH_ID WHERE R.REPOSITORY_ID=?""",
                (asset["REPOSITORY_ID"],),
            ).fetchone()
            freshness = "CURRENT" if run["SOURCE_COMMIT_ID"] == (current_head[0] if current_head else None) else "STALE"
            hotspots = self._hotspots(conn, run["DEPENDENCY_RUN_ID"], 10)
            return {
                "euc_id": euc_id, "dependency_run_id": run["DEPENDENCY_RUN_ID"],
                "analysis_id": run["ANALYSIS_ID"], "source_commit_id": run["SOURCE_COMMIT_ID"],
                "current_head_commit_id": current_head[0] if current_head else None,
                "engine_version": run["ENGINE_VERSION"], "status": run["STATUS"],
                "progress": run["PROGRESS"], "current_step": run["CURRENT_STEP"],
                "freshness": freshness, "graph_manifest_hash": run["GRAPH_MANIFEST_HASH"],
                "duration_ms": run["DURATION_MS"], "metrics": _json(run, "METRICS_JSON", {}),
                "warnings": _json(run, "WARNINGS_JSON", []), "hotspots": hotspots,
            }
        finally:
            conn.close()

    def lineage(self, euc_id: str, user_id: str, node_id: str, direction: str, depth: int) -> dict[str, Any]:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id)
            run = self._required_run(conn, euc_id)
            store = GraphStore(conn)
            directions = [direction.upper()] if direction != "both" else ["UPSTREAM", "DOWNSTREAM"]
            result = {"node": self._node(conn, run["DEPENDENCY_RUN_ID"], node_id), "directions": {}}
            for item in directions:
                traversal = traverse(store.load_adjacency(run["DEPENDENCY_RUN_ID"], item), node_id,
                                     min(depth, settings.dependency_max_depth), settings.dependency_max_response_nodes)
                nodes = self._nodes(conn, run["DEPENDENCY_RUN_ID"], list(traversal["distances"]))
                result["directions"][item.lower()] = {
                    **traversal, "nodes": [dict(node, depth=traversal["distances"].get(node["node_id"])) for node in nodes]
                }
            return result
        finally:
            conn.close()

    def impact(self, euc_id: str, user_id: str, node_id: str, max_depth: int) -> dict[str, Any]:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id)
            run = self._required_run(conn, euc_id)
            adjacency = GraphStore(conn).load_adjacency(run["DEPENDENCY_RUN_ID"], "DOWNSTREAM")
            result = traverse(adjacency, node_id, min(max_depth, settings.dependency_max_depth),
                              settings.dependency_max_response_nodes)
            nodes = self._nodes(conn, run["DEPENDENCY_RUN_ID"], list(result["distances"]))
            direct = sum(depth == 1 for depth in result["distances"].values())
            affected_sheets = sorted({node["sheet_id"] for node in nodes if node["sheet_id"]})
            external = sum(node["node_type"] == "EXTERNAL_WORKBOOK" for node in nodes)
            source = self._node(conn, run["DEPENDENCY_RUN_ID"], node_id)
            return {
                "node": source, "direct_dependents": direct,
                "indirect_dependents": max(0, len(result["distances"]) - direct),
                "total_downstream": len(result["distances"]), "affected_sheets": affected_sheets,
                "affected_sheet_count": len(affected_sheets), "affected_workbooks": external,
                "maximum_depth": result["maximum_depth"], "truncated": result["truncated"],
                "technical_criticality": source["technical_criticality"],
                "nodes": [dict(node, depth=result["distances"].get(node["node_id"])) for node in nodes[:250]],
            }
        finally:
            conn.close()

    def sheets(self, euc_id: str, user_id: str) -> dict[str, Any]:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id)
            run = self._required_run(conn, euc_id)
            nodes = [dict(row) for row in conn.execute(
                """SELECT S.SHEET_ID AS sheet_id,S.SHEET_NAME AS name,S.FORMULA_CELL_COUNT AS formula_count,
                          S.USED_CELL_COUNT AS used_cells
                   FROM EUC_SHEET_INVENTORY S WHERE S.ANALYSIS_ID=? ORDER BY S.SHEET_POSITION""",
                (run["ANALYSIS_ID"],),
            )]
            edges = [dict(row) for row in conn.execute(
                "SELECT * FROM EUC_SHEET_DEPENDENCIES WHERE DEPENDENCY_RUN_ID=? ORDER BY EDGE_WEIGHT DESC",
                (run["DEPENDENCY_RUN_ID"],),
            )]
            role_map = {}
            for edge in edges:
                role_map.setdefault(edge["SOURCE_SHEET_ID"], edge["SOURCE_ROLE"])
                role_map.setdefault(edge["TARGET_SHEET_ID"], edge["TARGET_ROLE"])
            return {"nodes": [dict(node, role=role_map.get(node["sheet_id"], "MIXED")) for node in nodes],
                    "edges": [{key.lower(): row[key] for key in row} for row in edges]}
        finally:
            conn.close()

    def hotspots(self, euc_id: str, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id)
            run = self._required_run(conn, euc_id)
            return self._hotspots(conn, run["DEPENDENCY_RUN_ID"], limit)
        finally:
            conn.close()

    def cycles(self, euc_id: str, user_id: str) -> list[dict[str, Any]]:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id)
            run = self._required_run(conn, euc_id)
            ledger = ledger_for_connection(conn)
            result = []
            for row in conn.execute("SELECT * FROM EUC_DEPENDENCY_CYCLES WHERE DEPENDENCY_RUN_ID=? ORDER BY CYCLE_SIZE DESC", (run["DEPENDENCY_RUN_ID"],)):
                result.append({"cycle_id": row["CYCLE_ID"], "cycle_size": row["CYCLE_SIZE"],
                               "severity": row["SEVERITY"], "sheets": json.loads(row["SHEETS_JSON"]),
                               "nodes": ledger.objects.get(conn, row["NODE_MANIFEST_HASH"])})
            return result
        finally:
            conn.close()

    def broken(self, euc_id: str, user_id: str, limit: int = 200) -> list[dict[str, Any]]:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id)
            run = self._required_run(conn, euc_id)
            return [{key.lower(): row[key] for key in row.keys()} for row in conn.execute(
                """SELECT E.*,N.DISPLAY_NAME AS SOURCE_NAME,N.CELL_ADDRESS,N.SHEET_ID
                   FROM EUC_DEPENDENCY_EDGES E JOIN EUC_DEPENDENCY_NODES N
                     ON N.DEPENDENCY_RUN_ID=E.DEPENDENCY_RUN_ID AND N.NODE_ID=E.SOURCE_NODE_ID
                   WHERE E.DEPENDENCY_RUN_ID=? AND E.RESOLUTION_STATUS!='RESOLVED'
                   ORDER BY N.SHEET_ID,N.CELL_ADDRESS LIMIT ?""",
                (run["DEPENDENCY_RUN_ID"], min(limit, 500)),
            )]
        finally:
            conn.close()

    def search(self, euc_id: str, user_id: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id)
            run = self._required_run(conn, euc_id)
            rows = conn.execute(
                """SELECT * FROM EUC_DEPENDENCY_NODES WHERE DEPENDENCY_RUN_ID=?
                   AND (DISPLAY_NAME LIKE ? OR CELL_ADDRESS LIKE ? OR NODE_ID LIKE ?)
                   ORDER BY TECHNICAL_CRITICALITY DESC LIMIT ?""",
                (run["DEPENDENCY_RUN_ID"], f"%{query}%", f"%{query}%", f"%{query}%", min(limit, 100)),
            )
            return [self._node_dict(row) for row in rows]
        finally:
            conn.close()

    def resolve_cell_node(self, euc_id: str, user_id: str, sheet_id: str, cell_address: str) -> dict[str, Any]:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id)
            run = self._required_run(conn, euc_id)
            row = conn.execute(
                "SELECT * FROM EUC_DEPENDENCY_NODES WHERE DEPENDENCY_RUN_ID=? AND SHEET_ID=? AND CELL_ADDRESS=?",
                (run["DEPENDENCY_RUN_ID"], sheet_id, cell_address.replace("$", "").upper()),
            ).fetchone()
            if not row:
                raise KeyError("Cell is not represented in the current dependency graph")
            return self._node_dict(row)
        finally:
            conn.close()

    def _catalog(self, conn, asset, user_id: str) -> ResolverCatalog:
        analysis_id = asset["LATEST_ANALYSIS_ID"]
        sheets = conn.execute("SELECT * FROM EUC_SHEET_INVENTORY WHERE ANALYSIS_ID=?", (analysis_id,)).fetchall()
        sheets_by_name = {row["SHEET_NAME"].casefold(): row["SHEET_ID"] for row in sheets}
        sheet_names = {row["SHEET_ID"]: row["SHEET_NAME"] for row in sheets}
        dimensions = {row["SHEET_ID"]: (max(1, row["MAX_ROW"] or 1), max(1, row["MAX_COLUMN"] or 1)) for row in sheets}
        stable_rows, stable_columns = {}, {}
        repository = conn.execute("SELECT DEFAULT_BRANCH_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (asset["REPOSITORY_ID"],)).fetchone()
        if repository and repository[0]:
            for row in conn.execute("SELECT SHEET_ID,ROW_ID,ROW_POSITION FROM SHEET_ROWS WHERE BRANCH_ID=? AND STATUS='ACTIVE'", (repository[0],)):
                stable_rows[(row["SHEET_ID"], int(row["ROW_POSITION"]) + 2)] = row["ROW_ID"]
            for row in conn.execute("SELECT SHEET_ID,COLUMN_ID,COLUMN_POSITION FROM SHEET_COLUMNS WHERE BRANCH_ID=? AND STATUS='ACTIVE'", (repository[0],)):
                stable_columns[(row["SHEET_ID"], int(row["COLUMN_POSITION"]) + 1)] = row["COLUMN_ID"]
        named_ranges, tables = {}, {}
        for row in conn.execute("SELECT * FROM EUC_OBJECT_INVENTORY WHERE ANALYSIS_ID=? AND OBJECT_TYPE IN ('NAMED_RANGE','TABLE')", (analysis_id,)):
            item = {"range": row["CELL_OR_RANGE"], "details": json.loads(row["DETAILS_JSON"] or "{}")}
            if row["OBJECT_TYPE"] == "NAMED_RANGE":
                named_ranges[(row["OBJECT_NAME"] or "").casefold()] = item
            else:
                tables[(row["SHEET_ID"], (row["OBJECT_NAME"] or "").casefold())] = item
        external_assets: dict[str, list[dict[str, str]]] = {}
        accessible = conn.execute(
            """SELECT A.EUC_ID,A.ORIGINAL_FILENAME,A.REPOSITORY_ID FROM EUC_ASSETS A
               JOIN WORKBOOK_REPOSITORIES R ON R.REPOSITORY_ID=A.REPOSITORY_ID
               LEFT JOIN REPOSITORY_MEMBERS M ON M.REPOSITORY_ID=R.REPOSITORY_ID AND M.USER_ID=?
               WHERE R.STATUS='ACTIVE' AND (R.CREATED_BY=? OR M.USER_ID=?)""",
            (user_id, user_id, user_id),
        )
        for row in accessible:
            external_assets.setdefault(row["ORIGINAL_FILENAME"].casefold(), []).append(
                {"euc_id": row["EUC_ID"], "repository_id": row["REPOSITORY_ID"]}
            )
        return ResolverCatalog(asset["REPOSITORY_ID"], asset["EUC_ID"], analysis_id,
                               sheets_by_name, sheet_names, dimensions, stable_rows,
                               stable_columns, named_ranges, tables, external_assets)

    @staticmethod
    def _resolvability(summary: dict) -> dict:
        total = max(1, summary["edge_count"])
        dynamic = summary["dynamic_count"]
        broken = summary["broken_count"]
        unresolved = max(0, summary["unresolved_count"] - dynamic - broken)
        fully = max(0, total - dynamic - broken - unresolved)
        return {"fully_resolvable": round(fully / total * 100, 2),
                "partial": round(unresolved / total * 100, 2),
                "dynamic": round(dynamic / total * 100, 2),
                "broken": round(broken / total * 100, 2)}

    @staticmethod
    def _sheet_roles(conn, analysis_id: str, sheet_edges: list[dict]) -> dict[str, str]:
        dependencies = Counter(edge["source_sheet_id"] for edge in sheet_edges)
        dependents = Counter(edge["target_sheet_id"] for edge in sheet_edges)
        roles = {}
        for row in conn.execute("SELECT * FROM EUC_SHEET_INVENTORY WHERE ANALYSIS_ID=?", (analysis_id,)):
            formulas = row["FORMULA_CELL_COUNT"] or 0
            constants = row["CONSTANT_CELL_COUNT"] or 0
            source_edges, target_edges = dependencies[row["SHEET_ID"]], dependents[row["SHEET_ID"]]
            if (row["CHART_COUNT"] or row["PIVOT_COUNT"]) and source_edges and not target_edges:
                role = "OUTPUT"
            elif constants > formulas * 4 and target_edges:
                role = "INPUT"
            elif row["TABLE_COUNT"] and formulas < constants / 4:
                role = "LOOKUP"
            elif formulas > constants:
                role = "CALCULATION"
            else:
                role = "MIXED"
            roles[row["SHEET_ID"]] = role
        return roles

    @staticmethod
    def _asset_access(conn, euc_id: str, user_id: str):
        asset = conn.execute("SELECT * FROM EUC_ASSETS WHERE EUC_ID=?", (euc_id,)).fetchone()
        if not asset:
            raise KeyError("EUC asset does not exist")
        _repository_access(conn, asset["REPOSITORY_ID"], user_id)
        return asset

    @staticmethod
    def _run(conn, euc_id: str, run_id: str | None = None):
        if run_id:
            return conn.execute("SELECT * FROM EUC_DEPENDENCY_RUNS WHERE EUC_ID=? AND DEPENDENCY_RUN_ID=?", (euc_id, run_id)).fetchone()
        return conn.execute(
            """SELECT * FROM EUC_DEPENDENCY_RUNS WHERE EUC_ID=? AND STATUS IN ('COMPLETED','COMPLETED_WITH_WARNINGS')
               ORDER BY STARTED_AT DESC,ROWID DESC LIMIT 1""", (euc_id,)
        ).fetchone()

    def _required_run(self, conn, euc_id: str):
        run = self._run(conn, euc_id)
        if not run:
            raise KeyError("Dependency graph has not been built")
        return run

    @staticmethod
    def _node_dict(row) -> dict[str, Any]:
        return {"node_id": row["NODE_ID"], "node_type": row["NODE_TYPE"],
                "sheet_id": row["SHEET_ID"], "row_id": row["ROW_ID"],
                "column_id": row["COLUMN_ID"], "cell_address": row["CELL_ADDRESS"],
                "display_name": row["DISPLAY_NAME"], "upstream_count": row["UPSTREAM_COUNT"],
                "downstream_count": row["DOWNSTREAM_COUNT"],
                "direct_upstream_count": row["DIRECT_UPSTREAM_COUNT"],
                "direct_downstream_count": row["DIRECT_DOWNSTREAM_COUNT"],
                "max_upstream_depth": row["MAX_UPSTREAM_DEPTH"],
                "max_downstream_depth": row["MAX_DOWNSTREAM_DEPTH"],
                "sheet_spread": row["SHEET_SPREAD"], "hub_score": row["HUB_SCORE"],
                "technical_criticality": row["TECHNICAL_CRITICALITY"],
                "node_role": row["NODE_ROLE"], "component_id": row["COMPONENT_ID"],
                "metadata": json.loads(row["METADATA_JSON"] or "{}")}

    def _node(self, conn, run_id: str, node_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM EUC_DEPENDENCY_NODES WHERE DEPENDENCY_RUN_ID=? AND NODE_ID=?", (run_id, node_id)).fetchone()
        if not row:
            raise KeyError("Dependency node does not exist")
        return self._node_dict(row)

    def _nodes(self, conn, run_id: str, node_ids: list[str]) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        result = []
        for start in range(0, len(node_ids), 500):
            batch = node_ids[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            result.extend(self._node_dict(row) for row in conn.execute(
                f"SELECT * FROM EUC_DEPENDENCY_NODES WHERE DEPENDENCY_RUN_ID=? AND NODE_ID IN ({placeholders})",
                (run_id, *batch),
            ))
        return sorted(result, key=lambda item: item["node_id"])

    def _hotspots(self, conn, run_id: str, limit: int) -> list[dict[str, Any]]:
        return [self._node_dict(row) for row in conn.execute(
            """SELECT * FROM EUC_DEPENDENCY_NODES WHERE DEPENDENCY_RUN_ID=?
               AND NODE_TYPE IN ('CELL','NAMED_RANGE','TABLE_COLUMN')
               ORDER BY TECHNICAL_CRITICALITY DESC,DOWNSTREAM_COUNT DESC LIMIT ?""",
            (run_id, min(limit, 200)),
        )]

    @staticmethod
    def _timeout(started: float) -> None:
        if time.perf_counter() - started > settings.dependency_analysis_timeout_seconds:
            raise TimeoutError("Dependency analysis exceeded the configured timeout")
