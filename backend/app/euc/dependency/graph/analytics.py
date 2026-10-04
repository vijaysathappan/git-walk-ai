"""Deterministic graph metrics and technical criticality (not business risk)."""

import math
from collections import Counter, deque

from .models import DependencyGraph
from .traversal import traverse


def weak_components(upstream: dict[str, list[str]], downstream: dict[str, list[str]]) -> dict[str, str]:
    unseen = set(upstream) | set(downstream)
    assignments = {}
    number = 0
    while unseen:
        number += 1
        root = min(unseen)
        queue = deque([root])
        unseen.remove(root)
        while queue:
            node = queue.popleft()
            assignments[node] = f"COMPONENT_{number}"
            for neighbor in upstream.get(node, []) + downstream.get(node, []):
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    queue.append(neighbor)
    return assignments


def calculate_node_metrics(graph: DependencyGraph, max_depth: int, traversal_cap: int) -> dict[str, dict]:
    upstream, downstream = graph.adjacency()
    components = weak_components(upstream, downstream)
    raw: dict[str, dict] = {}
    max_downstream = max_depth_value = max_spread = 1
    for node_id, node in graph.nodes.items():
        up = traverse(upstream, node_id, max_depth, traversal_cap)
        down = traverse(downstream, node_id, max_depth, traversal_cap)
        downstream_sheets = {
            graph.nodes[target].sheet_id for target in down["distances"]
            if target in graph.nodes and graph.nodes[target].sheet_id
        }
        direct_up = len(upstream.get(node_id, []))
        direct_down = len(downstream.get(node_id, []))
        role = "TRANSFORM" if direct_up and direct_down else "OUTPUT" if direct_up else "SOURCE"
        hub = math.log1p(len(up["distances"])) * math.log1p(len(down["distances"]))
        raw[node_id] = {
            "upstream_count": len(up["distances"]), "downstream_count": len(down["distances"]),
            "direct_upstream_count": direct_up, "direct_downstream_count": direct_down,
            "max_upstream_depth": up["maximum_depth"], "max_downstream_depth": down["maximum_depth"],
            "sheet_spread": len(downstream_sheets), "hub_score_raw": hub,
            "node_role": role, "component_id": components.get(node_id),
        }
        max_downstream = max(max_downstream, len(down["distances"]))
        max_depth_value = max(max_depth_value, down["maximum_depth"])
        max_spread = max(max_spread, len(downstream_sheets))
    max_hub = max((item["hub_score_raw"] for item in raw.values()), default=1) or 1
    for item in raw.values():
        criticality = (
            .4 * item["downstream_count"] / max_downstream
            + .2 * item["max_downstream_depth"] / max_depth_value
            + .2 * item["sheet_spread"] / max_spread
        ) * 100
        item["hub_score"] = round(item.pop("hub_score_raw") / max_hub * 100, 2)
        item["technical_criticality"] = round(min(100, criticality), 2)
    return raw


def sheet_graph(graph: DependencyGraph) -> list[dict]:
    weights = Counter()
    for edge in graph.edges.values():
        source = graph.nodes.get(edge.source_node_id)
        target = graph.nodes.get(edge.target_node_id)
        if source and target and source.sheet_id and target.sheet_id and source.sheet_id != target.sheet_id:
            weights[(source.sheet_id, target.sheet_id)] += edge.logical_cardinality
    return [
        {"source_sheet_id": source, "target_sheet_id": target, "edge_weight": weight}
        for (source, target), weight in sorted(weights.items())
    ]


def graph_summary(graph: DependencyGraph, node_metrics: dict[str, dict], cycles: list[list[str]]) -> dict:
    edges = list(graph.edges.values())
    resolved = sum(edge.resolution_status == "RESOLVED" for edge in edges)
    references = len(edges)
    formula_nodes = sum(
        1 for node in graph.nodes.values()
        if node.node_type == "CELL" and bool(node.metadata.get("formula"))
    )
    cross_sheet = sum(edge.category == "CROSS_SHEET" for edge in edges)
    external = sum(edge.category == "CROSS_WORKBOOK" for edge in edges)
    dynamic = sum(edge.category in {"DYNAMIC", "CONTROL"} for edge in edges)
    broken = sum(edge.resolution_status == "BROKEN" for edge in edges)
    unresolved = sum(edge.resolution_status not in {"RESOLVED"} for edge in edges)
    return {
        "formula_nodes": formula_nodes, "node_count": len(graph.nodes), "edge_count": len(edges),
        "logical_edge_count": sum(edge.logical_cardinality for edge in edges),
        "resolved_count": resolved, "unresolved_count": unresolved,
        "dynamic_count": dynamic, "broken_count": broken, "cycle_count": len(cycles),
        "cross_sheet_edges": cross_sheet, "external_dependencies": external,
        "dependency_coverage": round(resolved / references * 100, 2) if references else 100.0,
        "dependency_density": round(references / formula_nodes, 3) if formula_nodes else 0,
        "cross_sheet_coupling": round(cross_sheet / references * 100, 2) if references else 0,
        "external_coupling": round(external / references * 100, 2) if references else 0,
        "maximum_calculation_depth": max((item["max_upstream_depth"] for item in node_metrics.values()), default=0),
        "calculation_components": len({item["component_id"] for item in node_metrics.values()}),
    }
