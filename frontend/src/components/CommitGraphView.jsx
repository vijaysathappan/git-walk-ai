import React, { useEffect, useMemo, useRef, useState } from "react";
import { getRepositoryCommitGraph } from "../services/api";

const LANE_COLORS = ["#0969da", "#2da44e", "#bf8700", "#cf222e", "#8250df", "#1b7c83", "#bc4c00"];
const MARGIN = 24;

function clamp(min, value, max) {
  return Math.max(min, Math.min(max, value));
}

/**
 * A real multi-lane git-graph timeline: every branch of the repository gets
 * its own lane, commits are plotted by time, and lines connect each commit
 * to all of its parents (merge commits draw two curved lines into the
 * lanes they unified) — built from GET /repositories/{table_id}/commit-graph
 * (backend/app/repositories/commit_store.py::list_repository_commits),
 * which is branch-agnostic and carries every parent, unlike the existing
 * single-branch, first-parent-only commit list.
 */
export default function CommitGraphView({ tableId, onSelectCommit, onError }) {
  const [commits, setCommits] = useState([]);
  const [loading, setLoading] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [containerWidth, setContainerWidth] = useState(0);
  const wrapRef = useRef(null);

  useEffect(() => {
    if (!tableId) { setCommits([]); return; }
    setLoading(true);
    getRepositoryCommitGraph(tableId)
      .then((result) => setCommits(result.commits || []))
      .catch((error) => onError?.(error.message))
      .finally(() => setLoading(false));
  }, [tableId]);

  useEffect(() => {
    if (!wrapRef.current || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect?.width;
      if (width) setContainerWidth(width);
    });
    observer.observe(wrapRef.current);
    return () => observer.disconnect();
  }, []);

  const layout = useMemo(() => {
    const chronological = [...commits].sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
    const laneByBranch = new Map();
    let laneCount = 0;
    chronological.forEach((commit) => {
      if (!laneByBranch.has(commit.branch_id)) laneByBranch.set(commit.branch_id, laneCount++);
    });
    // Lane width, node radius, and row height scale with the available
    // container width and how many lanes/commits are in flight — generous
    // and "punchy" on a wide screen with few branches, still compact and
    // legible when 10-20 branches are active at once.
    const effectiveLaneCount = Math.max(1, laneCount);
    const laneWidth = clamp(30, (containerWidth || 900) / (effectiveLaneCount * 3), 90);
    const nodeRadius = clamp(4, laneWidth / 7, 9);
    const mergeNodeRadius = nodeRadius * 1.4;
    const rowHeight = clamp(40, laneWidth * 1.6, 70);
    const positioned = commits.map((commit, index) => {
      const lane = laneByBranch.get(commit.branch_id) ?? 0;
      // Each commit owns a full rowHeight-tall slot, node centered within
      // it, so the HTML label column (same rowHeight per row) lines up
      // exactly with its node instead of drifting from it.
      return { ...commit, lane, x: MARGIN + lane * laneWidth, y: MARGIN + index * rowHeight + rowHeight / 2 };
    });
    const byId = new Map(positioned.map((item) => [item.commit_id, item]));
    const edges = [];
    positioned.forEach((commit) => {
      const isMerge = (commit.parent_commit_ids || []).length > 1;
      (commit.parent_commit_ids || []).forEach((parentId) => {
        const parent = byId.get(parentId);
        if (parent) edges.push({ from: commit, to: parent, isMerge });
      });
    });
    return {
      positioned, edges, laneCount, laneWidth, nodeRadius, mergeNodeRadius, rowHeight,
      width: MARGIN * 2 + effectiveLaneCount * laneWidth,
      height: MARGIN * 2 + Math.max(1, positioned.length) * rowHeight,
    };
  }, [commits, containerWidth]);

  if (loading) return <div className="commit-graph-loading">Loading commit graph...</div>;
  if (!commits.length) return <div className="empty-state">No commits yet.</div>;

  return (
    <div className="commit-graph-outer">
      <div className="commit-graph-zoom-controls">
        <button onClick={() => setZoom((value) => Math.max(0.5, +(value - 0.2).toFixed(2)))} title="Zoom out">&#8722;</button>
        <span>{Math.round(zoom * 100)}%</span>
        <button onClick={() => setZoom((value) => Math.min(2.2, +(value + 0.2).toFixed(2)))} title="Zoom in">+</button>
        <button onClick={() => setZoom(1)} title="Reset zoom">Reset</button>
      </div>
      <div className="commit-graph-wrap" ref={wrapRef}>
      <svg
        width={layout.width * zoom}
        height={layout.height * zoom}
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        className="commit-graph-svg"
      >
        {layout.edges.map((edge, index) => {
          const color = LANE_COLORS[edge.to.lane % LANE_COLORS.length];
          const path = edge.from.lane === edge.to.lane
            ? `M ${edge.from.x} ${edge.from.y} L ${edge.to.x} ${edge.to.y}`
            : `M ${edge.from.x} ${edge.from.y} C ${edge.from.x} ${(edge.from.y + edge.to.y) / 2}, ${edge.to.x} ${(edge.from.y + edge.to.y) / 2}, ${edge.to.x} ${edge.to.y}`;
          return <path key={`${edge.from.commit_id}-${edge.to.commit_id}-${index}`} d={path} fill="none" stroke={color} strokeWidth={edge.isMerge ? layout.nodeRadius * 0.55 : layout.nodeRadius * 0.3} opacity={0.8} />;
        })}
        {layout.positioned.map((commit) => (
          <circle
            key={commit.commit_id} cx={commit.x} cy={commit.y}
            r={(commit.parent_commit_ids || []).length > 1 ? layout.mergeNodeRadius : layout.nodeRadius}
            fill={LANE_COLORS[commit.lane % LANE_COLORS.length]}
            className={`commit-graph-node${(commit.parent_commit_ids || []).length > 1 ? " commit-graph-node-merge" : ""}`}
            onClick={() => onSelectCommit?.(commit.commit_id)}
          >
            <title>{commit.branch_name}{commit.branch_type === "MAIN" ? " (main)" : ""} &middot; {commit.message} &middot; {commit.author_email || commit.author_user_id} &middot; {new Date(commit.created_at).toLocaleString()} &middot; {String(commit.commit_hash || "").slice(0, 9)}{(commit.parent_commit_ids || []).length > 1 ? " · merge" : ""}</title>
          </circle>
        ))}
      </svg>
      <div className="commit-graph-labels" style={{ paddingTop: MARGIN * zoom }}>
        {layout.positioned.map((commit) => (
          <button
            key={commit.commit_id}
            className="commit-graph-label"
            style={{ height: layout.rowHeight * zoom }}
            onClick={() => onSelectCommit?.(commit.commit_id)}
          >
            <strong>{commit.message}</strong>
            <small>
              <span className="commit-graph-label-branch" style={{ color: LANE_COLORS[commit.lane % LANE_COLORS.length] }}>
                {commit.branch_name}{commit.branch_type === "MAIN" ? " (main)" : ""}
              </span>
              {" · "}{commit.author_email || commit.author_user_id}{" · "}{new Date(commit.created_at).toLocaleDateString()}{" · "}{String(commit.commit_hash || "").slice(0, 7)}
              {(commit.parent_commit_ids || []).length > 1 ? " · merge" : ""}
            </small>
          </button>
        ))}
      </div>
      </div>
    </div>
  );
}
