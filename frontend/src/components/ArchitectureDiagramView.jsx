import React, { useMemo } from "react";

const COLUMN_WIDTH = 220;
const BOX_HEIGHT = 64;
const ROW_GAP = 18;
const MARGIN = 30;

/**
 * A small layered box-and-arrow SVG diagram for the Migration Advisor's
 * Target architecture tab. The data (target.architecture.components/edges)
 * was already structured graph data — this just gives it a spatial layout
 * instead of two disconnected flat lists. No force simulation needed: this
 * graph is small and mostly acyclic, so a simple column-by-target-type
 * layered layout is enough to read clearly.
 */
export default function ArchitectureDiagramView({ components, edges }) {
  const safeComponents = components || [];
  const safeEdges = edges || [];

  const layout = useMemo(() => {
    const columnOrder = [];
    const columnOf = new Map();
    safeComponents.forEach((item) => {
      const key = item.target_types?.[0] || "OTHER";
      if (!columnOrder.includes(key)) columnOrder.push(key);
      columnOf.set(item.id, key);
    });
    const rowByColumn = new Map();
    const positions = new Map();
    safeComponents.forEach((item) => {
      const column = columnOf.get(item.id);
      const columnIndex = columnOrder.indexOf(column);
      const row = rowByColumn.get(column) || 0;
      rowByColumn.set(column, row + 1);
      positions.set(item.id, {
        x: MARGIN + columnIndex * COLUMN_WIDTH,
        y: MARGIN + row * (BOX_HEIGHT + ROW_GAP),
        column,
      });
    });
    const maxRows = Math.max(1, ...[...rowByColumn.values()]);
    return {
      positions, columnOrder,
      width: MARGIN * 2 + Math.max(1, columnOrder.length) * COLUMN_WIDTH - (COLUMN_WIDTH - 180),
      height: MARGIN * 2 + maxRows * (BOX_HEIGHT + ROW_GAP),
    };
  }, [safeComponents]);

  if (!safeComponents.length) return <div className="empty-state compact">No target architecture model yet.</div>;

  const findByLooseKey = (key) => safeComponents.find((item) => item.id === key || item.name === key) || null;

  return (
    <div className="architecture-diagram-wrap">
      <svg viewBox={`0 0 ${layout.width} ${Math.max(220, layout.height)}`} className="architecture-diagram-svg">
        {safeEdges.map((edge) => {
          const source = findByLooseKey(edge.source);
          const target = findByLooseKey(edge.target);
          const sourcePos = source && layout.positions.get(source.id);
          const targetPos = target && layout.positions.get(target.id);
          if (!sourcePos || !targetPos) return null;
          const x1 = sourcePos.x + 180, y1 = sourcePos.y + BOX_HEIGHT / 2;
          const x2 = targetPos.x, y2 = targetPos.y + BOX_HEIGHT / 2;
          const midX = (x1 + x2) / 2;
          return (
            <path
              key={`${edge.source}-${edge.target}`}
              d={`M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`}
              className="architecture-diagram-edge" markerEnd="url(#architecture-arrowhead)"
            />
          );
        })}
        <defs>
          <marker id="architecture-arrowhead" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
            <path d="M0,0 L6,3 L0,6 Z" className="architecture-diagram-arrowhead" />
          </marker>
        </defs>
        {safeComponents.map((item) => {
          const pos = layout.positions.get(item.id);
          if (!pos) return null;
          return (
            <g key={item.id} transform={`translate(${pos.x},${pos.y})`}>
              <rect width={180} height={BOX_HEIGHT} rx={9} className="architecture-diagram-box" />
              <text x={12} y={24} className="architecture-diagram-title">{item.name}</text>
              <text x={12} y={42} className="architecture-diagram-meta">{item.unit_count} units</text>
              <text x={12} y={56} className="architecture-diagram-meta">{(item.target_types || []).join(" + ")}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
