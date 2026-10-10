import React, { useMemo, useState } from "react";

const WIDTH = 620;
const HEIGHT = 460;
const CENTER = { x: WIDTH / 2, y: HEIGHT / 2 };
const RADIUS = 170;

function statusColor(entry) {
  if (entry.is_active_now) return "#2da44e";
  if (entry.is_browser_viewing) return "#0969da";
  return "#8c959f";
}

/**
 * A radial node graph for Team Activity: the repository sits at a center
 * hub, each team member orbits it as a node (color = live/browser/offline
 * state, size = hours active today), replacing the flat table with a
 * glanceable "who's around, and how active" view. Same hand-rolled-SVG,
 * no-library convention as CommitGraphView/DependencyGraphView.
 */
export default function TeamActivityGraph({ activity, onSelect }) {
  const [hoveredId, setHoveredId] = useState(null);
  const nodes = useMemo(() => {
    const list = activity || [];
    const maxHours = Math.max(1, ...list.map((item) => item.hours_active_today || 0));
    return list.map((entry, index) => {
      const angle = (index / Math.max(1, list.length)) * Math.PI * 2 - Math.PI / 2;
      return {
        ...entry,
        x: CENTER.x + Math.cos(angle) * RADIUS,
        y: CENTER.y + Math.sin(angle) * RADIUS,
        radius: 14 + 14 * ((entry.hours_active_today || 0) / maxHours),
        color: statusColor(entry),
      };
    });
  }, [activity]);

  if (!activity?.length) return <div className="empty-state compact">No members with access to this repository yet.</div>;

  return (
    <div className="team-graph-wrap">
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="team-graph-svg" role="img" aria-label="Team activity graph">
        {nodes.map((node) => (
          <line key={`edge-${node.user_id}`} x1={CENTER.x} y1={CENTER.y} x2={node.x} y2={node.y} className="team-graph-edge" />
        ))}
        <g className="team-graph-hub" transform={`translate(${CENTER.x},${CENTER.y})`}>
          <circle r={36} />
          <text y={4} textAnchor="middle">REPO</text>
        </g>
        {nodes.map((node) => (
          <g
            key={node.user_id} transform={`translate(${node.x},${node.y})`}
            className="team-graph-node" onMouseEnter={() => setHoveredId(node.user_id)}
            onMouseLeave={() => setHoveredId((current) => (current === node.user_id ? null : current))}
            onClick={() => onSelect?.(node)}
          >
            {node.is_active_now ? <circle r={node.radius + 6} className="team-graph-live-ring" /> : null}
            <circle r={node.radius} fill={node.color} />
            <text y={node.radius + 14} textAnchor="middle" className="team-graph-label">{(node.display_name || node.email || "").split(" ")[0] || node.email}</text>
          </g>
        ))}
      </svg>
      {hoveredId ? (() => {
        const node = nodes.find((item) => item.user_id === hoveredId);
        if (!node) return null;
        return <div className="team-graph-tooltip">
          <strong>{node.display_name || node.email}</strong>
          <span>{node.role}</span>
          <small>{node.is_active_now ? "Live in Excel" : node.is_browser_viewing ? "Viewing in browser" : "Offline"} &middot; {node.hours_active_today || 0}h today</small>
          {node.device ? <small>{node.device.ip_address} &middot; {node.device.trust_status}</small> : null}
        </div>;
      })() : null}
      <div className="team-graph-legend">
        <span><i style={{ background: "#2da44e" }} /> Live in Excel</span>
        <span><i style={{ background: "#0969da" }} /> Viewing in browser</span>
        <span><i style={{ background: "#8c959f" }} /> Offline</span>
      </div>
    </div>
  );
}
