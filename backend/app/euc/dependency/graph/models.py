"""In-memory dependency graph contracts."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DependencyNode:
    node_id: str
    node_type: str
    display_name: str
    sheet_id: str | None = None
    row_id: str | None = None
    column_id: str | None = None
    cell_address: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DependencyEdge:
    source_node_id: str
    target_node_id: str
    dependency_type: str = "DEPENDS_ON"
    category: str = "DIRECT_CELL"
    resolution_status: str = "RESOLVED"
    logical_cardinality: int = 1
    metadata_json: str = "{}"

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.source_node_id, self.target_node_id, self.dependency_type, self.category)


@dataclass
class DependencyGraph:
    nodes: dict[str, DependencyNode] = field(default_factory=dict)
    edges: dict[tuple[str, str, str, str], DependencyEdge] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def add_node(self, node: DependencyNode) -> DependencyNode:
        self.nodes.setdefault(node.node_id, node)
        return self.nodes[node.node_id]

    def add_edge(self, edge: DependencyEdge) -> None:
        self.edges.setdefault(edge.key, edge)

    def adjacency(self) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        upstream = {node_id: [] for node_id in self.nodes}
        downstream = {node_id: [] for node_id in self.nodes}
        for edge in self.edges.values():
            if edge.dependency_type not in {"DEPENDS_ON", "RESOLVES_TO", "EXTERNAL_DEPENDENCY"}:
                continue
            upstream.setdefault(edge.source_node_id, []).append(edge.target_node_id)
            downstream.setdefault(edge.target_node_id, []).append(edge.source_node_id)
        return (
            {key: sorted(set(values)) for key, values in sorted(upstream.items())},
            {key: sorted(set(values)) for key, values in sorted(downstream.items())},
        )
