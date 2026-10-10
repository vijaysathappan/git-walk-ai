import React, { useMemo } from "react";

const ROLE_COLORS = { SOURCE: "#2da44e", TRANSFORM: "#0969da", OUTPUT: "#8250df" };
const WIDTH = 640;
const HEIGHT = 380;

/**
 * A real force-directed node-link diagram for the sheet-level dependency
 * graph (backend/app/euc/dependency/services/dependency_service.py::sheets()
 * already returns the complete node+edge set in one call). No charting
 * library — a small fixed-iteration spring simulation run once per graph,
 * matching this codebase's hand-rolled-SVG convention.
 */
function layoutForceDirected(nodes, edges) {
  if (!nodes.length) return [];
  const positions = nodes.map((node, index) => {
    const angle = (index / nodes.length) * Math.PI * 2;
    return { id: node.sheet_id, x: WIDTH / 2 + Math.cos(angle) * 140, y: HEIGHT / 2 + Math.sin(angle) * 140, vx: 0, vy: 0 };
  });
  const byId = new Map(positions.map((item) => [item.id, item]));
  const springs = edges
    .map((edge) => [byId.get(edge.source_sheet_id), byId.get(edge.target_sheet_id)])
    .filter(([a, b]) => a && b);

  for (let iteration = 0; iteration < 220; iteration++) {
    for (let i = 0; i < positions.length; i++) {
      for (let j = i + 1; j < positions.length; j++) {
        const a = positions[i], b = positions[j];
        const dx = a.x - b.x || (Math.random() - 0.5), dy = a.y - b.y || (Math.random() - 0.5);
        const distSq = Math.max(1, dx * dx + dy * dy);
        const dist = Math.sqrt(distSq);
        const force = 2200 / distSq;
        const ux = dx / dist, uy = dy / dist;
        a.vx += ux * force; a.vy += uy * force;
        b.vx -= ux * force; b.vy -= uy * force;
      }
    }
    springs.forEach(([a, b]) => {
      const dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.max(1, Math.sqrt(dx * dx + dy * dy));
      const force = (dist - 120) * 0.02;
      const ux = dx / dist, uy = dy / dist;
      a.vx += ux * force; a.vy += uy * force;
      b.vx -= ux * force; b.vy -= uy * force;
    });
    positions.forEach((point) => {
      point.vx += (WIDTH / 2 - point.x) * 0.002;
      point.vy += (HEIGHT / 2 - point.y) * 0.002;
      point.vx *= 0.85; point.vy *= 0.85;
      point.x = Math.max(34, Math.min(WIDTH - 34, point.x + point.vx));
      point.y = Math.max(34, Math.min(HEIGHT - 34, point.y + point.vy));
    });
  }
  return positions;
}

export default function DependencyGraphView({ nodes, edges, cycles, onSelectSheet }) {
  const safeNodes = nodes || [];
  const safeEdges = edges || [];
  const cycleSheetIds = useMemo(() => new Set((cycles || []).flatMap((cycle) => cycle.sheets || [])), [cycles]);
  const positions = useMemo(() => layoutForceDirected(safeNodes, safeEdges), [safeNodes, safeEdges]);
  const byId = useMemo(() => new Map(positions.map((item) => [item.id, item])), [positions]);
  const maxFormulas = Math.max(1, ...safeNodes.map((node) => node.formula_count || 0));

  if (!safeNodes.length) return <div className="empty-state compact">No sheets to visualize yet.</div>;

  return (
    <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="dependency-graph-svg" role="img" aria-label="Sheet dependency graph">
      {safeEdges.map((edge) => {
        const source = byId.get(edge.source_sheet_id);
        const target = byId.get(edge.target_sheet_id);
        if (!source || !target) return null;
        return <line
          key={`${edge.source_sheet_id}-${edge.target_sheet_id}`}
          x1={source.x} y1={source.y} x2={target.x} y2={target.y}
          className="dependency-graph-edge"
          strokeWidth={Math.min(6, 1 + Math.log2(1 + (edge.edge_weight || 1)))}
        />;
      })}
      {safeNodes.map((node) => {
        const pos = byId.get(node.sheet_id);
        if (!pos) return null;
        const isCycle = cycleSheetIds.has(node.sheet_id);
        const radius = 11 + 15 * ((node.formula_count || 0) / maxFormulas);
        return (
          <g
            key={node.sheet_id} transform={`translate(${pos.x},${pos.y})`}
            className="dependency-graph-node" onClick={() => onSelectSheet?.(node)}
          >
            {isCycle ? <circle r={radius + 6} className="dependency-graph-cycle-ring" /> : null}
            <circle r={radius} fill={ROLE_COLORS[node.role] || "#57606a"} />
            <text textAnchor="middle" dy={radius + 14} className="dependency-graph-label">{node.name}</text>
          </g>
        );
      })}
    </svg>
  );
}
