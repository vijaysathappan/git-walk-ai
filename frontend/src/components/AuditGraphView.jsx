import React, { useMemo, useState } from "react";

const FAMILY_COLORS = {
  commit: "#2da44e",
  merge: "#8250df",
  device: "#bf8700",
  ai: "#1b7c83",
  security: "#cf222e",
  general: "#57606a",
};
const FAMILY_ORDER = ["commit", "merge", "security", "device", "ai", "general"];
const ROW_HEIGHT = 72;
const COL_WIDTH = 64;
const MARGIN_X = 120;
const MARGIN_Y = 40;

function familyOf(eventType) {
  const upper = String(eventType || "").toUpperCase();
  if (upper.includes("COMMIT") || upper.includes("REVERT")) return "commit";
  if (upper.includes("MERGE")) return "merge";
  if (upper.includes("DEVICE") || upper.includes("SESSION")) return "device";
  if (upper.includes("AI_") || upper.includes("AGENT")) return "ai";
  if (upper.includes("SECURITY") || upper.includes("ROLE") || upper.includes("POLICY") || upper.includes("USER_STATUS")) return "security";
  return "general";
}

/**
 * Graph-native audit ledger view: every event is a node positioned by its
 * category lane (x) and time (y), with a real edge drawn to the event whose
 * hash matches its own `previous_event_hash` — the actual hash-chain, not a
 * decorative connector — so large volumes stay navigable via zoom instead of
 * becoming an unreadable wall of blocks. Reuses CommitGraphView's zoom-control
 * pattern; no charting library.
 */
export default function AuditGraphView({ events, onSelectEvent }) {
  const [zoom, setZoom] = useState(1);
  const [selectedId, setSelectedId] = useState(null);

  const layout = useMemo(() => {
    const chronological = [...(events || [])].sort(
      (a, b) => new Date(a.created_at) - new Date(b.created_at)
    );
    const byHash = new Map();
    chronological.forEach((event) => {
      if (event.event_hash) byHash.set(event.event_hash, event);
    });
    const positioned = chronological.map((event, index) => {
      const family = familyOf(event.event_type);
      const lane = FAMILY_ORDER.indexOf(family);
      return {
        ...event,
        family,
        x: MARGIN_X + index * COL_WIDTH,
        y: MARGIN_Y + lane * ROW_HEIGHT,
      };
    });
    const byId = new Map(positioned.map((item) => [item.event_id, item]));
    const edges = [];
    positioned.forEach((event) => {
      const prev = event.previous_event_hash ? byHash.get(event.previous_event_hash) : null;
      const prevPositioned = prev ? byId.get(prev.event_id) : null;
      if (prevPositioned) edges.push({ from: event, to: prevPositioned });
    });
    return {
      positioned, edges,
      width: MARGIN_X + Math.max(1, positioned.length) * COL_WIDTH + MARGIN_X / 2,
      height: MARGIN_Y * 2 + Math.max(0, FAMILY_ORDER.length - 1) * ROW_HEIGHT,
    };
  }, [events]);

  const selected = layout.positioned.find((event) => event.event_id === selectedId) || null;

  if (!events?.length) return <div className="empty-state">No audit events in this window.</div>;

  return (
    <div className="audit-graph-outer">
      <div className="commit-graph-zoom-controls">
        <button onClick={() => setZoom((value) => Math.max(0.5, +(value - 0.2).toFixed(2)))} title="Zoom out">&#8722;</button>
        <span>{Math.round(zoom * 100)}%</span>
        <button onClick={() => setZoom((value) => Math.min(2.2, +(value + 0.2).toFixed(2)))} title="Zoom in">+</button>
        <button onClick={() => setZoom(1)} title="Reset zoom">Reset</button>
      </div>
      <div className="audit-graph-wrap">
        <div className="audit-graph-lane-rail" style={{ height: layout.height * zoom }}>
          {FAMILY_ORDER.map((family, index) => (
            <span key={family} style={{ top: (MARGIN_Y + index * ROW_HEIGHT) * zoom, color: FAMILY_COLORS[family] }}>
              <i style={{ background: FAMILY_COLORS[family] }} />{family}
            </span>
          ))}
        </div>
        <div className="audit-graph-scroll">
          <svg
            width={layout.width * zoom}
            height={layout.height * zoom}
            viewBox={`0 0 ${layout.width} ${layout.height}`}
            className="audit-graph-svg"
          >
            {FAMILY_ORDER.map((family, index) => (
              <line
                key={family}
                x1={0} x2={layout.width}
                y1={MARGIN_Y + index * ROW_HEIGHT} y2={MARGIN_Y + index * ROW_HEIGHT}
                stroke={FAMILY_COLORS[family]} strokeWidth={2} opacity={0.14}
              />
            ))}
            {layout.edges.map((edge, index) => {
              const color = FAMILY_COLORS[edge.to.family];
              const midX = (edge.from.x + edge.to.x) / 2;
              const path = edge.from.y === edge.to.y
                ? `M ${edge.from.x} ${edge.from.y} L ${edge.to.x} ${edge.to.y}`
                : `M ${edge.from.x} ${edge.from.y} C ${midX} ${edge.from.y}, ${midX} ${edge.to.y}, ${edge.to.x} ${edge.to.y}`;
              return <path key={`${edge.from.event_id}-${edge.to.event_id}-${index}`} d={path} fill="none" stroke={color} strokeWidth={2} opacity={0.6} />;
            })}
            {layout.positioned.map((event) => (
              <g key={event.event_id}>
                {event.event_id === selectedId ? <circle cx={event.x} cy={event.y} r={13} fill={FAMILY_COLORS[event.family]} opacity={0.18} /> : null}
                <circle
                  cx={event.x}
                  cy={event.y}
                  r={event.event_id === selectedId ? 7 : 5}
                  fill={FAMILY_COLORS[event.family]}
                  stroke="#0d1117"
                  strokeWidth={1.5}
                  className="audit-graph-node"
                  onClick={() => { setSelectedId(event.event_id); onSelectEvent?.(event); }}
                >
                  <title>{event.event_type} &middot; {new Date(event.created_at).toLocaleString()}</title>
                </circle>
              </g>
            ))}
          </svg>
        </div>
      </div>
      {selected ? (
        <div className="audit-graph-detail">
          <header>
            <strong>{selected.event_type.replaceAll("_", " ")}</strong>
            <span className={`pill family-${selected.family}`}>{selected.family}</span>
            <time>{new Date(selected.created_at).toLocaleString()}</time>
          </header>
          <div className="audit-hash-link">
            <code className="audit-hash-self" title="This event's hash">{String(selected.event_hash || "").slice(0, 16) || "—"}</code>
            <span className="audit-hash-arrow">&larr; links to</span>
            <code className="audit-hash-prev" title="Predecessor event hash">{String(selected.previous_event_hash || "GENESIS").slice(0, 16)}</code>
          </div>
          <p>
            {selected.failure_reason ||
              Object.entries(selected.event_payload || {})
                .map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : value}`)
                .join(" / ") ||
              "Recorded without additional payload"}
          </p>
          <footer>
            <code>{selected.event_id}</code>
            <span>request {selected.request_id}</span>
            <span>trace {selected.trace_id}</span>
            {selected.commit_id ? <b>{selected.commit_id}</b> : null}
            {selected.merge_request_id ? <b>{selected.merge_request_id}</b> : null}
          </footer>
        </div>
      ) : null}
    </div>
  );
}
